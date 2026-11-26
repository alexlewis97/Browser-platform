"""Turn recorded literals into {variable} placeholders."""

from __future__ import annotations

import re
from typing import Any

from browser_platform.authoring.elements import is_label_element, resolve_fill_element, strategies_for_element
from browser_platform.workflow_inputs import pretty_label
from browser_platform.models.workflow import WorkflowInput

_BRACE = re.compile(r"\{([a-zA-Z_][\w]*)\}")


def placeholders_in_task(task: str) -> list[str]:
    names: list[str] = []
    for match in _BRACE.finditer(task or ""):
        name = match.group(1)
        if name not in names:
            names.append(name)
    return names


def generalize_events(
    events: list[Any],
    *,
    task: str,
    variables: dict[str, Any] | None = None,
) -> tuple[list[Any], list[WorkflowInput], dict[str, str], list[dict[str, Any]]]:
    """Replace captured input/select values with {vars}.

    Returns (events, input_schema, resolved_variables, suggestions).
    Suggestions are leftover literals the caller may want to promote to variables.
    """
    variables = {str(k): str(v) for k, v in (variables or {}).items() if v is not None}
    names = placeholders_in_task(task)
    # Inverse lookup from observed value to variable name.
    by_value = {str(v): k for k, v in variables.items()}
    # Placeholders without a provided value: bind in first-seen order to input-like events.
    unused_placeholders = [n for n in names if n not in variables]
    resolved = dict(variables)
    suggestions: list[dict[str, Any]] = []

    def _get(ev, key, default=None):
        if hasattr(ev, key):
            return getattr(ev, key)
        if isinstance(ev, dict):
            return ev.get(key, default)
        return default

    def _set(ev, key, value):
        if hasattr(ev, key):
            setattr(ev, key, value)
        elif isinstance(ev, dict):
            ev[key] = value

    def _bind_text(text: str | None) -> tuple[str | None, str | None]:
        if text is None or str(text) == "" or str(text).startswith("{"):
            return text, None
        raw = str(text)
        name = by_value.get(raw)
        if name is None:
            lowered = raw.lower().strip()
            for literal, var in list(by_value.items()):
                lit = str(literal).lower().strip()
                if lit == lowered:
                    name = var
                    break
                # Allow a slightly longer recorded string that starts with the known value.
                if len(lit) >= 4 and (lowered.startswith(lit) or lit.startswith(lowered) and abs(len(lit) - len(lowered)) <= 2):
                    name = var
                    break
        return raw, name

    def _rewrite_strategies(ev, literal: str, name: str) -> None:
        selectors = _get(ev, "selectors") or []
        for strategy in selectors:
            value = None
            if hasattr(strategy, "value"):
                value = strategy.value
            elif isinstance(strategy, dict):
                value = strategy.get("value")
            if not value:
                continue
            updated = apply_variables_to_url(str(value), {literal: name}) or str(value)
            if literal.lower() == str(value).lower():
                updated = "{" + name + "}"
            if hasattr(strategy, "value"):
                strategy.value = updated
            elif isinstance(strategy, dict):
                strategy["value"] = updated

    for ev in events:
        typ = _get(ev, "type")
        if typ not in {"input", "select_change", "click"}:
            continue
        element = _get(ev, "element") if isinstance(_get(ev, "element"), dict) else {}
        if typ in {"input", "select_change"} and is_label_element(element):
            label_text = str(element.get("text") or element.get("name") or "").strip()
            if _get(ev, "value") and str(_get(ev, "value")).strip() == label_text:
                # A fill of the label's own text is noise, not a typed parameter.
                continue
            resolved_el = resolve_fill_element(element)
            _set(ev, "element", resolved_el)
            _set(ev, "selectors", strategies_for_element(resolved_el, for_fill=True))
            element = resolved_el or {}
        candidates = []
        if typ in {"input", "select_change"}:
            raw = _get(ev, "value")
            if raw:
                candidates.append(("value", str(raw)))
        else:
            for key in ("value", "text"):
                raw = _get(ev, key)
                if raw:
                    candidates.append((key, str(raw)))
            for key in ("name", "text"):
                if element.get(key):
                    candidates.append(("element." + key, str(element[key])))
        bound = False
        for _source, text in candidates:
            raw, name = _bind_text(text)
            if name is None and typ in {"input", "select_change"} and unused_placeholders and raw and not raw.startswith("{"):
                name = unused_placeholders.pop(0)
                resolved[name] = raw
                by_value[raw] = name
            if not name:
                continue
            placeholder = "{" + name + "}"
            if typ in {"input", "select_change"}:
                _set(ev, "value", placeholder)
            if typ == "click":
                if _get(ev, "text"):
                    _set(ev, "text", placeholder)
                if _get(ev, "value") and not str(_get(ev, "value")).startswith("{"):
                    _set(ev, "value", placeholder)
            _rewrite_strategies(ev, raw or text, name)
            bound = True
            break
        if bound:
            continue
        if typ in {"input", "select_change"}:
            value = _get(ev, "value")
            if value is None or str(value) == "":
                continue
            text = str(value)
            element = _get(ev, "element")
            used = set(resolved) | {s.get("suggested_name") for s in suggestions}
            suggested = suggest_variable_name(text, element if isinstance(element, dict) else None, used=used)
            suggestions.append({
                "value": text,
                "url": _get(ev, "url"),
                "type": typ,
                "suggested_name": suggested,
                "element": {k: element.get(k) for k in ("id", "role", "name", "placeholder", "type") if isinstance(element, dict) and element.get(k)},
            })

    by_value = {str(v): k for k, v in resolved.items()}
    filtered = []
    for ev in events:
        typ = ev.type if hasattr(ev, "type") else ev.get("type")
        if typ == "navigation":
            url = ev.url if hasattr(ev, "url") else ev.get("url")
            rewritten = apply_variables_to_url(url, by_value)
            if rewritten and rewritten != url:
                if hasattr(ev, "url"):
                    ev.url = rewritten
                else:
                    ev["url"] = rewritten
            filtered.append(ev)
            continue
        if typ in {"input", "select_change"}:
            value = ev.value if hasattr(ev, "value") else ev.get("value")
            text = "" if value is None else str(value)
            element = ev.element if hasattr(ev, "element") else ev.get("element")
            if isinstance(element, dict) and is_label_element(element):
                label_text = str(element.get("text") or element.get("name") or "").strip()
                if text and label_text and text.strip() == label_text:
                    continue
            if text.startswith("{"):
                filtered.append(ev)
                continue
            # Drop site-chrome / accidental short literals that are not known variables.
            if text and (text in by_value or len(text) >= 3):
                filtered.append(ev)
            continue
        filtered.append(ev)
    events = filtered
    schema = [
        WorkflowInput(name=name, type="string", required=True, default=resolved.get(name), label=pretty_label(name))
        for name in list(dict.fromkeys([*names, *resolved.keys()]))
    ]
    return events, schema, resolved, suggestions


def suggest_variable_name(value: str, element: dict | None = None, used: set[str] | None = None) -> str:
    """Pick a stable variable name for a captured literal."""

    used = used or set()
    element = element or {}
    typ = (element.get("type") or "").lower()
    hints = [
        typ,
        element.get("placeholder") or "",
        element.get("name") or "",
        element.get("ariaLabel") or "",
        element.get("id") or "",
    ]
    blob = " ".join(str(h) for h in hints).lower()
    if typ == "email" or "@" in (value or "") or "email" in blob:
        base = "email"
    elif typ == "password" or "password" in blob:
        base = "password"
    elif typ in {"tel", "phone"} or "phone" in blob:
        base = "phone"
    elif "search" in blob or "query" in blob or "term" in blob:
        base = "query"
    else:
        import re
        raw = element.get("placeholder") or element.get("name") or element.get("id") or "value"
        base = re.sub(r"[^a-z0-9]+", "_", str(raw).lower()).strip("_") or "value"
        if base.isdigit():
            base = "value"
    name = base
    i = 2
    while name in used:
        name = f"{base}_{i}"
        i += 1
    return name


def value_url_variants(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    titled = text[:1].upper() + text[1:] if text else text
    lowered = text.lower()
    variants = [
        text,
        lowered,
        text.replace(" ", "_"),
        text.replace(" ", "-"),
        text.replace(" ", "+"),
        lowered.replace(" ", "_"),
        lowered.replace(" ", "-"),
        titled.replace(" ", "_"),
        titled.replace(" ", "-"),
        text.replace(" ", "%20"),
        lowered.replace(" ", "%20"),
        titled.replace(" ", "%20"),
    ]
    tokens = [tok for tok in text.replace("-", " ").replace("_", " ").split() if len(tok) >= 3]
    if len(tokens) >= 2:
        first = tokens[0]
        variants.extend([first, first.lower(), first[:1].upper() + first[1:]])
    # unique, longest first so we replace the most specific spelling
    unique = list(dict.fromkeys(v for v in variants if v))
    unique.sort(key=len, reverse=True)
    return unique


def url_placeholder(name: str, literal: str, variant: str) -> str:
    """Keep enough of the original spelling so replay can rebuild the slug."""
    text = str(literal or "").strip()
    if variant == text:
        return "{" + name + "}"
    slug = text.lower().replace(" ", "-")
    under = text.lower().replace(" ", "_")
    if variant in {text.lower(), slug}:
        return "{" + name + "|slug}"
    if variant == under:
        return "{" + name + "|slug_}"
    tokens = [tok for tok in text.replace("-", " ").split() if tok]
    if tokens and variant.lower() == tokens[0].lower() and len(tokens) >= 2:
        return "{" + name + "|head}"
    return "{" + name + "}"


def apply_variables_to_url(url: str | None, by_value: dict[str, str]) -> str | None:
    """Replace a parameter value in path segments or query values, never in the hostname."""

    if not url or not by_value:
        return url
    from urllib.parse import parse_qsl, urlparse, urlunparse, unquote
    from browser_platform.interpolate import is_navigable_url

    # Selector strategies (e.g. [id="query"]) are not URLs. Python 3.13 urlparse
    # treats a leading [ as an IPv6 host and raises ValueError.
    if not is_navigable_url(url) and "://" not in url:
        return url
    try:
        parsed = urlparse(url)
    except ValueError:
        return url

    def bind_piece(piece: str) -> str:
        if not piece or piece.startswith("{"):
            return piece
        decoded = unquote(piece)
        for literal, name in by_value.items():
            variants = [item for item in value_url_variants(literal) if item]
            exact = next((item for item in variants if decoded == item), None)
            if exact is not None:
                return url_placeholder(name, literal, exact)
            folded = next((item for item in variants if decoded.lower() == item.lower()), None)
            if folded is not None:
                # Use the URL's own spelling so Browser_automation stays {term}, not {term|slug_}.
                return url_placeholder(name, literal, decoded)
        return piece

    path = "/".join(bind_piece(part) for part in parsed.path.split("/"))
    query = parsed.query
    if query:
        parts: list[str] = []
        for key, value in parse_qsl(query, keep_blank_values=True):
            parts.append(f"{key}={bind_piece(value)}")
        # Preserve raw query if parse_qsl dropped a non-pair token.
        query = "&".join(parts) if parts else query
    return urlunparse(parsed._replace(path=path, query=query))

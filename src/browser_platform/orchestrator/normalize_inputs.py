"""Normalize chat tool inputs to the canonical entity a workflow expects."""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Iterable

from browser_platform.executor.output_validate import is_nav_footer_only
from browser_platform.interpolate import path_slug
from browser_platform.llm.openrouter import chat_complete, env_api_key, merge_usage, message_text
from browser_platform.models.workflow import WorkflowDefinition, WorkflowInput

CompleteFn = Callable[..., Awaitable[tuple[dict[str, Any], dict[str, Any]]]]

_PLACEHOLDER = re.compile(r"\{+\s*([a-zA-Z_][\w]*)(?:\|([a-zA-Z0-9_]+))?\s*\}+")

# Form / control fields that must stay as the user typed them.
_SKIP_NAMES = {
    "first_name", "last_name", "given_name", "family_name", "middle_name", "full_name",
    "email", "e_mail", "phone", "telephone", "mobile", "password", "passcode", "otp",
    "zip", "zip_code", "postal", "postal_code", "address", "street", "card", "card_number",
    "cvv", "cvc", "ssn", "quantity", "qty", "count", "age", "dob", "date_of_birth",
    "message", "comment", "notes", "text", "body", "password_confirm",
    "month", "year", "date", "page", "category", "sort", "order_by", "product_sort",
    "size", "topping", "toppings", "gender", "colour", "color", "product", "item",
    "query", "q", "term", "search", "keyword",
}

# Named entities whose nicknames need to become the official site value / slug.
_ENTITY_NAMES = {
    "team", "club", "side", "opponent", "opposition", "franchise",
    "organisation", "organization", "org", "university", "college", "school",
    "company", "vendor", "artist", "author", "player", "athlete",
    "venue", "stadium", "ground", "competition", "league", "tournament",
    "country", "nation", "city", "region", "party", "candidate",
    "institution", "department", "agency", "publisher", "museum", "hospital",
    "airline", "airport", "station", "constituency", "club_name", "team_name",
}

def _schema_item(definition: WorkflowDefinition | None, name: str) -> WorkflowInput | None:
    if definition is None:
        return None
    lowered = (name or "").lower()
    for item in definition.input_schema or []:
        if (item.name or "").lower() == lowered:
            return item
    return None


def _examples(item: WorkflowInput | None) -> list[str]:
    if item is None:
        return []
    out: list[str] = []
    for raw in (
        item.default,
        getattr(item, "example", None),
        *list(getattr(item, "examples", None) or []),
        *list(getattr(item, "enum", None) or []),
    ):
        text = str(raw).strip() if raw is not None else ""
        if text and text not in out:
            out.append(text)
    if getattr(item, "model_dump", None):
        data = item.model_dump()
        for key in ("example", "examples", "enum"):
            raw = data.get(key)
            values = raw if isinstance(raw, list) else [raw]
            for value in values:
                text = str(value).strip() if value is not None else ""
                if text and text not in out:
                    out.append(text)
    return out


def _step_blobs(definition: WorkflowDefinition | None) -> Iterable[str]:
    if definition is None:
        return []
    blobs: list[str] = []
    for step in definition.steps or []:
        parts = [
            getattr(step, "url", None) or "",
            getattr(step, "cssSelector", None) or "",
            getattr(step, "value", None) or "",
            getattr(step, "selectedText", None) or "",
            getattr(step, "xpath", None) or "",
        ]
        for strategy in getattr(step, "selectorStrategies", None) or []:
            parts.append(getattr(strategy, "value", None) or "")
        blobs.append(" ".join(str(p) for p in parts if p))
    return blobs


def used_in_url_path(definition: WorkflowDefinition | None, name: str) -> bool:
    lowered = (name or "").lower()
    for step in getattr(definition, "steps", None) or []:
        typ = (getattr(step, "type", None) or "").lower()
        url = getattr(step, "url", None) or ""
        if typ not in {"navigation", "navigate"} and "{" + lowered not in url.lower():
            continue
        if not url:
            continue
        for match in _PLACEHOLDER.finditer(url):
            if match.group(1).lower() == lowered:
                return True
    return False


def is_entity_parameter(name: str, definition: WorkflowDefinition | None = None) -> bool:
    key = re.sub(r"[^a-z0-9_]+", "_", (name or "").strip().lower()).strip("_")
    if not key or key in _SKIP_NAMES or key.endswith(("_email", "_phone", "_zip", "_password")):
        return False
    if key in _ENTITY_NAMES or key.endswith(("_team", "_club", "_org")):
        return True
    item = _schema_item(definition, name)
    blob = " ".join(
        [
            (getattr(item, "description", None) or ""),
            (getattr(item, "label", None) or ""),
            (getattr(definition, "description", None) or ""),
        ]
    ).lower()
    if any(word in blob for word in ("team", "club", "organisation", "organization", "university", "league")):
        return True
    # A path placeholder (especially {name|slug}) is an entity, but not month/page (skipped above).
    return used_in_url_path(definition, name)


def match_canonical_example(value: str, examples: Iterable[str]) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    slug = path_slug(text)
    compact = re.sub(r"[^a-z0-9]+", "", text.lower())
    for example in examples:
        candidate = str(example or "").strip()
        if not candidate:
            continue
        if candidate.lower() == text.lower():
            return candidate
        if path_slug(candidate) == slug:
            return candidate
        if re.sub(r"[^a-z0-9]+", "", candidate.lower()) == compact:
            return candidate
    return None


def _prefers_slug_form(examples: list[str]) -> bool:
    named = [ex for ex in examples if ex]
    if not named:
        return False
    return all(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", ex) or ex == path_slug(ex) for ex in named) and any("-" in ex for ex in named)


def _url_templates(definition: WorkflowDefinition | None, name: str) -> list[str]:
    lowered = (name or "").lower()
    found: list[str] = []
    for step in getattr(definition, "steps", None) or []:
        url = getattr(step, "url", None) or ""
        if not url:
            continue
        if any(m.group(1).lower() == lowered for m in _PLACEHOLDER.finditer(url)):
            found.append(url)
    return found


def _choose_form(canonical: str, slug: str | None, examples: list[str]) -> str:
    official = (canonical or "").strip()
    slug_val = (slug or path_slug(official)).strip()
    if not official:
        return official
    if _prefers_slug_form(examples) and slug_val:
        return slug_val
    return official


def parameter_hints(definition: WorkflowDefinition | None, name: str) -> dict[str, Any]:
    item = _schema_item(definition, name)
    examples = _examples(item)
    return {
        "name": name,
        "label": getattr(item, "label", None) or name,
        "description": getattr(item, "description", None) or "",
        "examples": examples,
        "urls": _url_templates(definition, name),
    }


async def llm_normalize_values(
    items: list[dict[str, Any]],
    *,
    complete: CompleteFn | None = None,
) -> tuple[dict[tuple[str, str], dict[str, str]], dict[str, Any]]:
    """Map (call_id, param) -> {canonical, slug}. Empty when unused or on failure."""

    usage = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": False}
    pending = [item for item in items if str(item.get("value") or "").strip()]
    if not pending:
        return {}, usage
    user_words = ""
    if any(item.get("check") for item in pending):
        user_words = str(pending[0].get("_user_words") or "")[:1200]
    if complete is None and not env_api_key():
        return {}, usage
    payload = []
    for item in pending:
        payload.append(
            {
                "call": item.get("call"),
                "param": item.get("name"),
                "value": item.get("value"),
                "description": item.get("description") or item.get("label") or "",
                "examples": item.get("examples") or [],
                "urls": item.get("urls") or [],
                **({"check": True} if item.get("check") else {}),
            }
        )
    system = (
        "You normalize entity inputs to the official name a website workflow expects. "
        "Use the parameter description, examples, and URL pattern (including {name|slug}). "
        "Map nicknames and abbreviations to that official name and its URL slug "
        "(for example Spurs → Tottenham Hotspur / tottenham-hotspur, "
        "Man Utd → Manchester United / manchester-united). "
        "Leave a value unchanged when it is already official or you are not sure it is the same entity. "
        "Do not invent a different entity. "
        "If the value is clearly a different kind of thing than the parameter describes "
        "(for example a league or competition given for a club parameter, or a person for a place), "
        'add "fits":false and "kind":"<what the value is>". '
        'For items with "check":true, also add "from": the exact words copied from user_words that name or '
        "refer to that value (a nickname, abbreviation or translation counts, e.g. \"Spurs\"), or \"\" when "
        "nothing in user_words refers to it (a value picked as an example, or to stand in for a whole league "
        "or group). "
        'When nothing names it but exactly one value follows from what the user did name (for example the one '
        'competition all the named clubs play in), keep it and add "implied_by": the exact words copied from '
        "user_words it follows from. Never use implied_by to pick one of many (a club for a league, an example). "
        'Reply JSON only: '
        '{"values":[{"call":"c1","param":"team","canonical":"Official Name","slug":"official-name","fits":true}]}.'
    )
    fn = complete or chat_complete
    try:
        message, usage = await fn(
            [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"inputs": payload, **({"user_words": user_words} if user_words else {})}, ensure_ascii=False
                    )[:4200],
                },
            ],
            max_tokens=300,
            response_format={"type": "json_object"},
        )
    except Exception:
        return {}, usage
    text = message_text(message) if isinstance(message, dict) else str(message or "")
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
    try:
        data = json.loads(text or "{}")
    except Exception:
        return {}, usage or {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": False}
    rows = data.get("values") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        rows = [data] if isinstance(data, dict) and data.get("canonical") else []
    checked = {(str(item.get("call") or ""), str(item.get("name") or "")) for item in pending if item.get("check")}
    mapped: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        call_id = str(row.get("call") or "")
        param = str(row.get("param") or row.get("name") or "")
        canonical = str(row.get("canonical") or row.get("value") or "").strip()
        slug = str(row.get("slug") or "").strip()
        if not param:
            continue
        if row.get("fits") is False:
            mapped[(call_id, param)] = {"canonical": "", "slug": "", "fits": False, "kind": str(row.get("kind") or "").strip()[:60]}
            continue
        if (call_id, param) in checked and _not_from_user(row, user_words):
            implied = " ".join(str(row.get("implied_by") or "").lower().split())
            if implied and canonical and _span_in_user_words(implied, user_words) and not stated_in(canonical, implied):
                # Not named, but the only value that follows from what was named: run it, and say so.
                mapped[(call_id, param)] = {"canonical": canonical, "slug": slug or path_slug(canonical), "implied_by": implied}
                continue
            mapped[(call_id, param)] = {"canonical": "", "slug": "", "stated": False}
            continue
        if not canonical:
            continue
        mapped[(call_id, param)] = {"canonical": canonical, "slug": slug or path_slug(canonical)}
    return mapped, usage or {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": False}


def apply_local_canonical(value: str, hints: dict[str, Any]) -> str | None:
    return match_canonical_example(value, hints.get("examples") or [])


def _not_from_user(row: dict[str, Any], user_words: str) -> bool:
    """True only when the model gave no span, or a span that is not in the user's words.

    The span is verified by code, so a model cannot vouch for a value the user never wrote."""

    if "from" not in row:
        return row.get("stated") is False
    span = " ".join(str(row.get("from") or "").lower().split())
    if not span:
        return True
    return span not in " ".join((user_words or "").lower().split())


def _span_in_user_words(span: str, user_words: str) -> bool:
    """Every word of the model's span is (a near spelling of) a word the user wrote."""

    words = _tokens(user_words)
    toks = _tokens(span)
    return bool(toks) and all(any(t == w or (len(t) >= 4 and _similar(t, w) >= FUZZY_THRESHOLD) for w in words) for t in toks)


_GENERIC_TOKENS = {"the", "and", "of", "fc", "cf", "afc", "club", "team", "city", "united", "town", "sport", "league", "cup"}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", (text or "").lower())


def stated_in(value: str, context: str) -> bool:
    """Cheap check that an entity value comes from the user's words (or an obvious short form)."""

    if not context:
        return True
    ctx = " ".join(_tokens(context))
    val_tokens = _tokens(value)
    if not val_tokens:
        return True
    if " ".join(val_tokens) in ctx:
        return True
    ctx_tokens = set(ctx.split())
    if any(len(t) >= 3 and t not in _GENERIC_TOKENS and t in ctx_tokens for t in val_tokens):
        return True
    initials = "".join(t[0] for t in val_tokens if t not in {"the", "of", "and"})
    if len(initials) >= 2 and initials in ctx_tokens:
        return True
    return False


def _edit_distance(a: str, b: str) -> int:
    """Optimal string alignment distance: insert, delete, substitute, swap adjacent letters."""

    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def _similar(a: str, b: str) -> float:
    """Spelling similarity of two words (0..1) from edit distance; words under 4 letters only match exactly."""

    if a == b:
        return 1.0
    if min(len(a), len(b)) < 4:
        return 0.0
    return 1.0 - _edit_distance(a, b) / max(len(a), len(b))


FUZZY_THRESHOLD = 0.8


def fuzzy_user_span(value: str, context: str) -> str | None:
    """The user's word that is a misspelling of a significant word of ``value`` (\"chelse\" for
    Chelsea), or None. Used to accept a planner's corrected spelling as stated by the user."""

    words = [t for t in _tokens(context) if len(t) >= 4]
    best: tuple[float, str] | None = None
    for vt in _tokens(value):
        if len(vt) < 4 or vt in _GENERIC_TOKENS:
            continue
        for w in words:
            score = _similar(vt, w)
            if score >= FUZZY_THRESHOLD and (best is None or score > best[0]):
                best = (score, w)
    return best[1] if best else None


def _is_abbreviation(short: str, word: str) -> bool:
    """``short`` abbreviates ``word``: same first letter and its letters in order ("utd" -> united)."""

    if len(short) < 2 or len(short) >= len(word) or short[0] != word[0]:
        return False
    it = iter(word)
    return all(ch in it for ch in short)


def _token_match(a: str, b: str) -> float:
    """How well one user word matches one option word. Whole-word identity beats a prefix or
    abbreviation, which beat a spelling-close word; two different words that merely share a
    prefix ("city" / "united") score 0."""

    if a == b:
        return 1.0
    if len(a) >= 3 and b.startswith(a):
        return 0.92  # a truncation: "man" for manchester, "totten" for tottenham
    if _is_abbreviation(a, b):
        return 0.88  # "utd" for united
    if a.isdigit() or b.isdigit():
        return 0.0
    sim = _similar(a, b)
    return sim if sim >= FUZZY_THRESHOLD else 0.0


def _option_score(value_tokens: list[str], option: str) -> float:
    """Every user word must match a distinct option word; the score is the weakest match,
    with a small bonus for covering more of the option (Tottenham Hotspur vs Tottenham)."""

    ot = _tokens(option)
    if not value_tokens or not ot:
        return 0.0
    free = list(ot)
    scores: list[float] = []
    for vt in value_tokens:
        best_i, best = -1, 0.0
        for i, w in enumerate(free):
            m = _token_match(vt, w)
            if m > best:
                best_i, best = i, m
        if best <= 0:
            return 0.0
        scores.append(best)
        free.pop(best_i)
    coverage = (len(ot) - len(free)) / len(ot)
    return min(scores) + 0.01 * coverage


def fuzzy_known(value: str, known: Iterable[str]) -> tuple[str | None, list[str]]:
    """Best spelling match for ``value`` among an input's own enum/example values.

    Returns (match, []) for one clear best match at or above the threshold, (None, tied) when
    two or more candidates tie for best, and (None, []) when nothing is close enough. A value
    that is already one of the options (ignoring case and punctuation) is returned as is."""

    vt = _tokens(value)
    if not vt or all(t in _GENERIC_TOKENS for t in vt):
        return None, []
    options = list(dict.fromkeys(str(k) for k in known if str(k or "").strip()))
    for opt in options:
        if _tokens(opt) == vt:
            return opt, []
    compact = "".join(vt)
    scored: list[tuple[float, str]] = []
    for cand in options:
        score = _option_score(vt, cand)
        whole = _similar(compact, "".join(_tokens(cand)))
        if whole >= 0.9:
            score = max(score, whole)  # one-word typo of a run-together name
        if score >= FUZZY_THRESHOLD:
            scored.append((score, cand))
    if not scored:
        return None, []
    scored.sort(key=lambda x: -x[0])
    top = scored[0][0]
    tied = [c for sc, c in scored if top - sc < 0.005]
    if len(tied) > 1:
        return None, tied[:4]
    return scored[0][1], []


async def normalize_prepared_calls(
    calls: list[dict[str, Any]],
    *,
    definitions: dict[str, WorkflowDefinition] | None = None,
    complete: CompleteFn | None = None,
    user_words: str | None = None,
) -> dict[str, Any]:
    """Rewrite entity arguments in place. Returns LLM usage.

    With ``user_words`` (the request plus recent turns), an entity value that the user never
    named is double-checked in the same normaliser call; if the model confirms it was not
    stated (the planner picked an example club for a league question, say), the call gets
    ``unstated`` and is not run with an invented value."""

    pending: list[dict[str, Any]] = []
    for call in calls:
        raw = dict(call.get("raw_arguments") or call.get("arguments") or {})
        call["raw_arguments"] = dict(raw)
        args = dict(raw)
        definition = (definitions or {}).get(call.get("workflow_id") or "")
        if definition is None:
            definition = call.get("_definition")
        call["arguments"] = args
        for name, value in list(args.items()):
            if isinstance(value, (list, tuple)):
                # Each list item the user named is resolved against the input's own values;
                # an item is never dropped (unmatched items are passed on as given).
                known = _examples(_schema_item(definition, name))
                if known:
                    fixed = []
                    for v in value:
                        best, tied = fuzzy_known(str(v), known) if not match_canonical_example(str(v), known) else (None, [])
                        if best and best != v:
                            call.setdefault("resolved", []).append({"param": name, "value": best, "resolved_from": str(v)})
                            fixed.append(best)
                        else:
                            if tied:
                                call.setdefault("fuzzy_ties", []).append({"param": name, "value": str(v), "options": tied})
                            fixed.append(match_canonical_example(str(v), known) or v)
                    args[name] = fixed
                continue
            text = "" if value is None else str(value).strip()
            if not text or not is_entity_parameter(name, definition):
                continue
            hints = parameter_hints(definition, name)
            check = bool(user_words) and not stated_in(text, user_words or "")
            if check:
                # The planner may already have corrected the user's spelling ("chelse" -> Chelsea).
                span = fuzzy_user_span(text, user_words or "")
                if span:
                    check = False
                    call.setdefault("resolved", []).append({"param": name, "value": text, "resolved_from": span})
            local = apply_local_canonical(text, hints)
            if not local and hints.get("examples"):
                best, tied = fuzzy_known(text, hints.get("examples") or [])
                if best:
                    local = best
                    call.setdefault("resolved", []).append({"param": name, "value": best, "resolved_from": text})
                elif tied:
                    # A tie never overrides the planner: its value stands (the normaliser may still
                    # canonicalise it); the tie is recorded for the run.
                    call.setdefault("fuzzy_ties", []).append({"param": name, "value": text, "options": tied})
            if local and local != text:
                args[name] = _choose_form(local, path_slug(local), hints.get("examples") or [])
                if check:
                    pending.append({"call": call.get("id"), "name": name, "value": text, **hints, "check": True,
                                    "check_only": True, "_user_words": user_words, "workflow_id": call.get("workflow_id")})
                continue
            if local == text or (local and path_slug(local) == path_slug(text) and local.lower() == text.lower()):
                # Already matches an example spelling, aside from case — keep official example.
                if local and local != text:
                    args[name] = local
                if check:
                    pending.append({"call": call.get("id"), "name": name, "value": text, **hints, "check": True,
                                    "check_only": True, "_user_words": user_words, "workflow_id": call.get("workflow_id")})
                continue
            pending.append(
                {
                    "call": call.get("id"),
                    "name": name,
                    "value": text,
                    **hints,
                    **({"check": True, "_user_words": user_words} if check else {}),
                    "workflow_id": call.get("workflow_id"),
                }
            )
        call["arguments"] = args
        call["normalized"] = True
    mapped, usage = await llm_normalize_values(pending, complete=complete)
    for call in calls:
        definition = (definitions or {}).get(call.get("workflow_id") or "") or call.get("_definition")
        args = dict(call.get("arguments") or {})
        raw = call.get("raw_arguments") or {}
        for name, value in list(args.items()):
            if isinstance(value, (list, tuple)):
                continue
            text = "" if value is None else str(value).strip()
            row = mapped.get((str(call.get("id") or ""), name))
            if not row:
                continue
            if row.get("stated") is False:
                item = _schema_item(definition, name)
                if item is not None and item.required is False:
                    # An optional control the user did not name is simply left out (the page's
                    # own default applies); only a missing required value blocks the call.
                    args.pop(name, None)
                    continue
                hints = parameter_hints(definition, name)
                call.setdefault("unstated", []).append(
                    {"param": name, "value": text, "expects": hints.get("description") or hints.get("label") or name}
                )
                continue
            if row.get("fits") is False:
                hints = parameter_hints(definition, name)
                call.setdefault("kind_mismatch", []).append(
                    {"param": name, "value": text, "value_kind": row.get("kind") or "", "expects": hints.get("description") or hints.get("label") or name}
                )
                continue
            if row.get("implied_by"):
                call.setdefault("inferred", []).append({"param": name, "value": text, "implied_by": row["implied_by"]})
            # If the first pass already changed this field locally, do not overwrite.
            if raw.get(name) is not None and str(raw.get(name)).strip() != text:
                continue
            hints = parameter_hints(definition, name)
            chosen = _choose_form(row.get("canonical") or "", row.get("slug"), hints.get("examples") or [])
            if chosen:
                args[name] = chosen
        call["arguments"] = args
    return usage


async def normalize_arguments(
    definition: WorkflowDefinition | None,
    arguments: dict[str, Any],
    *,
    call_id: str = "c1",
    complete: CompleteFn | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    call = {"id": call_id, "arguments": dict(arguments or {}), "workflow_id": "wf"}
    usage = await normalize_prepared_calls([call], definitions={"wf": definition} if definition is not None else None, complete=complete)
    return dict(call.get("arguments") or {}), usage

"""Explore mode: turn families of on-page links into URL parameters (generic, no site rules).

A page that offers alternatives as links (date or month navigation, a competition or
category switcher, a fixtures/results filter) usually encodes the choice in one URL path
segment or one query value. Links that match the current URL everywhere except that one
place form a *family*; the varying place becomes a parameter, typed by its values:

- every value an ISO day (YYYY-MM-DD)  -> ``date`` input, ``{name|ymd}``, runtime default today
- every value a month (YYYY-MM)        -> ``month`` input, ``{name|ym}``, runtime default this month
- every value a year (YYYY)            -> ``year`` input
- otherwise                            -> an option input whose examples are the link values,
                                          default = the page's current value (page state)

Proposals are verified by the normal explore verifier (alternate URL must change the page).
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit

from browser_platform.workflow_inputs import pretty_label

_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_MONTH = re.compile(r"^\d{4}-\d{2}$")
_YEAR = re.compile(r"^(19|20)\d{2}$")
_LINK = re.compile(r"<a\b[^>]*?href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
MIN_ALTERNATIVES = 2
MAX_EXAMPLES = 12
MAX_ENUM = 6  # a small closed control (filter, sort, view); larger families stay open with examples


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()[:60]


def _kind(values: list[str]) -> str:
    if values and all(_ISO_DAY.match(v) for v in values):
        return "date"
    if values and all(_ISO_MONTH.match(v) for v in values):
        return "month"
    if values and all(_YEAR.match(v) for v in values):
        return "year"
    return "option"


def _segments(path: str) -> list[str]:
    return [p for p in path.split("/") if p != ""]


def _template_segments(template: str) -> list[str]:
    return _segments(urlsplit(template).path) if template else []


def propose_from_link_families(
    html: str,
    url: str,
    *,
    existing: set[str] | None = None,
    step_index: int = 0,
    step_template: str = "",
) -> list[dict[str, Any]]:
    """Parameters for the URL places that on-page link families vary."""

    existing = {str(n).lower() for n in (existing or set())}
    if not html or not url:
        return []
    here = urlsplit(url)
    cur = _segments(here.path)
    cur_query = dict(parse_qsl(here.query, keep_blank_values=True))
    template_segs = _template_segments(step_template)
    path_families: dict[int, dict[str, str]] = {}
    query_families: dict[str, dict[str, str]] = {}
    child_family: dict[str, str] = {}
    self_label = ""
    for href, inner in _LINK.findall(html):
        target = urlsplit(urljoin(url, unescape(href.strip())))
        if target.netloc and target.netloc.lower() != here.netloc.lower():
            continue
        segs = _segments(target.path)
        query = dict(parse_qsl(target.query, keep_blank_values=True))
        label = _text(inner)
        if segs == cur and query == cur_query:
            self_label = self_label or label  # how the page labels its current (default) view
            continue
        if segs == cur:
            diff = [k for k in set(query) | set(cur_query) if query.get(k) != cur_query.get(k)]
            if len(diff) == 1 and query.get(diff[0]):
                query_families.setdefault(diff[0], {})[query[diff[0]]] = label
            continue
        if len(segs) == len(cur) + 1 and segs[:-1] == cur and not query:
            child_family[segs[-1]] = label  # /page -> /page/<value>
            continue
        if len(segs) != len(cur) or query:
            continue
        diff = [i for i, (a, b) in enumerate(zip(segs, cur)) if a != b]
        if len(diff) == 1:
            path_families.setdefault(diff[0], {})[segs[diff[0]]] = label

    proposals: list[dict[str, Any]] = []
    used = set(existing)
    for pos, values in sorted(path_families.items()):
        if len(values) < MIN_ALTERNATIVES:
            continue
        if pos < len(template_segs) and "{" in template_segs[pos]:
            continue  # already a parameter
        current = cur[pos]
        options = [current] + [v for v in values if v != current]
        kind = _kind(options)
        if kind == "option" and _is_view_family(pos, cur, values, template_segs):
            continue  # a different view of the same parameterised thing: a sibling tool, not an input
        name = _name_for(kind, cur, pos, used)
        if not name:
            continue
        if self_label and current not in values:
            values = {current: self_label, **values}
        proposals.append(_proposal(name, kind, current, options, values, step_index, url, pos=pos))
        used.add(name)
    child_values = list(child_family)
    child_kind = _kind(child_values)
    if len(child_values) >= 3 and child_kind in {"date", "month", "year"} and child_kind not in used:
        # A page that links to dated children of itself (/fixtures -> /fixtures/2026-11): the
        # child segment is an optional parameter with a runtime default.
        proposal = _proposal(child_kind, child_kind, "", child_values, child_family, step_index, url, pos=len(cur))
        proposal["binding"]["append"] = True
        from browser_platform.dates import now_value

        now = now_value({"date": "day", "month": "month", "year": "year"}[child_kind])
        others = [v for v in sorted(child_values) if v != now]
        proposal["verify_with"] = next((v for v in others if v > now), others[-1] if others else None)
        proposals.append(proposal)
        used.add(child_kind)
    for key, values in sorted(query_families.items()):
        if len(values) < MIN_ALTERNATIVES or "{" in step_template.split("?", 1)[-1] and key in step_template:
            continue
        name = re.sub(r"[^a-z0-9_]+", "_", key.lower()).strip("_") or "option"
        if name in used:
            continue
        current = cur_query.get(key, "")
        options = ([current] if current else []) + [v for v in values if v != current]
        kind = _kind(options)
        proposal = _proposal(name, kind, current, options, values, step_index, url, query_key=key)
        if not current:
            shown = f" ('{self_label}')" if self_label else ""
            proposal["description"] += f" Leave it out for the page's default view{shown}."
            if self_label:
                proposal["default_view"] = self_label
        proposals.append(proposal)
        used.add(name)
    return proposals


def _name_for(kind: str, cur: list[str], pos: int, used: set[str]) -> str | None:
    if kind in {"date", "month", "year"}:
        return None if kind in used else kind
    # Name an option segment after the closest fixed segment that follows or precedes it
    # (/football/{x}/table -> "table_choice" is worse than a neutral, describable name).
    base = "option"
    for candidate in (f"{base}", f"{base}_{pos}"):
        if candidate not in used:
            return candidate
    return None


def _proposal(
    name: str,
    kind: str,
    current: str,
    options: list[str],
    labels: dict[str, str],
    step_index: int,
    url: str,
    *,
    pos: int | None = None,
    query_key: str | None = None,
) -> dict[str, Any]:
    filt = {"date": "|ymd", "month": "|ym", "year": ""}.get(kind, "|slug" if pos is not None else "")
    placeholder = "{" + name + filt + "}"
    alternatives = [v for v in options if v and v != current]
    examples = [v for v in options if v][:MAX_EXAMPLES]
    described = ", ".join(f"{v} ({labels[v]})" if labels.get(v) and labels[v].lower() != v.lower() else v for v in examples[:6])
    proposal: dict[str, Any] = {
        "name": name,
        "label": pretty_label(name),
        "required": False,
        "examples": examples,
        "description": f"Optional page choice taken from the page's own links, e.g. {described}.",
        "reason": f"{len(options)} links on the page differ from this URL only at this place.",
        "verify_with": alternatives[0] if alternatives else None,
        "link_family": {"kind": kind, "options": examples, "labels": {v: labels.get(v, "") for v in examples}},
    }
    if kind == "date":
        proposal.update({"format": "YYYY-MM-DD", "runtime_default": "today",
                         "description": "Optional calendar day (YYYY-MM-DD). Defaults to today at run time."})
    elif kind == "month":
        proposal.update({"format": "YYYY-MM", "runtime_default": "current_month",
                         "description": "Optional month (YYYY-MM). Defaults to the current month at run time."})
    elif kind == "year":
        proposal.update({"format": "YYYY", "default": current})
    elif current:
        proposal["default"] = current  # the page's current selection (page state)
    if kind == "option":
        # Path/query slugs are identifiers; the planner and run path can map labels to them.
        proposal["format"] = "slug"
        labels = {v: labels.get(v, "") for v in examples}
        mapping = {v: v for v in examples if v}
        for slug, label in labels.items():
            if label and str(label).strip():
                mapping[str(label).strip()] = slug
        if mapping:
            proposal["value_map"] = mapping
    if kind == "option" and 2 <= len(examples) <= MAX_ENUM:
        # The page's own links are the choices it offers: shown to the planner as name=a|b.
        proposal["enum"] = list(examples)
    if query_key is not None:
        proposal["binding"] = {"kind": "url_query", "step_index": step_index, "key": query_key,
                               "placeholder": placeholder, "source_url": url}
    else:
        proposal["binding"] = {"kind": "url_segment", "step_index": step_index, "position": pos,
                               "replace": current, "placeholder": placeholder, "source_url": url}
    return proposal


MAX_VIEWS = 6


def _is_view_family(pos: int, cur: list[str], values: dict[str, str] | None, template_segs: list[str]) -> bool:
    """The last path segment of an already-parameterised URL, with only a few alternatives,
    names a *view* of that entity (/{competition}/table vs /fixtures), not a value to pick."""

    if pos != len(cur) - 1 or len(cur) < 2:
        return False
    if not any("{" in seg for seg in template_segs):
        return False
    return values is None or len(values) <= MAX_VIEWS


def propose_sibling_views(html: str, url: str, template: str, *, limit: int = 6) -> list[dict[str, Any]]:
    """Other views of the same parameterised entity that the page links to.

    For a tool templated ``/x/{competition|slug}/table`` whose page links to
    ``/x/championship/scores-fixtures``, the sibling view is ``/x/{competition|slug}/scores-fixtures``:
    every placeholder keeps its current value and exactly one fixed segment differs.
    These are offered as *derived tool* suggestions; nothing is created automatically.
    """

    if not html or not url:
        return []
    template = template or url
    here = urlsplit(url)
    cur = _segments(here.path)
    tmpl = _template_segments(template)
    if len(cur) != len(tmpl):
        return []
    fixed = [i for i, seg in enumerate(tmpl) if "{" not in seg]
    found: dict[str, dict[str, Any]] = {}
    for href, inner in _LINK.findall(html):
        target = urlsplit(urljoin(url, unescape(href.strip())))
        if target.netloc and target.netloc.lower() != here.netloc.lower():
            continue
        segs = _segments(target.path)
        if len(segs) != len(cur) or target.query:
            continue
        diff = [i for i, (a, b) in enumerate(zip(segs, cur)) if a != b]
        if len(diff) != 1 or diff[0] not in fixed or diff[0] != len(cur) - 1:
            continue
        if not _is_view_family(diff[0], cur, None, tmpl):
            continue
        pos = diff[0]
        if any(_ISO_DAY.match(s) or _ISO_MONTH.match(s) for s in (segs[pos],)):
            continue
        new_tmpl = list(tmpl)
        new_tmpl[pos] = segs[pos]
        base = f"{here.scheme}://{here.netloc}/" if here.netloc else "/"
        key = segs[pos]
        if key not in found:
            found[key] = {
                "view": key,
                "label": _text(inner) or key.replace("-", " "),
                "url_template": base + "/".join(new_tmpl),
                "sample_url": base + "/".join(segs),
            }
    return list(found.values())[:limit]

def family_value_map(proposal: dict[str, Any]) -> dict[str, str]:
    """Label and slug aliases for a link-family proposal."""

    if proposal.get("value_map"):
        return {str(k): str(v) for k, v in proposal["value_map"].items() if k and v}
    family = proposal.get("link_family") or {}
    mapping: dict[str, str] = {}
    for slug in family.get("options") or proposal.get("examples") or []:
        if slug:
            mapping[str(slug)] = str(slug)
    for slug, label in (family.get("labels") or {}).items():
        if slug:
            mapping[str(slug)] = str(slug)
        if label and str(label).strip():
            mapping[str(label).strip()] = str(slug)
    return mapping


def _option_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _option_slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")


def _same_option(sample: Any, option: Any) -> bool:
    if sample in (None, "") or option in (None, ""):
        return False
    if _option_key(sample) == _option_key(option):
        return True
    a, b = _option_slug(sample), _option_slug(option)
    if a == b:
        return True
    # "Travel" matches "travel_2"; "Poetry" matches "poetry_23".
    return b.startswith(a + "-") or a.startswith(b + "-")


def reuse_existing_family_input(definition: Any, proposal: dict[str, Any]) -> str | None:
    """If a link family describes an existing unused input, reuse that input's name.

    Stops explore from adding a defaulted slug parameter (e.g. option=travel_2) while a
    required human parameter (e.g. category=Travel) is left unbound.
    """

    family = proposal.get("link_family") or {}
    if family.get("kind") not in {None, "", "option"}:
        return None
    from browser_platform.workflow_inputs import placeholders_in_definition

    bound = set(placeholders_in_definition(definition))
    mapping = family_value_map(proposal)
    options = list(family.get("options") or proposal.get("examples") or [])
    if not options and not mapping:
        return None
    for item in getattr(definition, "input_schema", None) or []:
        name = getattr(item, "name", None)
        if not name or name in bound or name == proposal.get("name"):
            continue
        samples = [getattr(item, "default", None), *(getattr(item, "examples", None) or [])]
        samples.extend(mapping.keys())
        # Only the input's own samples should trigger a match; mapping keys are aliases
        # of the family, not of this input. Compare the input's samples to family values.
        own = [getattr(item, "default", None), *(getattr(item, "examples", None) or [])]
        for sample in own:
            if sample in (None, ""):
                continue
            if str(sample) in mapping or str(sample).strip() in mapping:
                return name
            if any(_same_option(sample, option) for option in options):
                return name
            if any(_same_option(sample, label) for label in mapping):
                return name
    return None


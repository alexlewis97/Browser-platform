"""Check that a required parameter is actually reflected in the page state."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

# A short results page (local fixtures, small extracts) can be scoped by body text.
# A long directory (all fixtures, all teams) mentioning the value once is not enough.
_SHORT_PAGE = 2500
_WEAK_QUERY_KEYS = {"q", "query", "s", "search", "term", "k", "keyword"}


def value_slugs(value: str) -> list[str]:
    text = (value or "").strip()
    if not text:
        return []
    lowered = text.lower()
    tokens = [t for t in re.split(r"\W+", lowered) if t]
    slugs = [
        lowered,
        lowered.replace(" ", "-"),
        lowered.replace(" ", "_"),
        lowered.replace(" ", ""),
        "-".join(tokens),
        "_".join(tokens),
        "".join(tokens),
    ]
    if tokens:
        slugs.extend(tokens)
    unique: list[str] = []
    for item in slugs:
        if item and item not in unique:
            unique.append(item)
    unique.sort(key=len, reverse=True)
    return unique


def _contains(haystack: str, value: str) -> bool:
    if not haystack or not value:
        return False
    lowered = haystack.lower()
    if value.lower() in lowered:
        return True
    # Allow "england men" to match "England Men's" / "england-men"
    compact_hay = re.sub(r"\W+", "", lowered)
    compact_val = re.sub(r"\W+", "", value.lower())
    return bool(compact_val) and compact_val in compact_hay


def url_path_includes_value(url: str, value: str) -> bool:
    path = (urlparse(url or "").path or "").lower()
    for slug in value_slugs(value):
        if len(slug) >= 3 and slug in path:
            return True
    return False


def url_only_has_weak_query(url: str, value: str) -> bool:
    """True when the value appears only as a typeahead/search query, not in the path."""
    if url_path_includes_value(url, value):
        return False
    parsed = urlparse(url or "")
    query = parse_qs(parsed.query)
    for key, values in query.items():
        if key.lower() not in _WEAK_QUERY_KEYS:
            continue
        if any(_contains(item, value) for item in values):
            return True
    return False


def is_not_found_page(*, url: str = "", title: str = "", headings: list[str] | None = None, text: str = "") -> bool:
    hay = " ".join([title or "", " ".join(headings or []), (text or "")[:800]]).lower()
    if re.search(r"\b404\b", hay) and re.search(r"not found|couldn.?t find that page|page not found", hay):
        return True
    return False


def parameter_reflected(
    *,
    url: str,
    title: str = "",
    headings: list[str] | None = None,
    text: str = "",
    selected: list[str] | None = None,
    value: str,
) -> bool:
    """Return True when the page is actually scoped to `value`, not merely mentioning it."""
    value = (value or "").strip()
    if not value:
        return True
    if is_not_found_page(url=url, title=title, headings=headings, text=text):
        return False
    if url_path_includes_value(url, value):
        return True
    if _contains(title or "", value):
        return True
    for heading in headings or []:
        if _contains(heading, value):
            return True
    for item in selected or []:
        if _contains(item, value):
            return True
    body = text or ""
    if _contains(body, value) and len(body) <= _SHORT_PAGE and not url_only_has_weak_query(url, value):
        return True
    return False


_FORM_NAMES = frozenset({
    "first_name", "last_name", "firstname", "lastname", "full_name", "middle_name",
    "zip", "zip_code", "postal", "postal_code", "postcode",
    "email", "gender", "mobile", "phone", "telephone", "tel",
    "customer", "size", "toppings", "topping", "password", "username", "user",
    "address", "city", "state", "country", "card", "cvv", "comment", "comments",
    "message", "company", "dob", "date_of_birth", "age",
})
_FILTER_NAMES = frozenset({
    "team", "month", "date", "query", "term", "search", "q", "tab", "sort",
    "page", "filter", "club", "competition", "league", "category", "section", "year",
})
_LOCATOR_NAMES = frozenset({"product", "item", "article", "result", "book"})
_CHECKOUT_TASK = re.compile(r"\b(check\s*out|checkout|finish the order|place the order)\b", re.I)
_SUBMIT_TASK = re.compile(r"\b(submit|send the form|post the form|echo)\b", re.I)
_CONFIRM_HINT = re.compile(
    r"thank you for your order|checkout-complete|thanks for submitting|confirmation|success|custname|order has been dispatched",
    re.I,
)
_PRE_ACTION_URL = re.compile(
    r"inventory\.html|/cart\.html$|/forms/post/?$|automation-practice-form|inventory-container",
    re.I,
)


def classify_parameter(name: str, *, steps: list | None = None, task: str = "") -> str:
    """Classify an input as a page filter/locator or a form-entry field."""

    n = re.sub(r"[^a-z0-9_]+", "_", (name or "").strip().lower()).strip("_")
    if n in _FORM_NAMES or n.endswith(("_name", "_email", "_phone", "_zip")):
        return "form"
    if n in _LOCATOR_NAMES:
        return "locator"
    if n in _FILTER_NAMES:
        return "filter"
    for step in steps or []:
        typ = getattr(step, "type", None) or (step.get("type") if isinstance(step, dict) else None)
        url = getattr(step, "url", None) or (step.get("url") if isinstance(step, dict) else "") or ""
        value = getattr(step, "value", None) or (step.get("value") if isinstance(step, dict) else "") or ""
        selected = getattr(step, "selectedText", None) or ""
        target = getattr(step, "target_text", None) or ""
        blob = " ".join([url, value, str(selected), str(target)])
        if "{" + n in blob and (typ in {"navigation", "navigate"} or "{" + n in url):
            return "filter"
        if typ == "click" and "{" + n in blob:
            return "locator"
        if typ in {"input", "select_change"} and "{" + n in blob:
            return "form"
    return "filter"


def _event_type(event) -> str:
    return str(getattr(event, "type", None) or (event.get("type") if isinstance(event, dict) else "") or "")


def _event_value(event) -> str:
    raw = getattr(event, "value", None)
    if raw is None and isinstance(event, dict):
        raw = event.get("value")
    return "" if raw is None else str(raw)


def value_was_typed(events: list | None, value: str) -> bool:
    """True when a recorded input/select actually typed or chose `value`."""

    want = (value or "").strip().lower()
    if not want:
        return True
    for event in events or []:
        if _event_type(event) not in {"input", "select_change"}:
            continue
        got = _event_value(event).strip().lower()
        if got == want or (len(want) >= 4 and want in got):
            return True
    return False


def value_echoed(*, title: str = "", headings: list[str] | None = None, text: str = "", value: str) -> bool:
    """True when the confirmation, modal, or echo payload contains `value`."""

    value = (value or "").strip()
    if not value:
        return True
    if _contains(title or "", value):
        return True
    for heading in headings or []:
        if _contains(heading, value):
            return True
    return _contains(text or "", value)


def locator_chosen(events: list | None, value: str) -> bool:
    want = (value or "").strip().lower()
    if not want:
        return True
    for event in events or []:
        typ = _event_type(event)
        if typ not in {"click", "input", "select_change"}:
            continue
        parts = [_event_value(event)]
        text = getattr(event, "text", None) or (event.get("text") if isinstance(event, dict) else None)
        if text:
            parts.append(str(text))
        element = getattr(event, "element", None) or (event.get("element") if isinstance(event, dict) else None) or {}
        if isinstance(element, dict):
            parts.extend(str(element.get(k) or "") for k in ("name", "text", "ariaLabel"))
        blob = " ".join(parts).lower()
        if want in blob:
            return True
    return False


def terminal_goal_missing(task: str, *, url: str = "", title: str = "", text: str = "") -> str | None:
    """If the task ends in submit/checkout, require a confirmation page, not a pre-action URL."""

    hay = " ".join([title or "", text or "", url or ""])
    if _CHECKOUT_TASK.search(task or ""):
        if re.search(r"checkout-complete|thank you for your order", hay, re.I):
            return None
        return f"The demonstration stopped before checkout finished (page was {url or 'unknown'}). Complete checkout through Finish and extract the confirmation."
    if _SUBMIT_TASK.search(task or ""):
        if _CONFIRM_HINT.search(hay):
            return None
        if _PRE_ACTION_URL.search(url or "") or not (text or "").strip():
            return f"The form was not submitted (page was {url or 'unknown'}). Submit it and extract the confirmation or echoed result."
    return None


def missing_parameters(
    *,
    url: str,
    title: str = "",
    headings: list[str] | None = None,
    text: str = "",
    selected: list[str] | None = None,
    values: dict[str, str],
    kinds: dict[str, str] | None = None,
    events: list | None = None,
    task: str = "",
    steps: list | None = None,
) -> list[str]:
    """Return unmet required inputs. Filters must be reflected; form fields must be typed or echoed."""

    missing: list[str] = []
    for name, value in (values or {}).items():
        kind = (kinds or {}).get(name) or classify_parameter(name, steps=steps, task=task)
        if kind == "form":
            if value_was_typed(events, str(value)) or value_echoed(title=title, headings=headings, text=text, value=str(value)):
                continue
            missing.append(f"{name}={value}")
            continue
        if kind == "locator":
            if locator_chosen(events, str(value)) or parameter_reflected(
                url=url, title=title, headings=headings, text=text, selected=selected, value=str(value)
            ):
                continue
            missing.append(f"{name}={value}")
            continue
        if not parameter_reflected(url=url, title=title, headings=headings, text=text, selected=selected, value=str(value)):
            missing.append(f"{name}={value}")
    goal = terminal_goal_missing(task, url=url, title=title, text=text)
    if goal:
        missing.append(goal)
    return missing

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


def missing_parameters(
    *,
    url: str,
    title: str = "",
    headings: list[str] | None = None,
    text: str = "",
    selected: list[str] | None = None,
    values: dict[str, str],
) -> list[str]:
    missing: list[str] = []
    for name, value in values.items():
        if not parameter_reflected(url=url, title=title, headings=headings, text=text, selected=selected, value=str(value)):
            missing.append(f"{name}={value}")
    return missing

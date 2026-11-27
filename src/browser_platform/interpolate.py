"""Replace {var} and {{var}} placeholders from workflow inputs / context."""

from __future__ import annotations

import re
from typing import Any

_BRACE = re.compile(r"\{\{?\s*([a-zA-Z_][\w]*)(?:\|([a-zA-Z0-9_]+))?\s*\}?\}")

_YM_FORMATS = {"ym", "yyyymm", "year_month", "current", "current_month"}
_MONTH_NAMES = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


_PLACEHOLDER_VALUES = frozenset({
    "current", "current_month", "now", "$current_month", "default", "auto", "none", "null",
})


def is_placeholder_value(value: object) -> bool:
    """True for tokens like 'current' that are not a real user-stated value."""

    text = str(value or "").strip().lower()
    return text in _PLACEHOLDER_VALUES


def current_year_month() -> str:
    """Calendar month in the user's time zone (default Asia/Jerusalem), as YYYY-MM."""
    from browser_platform.dates import now_value

    return now_value("month")


def shift_year_month(value: str | None, months: int = 1) -> str:
    year_s, month_s = year_month(value).split("-")
    year, month = int(year_s), int(month_s) + int(months)
    while month > 12:
        month -= 12
        year += 1
    while month < 1:
        month += 12
        year -= 1
    return f"{year:04d}-{month:02d}"


def year_month(value: str | None) -> str:
    """Normalize a month value to YYYY-MM. Empty or 'current' uses the current month.

    Relative expressions ("last month", "yesterday") resolve deterministically.
    """
    from browser_platform.dates import resolve

    text = str(value or "").strip()
    if not text or text.lower() in {"current", "current_month", "now", "$current_month"}:
        return current_year_month()
    resolved = resolve(text, "month")
    return resolved or current_year_month()


def format_date(value: str | None, grain: str) -> str:
    """Interpolation filter for date grains; unparseable text is passed through unchanged."""
    from browser_platform.dates import now_value, resolve

    text = str(value or "").strip()
    if not text:
        return now_value(grain)
    return resolve(text, grain) or text


_DATE_FILTERS = {"ymd": "date", "date": "date", "day": "date", "iso_date": "date", "y": "year", "year": "year", "yyyy": "year"}


def format_value(value: str, fmt: str | None) -> str:
    text = str(value or "")
    if not fmt:
        return text
    if fmt in {"slug", "hyphen"}:
        # Site convention: join words with hyphens ("England men" → "england-men").
        return path_slug(text)
    if fmt in {"slug_", "underscore"}:
        return re.sub(r"\s+", "_", text.strip()).lower()
    if fmt == "head":
        # First-word fallback when a site only uses the leading token ("England men" → "england").
        tokens = [tok for tok in re.split(r"\W+", text.strip()) if tok]
        return (tokens[0] if tokens else text).lower()
    if fmt in _YM_FORMATS:
        return year_month(text)
    if fmt in _DATE_FILTERS:
        return format_date(text, _DATE_FILTERS[fmt])
    if fmt == "dmy":
        iso = format_date(text, "date")
        parts = iso.split("-")
        return f"{parts[2]}/{parts[1]}/{parts[0]}" if len(parts) == 3 else iso
    return text



def lookup_variable(variables: dict[str, Any] | None, key: str) -> Any:
    """Resolve a placeholder name, ignoring key case (Team vs team)."""

    if not variables:
        return None
    if key in variables and variables[key] is not None:
        return variables[key]
    lowered = {str(name).lower(): value for name, value in variables.items()}
    value = lowered.get(key.lower())
    return value

def interpolate(value: Any, variables: dict[str, Any] | None) -> Any:
    if not isinstance(value, str):
        return value
    variables = variables or {}

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        fmt = match.group(2) if match.lastindex and match.lastindex >= 2 else None
        found = lookup_variable(variables, key)
        if fmt in _YM_FORMATS:
            return year_month(None if found is None or str(found).strip() == "" else str(found))
        if fmt in _DATE_FILTERS and (found is None or str(found).strip() == ""):
            return format_date(None, _DATE_FILTERS[fmt])
        if found is not None:
            return format_value(str(found), fmt)
        return match.group(0)

    return _BRACE.sub(repl, value)


def path_slug(value: str) -> str:
    text = re.sub(r"\s+", "-", (value or "").strip())
    return text.lower()


def interpolate_url(url: Any, variables: dict[str, Any] | None) -> Any:
    """Interpolate a URL, then rewrite path segments that are input values into slugs.

    ``/teams/{team}`` with team=Arsenal becomes ``/teams/arsenal``. Wikipedia titles
    are left to ``normalize_wikipedia_url`` and are not slugged here.
    """
    if not isinstance(url, str):
        return interpolate(url, variables)
    updated = interpolate(url, variables or {})
    if isinstance(updated, str) and "?" in updated:
        # An optional query parameter whose input was not given is dropped, not sent empty.
        base, _, query = updated.partition("?")
        kept = [pair for pair in query.split("&") if pair and not re.fullmatch(r"[^=]+=(\{[^}]*\})?", pair)]
        updated = base + ("?" + "&".join(kept) if kept else "")
    if not isinstance(updated, str) or not (variables or {}):
        return updated
    from urllib.parse import unquote, urlparse, urlunparse

    parsed = urlparse(updated)
    host = (parsed.netloc or "").lower()
    if "wikipedia.org" in host or "wikimedia.org" in host:
        return updated
    parts = parsed.path.split("/")
    changed = False
    raw_values = [str(v).strip() for v in variables.values() if v is not None and str(v).strip()]
    for i, part in enumerate(parts):
        if not part:
            continue
        decoded = unquote(part)
        for text in raw_values:
            candidates = {
                text,
                text.replace(" ", "_"),
                text.replace(" ", "-"),
                text.replace(" ", "+"),
            }
            lowered = {item.lower() for item in candidates}
            if decoded in candidates or decoded.lower() in lowered:
                slug = path_slug(text)
                if parts[i] != slug:
                    parts[i] = slug
                    changed = True
                break
    if not changed:
        return updated
    return urlunparse(parsed._replace(path="/".join(parts)))



def is_navigable_url(url: str | None) -> bool:
    """True when `url` is a real http(s)/about URL, never a CSS selector or XPath."""

    text = (url or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.startswith(("about:", "data:")):
        return True
    if text.startswith(("#", ".", "[", "/", "(")) or lowered.startswith(("xpath=", "css=", "text=", "//")):
        return False
    if "id=" in text and "://" not in text:
        return False
    if "://" not in text:
        return False
    from urllib.parse import urlparse

    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or parsed.netloc or "").strip()
    if not host or "id=" in host or '"' in host or "'" in host:
        return False
    return True

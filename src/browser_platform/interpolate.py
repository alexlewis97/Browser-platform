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


def current_year_month() -> str:
    """Calendar month in Asia/Jerusalem, as YYYY-MM."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Jerusalem")).strftime("%Y-%m")


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
    """Normalize a month value to YYYY-MM. Empty or 'current' uses the current month."""
    text = str(value or "").strip()
    if not text or text.lower() in {"current", "current_month", "now", "$current_month"}:
        return current_year_month()
    match = re.match(r"^(20\d{2})[-/](\d{1,2})(?:[-/]\d{1,2})?$", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}"
    tokens = re.findall(r"[A-Za-z]+|\d{4}|\d{1,2}", text)
    month_num = None
    year = None
    for tok in tokens:
        low = tok.lower()
        if low in _MONTH_NAMES:
            month_num = _MONTH_NAMES[low]
        elif re.fullmatch(r"20\d{2}", tok):
            year = int(tok)
    if month_num:
        year = year or int(current_year_month()[:4])
        return f"{year:04d}-{month_num:02d}"
    return current_year_month()




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

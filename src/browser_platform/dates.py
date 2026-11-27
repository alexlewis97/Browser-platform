"""Deterministic date resolution for typed workflow inputs.

The planner passes what the user said ("yesterday", "last Saturday", "June 2026", "2026-11-26");
code resolves it against the server clock in the user's time zone. The LLM never computes dates.

Grains: ``date`` (YYYY-MM-DD), ``month`` (YYYY-MM), ``year`` (YYYY).
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Jerusalem"

# Input formats (WorkflowInput.format) that carry a calendar grain.
DATE_FORMATS = {"date", "day", "ymd", "yyyy-mm-dd", "iso_date"}
MONTH_FORMATS = {"month", "ym", "yyyymm", "year_month", "current_month"}
YEAR_FORMATS = {"year", "yyyy", "season"}

# Tokens that mean "resolve at run time to now" (a runtime default, not a user value).
NOW_TOKENS = {"current", "now", "today", "this", "current_month", "current_date", "current_year", "$current_month", "$today"}

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10}


def user_tz() -> ZoneInfo:
    name = os.environ.get("BROWSER_PLATFORM_TIMEZONE") or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def today(now: datetime | date | None = None) -> date:
    if isinstance(now, datetime):
        return now.astimezone(user_tz()).date() if now.tzinfo else now.date()
    if isinstance(now, date):
        return now
    return datetime.now(user_tz()).date()


def grain_of(fmt: str | None, name: str | None = None) -> str | None:
    """Calendar grain implied by an input's declared format (never guessed from free text)."""

    f = str(fmt or "").strip().lower()
    if f in DATE_FORMATS:
        return "date"
    if f in MONTH_FORMATS:
        return "month"
    if f in YEAR_FORMATS:
        return "year"
    return None


def _shift_month(d: date, months: int) -> date:
    y, m = d.year, d.month + months
    while m > 12:
        m, y = m - 12, y + 1
    while m < 1:
        m, y = m + 12, y - 1
    return date(y, m, 1)


def _num(tok: str) -> int | None:
    if tok.isdigit():
        return int(tok)
    return _NUM_WORDS.get(tok)


def parse_expression(text: str, *, now: datetime | date | None = None) -> tuple[date, str] | None:
    """Parse a date expression into (anchor date, natural grain) or None if not a date."""

    t0 = today(now)
    raw = str(text or "").strip()
    s = re.sub(r"[,]+", " ", raw.lower())
    s = re.sub(r"\b(the|on|of|for|from|at|in the)\b", " ", s)
    s = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", s)
    s = " ".join(s.split())
    if not s:
        return None
    if s in NOW_TOKENS or s in {"tonight", "this day"}:
        return t0, "date"
    if s in {"yesterday", "last night"}:
        return t0 - timedelta(days=1), "date"
    if s == "tomorrow":
        return t0 + timedelta(days=1), "date"
    if s in {"day before yesterday"}:
        return t0 - timedelta(days=2), "date"
    if s in {"day after tomorrow"}:
        return t0 + timedelta(days=2), "date"
    if s in {"this month", "current month"}:
        return t0.replace(day=1), "month"
    if s in {"last month", "previous month"}:
        return _shift_month(t0, -1), "month"
    if s in {"next month"}:
        return _shift_month(t0, 1), "month"
    if s in {"this year", "current year"}:
        return date(t0.year, 1, 1), "year"
    if s in {"last year", "previous year"}:
        return date(t0.year - 1, 1, 1), "year"
    if s in {"next year"}:
        return date(t0.year + 1, 1, 1), "year"
    if s in {"this week"}:
        return t0 - timedelta(days=t0.weekday()), "date"
    if s in {"last week"}:
        return t0 - timedelta(days=t0.weekday() + 7), "date"
    if s in {"next week"}:
        return t0 + timedelta(days=7 - t0.weekday()), "date"
    # Weekends anchor on their Saturday. "This weekend" on a Sunday is today's weekend.
    if s in {"this weekend", "the weekend", "weekend", "coming weekend", "this coming weekend"}:
        return t0 + timedelta(days=(5 - t0.weekday()) if t0.weekday() <= 5 else -1), "date"
    if s in {"next weekend"}:
        sat = t0 + timedelta(days=(5 - t0.weekday()) if t0.weekday() <= 5 else -1)
        return sat + timedelta(days=7), "date"
    if s in {"last weekend", "previous weekend"}:
        sat = t0 + timedelta(days=(5 - t0.weekday()) if t0.weekday() <= 5 else -1)
        return sat - timedelta(days=7), "date"
    m = re.fullmatch(r"(\w+) (day|days|week|weeks|month|months|year|years) ago", s)
    n = _num(m.group(1)) if m else None
    if m and n is not None:
        unit = m.group(2).rstrip("s")
        if unit == "day":
            return t0 - timedelta(days=n), "date"
        if unit == "week":
            return t0 - timedelta(days=7 * n), "date"
        if unit == "month":
            return _shift_month(t0, -n), "month"
        return date(t0.year - n, 1, 1), "year"
    m = re.fullmatch(r"in (\w+) (day|days|week|weeks|month|months)", s)
    n = _num(m.group(1)) if m else None
    if m and n is not None:
        unit = m.group(2).rstrip("s")
        if unit == "day":
            return t0 + timedelta(days=n), "date"
        if unit == "week":
            return t0 + timedelta(days=7 * n), "date"
        return _shift_month(t0, n), "month"
    m = re.fullmatch(r"(?:(last|this|next|previous|coming) )?(\w+)", s)
    if m and m.group(2) in _WEEKDAYS:
        wd = _WEEKDAYS[m.group(2)]
        back = (t0.weekday() - wd) % 7
        fwd = (wd - t0.weekday()) % 7
        mod = m.group(1)
        if mod in {"last", "previous"}:
            return t0 - timedelta(days=back or 7), "date"
        if mod in {"next", "coming"}:
            return t0 + timedelta(days=fwd or 7), "date"
        if mod == "this":
            return t0 + timedelta(days=fwd), "date"
        # Bare weekday: the nearest one (ties go to the past).
        return (t0 - timedelta(days=back)) if back <= fwd else (t0 + timedelta(days=fwd)), "date"
    # ISO forms.
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)), "date")
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})", s)
    if m:
        return _safe(int(m.group(1)), int(m.group(2)), 1, "month")
    # Day-first numeric (dd/mm/yyyy), the convention in Israel and Europe.
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", s)
    if m:
        year = int(m.group(3))
        year = year + 2000 if year < 100 else year
        return _safe(year, int(m.group(2)), int(m.group(1)), "date")
    m = re.fullmatch(r"(\d{4})", s)
    if m and 1900 < int(s) < 2100:
        return date(int(s), 1, 1), "year"
    # Month names: "15 june 2026", "june 15 2026", "june 2026", "june", "15 june".
    toks = s.split()
    month = next((_MONTHS[t] for t in toks if t in _MONTHS), None)
    if month is None:
        return None
    rest = [t for t in toks if t not in _MONTHS and t not in _WEEKDAYS]
    year = next((int(t) for t in rest if re.fullmatch(r"\d{4}", t)), None)
    day = next((int(t) for t in rest if re.fullmatch(r"\d{1,2}", t)), None)
    if any(not re.fullmatch(r"\d{1,4}", t) for t in rest):
        return None
    year = year or t0.year
    if day:
        return _safe(year, month, day, "date")
    return _safe(year, month, 1, "month")


def _safe(y: int, m: int, d: int, grain: str) -> tuple[date, str] | None:
    try:
        return date(y, m, d), grain
    except ValueError:
        return None


def resolve(value: object, grain: str, *, now: datetime | date | None = None) -> str | None:
    """Resolve a user/planner value to a canonical string at ``grain``; None if not a date.

    A coarser expression than the grain asks for (``June 2026`` for a ``date`` input) is not
    silently narrowed to a day: it returns None so the caller can ask instead of guessing.
    """

    parsed = parse_expression(str(value or ""), now=now)
    if parsed is None:
        return None
    anchor, natural = parsed
    order = {"year": 0, "month": 1, "date": 2}
    if order.get(natural, 2) < order.get(grain, 2):
        return None
    return render(anchor, grain)


def render(d: date, grain: str) -> str:
    if grain == "year":
        return f"{d.year:04d}"
    if grain == "month":
        return f"{d.year:04d}-{d.month:02d}"
    return d.isoformat()


def now_value(grain: str, *, now: datetime | date | None = None) -> str:
    return render(today(now), grain)


_WD = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_REL = re.compile(
    r"\b(day before yesterday|day after tomorrow|yesterday|today|tonight|tomorrow|last night"
    r"|(?:last|this|next|previous|coming|this coming) (?:weekend|week|month|year|" + _WD + r")"
    r"|the weekend"
    r"|(?:\d+|a|an|one|two|three|four|five|six|seven|ten) (?:days?|weeks?|months?|years?) ago"
    r"|in (?:\d+|a|an|one|two|three|four|five|six|seven|ten) (?:days?|weeks?|months?)"
    r"|(?:on )?(?:" + _WD + r"))(?:'s)?\b",
    re.I,
)


def find_expressions(text: str) -> list[str]:
    """Relative date expressions in free text, in order ("yesterday", "last saturday")."""

    out: list[str] = []
    for match in _REL.finditer(text or ""):
        expr = re.sub(r"^on ", "", match.group(1).lower())
        if expr not in out:
            out.append(expr)
    return out


def anchor_to_message(value: object, grain: str, message: str, *, now: datetime | date | None = None) -> str | None:
    """If the user spoke in relative terms and the planner sent a computed date that matches
    none of them, return the code-resolved date instead (None = keep the value).

    Deterministic: the server clock in the user's time zone decides what "yesterday" is.
    """

    text = str(value or "").strip()
    if not text:
        return None
    exprs = find_expressions(message)
    if not exprs:
        return None
    own = resolve(text, grain, now=now)
    if text.lower() in {e.lower() for e in exprs}:
        return None  # already the user's words; resolution happens downstream
    candidates = [r for r in (resolve(e, grain, now=now) for e in exprs) if r]
    if not candidates or own in candidates:
        return None
    # An absolute date the user typed themselves wins over any relative words.
    if own and re.search(re.escape(text), message or "", re.I):
        return None
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else None


__all__ = ["anchor_to_message", "find_expressions", "DATE_FORMATS", "MONTH_FORMATS", "YEAR_FORMATS", "NOW_TOKENS", "grain_of", "now_value", "parse_expression", "render", "resolve", "today", "user_tz"]

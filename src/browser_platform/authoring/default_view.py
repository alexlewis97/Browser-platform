"""Detect a page's default or current-day view and record it on the tool.

Listing pages often show today's items under a 'Today' heading, an active tab,
or a date-strip selection, without printing a date on each row. Explore stores
that label as ``default_view`` on the matching optional control (or on the
workflow) so later date normalisation can date those items.

Detection is generic: active/current UI, headings, today-words, and today's
date in common formats. No site-specific rules.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from html import unescape
from typing import Any

from browser_platform.interpolate import interpolate_url
from browser_platform.models.workflow import WorkflowDefinition

_TODAY_WORDS = (
    "today",
    "tonight",
    "todays",
    "today's",
    "aujourd'hui",
    "hoy",
    "oggi",
    "heute",
    "vandaag",
    "dzisiaj",
    "idag",
    "i dag",
    "сегодня",
    "היום",
)
_TODAY_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _TODAY_WORDS if " " not in w) + r")\b|i dag|aujourd'hui|today's",
    re.I,
)
# Opening tags that look like the current / selected control.
_ACTIVE_OPEN = re.compile(
    r"<([a-zA-Z][\w:-]*)\b[^>]*?(?:"
    r"aria-current\s*=\s*[\"'](?:page|date|true|location|step)[\"']"
    r"|aria-selected\s*=\s*[\"']true[\"']"
    r"|aria-pressed\s*=\s*[\"']true[\"']"
    r"|data-(?:current|selected|active)\s*=\s*[\"'](?:true|page|date|1)[\"']"
    r"|class\s*=\s*[\"'][^\"']*(?:^|[\s_-])(?:is-)?(?:active|current|selected|today)(?:[\s_-]|$)[^\"']*[\"']"
    r")[^>]*>",
    re.I,
)
_HEADING = re.compile(r"<h([1-4])\b[^>]*>(.*?)</h\1>", re.I | re.S)
_INNER_END = re.compile(r"</[a-zA-Z][\w:-]*>")
_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][\w]*)(?:\|[^}]*)?\}")


def _visible_text(fragment: str, limit: int = 80) -> str:
    text = unescape(_TAG.sub(" ", fragment or ""))
    return _WS.sub(" ", text).strip()[:limit]


def _month_names(today: date) -> list[str]:
    full = calendar.month_name[today.month]
    abbr = calendar.month_abbr[today.month]
    return [n for n in (full, abbr, full[:3], abbr.rstrip(".")) if n]


def _ordinal(day: int) -> str:
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def today_date_patterns(today: date | None = None) -> list[str]:
    """Human-visible forms of today's date, longest first, for generic matching."""

    today = today or date.today()
    day = today.day
    month = today.month
    year = today.year
    full, abbr = calendar.month_name[month], calendar.month_abbr[month]
    weekday, wabbr = calendar.day_name[today.weekday()], calendar.day_abbr[today.weekday()]
    ord_day = _ordinal(day)
    patterns = [
        today.isoformat(),
        f"{weekday} {ord_day} {full} {year}",
        f"{weekday} {ord_day} {full}",
        f"{weekday} {ord_day} {abbr}",
        f"{wabbr} {ord_day} {full}",
        f"{wabbr} {ord_day} {abbr}",
        f"{ord_day} {full} {year}",
        f"{ord_day} {full}",
        f"{ord_day} {abbr} {year}",
        f"{ord_day} {abbr}",
        f"{day:02d} {full} {year}",
        f"{day} {full} {year}",
        f"{full} {day}, {year}",
        f"{full} {day} {year}",
        f"{day:02d} {abbr} {year}",
        f"{day} {abbr} {year}",
        f"{abbr} {day}, {year}",
        f"{abbr} {day} {year}",
        f"{wabbr} {day} {abbr}",
        f"{weekday} {day} {abbr}",
        f"{weekday} {day} {full}",
        f"{wabbr} {day} {full}",
        f"{day:02d} {abbr}",
        f"{day} {abbr}",
        f"{abbr} {day}",
        f"{full} {day}",
        f"{day:02d}/{month:02d}/{year}",
        f"{day}/{month}/{year}",
        f"{month:02d}/{day:02d}/{year}",
        f"{day:02d}.{month:02d}.{year}",
        f"{day}.{month}.{year}",
        f"{day:02d}/{month:02d}",
        f"{day}/{month}",
        f"{day:02d}.{month:02d}",
        f"{month:02d}/{day:02d}",
    ]
    # Unique, longest first so "27 November 2026" wins over "27 November".
    seen: set[str] = set()
    out: list[str] = []
    for item in patterns:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    out.sort(key=len, reverse=True)
    return out


def _is_today_label(text: str, today: date) -> str | None:
    """Return a normalised label if *text* names today, else None."""

    cleaned = _WS.sub(" ", (text or "").strip())
    if not cleaned or len(cleaned) > 60:
        return None
    lower = cleaned.lower()
    # Reject obvious other-day words unless the whole label is today's date.
    if re.search(r"\b(yesterday|tomorrow|tonight's)?\b", lower) and not _TODAY_RE.search(lower):
        if "yesterday" in lower or "tomorrow" in lower:
            return None
    if _TODAY_RE.search(cleaned):
        # Keep a short phrase ("Nov Today", "Today's fixtures" -> "Today")
        match = _TODAY_RE.search(cleaned)
        word = match.group(0)
        # If a month name for this month also appears, combine like "Nov Today".
        month_hit = next((m for m in _month_names(today) if re.search(rf"\b{re.escape(m)}\b", cleaned, re.I)), None)
        if month_hit and month_hit.lower() not in word.lower():
            return f"{month_hit} {word}".replace(" today's", " Today").strip()
        # Prefer the canonical "Today" when the label is just the word plus noise.
        if re.fullmatch(rf"{re.escape(word)}('s)?(\s+(games?|fixtures?|matches?|results?|events?))?", cleaned, re.I):
            return "Today" if word.lower() in {"today", "todays", "today's"} else word
        return cleaned
    for pattern in today_date_patterns(today):
        if re.search(re.escape(pattern), cleaned, re.I):
            return cleaned
    return None


def _active_labels(html: str) -> list[str]:
    labels: list[str] = []
    for match in _ACTIVE_OPEN.finditer(html or ""):
        start = match.end()
        # Read a short window of inner HTML; stop at the next close of this tag if cheap.
        window = (html or "")[start : start + 400]
        close = re.search(rf"</{re.escape(match.group(1))}\s*>", window, re.I)
        fragment = window[: close.start()] if close else window.split("<", 1)[0]
        text = _visible_text(fragment)
        if text:
            labels.append(text)
    return labels


def _heading_labels(html: str) -> list[str]:
    return [_visible_text(m.group(2)) for m in _HEADING.finditer(html or "") if _visible_text(m.group(2))]


def detect_default_view(html: str, *, today: date | None = None) -> str | None:
    """Return a short label for the page's default or current-day view, if any."""

    if not html:
        return None
    today = today or date.today()
    groups = [
        _active_labels(html),
        _heading_labels(html),
    ]
    loose: list[str] = []
    for match in re.finditer(
        r"<(?:span|li|div|p|strong|em|time|td|th|label)\b[^>]*>([^<]{1,60})</(?:span|li|div|p|strong|em|time|td|th|label)>",
        html or "",
        re.I,
    ):
        text = _visible_text(match.group(1))
        if text:
            loose.append(text)
    groups.append(loose)

    def _month_in(text: str) -> str | None:
        return next((m for m in _month_names(today) if re.search(rf"\b{re.escape(m)}\b", text, re.I)), None)

    # Prefer an active tab/date-strip, then a heading, then a loose section label.
    # Month headings are kept alongside today-labels so "Nov" + "Today" become "Nov Today".
    all_raw: list[str] = []
    picked: str | None = None
    for raw in groups:
        all_raw.extend(raw)
        hits = [label for text in raw if (label := _is_today_label(text, today))]
        if hits and picked is None:
            picked = hits[0]
        combined = next((item for item in hits if _month_in(item) and _TODAY_RE.search(item)), None)
        today_only = next((item for item in hits if _TODAY_RE.search(item)), None)
        month_only = next((text for text in raw if _month_in(text) and not _TODAY_RE.search(text)), None)
        if combined:
            return combined
        if today_only and month_only:
            return f"{_month_in(month_only)} {_TODAY_RE.search(today_only).group(0)}"
        if today_only and picked is None:
            picked = today_only
        elif hits and picked is None:
            picked = hits[0]
    if picked and _TODAY_RE.search(picked) and not _month_in(picked):
        month_only = next((text for text in all_raw if _month_in(text) and not _TODAY_RE.search(text)), None)
        if month_only:
            return f"{_month_in(month_only)} {_TODAY_RE.search(picked).group(0)}"
    return picked


_MONTH_WORDS = "|".join(
    [calendar.month_name[i] for i in range(1, 13)] + [calendar.month_abbr[i] for i in range(1, 13)]
)
_WEEKDAY_WORDS = "|".join(list(calendar.day_name) + list(calendar.day_abbr))
_DATE_SECTION = re.compile(
    r"^(?:"
    rf"(?:(?:{_WEEKDAY_WORDS})\s+)?(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{_MONTH_WORDS})(?:\s+(?P<year>\d{{4}}))?"
    r"|"
    rf"(?:(?:{_WEEKDAY_WORDS})\s+)?(?P<month2>{_MONTH_WORDS})\s+(?P<day2>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<year2>\d{{4}}))?"
    r")$",
    re.I,
)


def _is_date_section_label(text: str) -> bool:
    cleaned = _WS.sub(" ", (text or "").strip())
    if not cleaned or len(cleaned) > 60:
        return False
    return bool(_DATE_SECTION.match(cleaned))


def default_view_variables(definition: WorkflowDefinition, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    """Required inputs only, so the page's own default (not a selected tab) is fetched."""

    given = dict(variables or {})
    out: dict[str, Any] = {}
    for item in definition.input_schema or []:
        if item.required is not True:
            continue
        if given.get(item.name) not in (None, ""):
            out[item.name] = given[item.name]
        elif item.default not in (None, ""):
            out[item.name] = item.default
        elif getattr(item, "examples", None):
            out[item.name] = item.examples[0]
    return out


def default_view_urls(definition: WorkflowDefinition, variables: dict[str, Any] | None = None) -> list[str]:
    """Candidate start URLs for the page default: optional path/query placeholders dropped first."""

    nav = next((getattr(step, "url", None) for step in (definition.steps or []) if getattr(step, "url", None)), None) or ""
    if not nav:
        return []
    optional = [item.name for item in (definition.input_schema or []) if item.required is not True]
    stripped = nav
    for name in optional:
        stripped = re.sub(rf"/{{{re.escape(name)}(?:\|[^}}]*)?}}", "", stripped)
    urls: list[str] = []
    for template in (stripped, nav):
        url = interpolate_url(template, variables or {})
        if not isinstance(url, str) or "://" not in url:
            continue
        if _PLACEHOLDER.search(url):
            continue
        if url not in urls:
            urls.append(url)
    return urls


def attach_default_view(definition: WorkflowDefinition, label: str) -> WorkflowDefinition:
    """Record *label* on the optional view control, else on the workflow itself."""

    label = (label or "").strip()
    if not label:
        return definition
    derived = definition.model_copy(deep=True)
    nav_url = next((step.url for step in (derived.steps or []) if step.url), "") or ""
    query = nav_url.split("?", 1)[1] if "?" in nav_url else ""
    optional = [item for item in (derived.input_schema or []) if item.required is not True]

    def _in_part(part: str, item: Any) -> bool:
        return bool(re.search(rf"\{{{re.escape(item.name)}(?:\|[^}}]*)?\}}", part or ""))

    target = next((item for item in optional if _in_part(query, item)), None)
    if target is None:
        target = next((item for item in optional if getattr(item, "enum", None) and _in_part(nav_url, item)), None)
    if target is None:
        target = next((item for item in optional if getattr(item, "enum", None)), None)
    if target is None:
        target = next((item for item in optional if _in_part(nav_url, item)), None)
    if target is not None:
        derived.input_schema = [
            item.model_copy(update={"default_view": label}) if item.name == target.name else item
            for item in derived.input_schema
        ]
    else:
        derived.default_view = label
    return derived

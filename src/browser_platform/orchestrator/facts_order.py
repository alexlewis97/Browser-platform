"""Dates and times in extracted facts as sortable data, and orderings computed in code.

Synthesis models misorder times ("who plays first") and sometimes claim "none" while the
facts list items. This module turns facts into events (date, time, context), computes the
ordering a question asks for, and checks a drafted answer against it."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from browser_platform import dates

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTHS["sept"] = 9
_MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE_RE = re.compile(
    r"\b(?P<iso>\d{4}-\d{2}-\d{2})\b"
    r"|\b(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m1>" + _MON + r")\b\.?(?:,?\s+(?P<y1>\d{4}))?"
    r"|\b(?P<m2>" + _MON + r")\.?\s+(?P<d2>\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(?P<y2>\d{4}))?",
    re.I,
)
_TIME_RE = re.compile(r"(?<![\d:])(?P<h>[01]?\d|2[0-3])[:.](?P<m>[0-5]\d)(?![\d:])(?:\s*(?P<ap>[ap]\.?m\.?))?", re.I)

ORDER_WORDS = {
    "asc": {"first", "earliest", "soonest", "sooner", "next", "before", "earlier"},
    "desc": {"last", "latest", "later"},
}


def _year_for(month: int, day: int, today: date) -> int:
    best = today.year
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            cand = date(y, month, day)
        except ValueError:
            continue
        if abs((cand - today).days) < abs((date(best, month, min(day, 28)) - today).days):
            best = y
    return best


def _date_of(m: re.Match, today: date) -> date | None:
    try:
        if m.group("iso"):
            return date.fromisoformat(m.group("iso"))
        day = int(m.group("d1") or m.group("d2"))
        month = _MONTHS[(m.group("m1") or m.group("m2")).lower()[:3]]
        year = m.group("y1") or m.group("y2")
        return date(int(year), month, day) if year else date(_year_for(month, day, today), month, day)
    except (ValueError, KeyError, TypeError):
        return None


def parse_events(facts: str, *, today: date | None = None, undated_day: date | None = None) -> list[dict[str, Any]]:
    """Times in reading order, each tied to the nearest preceding date (or ``undated_day``)."""

    text = " ".join(str(facts or "").split())
    today = today or dates.today()
    marks: list[tuple[int, str, Any]] = []
    for m in _DATE_RE.finditer(text):
        d = _date_of(m, today)
        if d:
            marks.append((m.start(), "date", d))
    for m in _TIME_RE.finditer(text):
        h, mi = int(m.group("h")), int(m.group("m"))
        ap = (m.group("ap") or "").lower().replace(".", "")
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        marks.append((m.start(), "time", (h, mi, m)))
    marks.sort(key=lambda x: x[0])
    events: list[dict[str, Any]] = []
    current: date | None = undated_day
    date_end = 0
    for pos, kind, value in marks:
        if kind == "date":
            current = value
            m = _DATE_RE.match(text, pos)
            date_end = m.end() if m else pos
            continue
        h, mi, m = value
        hhmm = f"{h:02d}:{mi:02d}"
        if events and events[-1]["time"] == hhmm and events[-1]["date"] == (current.isoformat() if current else None) and pos - events[-1]["_pos"] < 120:
            events[-1]["_end"] = m.end()
            continue  # the same time printed twice for one row
        # Context: the words since the previous date or time mark, so the row's own names are kept.
        prev = events[-1]["_end"] if events else 0
        context = text[max(prev, date_end, pos - 120):m.end()].strip(" ,;")
        events.append({
            "date": current.isoformat() if current else None,
            "time": hhmm,
            "sort": f"{current.isoformat() if current else '9999-99-99'}T{hhmm}",
            "context": context[-120:],
            "_pos": pos,
            "_end": m.end(),
        })
    for e in events:
        e.pop("_pos", None)
        e.pop("_end", None)
    return events


def order_direction(question: str) -> str | None:
    """'asc' or 'desc' for an ordering question; words inside date phrases ("next weekend",
    "last month") do not count."""

    text = (question or "").lower()
    for expr in sorted(dates.find_expressions(text), key=len, reverse=True):
        text = text.replace(expr.lower(), " ")
    words = set(re.findall(r"[a-z]+", text))
    if words & ORDER_WORDS["asc"]:
        return "asc"
    if words & ORDER_WORDS["desc"]:
        return "desc"
    return None


def _aliases(item: dict[str, Any]) -> list[str]:
    vals = list(_label(item).values()) + [v for k, v in (item.get("raw_inputs") or {}).items() if k in _label(item)]
    return sorted({str(v).lower() for v in vals if isinstance(v, str) and v.strip()})


def _label(item: dict[str, Any]) -> dict[str, Any]:
    keep = {}
    for k, v in (item.get("inputs") or {}).items():
        if v in (None, "") or re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", str(v)):
            continue
        keep[k] = v
    return keep


def _today_view(item: dict[str, Any]) -> bool:
    return any("today" in str(v.get("page_label") or "").lower() for v in item.get("page_view") or [] if isinstance(v, dict))


def question_days(question: str) -> list[str]:
    """Calendar days the question refers to; a weekend covers Saturday and Sunday."""

    from datetime import timedelta

    days: set[str] = set()
    for expr in dates.find_expressions(question or ""):
        value = dates.resolve(expr, "date")
        if not value:
            continue
        days.add(value)
        if "weekend" in expr.lower():
            d = date.fromisoformat(value)
            start = d - timedelta(days=(d.weekday() - 5)) if d.weekday() >= 5 else d
            days.update({start.isoformat(), (start + timedelta(days=1)).isoformat()})
    return sorted(days)


def computed_facts(compact: list[dict[str, Any]], question: str, *, today: date | None = None) -> dict[str, Any]:
    """Structured events per result, plus the ordering or day filter the question asks for."""

    today = today or dates.today()
    qdays = question_days(question)
    out: dict[str, Any] = {}
    per_item: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for item in compact:
        if item.get("status") != "succeeded" or item.get("data_status") == "no_data":
            continue
        undated = today if _today_view(item) else None
        events = parse_events(item.get("facts") or "", today=today, undated_day=undated)
        if events:
            per_item.append((item, events))
    if not per_item:
        return out
    if qdays:
        hits = [
            {"inputs": _label(item), **{k: e[k] for k in ("date", "time", "context")}}
            for item, events in per_item for e in events if e["date"] in qdays
        ]
        out["events_on_question_dates"] = {"dates": qdays, "count": len(hits), "events": hits[:20]}
    direction = order_direction(question)
    if direction and len(per_item) >= 2:
        rows = []
        undated: list[dict[str, Any]] = []
        for item, events in per_item:
            dated = [e for e in events if e["date"]]
            if qdays:
                dated = [e for e in dated if e["date"] in qdays]
            pool = [e for e in dated if e["date"] >= today.isoformat()] or dated
            if not pool:
                if not any(e["date"] for e in events):
                    first = events[0]
                    undated.append({"inputs": _label(item), "time": first["time"], "context": first["context"]})
                continue
            pick = min(pool, key=lambda e: e["sort"]) if direction == "asc" else max(pool, key=lambda e: e["sort"])
            rows.append({"inputs": _label(item), "_names": _aliases(item), "date": pick["date"], "time": pick["time"], "context": pick["context"], "_sort": pick["sort"]})
        rows.sort(key=lambda r: r["_sort"], reverse=(direction == "desc"))
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        for a, b in zip(rows, rows[1:]):
            if a["_sort"] == b["_sort"]:
                a["tied_with_next"] = True
                b["rank"] = a["rank"]
        for r in rows:
            r.pop("_sort", None)
        if len(rows) + len(undated) >= 2 and rows:
            out["computed_order"] = {
                "order": "earliest upcoming first" if direction == "asc" else "latest first",
                "times": "as printed on the page",
                "rows": rows,
                **({"undated": undated, "undated_note": "the page printed no date for these items, so they are not ranked"} if undated else {}),
            }
    return out


def _names(row: dict[str, Any]) -> list[str]:
    return row.get("_names") or [str(v).lower() for v in (row.get("inputs") or {}).values() if isinstance(v, str)]


def _mentions(text: str, name: str) -> int:
    """Position of the first mention of an entity name (or its first significant word); -1 if absent."""

    low = text.lower()
    if name in low:
        return low.index(name)
    for word in re.findall(r"[a-z0-9]{4,}", name):
        m = re.search(r"\b" + re.escape(word), low)
        if m:
            return m.start()
    return -1


_NONE_RE = re.compile(r"\b(no|none|not any|nothing)\b", re.I)


def answer_conflicts(answer: str, computed: dict[str, Any]) -> list[str]:
    """Cheap checks of a drafted answer's lead against the computed data."""

    lead = re.split(r"\n|(?<=[.!?])\s", (answer or "").strip(), maxsplit=1)[0]
    problems: list[str] = []
    order = (computed.get("computed_order") or {}).get("rows") or []
    if len(order) >= 2:
        positions = []
        for row in order:
            pos = min([p for p in (_mentions(lead, n) for n in _names(row)) if p >= 0] or [-1])
            positions.append(pos)
        named = [(p, i) for i, p in enumerate(positions) if p >= 0]
        top = {i for i, r in enumerate(order) if r.get("rank") == 1}
        # Only a lead that names a lower-ranked entity first contradicts the order (ties allowed).
        if named and min(named)[1] not in top and not (computed["computed_order"].get("undated")):
            first = order[0]
            problems.append(f"computed_order rank 1 is {first['inputs']} ({first['date']} {first['time']}), but the answer leads with another")
    hits = computed.get("events_on_question_dates") or {}
    if hits.get("count") and _NONE_RE.search(lead):
        problems.append(f"events_on_question_dates lists {hits['count']} item(s) on {', '.join(hits.get('dates') or [])}, but the answer says none")
    return problems


def answer_from_computed(computed: dict[str, Any]) -> str:
    """Deterministic answer when the model keeps contradicting the computed data."""

    order = (computed.get("computed_order") or {}).get("rows") or []
    lines: list[str] = []
    if len(order) >= 2:
        first = order[0]
        who = ", ".join(str(v) for v in first["inputs"].values())
        lines.append(f"**{who}** is {'first' if computed['computed_order']['order'].startswith('earliest') else 'latest'}: {first['context']} ({first['date']} {first['time']}, page time).")
        lines.append("")
        lines.append("| Rank | Input | Date | Time | Item |")
        lines.append("| --- | --- | --- | --- | --- |")
        for r in order:
            lines.append(f"| {r['rank']} | {', '.join(str(v) for v in r['inputs'].values())} | {r['date']} | {r['time']} | {r['context']} |")
        for r in computed["computed_order"].get("undated") or []:
            lines.append(f"| ? | {', '.join(str(v) for v in r['inputs'].values())} | not shown | {r['time']} | {r['context']} |")
        return "\n".join(lines)
    hits = computed.get("events_on_question_dates") or {}
    if hits.get("count"):
        lines.append(f"{hits['count']} item(s) on {', '.join(hits['dates'])}:")
        lines.append("")
        lines.append("| Input | Date | Time | Item |")
        lines.append("| --- | --- | --- | --- |")
        for e in hits["events"]:
            lines.append(f"| {', '.join(str(v) for v in e['inputs'].values())} | {e['date']} | {e['time']} | {e['context']} |")
    return "\n".join(lines)


def for_prompt(computed: dict[str, Any]) -> dict[str, Any]:
    """The computed data without internal matching fields."""

    out = dict(computed)
    if "computed_order" in out:
        order = dict(out["computed_order"])
        order["rows"] = [{k: v for k, v in r.items() if not k.startswith("_")} for r in order.get("rows") or []]
        out["computed_order"] = order
    return out


# --- numeric facts in tabular results ------------------------------------------------------

_NUM_CELL = re.compile(r"^[+\-−]?[£$€¥₪]?\s?[+\-−]?\d[\d,]*(?:\.\d+)?\s?%?$")


def _num(cell: str) -> float | None:
    c = cell.strip().replace("−", "-")
    if not _NUM_CELL.match(c):
        return None
    try:
        return float(re.sub(r"[£$€¥₪,%\s]", "", c))
    except ValueError:
        return None


def _cells(block: str) -> list[str]:
    return [" ".join(c.split()) for c in block.split("\t") if c.strip()]


def parse_table(raw: str) -> dict[str, Any] | None:
    """Rows of a table from layout-aware page text (cells split by tabs, rows by blank lines).

    Generic: the header is the tab-separated block just before the first row with the same
    number of cells; numeric columns are the ones whose cells parse as numbers in most rows;
    a row whose first cell starts with an integer that follows the row order gets a rank."""

    text = str(raw or "")
    if "\t" not in text:
        return None
    blocks = [b for b in re.split(r"\n[ \r]*\n", text) if "\t" in b]
    parsed = [_cells(b) for b in blocks]
    sizes = [len(c) for c in parsed if len(c) >= 3 and sum(_num(x) is not None for x in c) >= 2]
    if len(sizes) < 3:
        return None
    width = max(set(sizes), key=sizes.count)
    rows_cells = [c for c in parsed if len(c) == width and sum(_num(x) is not None for x in c) >= 2]
    header = next((c for c in parsed if len(c) == width and sum(_num(x) is not None for x in c) == 0), None)
    if len(rows_cells) < 3:
        return None
    labels = [h for h in (header or [f"column {i + 1}" for i in range(width)])]
    numeric_cols = [
        i for i in range(1, width)
        if sum(_num(r[i]) is not None for r in rows_cells) >= 0.8 * len(rows_cells)
    ]
    rows = []
    for order, r in enumerate(rows_cells, 1):
        first = r[0]
        m = re.match(r"^(\d{1,4})\s+(.+)$", first)
        rank = int(m.group(1)) if m else None
        name = m.group(2) if m else first
        values = {labels[i]: _num(r[i]) for i in numeric_cols if _num(r[i]) is not None}
        rows.append({"name": name, "rank": rank, "order": order, "values": values})
    ranked = sum(1 for r in rows if r["rank"] is not None and abs(r["rank"] - r["order"]) <= 3)
    if ranked < 0.8 * len(rows):
        for r in rows:
            r["rank"] = None
    return {"columns": [labels[i] for i in numeric_cols], "labels": labels, "rows": rows}


_MORE = {"more", "most", "higher", "highest", "greater", "greatest", "bigger", "biggest", "larger", "largest", "max", "maximum", "top", "above", "better", "best", "ahead", "over"}
_LESS = {"less", "least", "fewer", "fewest", "lower", "lowest", "smaller", "smallest", "min", "minimum", "bottom", "below", "worse", "worst", "behind", "under", "cheaper", "cheapest"}
_SUPERLATIVE = {"most", "least", "highest", "lowest", "greatest", "fewest", "biggest", "smallest", "largest", "top", "bottom", "best", "worst", "cheapest", "max", "maximum", "min", "minimum"}
_POSITION_WORDS = {"above", "below", "top", "bottom", "position", "place", "rank", "ranked", "ahead", "behind", "higher", "lower", "standing", "standings"}


def _metric_for(question_tokens: list[str], columns: list[str]) -> str | None:
    """The numeric column the question names (whole word, prefix or abbreviation: 'pts' -> Points)."""

    from browser_platform.orchestrator.normalize_inputs import _is_abbreviation

    best: tuple[float, str] | None = None
    for col in columns:
        ct = re.findall(r"[a-z]+", col.lower())
        score = 0.0
        for q in question_tokens:
            for w in ct:
                if q == w or (len(q) >= 4 and (w.startswith(q) or q.startswith(w)) and min(len(q), len(w)) >= 4):
                    score += 1.0
                elif _is_abbreviation(q, w):
                    score += 0.8
        if score and (best is None or score > best[0] + 1e-9):
            best = (score, col)
        elif score and best and abs(score - best[0]) < 1e-9:
            best = (score, "")  # tie: no clear column
    return best[1] if best and best[1] else None


def _row_mentions(question: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows the user names, in the order named (whole name, a significant word, or a near spelling)."""

    from browser_platform.orchestrator.normalize_inputs import _GENERIC_TOKENS, _similar, _tokens

    qtok = _tokens(question)
    found: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        words = [w for w in _tokens(row["name"]) if len(w) >= 3 and w not in _GENERIC_TOKENS and w not in {"and", "the"}]
        pos = None
        for i, q in enumerate(qtok):
            if len(q) < 3:
                continue
            if any(q == w or (len(q) >= 4 and _similar(q, w) >= 0.8) for w in words):
                pos = i
                break
        if pos is not None:
            found.append((pos, row))
    # One user word naming several rows is ambiguous: keep a row only if its word is unique to it.
    by_pos: dict[int, list[dict[str, Any]]] = {}
    for pos, row in found:
        by_pos.setdefault(pos, []).append(row)
    return [rows_[0] for pos, rows_ in sorted(by_pos.items()) if len(rows_) == 1]


def table_comparison(items: list[dict[str, Any]], question: str) -> dict[str, Any] | None:
    """For a comparison question over a table (more/less, above/below, top/bottom), the answer
    computed from the parsed rows."""

    from browser_platform.orchestrator.normalize_inputs import _tokens

    qtok = _tokens(question)
    words = set(qtok)
    direction = "more" if words & _MORE else "less" if words & _LESS else None
    if not direction:
        return None
    for item in items:
        if item.get("status") not in {"succeeded", "succeeded_with_fallback"}:
            continue
        raw = "\n".join(str(v) for v in (item.get("outputs") or {}).values() if isinstance(v, str))
        table = parse_table(raw)
        if not table:
            continue
        metric = _metric_for([t for t in qtok if t not in _MORE | _LESS], table["columns"])
        use_rank = metric is None and bool(words & _POSITION_WORDS) and any(r["rank"] is not None for r in table["rows"])
        if not metric and not use_rank:
            continue
        label_words = {w for lab in table.get("labels") or [] for w in re.findall(r"[a-z]+", lab.lower())}
        named = _row_mentions(" ".join(t for t in qtok if t not in label_words), table["rows"])
        superlative = bool(words & _SUPERLATIVE)
        pool = named if len(named) >= 2 else (table["rows"] if (not named or superlative) else [])
        if superlative and len(named) < 2:
            named = []
        if len(pool) < 2:
            continue

        def key(r: dict[str, Any]) -> float:
            if use_rank:
                return -(r["rank"] if r["rank"] is not None else 1e9)  # rank 1 is highest
            return r["values"].get(metric, float("-inf"))

        ordered = sorted(pool, key=key, reverse=(direction == "more"))
        top = ordered[0]
        tied = [r for r in ordered if key(r) == key(top)]
        out = {
            "by": "rank (row position)" if use_rank else metric,
            "direction": "highest first" if direction == "more" else "lowest first",
            "named": [r["name"] for r in named],
            "rows": [
                {"name": r["name"], **({"rank": r["rank"]} if r["rank"] is not None else {}),
                 **({"value": r["values"].get(metric)} if metric else {})}
                for r in ordered[:10]
            ],
            "answer": top["name"] if len(tied) == 1 else None,
            **({"tied": [r["name"] for r in tied]} if len(tied) > 1 else {}),
        }
        if len(named) == 2 and re.match(r"^\s*(is|are|was|were|does|do|did|has|have)\b", question, re.I):
            # "Is A above B?": yes when the first-named row wins.
            out["yes_no"] = "yes" if out["answer"] == named[0]["name"] else ("no" if out["answer"] else None)
        return out
    return None


def table_conflicts(answer: str, comparison: dict[str, Any] | None) -> list[str]:
    if not comparison or not answer:
        return []
    lead = re.split(r"\n|(?<=[.!?])\s", answer.strip(), maxsplit=1)[0]
    problems = []
    yn = comparison.get("yes_no")
    m = re.match(r"^\W*(yes|no)\b", lead, re.I)
    if yn and m and m.group(1).lower() != yn:
        problems.append(f"table_comparison answers {yn} ({comparison['answer']} wins by {comparison['by']}), but the answer says {m.group(1)}")
    winner = comparison.get("answer")
    named = comparison.get("named") or []
    if winner and len(named) >= 2 and not m:
        positions = [(_mentions(lead, n.lower()), n) for n in named]
        positions = [(p, n) for p, n in positions if p >= 0]
        if positions and min(positions)[1] != winner:
            problems.append(f"table_comparison says {winner} wins by {comparison['by']}, but the answer leads with {min(positions)[1]}")
    return problems


def answer_from_table(comparison: dict[str, Any]) -> str:
    rows = comparison.get("rows") or []
    head = ""
    if comparison.get("yes_no"):
        head = f"{comparison['yes_no'].capitalize()}: "
    winner = comparison.get("answer") or " and ".join(comparison.get("tied") or [])
    lines = [f"{head}**{winner}** {'is' if comparison.get('answer') else 'are level'} by {comparison['by']}.", "", "| Name | Rank | Value |", "| --- | --- | --- |"]
    for r in rows:
        if comparison.get("named") and r["name"] not in comparison["named"]:
            continue
        lines.append(f"| {r['name']} | {r.get('rank', '')} | {r.get('value', '')} |")
    return "\n".join(lines)

"""Keep chat answers tied to what the workflow extracts actually contain.

- ``data_status``: an extract that is empty or only shows an empty-state message
  ("no fixtures", "not found", ...) is *no data / unverified*, never proof of a negative.
- ``ungrounded_claims``: cheap deterministic check of a synthesised answer. Numbers and
  quoted / bold phrases must appear in the evidence (extracts, the question, the inputs,
  today's date). Anything else is treated as invented and the answer is not used.
"""

from __future__ import annotations

import re
from typing import Any

from browser_platform import dates

_EMPTY_STATE = re.compile(
    r"\b(no (?:fixtures|results|matches|events|games|data|items|records|entries|records found|upcoming)"
    r"|nothing (?:to show|found|scheduled)|not found|no .{0,30} (?:found|available|scheduled)"
    r"|there are no|there is no|currently no|0 results)\b",
    re.I,
)
_MONTHS = {
    name: index
    for index, names in enumerate(
        [
            ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",),
            ("june", "jun"), ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"),
            ("october", "oct"), ("november", "nov"), ("december", "dec"),
        ],
        1,
    )
    for name in names
}


def data_status(facts: str | None, *, status: str | None = None) -> str:
    """``ok`` | ``no_data`` (empty or an empty-state page) | ``failed``."""

    if status and status not in {"succeeded", "succeeded_with_fallback"}:
        return "failed"
    text = " ".join(str(facts or "").split())
    if not text:
        return "no_data"
    # An empty-state message dominating a short extract means the page showed nothing.
    if _EMPTY_STATE.search(text) and len(text) < 400:
        return "no_data"
    return "ok"


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


_CITATION = re.compile(r"\b[\w.]+\s*=\s*[^\s,;)\]]+")
_SCORE = re.compile(r"\d+\s*[-–:]\s*\d+")


def _numbers(text: str) -> set[str]:
    return {str(int(n)) if n.isdigit() and len(n) < 6 else n for n in re.findall(r"\d+", text)}


def date_context(question: str) -> dict[str, Any]:
    """Today and the calendar days the question refers to, resolved by code (for synthesis)."""

    today = dates.today()
    out: dict[str, Any] = {"today": today.strftime("%A %d %B %Y") + f" ({today.isoformat()})"}
    resolved = {}
    for expr in dates.find_expressions(question or ""):
        value = dates.resolve(expr, "date")
        if value:
            try:
                from datetime import date as _date

                resolved[expr] = _date.fromisoformat(value).strftime("%A %d %B %Y") + f" ({value})"
            except ValueError:
                resolved[expr] = value
    if resolved:
        out["dates_in_question"] = resolved
    return out


def evidence_text(compact: list[dict[str, Any]], question: str, extra: str = "") -> str:
    parts = [question or "", extra or ""]
    for item in compact:
        parts.append(str(item.get("facts") or ""))
        for value in (item.get("inputs") or {}).values():
            parts.append(str(value))
        for value in (item.get("raw_inputs") or {}).values():
            parts.append(str(value))
        parts.append(str(item.get("workflow") or ""))
    today = dates.today()
    parts.append(today.isoformat())
    # Dates the user referred to relatively ("last Saturday") resolved by code, so an answer may name them.
    for expr in dates.find_expressions(question or ""):
        for grain in ("date", "month"):
            value = dates.resolve(expr, grain)
            if value:
                parts.append(value)
                try:
                    from datetime import date as _date

                    d = _date.fromisoformat(value if len(value) == 10 else value + "-01")
                    parts.append(d.strftime("%A %d %B %Y"))
                except Exception:
                    pass
    return "\n".join(parts)


_STOP = {"the", "and", "for", "with", "that", "this", "from", "was", "were", "are", "has", "have", "had", "you", "your"}


def _significant(text: str) -> list[str]:
    return [t for t in _norm(text).split() if len(t) >= 3 and t not in _STOP]


def ungrounded_claims(answer: str, compact: list[dict[str, Any]], question: str, extra: str = "") -> list[str]:
    """Numbers and quoted text in the answer that the evidence does not contain.

    Deliberately narrow (low false positives): headings, bold labels and paraphrase are
    formatting; a number or a quoted on-page message that is nowhere in the extracts is not."""

    if not answer:
        return []
    evidence = evidence_text(compact, question, extra)
    ev_tokens = set(_norm(evidence).split())
    ev_numbers = _numbers(evidence)
    for word in re.findall(r"[A-Za-z]+", evidence.lower()):
        if word in _MONTHS:
            ev_numbers.add(str(_MONTHS[word]))
    text = _CITATION.sub(" ", answer)
    # Identifiers (workflow/run ids, hashes) and URLs are references, not page facts.
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{8,}\b", " ", text, flags=re.I)
    problems: list[str] = []
    scores = {m.group(0) for m in _SCORE.finditer(text)}
    for number in sorted(_numbers(text)):
        if number in ev_numbers:
            continue
        small = number.isdigit() and int(number) <= 10
        in_score = any(re.search(rf"(?<!\d){re.escape(number)}(?!\d)", s) for s in scores)
        if small and not in_score:
            continue  # counts ("3 fixtures") are synthesis, not page claims
        problems.append(number)
    # Quoted text (double quotes only; apostrophes are not quotes) presented as what the page says.
    for quoted in re.findall(r"[\"“”]([^\"“”\n]{4,200})[\"“”]", text):
        words = _significant(quoted)
        if len(words) < 2:
            continue
        present = sum(1 for w in words if w in ev_tokens)
        if present / len(words) < 0.6:
            problems.append(quoted.strip())
    return problems

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


def evidence_text(compact: list[dict[str, Any]], question: str) -> str:
    parts = [question or ""]
    for item in compact:
        parts.append(str(item.get("facts") or ""))
        for value in (item.get("inputs") or {}).values():
            parts.append(str(value))
        for value in (item.get("raw_inputs") or {}).values():
            parts.append(str(value))
        parts.append(str(item.get("workflow") or ""))
    today = dates.today()
    parts.append(today.isoformat())
    return "\n".join(parts)


def ungrounded_claims(answer: str, compact: list[dict[str, Any]], question: str) -> list[str]:
    if not answer:
        return []
    evidence = evidence_text(compact, question)
    ev_norm = _norm(evidence)
    ev_numbers = _numbers(evidence)
    for word in re.findall(r"[A-Za-z]+", evidence.lower()):
        if word in _MONTHS:
            ev_numbers.add(str(_MONTHS[word]))
    text = _CITATION.sub(" ", answer)
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
    phrases = re.findall(r"[\"“”']([^\"“”'\n]{4,120})[\"“”']|\*\*([^*\n]{2,120})\*\*", text)
    for quoted, bold in phrases:
        phrase = (quoted or bold).strip()
        if not phrase or not re.search(r"[A-Za-z\u0590-\u05FF]", phrase):
            continue
        if _norm(phrase) and _norm(phrase) not in ev_norm:
            problems.append(phrase)
    return problems

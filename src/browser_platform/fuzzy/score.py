"""Score candidate labels against a query with rapidfuzz, then accept or abstain."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from rapidfuzz import fuzz

from browser_platform.fuzzy.candidates import Candidate
from browser_platform.fuzzy.normalize import name_variants, normalize_text


def score_text(query: str, candidate: str) -> float:
    """Best of WRatio / token set / token sort / ratio across name-order variants (0–100)."""

    best = 0.0
    q_vars = name_variants(query)
    c_vars = name_variants(candidate)
    for q in q_vars:
        if not q:
            continue
        for c in c_vars:
            if not c:
                continue
            best = max(
                best,
                float(fuzz.WRatio(q, c)),
                float(fuzz.token_set_ratio(q, c)),
                float(fuzz.token_sort_ratio(q, c)),
                float(fuzz.ratio(q, c)),
            )
            if best >= 99.9:
                return 100.0
    return best


@dataclass
class FuzzyDecision:
    accepted: bool
    reason: str
    query: str
    winner: Candidate | None
    runner_up: Candidate | None
    ranked: list[Candidate] = field(default_factory=list)
    threshold: float = 90.0
    margin: float = 8.0

    def top_n(self, n: int = 5) -> list[Candidate]:
        return self.ranked[:n]


def score_candidates(
    query: str,
    candidates: Iterable[Candidate],
    *,
    threshold: float = 90.0,
    margin: float = 8.0,
) -> FuzzyDecision:
    ranked: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for cand in candidates:
        text = cand.text or ""
        key = (normalize_text(text), cand.selector or "")
        if not normalize_text(text) or key in seen:
            continue
        seen.add(key)
        scored = cand.model_copy(update={"score": round(score_text(query, text), 2)})
        ranked.append(scored)
    ranked.sort(key=lambda c: (-(c.score or 0), c.text))

    winner = ranked[0] if ranked else None
    runner_up = ranked[1] if len(ranked) > 1 else None
    if winner is None:
        return FuzzyDecision(
            accepted=False,
            reason="no_candidates",
            query=query,
            winner=None,
            runner_up=None,
            ranked=[],
            threshold=threshold,
            margin=margin,
        )
    win_score = float(winner.score or 0)
    second = float(runner_up.score or 0) if runner_up else 0.0
    gap = win_score - second
    if win_score < threshold:
        return FuzzyDecision(
            accepted=False,
            reason="below_threshold",
            query=query,
            winner=winner,
            runner_up=runner_up,
            ranked=ranked,
            threshold=threshold,
            margin=margin,
        )
    if runner_up is not None and gap < margin:
        return FuzzyDecision(
            accepted=False,
            reason="ambiguous_margin",
            query=query,
            winner=winner,
            runner_up=runner_up,
            ranked=ranked,
            threshold=threshold,
            margin=margin,
        )
    return FuzzyDecision(
        accepted=True,
        reason="accepted",
        query=query,
        winner=winner,
        runner_up=runner_up,
        ranked=ranked,
        threshold=threshold,
        margin=margin,
    )


def query_for_step(step: Any, variables: dict[str, Any] | None = None) -> str:
    from browser_platform.interpolate import interpolate

    raw = (
        getattr(step, "fuzzy_query", None)
        or getattr(step, "target_text", None)
        or getattr(step, "selectedText", None)
        or getattr(step, "value", None)
        or getattr(step, "description", None)
        or ""
    )
    return str(interpolate(raw, variables) or "").strip()

"""Ordered selector attempts for a workflow-use step.

Order inside the workflow-use / selector tier:
  1. cachedSelector (last write-back / action cache winner)
  2. selectorStrategies sorted by priority (then declared order)
  3. legacy cssSelector / xpath / target_text
  4. heuristics derived from target_text, labels, and description
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from browser_platform.models.workflow import SelectorStrategy, WorkflowStep

Kind = Literal[
    "cached",
    "selector",
    "heuristic",
]


@dataclass(frozen=True)
class SelectorAttempt:
    kind: Kind
    strategy: SelectorStrategy

    @property
    def type(self) -> str:
        return self.strategy.type

    @property
    def value(self) -> str:
        return self.strategy.value

    @property
    def metadata(self) -> dict[str, Any]:
        return self.strategy.metadata


def _strategy(type_: str, value: str, priority: int, kind_meta: str | None = None, **meta: Any) -> SelectorStrategy:
    metadata = dict(meta)
    if kind_meta:
        metadata.setdefault("source", kind_meta)
    return SelectorStrategy(type=type_, value=value, priority=priority, metadata=metadata)


def _quoted_phrases(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"""['"]([^'"]{1,80})['"]""", text)]


def build_selector_plan(
    step: WorkflowStep,
    *,
    cached: SelectorStrategy | None = None,
) -> list[SelectorAttempt]:
    """Return de-duplicated attempts in execution order."""

    attempts: list[SelectorAttempt] = []
    seen: set[tuple[str, str, str]] = set()

    def add(kind: Kind, strategy: SelectorStrategy) -> None:
        key = (strategy.type, strategy.value, str(strategy.metadata.get("role", "")))
        if not strategy.type:
            return
        if key in seen:
            return
        seen.add(key)
        attempts.append(SelectorAttempt(kind=kind, strategy=strategy))

    winner = cached or step.cachedSelector
    if winner:
        add("cached", winner)

    strategies = list(step.strategies())
    strategies.sort(key=lambda s: s.priority)
    for strategy in strategies:
        add("selector", strategy)

    if step.cssSelector:
        add("selector", _strategy("css", step.cssSelector, 500, "legacy_css"))
    if step.xpath:
        add("selector", _strategy("xpath", step.xpath, 510, "legacy_xpath"))
    if step.target_text:
        add("selector", _strategy("text_exact", step.target_text, 520, "target_text"))
        add("heuristic", _strategy("text_fuzzy", step.target_text, 800, "target_text_fuzzy"))
        add("heuristic", _strategy("role_text", step.target_text, 810, "target_role", role="button"))
        add("heuristic", _strategy("placeholder", step.target_text, 820, "target_placeholder"))
        add("heuristic", _strategy("aria_label", step.target_text, 830, "target_aria"))

    if step.description:
        for phrase in _quoted_phrases(step.description):
            add("heuristic", _strategy("text_exact", phrase, 850, "description"))
            add("heuristic", _strategy("text_fuzzy", phrase, 860, "description_fuzzy"))

    return attempts

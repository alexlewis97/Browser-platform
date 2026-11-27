"""Merge fuzzy / HITL settings from platform defaults, workflow, and step."""

from __future__ import annotations

from typing import Any

from browser_platform.models.workflow import (
    AliasMapping,
    FuzzyMatchConfig,
    HitlConfig,
    WorkflowDefinition,
    WorkflowStep,
)


def merge_fuzzy(settings: Any, definition: WorkflowDefinition, step: WorkflowStep | None = None) -> FuzzyMatchConfig:
    base = FuzzyMatchConfig(
        enabled=True,
        threshold=float(getattr(settings, "fuzzy_threshold", 90.0) or 90.0),
        margin=float(getattr(settings, "fuzzy_margin", 8.0) or 8.0),
        top_n=int(getattr(settings, "fuzzy_top_n", 5) or 5),
        include_screenshot=bool(getattr(settings, "fuzzy_include_screenshot", False)),
    )
    for layer in (definition.fuzzy, getattr(step, "fuzzy", None) if step is not None else None):
        if layer is None:
            continue
        updates = layer.model_dump(exclude_none=True)
        base = base.model_copy(update=updates)
    return base


def merge_hitl(settings: Any, definition: WorkflowDefinition, step: WorkflowStep | None = None) -> HitlConfig:
    base = HitlConfig(
        mode=getattr(settings, "hitl_mode", "always_on_uncertain") or "always_on_uncertain",  # type: ignore[arg-type]
        timeout_s=float(getattr(settings, "hitl_timeout_s", 300.0) or 300.0),
        timeout_policy=getattr(settings, "hitl_timeout_policy", "fail") or "fail",  # type: ignore[arg-type]
    )
    for layer in (definition.hitl, getattr(step, "hitl", None) if step is not None else None):
        if layer is None:
            continue
        updates = layer.model_dump(exclude_none=True)
        base = base.model_copy(update=updates)
    return base


def find_alias(aliases: list[AliasMapping], step_index: int, query: str) -> AliasMapping | None:
    from browser_platform.fuzzy.normalize import name_variants, normalize_text

    q_vars = name_variants(query)
    q_norm = normalize_text(query)
    for alias in aliases or []:
        if alias.step_index != step_index:
            continue
        stored = {alias.query_normalized, normalize_text(alias.query), *name_variants(alias.query)}
        if q_norm in stored or q_vars & stored:
            return alias
    return None


def make_alias(step_index: int, query: str, candidate) -> AliasMapping:
    from browser_platform.fuzzy.normalize import normalize_text

    return AliasMapping(
        step_index=step_index,
        query=query,
        query_normalized=normalize_text(query),
        chosen_text=candidate.text,
        chosen_id=getattr(candidate, "id", None),
        cssSelector=candidate.selector if getattr(candidate, "selector_type", "css") == "css" else None,
        xpath=candidate.selector if getattr(candidate, "selector_type", None) == "xpath" else None,
        role=candidate.role,
    )


_FUZZY_STEP_TYPES = frozenset({"click", "double_click", "right_click", "input", "select_change", "key_press"})


def fuzzy_applies(step: WorkflowStep, config: FuzzyMatchConfig) -> bool:
    if not config.enabled:
        return False
    return step.type in _FUZZY_STEP_TYPES

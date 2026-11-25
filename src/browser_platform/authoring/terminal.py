"""Helpers for workflows that end with an extract or a final action."""

from __future__ import annotations

import re
from typing import Any

from browser_platform.models.workflow import WorkflowStep

EXTRACT_TYPES = frozenset({"extract", "extract_page_content"})
ACTION_TYPES = frozenset({"click", "input", "select_change", "key_press"})
TERMINAL_TYPES = EXTRACT_TYPES | ACTION_TYPES

_SUBMIT_TEXT = re.compile(
    r"\b(submit|send|sign[\s-]?in|log[\s-]?in|login|save|confirm|continue|next|apply|search|go|register|sign[\s-]?up|post|checkout|pay)\b",
    re.I,
)
_ACTION_GOAL = re.compile(
    r"\b(submit|fill(?:\s+out|\s+the\s+form)?|log[\s-]?in|sign[\s-]?in|login|press|click\s+submit|send\s+the\s+form|post\s+the|complete\s+the\s+form)\b",
    re.I,
)
_EXTRACT_GOAL = re.compile(
    r"\b(extract|heading|paragraph|fixtures?|scores?|first paragraph|page title|find the|search)\b",
    re.I,
)


def is_extract(step: WorkflowStep | dict[str, Any] | None) -> bool:
    typ = _type(step)
    return typ in EXTRACT_TYPES


def is_action(step: WorkflowStep | dict[str, Any] | None) -> bool:
    typ = _type(step)
    return typ in ACTION_TYPES


def is_terminal(step: WorkflowStep | dict[str, Any] | None) -> bool:
    typ = _type(step)
    return typ in TERMINAL_TYPES


def is_submit_like(step: WorkflowStep | dict[str, Any] | None) -> bool:
    if step is None:
        return False
    typ = _type(step)
    if typ == "key_press":
        key = _get(step, "key") or ""
        return str(key).lower() in {"enter", "return"}
    if typ not in {"click"}:
        return typ in {"input", "select_change"}
    blob = " ".join(
        str(part or "")
        for part in (
            _get(step, "target_text"),
            _get(step, "description"),
            _get(step, "cssSelector"),
            ((_get(step, "element") or {}) if isinstance(_get(step, "element"), dict) else {}).get("type"),
            ((_get(step, "element") or {}) if isinstance(_get(step, "element"), dict) else {}).get("text"),
            ((_get(step, "element") or {}) if isinstance(_get(step, "element"), dict) else {}).get("name"),
        )
    )
    el = _get(step, "element") if isinstance(_get(step, "element"), dict) else {}
    typ_attr = str((el or {}).get("type") or "").lower()
    if typ_attr in {"submit", "button"} or (el or {}).get("role") == "button":
        if _SUBMIT_TEXT.search(blob) or typ_attr == "submit" or not blob.strip():
            return True
    return bool(_SUBMIT_TEXT.search(blob))


def last_action(steps: list[WorkflowStep] | None) -> WorkflowStep | None:
    for step in reversed(steps or []):
        if is_action(step):
            return step
    return None


def last_terminal(steps: list[WorkflowStep] | None) -> WorkflowStep | None:
    for step in reversed(steps or []):
        if is_terminal(step):
            return step
    return None


def last_terminal_index(steps: list[WorkflowStep] | None) -> int | None:
    for index in range(len(steps or []) - 1, -1, -1):
        if is_terminal(steps[index]):
            return index
    return None


def rec_id(step: WorkflowStep | dict[str, Any] | None) -> str:
    return str(_get(step, "rec_id") or "")


def should_append_extract(steps: list[WorkflowStep], *, task: str = "") -> bool:
    """Only invent an extract step when the task is a read and the recording didn't end on an action."""
    if not steps:
        return True
    if is_extract(steps[-1]):
        return False
    goal = task or ""
    action_goal = bool(_ACTION_GOAL.search(goal))
    extract_goal = bool(_EXTRACT_GOAL.search(goal))
    last = last_action(steps)
    if last is not None and (is_submit_like(last) or action_goal):
        return False
    if extract_goal:
        return True
    if is_action(steps[-1]):
        return False
    return True


def trim_after_terminal(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    index = last_terminal_index(steps)
    if index is None:
        return list(steps)
    return list(steps[: index + 1])


def ensure_terminal(steps: list[WorkflowStep], original: list[WorkflowStep] | None = None) -> list[WorkflowStep]:
    """Keep the existing terminal step; never invent an extract to satisfy the old rule."""
    current = trim_after_terminal(list(steps))
    if current and is_terminal(current[-1]):
        return current
    source = original or steps
    term = last_terminal(source)
    if term is None:
        return current
    ident = rec_id(term)
    if ident and any(rec_id(step) == ident for step in current):
        return trim_after_terminal(current)
    return current + [term]


def protect_last_action(after: list[WorkflowStep], before: list[WorkflowStep]) -> list[WorkflowStep]:
    """If a prune pass dropped the final action, put it back."""
    needed = last_action(before)
    if needed is None:
        return after
    ident = rec_id(needed)
    if ident and any(rec_id(step) == ident for step in after):
        return after
    if any(step is needed for step in after):
        return after
    # Fall back to type+description match.
    for step in after:
        if step.type == needed.type and (step.description or "") == (needed.description or "") and (step.target_text or "") == (needed.target_text or ""):
            return after
    return list(after) + [needed]


def final_action_label(step: WorkflowStep | dict[str, Any] | None) -> str:
    if step is None:
        return ""
    typ = _type(step)
    target = (
        _get(step, "target_text")
        or _get(step, "cssSelector")
        or _get(step, "value")
        or _get(step, "selectedText")
        or _get(step, "key")
        or _get(step, "extractionGoal")
        or "element"
    )
    if typ == "click":
        return f"Final step: click {target}"
    if typ == "input":
        return f"Final step: fill {target}"
    if typ == "select_change":
        return f"Final step: choose {target}"
    if typ == "key_press":
        return f"Final step: press {target}"
    if typ in EXTRACT_TYPES:
        return f"Final step: extract {target}"
    return f"Final step: {typ}"


def mark_action_output(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    if steps and is_action(steps[-1]):
        if not steps[-1].output:
            steps[-1].output = "result"
    return steps


def _type(step: WorkflowStep | dict[str, Any] | None) -> str:
    if step is None:
        return ""
    if isinstance(step, dict):
        return str(step.get("type") or "")
    return str(getattr(step, "type", "") or "")


def _get(step: WorkflowStep | dict[str, Any] | None, name: str) -> Any:
    if step is None:
        return None
    if isinstance(step, dict):
        return step.get(name)
    return getattr(step, name, None)

"""Turn saved workflows into tool schemas for chat and MCP."""

from __future__ import annotations

import re
from typing import Any

from browser_platform.authoring.terminal import is_extract, is_submit_like
from browser_platform.models.workflow import StoredWorkflow, WorkflowDefinition, WorkflowInput
from browser_platform.workflow_inputs import (
    ensure_definition_inputs,
    has_runtime_default,
    input_is_required,
    pretty_label,
)
from browser_platform.interpolate import is_placeholder_value

_NAME = re.compile(r"[^a-zA-Z0-9_]+")


def tool_name(stored: StoredWorkflow) -> tuple[str, str]:
    raw = _NAME.sub("_", (stored.definition.name or "workflow").strip().lower()).strip("_")
    if not raw or not raw[0].isalpha():
        raw = f"wf_{raw}" if raw else "workflow"
    suffix = (stored.id or "")[:8]
    # Keep the human name when unique; caller dedupes collisions with the suffix.
    return raw[:60], suffix


def is_action_ending(definition: WorkflowDefinition | None) -> bool:
    """True when the last non-extract step can mutate the page (needs confirm).

    Any click / key_press / input / select / submit counts — not only labels that
    match a small submit word list — so destructive actions like Delete require confirm.
    """

    if definition is None:
        return False
    if getattr(definition, "read_only", None) is True:
        return False
    last = None
    for step in reversed(definition.steps or []):
        # A post-submit navigation (checkout-complete, /post) is not the action.
        if is_extract(step) or (step.type or "").lower() in {"navigation", "navigate"}:
            continue
        last = step
        break
    if last is None:
        return False
    typ = (last.type or "").lower()
    if typ in {"click", "submit", "input", "select_change", "key_press", "fill"}:
        return True
    return is_submit_like(last)


def _json_type(item: WorkflowInput) -> str:
    typ = (item.type or "string").lower()
    if typ in {"number", "integer", "int", "float"}:
        return "number"
    if typ in {"bool", "boolean"}:
        return "boolean"
    return "string"


def parameter_schema(item: WorkflowInput) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": _json_type(item)}
    label = pretty_label(item.name, label=item.label, description=item.description)
    desc = item.description or label
    example = item.default if item.default is not None else getattr(item, "example", None)
    if is_placeholder_value(example):
        example = None
    if has_runtime_default(item) or item.required is False:
        desc = (
            f"{desc} Optional. Omit this unless the user named a specific value. "
            "Do not send placeholders such as current, now, or default."
        ).strip()
    if example is not None and str(example).strip():
        desc = f"{desc} (e.g. {example})"
        schema["examples"] = [example]
    if desc:
        schema["description"] = desc
    enum = getattr(item, "enum", None) or (item.model_dump().get("enum") if hasattr(item, "model_dump") else None)
    if enum and isinstance(enum, list) and enum:
        schema["enum"] = list(enum)
    return schema


def tool_from_workflow(stored: StoredWorkflow, *, name: str | None = None) -> dict[str, Any]:
    definition = ensure_definition_inputs(stored.definition)
    inputs = list(definition.input_schema or [])
    properties = {item.name: parameter_schema(item) for item in inputs if item.name}
    required = [item.name for item in inputs if item.name and input_is_required(item)]
    action = is_action_ending(definition)
    description = (definition.description or definition.name or "Run this saved browser workflow.").strip()
    if action:
        description += " This workflow ends in an action (click or submit) and needs confirmation before it runs."
    tool_id = name or tool_name(stored)[0]
    return {
        "type": "function",
        "function": {
            "name": tool_id,
            "description": description[:500],
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
        "workflow_id": stored.id,
        "workflow_name": definition.name,
        "action_ending": action,
        "input_schema": [item.model_dump(exclude_none=True) for item in inputs],
    }



def _is_low_quality_step(step) -> bool:
    text = (getattr(step, "target_text", None) or "")
    css = (getattr(step, "cssSelector", None) or "").strip()
    if step.type == "click" and (len(text) > 80 or css in {"", "body", "html", "body > div", "#root"}):
        return True
    if step.type == "input" and str(getattr(step, "value", "") or "").strip().lower() in {"login", "continue", "submit", "finish"}:
        return True
    return False


def _workflow_tool_quality(stored: StoredWorkflow) -> tuple[int, int, int, int]:
    """Prefer parameterized, stable-selector workflows over noisy recordings."""

    steps = list(stored.definition.steps or [])
    bound_locators = 0
    bound_values = 0
    stable = 0
    junk = 0
    for step in steps:
        if _is_low_quality_step(step):
            junk += 1
        css = step.cssSelector or ""
        blob = " ".join(
            [
                css,
                step.value or "",
                step.selectedText or "",
                step.url or "",
                *(getattr(s, "value", "") or "" for s in (step.selectorStrategies or [])),
            ]
        )
        if step.type != "navigation" and (
            "{" in css
            or any(
                "{" in (getattr(s, "value", "") or "") and getattr(s, "type", "") in {"css", "xpath", "id"}
                for s in (step.selectorStrategies or [])
            )
        ):
            bound_locators += 1
        if str(step.value or "").startswith("{") or str(step.selectedText or "").startswith("{"):
            bound_values += 1
        if css.startswith("#") or "data-test" in css or "name=" in css or css.startswith("label["):
            stable += 1
    starts_with_nav = 1 if steps and steps[0].type == "navigation" else 0
    return (starts_with_nav, bound_locators, bound_values, stable - junk, -len(steps))


def tools_from_workflows(items: list[StoredWorkflow]) -> list[dict[str, Any]]:
    used: set[str] = set()
    tools: list[dict[str, Any]] = []
    grouped: dict[str, list[StoredWorkflow]] = {}
    for stored in items:
        base, _suffix = tool_name(stored)
        grouped.setdefault(base, []).append(stored)
    ordered: list[StoredWorkflow] = []
    for rows in grouped.values():
        rows.sort(key=_workflow_tool_quality, reverse=True)
        ordered.extend(rows)
    for stored in ordered:
        base, suffix = tool_name(stored)
        name = base
        if name in used:
            name = f"{base}_{suffix}" if suffix else f"{base}_{len(used)+1}"
        used.add(name)
        tool = tool_from_workflow(stored, name=name)
        tool["base_name"] = base
        tool["primary"] = name == base
        tools.append(tool)
    return tools


def openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip bookkeeping fields so the payload is valid for OpenRouter."""

    return [{"type": "function", "function": item["function"]} for item in tools if item.get("function")]


def mcp_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for item in tools:
        fn = item.get("function") or {}
        out.append(
            {
                "name": fn.get("name"),
                "description": fn.get("description") or "",
                "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


def lookup_tool(tools: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for item in tools:
        if (item.get("function") or {}).get("name") == name or item.get("workflow_id") == name:
            return item
    return None

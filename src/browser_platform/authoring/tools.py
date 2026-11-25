"""Turn saved workflows into tool schemas for chat and MCP."""

from __future__ import annotations

import re
from typing import Any

from browser_platform.authoring.terminal import is_extract, is_submit_like
from browser_platform.models.workflow import StoredWorkflow, WorkflowDefinition, WorkflowInput
from browser_platform.workflow_inputs import ensure_definition_inputs, pretty_label

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
        if is_extract(step):
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
    required = [item.name for item in inputs if item.name and item.required is not False]
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


def tools_from_workflows(items: list[StoredWorkflow]) -> list[dict[str, Any]]:
    used: set[str] = set()
    tools: list[dict[str, Any]] = []
    for stored in items:
        base, suffix = tool_name(stored)
        name = base
        if name in used:
            name = f"{base}_{suffix}" if suffix else f"{base}_{len(used)+1}"
        used.add(name)
        tools.append(tool_from_workflow(stored, name=name))
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

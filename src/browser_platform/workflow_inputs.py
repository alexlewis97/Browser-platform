"""Named workflow inputs: labels, JSON-schema import, and placeholder discovery."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from browser_platform.interpolate import current_year_month, is_placeholder_value, year_month
from browser_platform.models.workflow import WorkflowDefinition, WorkflowInput

_BRACE = re.compile(r"\{\{?\s*([a-zA-Z_][\w]*)(?:\|[a-zA-Z0-9_]+)?\s*\}?\}")
_RESERVED = frozenset({"url", "intent", "effect"})

# Human labels for common variable names shown on the Run screen.
LABELS = {
    "term": "Search term",
    "query": "Search query",
    "q": "Search query",
    "search": "Search term",
    "name": "Name",
    "status": "Status",
    "url": "URL",
    "email": "Email",
    "title": "Title",
}


def pretty_label(name: str, *, label: str | None = None, description: str | None = None) -> str:
    if label and str(label).strip():
        return str(label).strip()
    key = (name or "").strip()
    if key.lower() in LABELS:
        return LABELS[key.lower()]
    text = key.replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else "Input"


def placeholders_in(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        for match in _BRACE.finditer(value):
            name = match.group(1)
            if name not in found:
                found.append(name)
    elif isinstance(value, dict):
        for item in value.values():
            for name in placeholders_in(item):
                if name not in found:
                    found.append(name)
    elif isinstance(value, list):
        for item in value:
            for name in placeholders_in(item):
                if name not in found:
                    found.append(name)
    return found


def placeholders_in_definition(definition: WorkflowDefinition | dict[str, Any]) -> list[str]:
    if isinstance(definition, WorkflowDefinition):
        payload = definition.model_dump()
    else:
        payload = dict(definition)
    names: list[str] = []
    for source in (payload.get("steps"), payload.get("description"), payload.get("pomerado_input")):
        for name in placeholders_in(source):
            if name not in names:
                names.append(name)
    return names


def inputs_from_json_schema(schema: dict[str, Any] | None, examples: dict[str, Any] | None = None) -> list[WorkflowInput]:
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = {str(item) for item in (schema.get("required") or [])}
    examples = examples or {}
    items: list[WorkflowInput] = []
    for name, prop in properties.items():
        if name in _RESERVED:
            continue
        info = prop if isinstance(prop, dict) else {}
        default = examples.get(name)
        if default is None:
            default = info.get("default")
        if default is not None and not isinstance(default, (str, int, float, bool)):
            default = str(default)
        description = info.get("description")
        title = info.get("title")
        # JSON Schema "title" is often a constraint label like "maxLength(300)"; skip those.
        label_src = None if (isinstance(title, str) and "(" in title) else title
        items.append(
            WorkflowInput(
                name=str(name),
                type="string",
                required=(name in required) if required else False,
                default=default if isinstance(default, (str, int, float, bool)) else None,
                label=pretty_label(str(name), label=label_src, description=description if isinstance(description, str) else None),
                description=description if isinstance(description, str) else None,
            )
        )
    return items


def inputs_from_integration(directory: str | Path, input_values: dict[str, Any] | None = None) -> list[WorkflowInput]:
    examples = {k: v for k, v in (input_values or {}).items() if k not in _RESERVED}
    schema = None
    path = Path(directory) / "pomerado.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            schema = data.get("inputSchema") or data.get("input_schema")
        except Exception:
            schema = None
    items = inputs_from_json_schema(schema if isinstance(schema, dict) else None, examples)
    have = {item.name for item in items}
    for name, value in examples.items():
        if name in have:
            continue
        items.append(
            WorkflowInput(
                name=name,
                type="string",
                required=True,
                default=value if isinstance(value, (str, int, float, bool)) else str(value),
                label=pretty_label(name),
            )
        )
    return items


def merge_input_schema(*groups: list[WorkflowInput] | None) -> list[WorkflowInput]:
    by_name: dict[str, WorkflowInput] = {}
    for group in groups:
        for item in group or []:
            current = by_name.get(item.name)
            if current is None:
                by_name[item.name] = item
                continue
            by_name[item.name] = current.model_copy(
                update={
                    "required": True if current.required or item.required or current.required is None else item.required,
                    "default": current.default if current.default is not None else item.default,
                    "label": getattr(current, "label", None) or getattr(item, "label", None),
                    "description": getattr(current, "description", None) or getattr(item, "description", None),
                }
            )
    return list(by_name.values())



def has_runtime_default(item: WorkflowInput | None) -> bool:
    """Optional explore inputs such as month default at run time, not a pinned value."""

    if item is None:
        return False
    name = (item.name or "").strip().lower()
    runtime = str(getattr(item, "runtime_default", None) or "").strip().lower()
    fmt = str(getattr(item, "format", None) or "").strip().lower()
    default = item.default
    if runtime in {"current_month", "current", "current_year_month"}:
        return True
    if fmt in {"ym", "yyyymm", "year_month", "current", "current_month"}:
        return True
    if name in {"month", "date"} and (default in (None, "") or is_placeholder_value(default)):
        return True
    return False


def input_is_required(item: WorkflowInput) -> bool:
    if has_runtime_default(item) or item.required is False:
        return False
    return item.required is not False


def concrete_default(item: WorkflowInput | None) -> str | None:
    if item is None:
        return None
    if has_runtime_default(item):
        name = (item.name or "").strip().lower()
        if name in {"month", "date"} or str(getattr(item, "runtime_default", "") or "").lower() in {
            "current_month", "current", "current_year_month",
        } or str(getattr(item, "format", "") or "").lower() in {"ym", "yyyymm", "year_month", "current", "current_month"}:
            return current_year_month()
    default = item.default
    if default not in (None, "") and not is_placeholder_value(default):
        return str(default)
    if is_placeholder_value(default) and (item.name or "").strip().lower() in {"month", "date"}:
        return current_year_month()
    return None


def resolve_input_values(definition: WorkflowDefinition | None, variables: dict[str, Any] | None) -> dict[str, Any]:
    """Replace placeholders and fill runtime defaults with concrete values (e.g. 2026-11)."""

    out = dict(variables or {})
    items = {item.name: item for item in (getattr(definition, "input_schema", None) or []) if item.name}
    for name, item in items.items():
        raw = out.get(name)
        if raw in (None, "") or is_placeholder_value(raw):
            filled = concrete_default(item)
            if filled is not None:
                out[name] = filled
            elif is_placeholder_value(raw):
                out.pop(name, None)
    # Drop placeholders even when the schema has no matching entry (e.g. planner-invented month=current).
    for name, raw in list(out.items()):
        if is_placeholder_value(raw):
            key = str(name or "").strip().lower()
            if key in {"month", "date"}:
                out[name] = current_year_month()
            else:
                out.pop(name, None)
    return out


def inputs_for_output_validation(
    inputs: dict[str, Any] | None,
    *,
    definition: WorkflowDefinition | None = None,
    raw_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Inputs the output validator may require in the extract.

    Placeholders (current, now, …) and unstated runtime/schema defaults are omitted.
    User-stated values are kept.
    """

    items = {item.name: item for item in (getattr(definition, "input_schema", None) or []) if item.name}
    out: dict[str, Any] = {}
    for key, value in (inputs or {}).items():
        if is_placeholder_value(value) or value in (None, ""):
            continue
        raw = None if raw_inputs is None else raw_inputs.get(key)
        if raw_inputs is not None and (raw in (None, "") or is_placeholder_value(raw)):
            continue
        item = items.get(key)
        if raw_inputs is None and has_runtime_default(item) and str(value) == str(concrete_default(item)):
            continue
        out[key] = value
    return out


def missing_required_inputs(definition: WorkflowDefinition, variables: dict[str, Any] | None) -> list[str]:
    values = variables or {}
    missing: list[str] = []
    for item in definition.input_schema or []:
        if not input_is_required(item):
            continue
        value = values.get(item.name)
        if value is None or not str(value).strip() or is_placeholder_value(value):
            missing.append(item.name)
    return missing


def ensure_definition_inputs(definition: WorkflowDefinition) -> WorkflowDefinition:
    """Add required input_schema entries for any {placeholders} found in the workflow."""

    discovered = [
        WorkflowInput(name=name, type="string", required=True, label=pretty_label(name))
        for name in placeholders_in_definition(definition)
        if name not in _RESERVED
    ]
    if not discovered and not definition.input_schema:
        return definition
    merged = merge_input_schema(definition.input_schema, discovered)
    if merged == list(definition.input_schema or []):
        return definition
    return definition.model_copy(update={"input_schema": merged})


__all__ = [
    "ensure_definition_inputs",
    "inputs_from_integration",
    "inputs_from_json_schema",
    "merge_input_schema",
    "missing_required_inputs",
    "placeholders_in",
    "placeholders_in_definition",
    "pretty_label",
]

"""Named workflow inputs: labels, JSON-schema import, and placeholder discovery."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from browser_platform.interpolate import current_year_month, is_placeholder_value, year_month
from browser_platform.models.workflow import WorkflowDefinition, WorkflowInput, WorkflowStep

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
                    # An explicit declaration wins: a placeholder found in the steps must not turn an
                    # input the author marked optional (with a default) back into a required one.
                    "required": current.required if current.required is not None else (True if item.required is None else item.required),
                    "default": current.default if current.default is not None else item.default,
                    "label": getattr(current, "label", None) or getattr(item, "label", None),
                    "description": getattr(current, "description", None) or getattr(item, "description", None),
                }
            )
    return list(by_name.values())



_FILTER_GRAIN = {"ym": "month", "yyyymm": "month", "year_month": "month", "current_month": "month",
                 "ymd": "date", "date": "date", "day": "date", "iso_date": "date", "dmy": "date",
                 "y": "year", "year": "year", "yyyy": "year"}
_RUNTIME_TOKENS = {"current_month", "current", "current_year_month", "today", "current_date", "now", "current_year"}
# Where a default came from. Only these may fill an input the user did not state.
TRUSTED_DEFAULT_SOURCES = {"page", "verified", "author", "runtime"}


def input_grain(item: WorkflowInput | None, definition: WorkflowDefinition | None = None) -> str | None:
    """Calendar grain of a typed date input: from its declared format, else from the
    interpolation filter the workflow uses it with ({month|ym}, {day|ymd}, {season|y})."""

    from browser_platform.dates import grain_of

    if item is None:
        return None
    grain = grain_of(getattr(item, "format", None))
    if grain:
        return grain
    if definition is not None:
        pattern = re.compile(r"\{\{?\s*" + re.escape(item.name) + r"\|([a-zA-Z0-9_]+)")
        for blob in _definition_text(definition):
            for match in pattern.finditer(blob):
                found = _FILTER_GRAIN.get(match.group(1).lower())
                if found:
                    return found
    return None


def _definition_text(definition: WorkflowDefinition) -> list[str]:
    out: list[str] = []
    for step in definition.steps or []:
        for key in ("url", "value", "target_text", "cssSelector", "selectedText", "extractionGoal"):
            value = getattr(step, key, None)
            if isinstance(value, str):
                out.append(value)
    return out


def has_runtime_default(item: WorkflowInput | None, definition: WorkflowDefinition | None = None) -> bool:
    """Optional inputs that default at run time (e.g. the current month), not a pinned value."""

    if item is None:
        return False
    name = (item.name or "").strip().lower()
    runtime = str(getattr(item, "runtime_default", None) or "").strip().lower()
    fmt = str(getattr(item, "format", None) or "").strip().lower()
    default = item.default
    if runtime in _RUNTIME_TOKENS:
        return True
    if fmt in {"ym", "yyyymm", "year_month", "current", "current_month"}:
        return True
    if str(getattr(item, "default_source", "") or "") == "runtime":
        return True
    if input_grain(item, definition) and str(default or "").strip().lower() in _RUNTIME_TOKENS:
        return True
    if name in {"month", "date"} and (default in (None, "") or is_placeholder_value(default)):
        return True
    return False


def input_is_required(item: WorkflowInput, definition: WorkflowDefinition | None = None) -> bool:
    if has_runtime_default(item, definition) or item.required is False:
        return False
    return item.required is not False


def default_is_trusted(item: WorkflowInput | None) -> bool:
    """A literal default may be used only if its provenance is known (page state, verified,
    an author's explicit choice, or a runtime token). Examples are never defaults."""

    if item is None:
        return False
    source = getattr(item, "default_source", None)
    return source is None or str(source) in TRUSTED_DEFAULT_SOURCES


def concrete_default(item: WorkflowInput | None, definition: WorkflowDefinition | None = None) -> str | None:
    from browser_platform.dates import now_value

    if item is None:
        return None
    if has_runtime_default(item, definition):
        grain = input_grain(item, definition)
        if grain:
            return now_value(grain)
        name = (item.name or "").strip().lower()
        if name in {"month", "date"} or str(getattr(item, "runtime_default", "") or "").lower() in {
            "current_month", "current", "current_year_month",
        } or str(getattr(item, "format", "") or "").lower() in {"ym", "yyyymm", "year_month", "current", "current_month"}:
            return current_year_month()
    default = item.default
    if default not in (None, "") and not is_placeholder_value(default) and default_is_trusted(item):
        return str(default)
    if is_placeholder_value(default) and (item.name or "").strip().lower() in {"month", "date"}:
        return current_year_month()
    return None


def resolve_typed_value(item: WorkflowInput, value: Any, definition: WorkflowDefinition | None = None) -> Any:
    """Resolve a date-typed value ("yesterday", "June 2026") to its canonical form; other
    values are returned unchanged. Unparseable dates are returned unchanged (see invalid_typed_inputs)."""

    from browser_platform.dates import resolve

    grain = input_grain(item, definition)
    if not grain or value in (None, ""):
        return value
    return resolve(str(value), grain) or value


def resolve_input_values(definition: WorkflowDefinition | None, variables: dict[str, Any] | None) -> dict[str, Any]:
    """Replace placeholders, fill runtime defaults, and resolve typed (date/month/year) values."""

    out = dict(variables or {})
    items = {item.name: item for item in (getattr(definition, "input_schema", None) or []) if item.name}
    for name, item in items.items():
        raw = out.get(name)
        if raw in (None, "") or is_placeholder_value(raw):
            filled = concrete_default(item, definition)
            if filled is not None:
                out[name] = filled
            elif is_placeholder_value(raw):
                out.pop(name, None)
        else:
            out[name] = resolve_typed_value(item, raw, definition)
    # Drop placeholders even when the schema has no matching entry (e.g. planner-invented month=current).
    for name, raw in list(out.items()):
        if is_placeholder_value(raw):
            key = str(name or "").strip().lower()
            if key in {"month", "date"}:
                out[name] = current_year_month()
            else:
                out.pop(name, None)
    return out


def finalize_authored_inputs(definition: WorkflowDefinition) -> WorkflowDefinition:
    """Authoring output policy: a demonstration/example value is never a default.

    A literal default survives only with a trusted ``default_source`` (page state, verified,
    author, runtime). Everything else moves to ``examples``; a required input stays required,
    and an optional one keeps working without a value (the step uses the page's own state).
    """

    updated = definition.model_copy(deep=True)
    out: list[WorkflowInput] = []
    for item in updated.input_schema or []:
        default = item.default
        source = getattr(item, "default_source", None)
        if has_runtime_default(item, updated):
            item = item.model_copy(update={"default": None if not is_placeholder_value(default) else default})
            setattr(item, "default_source", "runtime")
        elif default not in (None, "") and str(source or "") not in TRUSTED_DEFAULT_SOURCES - {"author"}:
            examples = [str(v) for v in (getattr(item, "examples", None) or [])]
            if str(default) not in examples:
                examples.insert(0, str(default))
            item = item.model_copy(update={"default": None})
            setattr(item, "examples", examples[:8])
            if hasattr(item, "default_source"):
                setattr(item, "default_source", None)
        out.append(item)
    updated.input_schema = out
    return updated


MIGRATION_TAG = "defaults-v1"
_ACTION_STEP_TYPES = {"click", "input", "select_change", "key_press"}


def _uses(definition: WorkflowDefinition, name: str) -> dict[str, bool]:
    """Where an input is interpolated: in navigation/URL text, or only in action steps."""

    pat = re.compile(r"\{\{?\s*" + re.escape(name) + r"(?:\|[a-zA-Z0-9_]+)?\s*\}?\}")
    in_url = in_action = False
    for step in definition.steps or []:
        payload = json.dumps(step.model_dump(exclude_none=True), default=str)
        if not pat.search(payload):
            continue
        if step.type in _ACTION_STEP_TYPES:
            in_action = True
        else:
            in_url = True
    return {"url": in_url, "action": in_action}


def migrate_defaults(definition: WorkflowDefinition) -> tuple[WorkflowDefinition, list[dict[str, Any]]]:
    """One-off cleanup for workflows saved before default provenance existed.

    - runtime tokens (current month, today) -> default_source=runtime, kept;
    - a literal default on a required input is an authoring example -> moved to examples;
    - a literal default on an optional input used only by action steps -> removed and the
      step is skipped when no value is given, so the page keeps its own state (default_source=page);
    - any other literal default without provenance -> moved to examples and the input becomes required.
    Inputs that already carry a default_source are left alone. Idempotent.
    """

    updated = definition.model_copy(deep=True)
    changes: list[dict[str, Any]] = []
    out: list[WorkflowInput] = []
    for item in updated.input_schema or []:
        source = getattr(item, "default_source", None)
        default = item.default
        if source is not None:
            out.append(item)
            continue
        if has_runtime_default(item, updated):
            item = item.model_copy()
            setattr(item, "default_source", "runtime")
            changes.append({"input": item.name, "action": "runtime", "default": default})
            out.append(item)
            continue
        if default in (None, "") or is_placeholder_value(default):
            out.append(item)
            continue
        examples = [str(v) for v in (getattr(item, "examples", None) or [])]
        if str(default) not in examples:
            examples.insert(0, str(default))
        uses = _uses(updated, item.name)
        new = item.model_copy(update={"default": None})
        setattr(new, "examples", examples[:8])
        if item.required is False and uses["action"] and not uses["url"]:
            setattr(new, "default_source", "page")
            setattr(new, "omit", "skip_step")
            changes.append({"input": item.name, "action": "page_state", "removed_default": default})
        else:
            new = new.model_copy(update={"required": True})
            changes.append({"input": item.name, "action": "now_required" if item.required is False else "example_only", "removed_default": default})
        out.append(new)
    updated.input_schema = out
    return updated, changes


def invalid_typed_inputs(definition: WorkflowDefinition | None, variables: dict[str, Any] | None) -> list[str]:
    """Human-readable problems for typed values that are not a valid date at their grain."""

    from browser_platform.dates import resolve

    problems: list[str] = []
    for item in getattr(definition, "input_schema", None) or []:
        value = (variables or {}).get(item.name)
        grain = input_grain(item, definition)
        if not grain or value in (None, "") or is_placeholder_value(value):
            continue
        if resolve(str(value), grain) is None:
            want = {"date": "a specific day", "month": "a month", "year": "a year"}[grain]
            problems.append(f"{item.name} must be {want} (got '{value}')")
    return problems


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
        if raw_inputs is None and has_runtime_default(item, definition) and str(value) == str(concrete_default(item, definition)):
            continue
        out[key] = value
    return out


def missing_required_inputs(definition: WorkflowDefinition, variables: dict[str, Any] | None) -> list[str]:
    values = variables or {}
    missing: list[str] = []
    for item in definition.input_schema or []:
        if not input_is_required(item, definition):
            continue
        value = values.get(item.name)
        if value is None or not str(value).strip() or is_placeholder_value(value):
            # Consult resolved defaults first: only ask when nothing trustworthy can fill it.
            if concrete_default(item, definition) is None:
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
    "default_is_trusted",
    "ensure_definition_inputs",
    "migrate_defaults",
    "finalize_authored_inputs",
    "input_grain",
    "invalid_typed_inputs",
    "resolve_typed_value",
    "inputs_from_integration",
    "inputs_from_json_schema",
    "merge_input_schema",
    "missing_required_inputs",
    "placeholders_in",
    "placeholders_in_definition",
    "pretty_label",
]


def skippable_step_inputs(step: WorkflowStep, definition: WorkflowDefinition, variables: dict[str, Any]) -> list[str]:
    """Optional inputs an action step depends on that were not given (and have no default).

    When every placeholder of an action step refers to such an input, the step is skipped so
    the page keeps its own state, instead of typing/clicking an empty value."""

    if step.type not in _ACTION_STEP_TYPES:
        return []
    payload = json.dumps(step.model_dump(exclude_none=True), default=str)
    names = set(re.findall(r"\{\{?\s*([a-zA-Z_][\w]*)(?:\|[a-zA-Z0-9_]+)?\s*\}?\}", payload))
    if not names:
        return []
    items = {item.name: item for item in (definition.input_schema or [])}
    absent: list[str] = []
    for name in names:
        item = items.get(name)
        if item is None:
            return []
        value = variables.get(name)
        if value not in (None, "") or input_is_required(item, definition):
            return []
        absent.append(name)
    return sorted(absent)

"""Cheap creation-time check that workflow inputs make sense for the task and steps."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from browser_platform.authoring.tools import is_action_ending
from browser_platform.interpolate import interpolate, interpolate_url
from browser_platform.llm.openrouter import chat_complete, env_api_key, usage_dict
from browser_platform.models.workflow import WorkflowDefinition, WorkflowInput, WorkflowStep
from browser_platform.workflow_inputs import ensure_definition_inputs, placeholders_in_definition, pretty_label

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][\w]*)(?:\|[a-zA-Z0-9_]+)?\}")


def _step_blob(step: WorkflowStep) -> str:
    parts = [
        step.type,
        step.url,
        step.value,
        step.target_text,
        step.cssSelector,
        step.xpath,
        step.description,
        step.extractionGoal,
        step.fuzzy_query,
        json.dumps(step.selectorStrategies or [], default=str),
    ]
    return " ".join(str(p) for p in parts if p)


def _used_names(definition: WorkflowDefinition) -> set[str]:
    found = set(placeholders_in_definition(definition))
    return found


def _examples(inputs: list[WorkflowInput]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in inputs:
        raw = item.default if item.default is not None else getattr(item, "example", None)
        if raw is not None and str(raw).strip():
            out[item.name] = str(raw)
    return out


def deterministic_findings(definition: WorkflowDefinition, *, task: str = "") -> list[dict[str, Any]]:
    definition = ensure_definition_inputs(definition)
    inputs = list(definition.input_schema or [])
    used = _used_names(definition)
    blobs = " ".join(_step_blob(step) for step in definition.steps or [])
    findings: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for item in inputs:
        name = (item.name or "").strip()
        if not name:
            findings.append(
                {
                    "severity": "error",
                    "code": "empty_name",
                    "message": "An input is missing a name.",
                    "suggestion_label": "Remove this empty input",
                    "patch": {"op": "remove_param", "name": name},
                }
            )
            continue
        if name in seen_names:
            findings.append(
                {
                    "severity": "error",
                    "code": "duplicate_name",
                    "message": f"Input '{name}' is declared more than once.",
                    "suggestion_label": f"Keep a single '{name}' input",
                    "patch": {"op": "remove_param", "name": name, "duplicate": True},
                }
            )
        seen_names.add(name)
        typ = (item.type or "string").lower()
        if typ not in {"string", "number", "bool", "boolean", "integer", "int", "float"}:
            findings.append(
                {
                    "severity": "warning",
                    "code": "odd_type",
                    "message": f"Input '{name}' has unusual type '{item.type}'. Use string, number, or bool.",
                    "suggestion_label": f"Set {name} type to string",
                    "patch": {"op": "set_param", "name": name, "field": "type", "value": "string"},
                }
            )
        if name not in used:
            findings.append(
                {
                    "severity": "error",
                    "code": "unused_input",
                    "message": f"Input '{name}' is not used in any step or URL.",
                    "suggestion_label": f"Remove unused input '{name}'",
                    "patch": {"op": "remove_param", "name": name},
                }
            )
        example = _examples([item]).get(name)
        if not example and item.required is not False:
            findings.append(
                {
                    "severity": "warning",
                    "code": "missing_example",
                    "message": f"Input '{pretty_label(name, label=item.label)}' has no example value.",
                    "suggestion_label": None,
                    "patch": None,
                }
            )

    # Hard-coded literals that look like they should have been parameters.
    examples = _examples(inputs)
    for step in definition.steps or []:
        url = step.url or ""
        if not url:
            continue
        if _PLACEHOLDER.search(url):
            continue
        for name, example in examples.items():
            if example and len(str(example)) >= 3 and str(example) in url:
                findings.append(
                    {
                        "severity": "error",
                        "code": "hardcoded_example",
                        "message": f"Step URL hard-codes '{example}' instead of using {{{name}}}.",
                        "suggestion_label": f"Bind {{{name}}} in the URL",
                        "patch": {"op": "bind_url", "name": name, "example": example, "url": url},
                    }
                )
        # Common entity that isn't declared, e.g. a team in a fixtures URL.
        if "/teams/" in url.lower() and "team" not in {i.name for i in inputs} and "{team" not in url:
            findings.append(
                {
                    "severity": "error",
                    "code": "missing_team_input",
                    "message": "A team appears hard-coded in a fixtures URL, but there is no 'team' input.",
                    "suggestion_label": "Add a required Team input and bind it in the URL",
                    "patch": {
                        "op": "add_param",
                        "name": "team",
                        "label": "Team",
                        "required": True,
                        "example": url.rstrip("/").split("/")[-1] or "Arsenal",
                    },
                }
            )

    # Example values should interpolate to a usable URL or fill.
    sample = examples
    for step in definition.steps or []:
        if step.url and sample:
            rendered = interpolate_url(step.url, sample)
            if isinstance(rendered, str) and rendered.startswith("http"):
                parsed = urlparse(rendered)
                if "{" in rendered or not parsed.netloc:
                    findings.append(
                        {
                            "severity": "error",
                            "code": "bad_example_url",
                            "message": f"Examples do not produce a valid URL ({rendered[:120]}).",
                            "suggestion_label": None,
                            "patch": None,
                        }
                    )
        if step.value and "{" in str(step.value) and sample:
            rendered = interpolate(step.value, sample)
            if isinstance(rendered, str) and "{" in rendered:
                findings.append(
                    {
                        "severity": "warning",
                        "code": "unfilled_example",
                        "message": f"Examples leave a placeholder unfilled: {rendered[:80]}.",
                        "suggestion_label": None,
                        "patch": None,
                    }
                )
    void = task  # reserved for the LLM pass
    del void
    # Dedupe by code+message
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in findings:
        key = f"{item.get('code')}:{item.get('message')}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


async def validate_inputs(
    definition: WorkflowDefinition | dict[str, Any],
    *,
    task: str = "",
    use_llm: bool = True,
) -> dict[str, Any]:
    """Return findings plus usage. Never blocks; callers decide whether to publish."""

    if isinstance(definition, dict):
        definition = WorkflowDefinition.model_validate(definition)
    definition = ensure_definition_inputs(definition)
    findings = deterministic_findings(definition, task=task)
    usage = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": False}
    if use_llm and env_api_key():
        try:
            extra, usage = await _llm_findings(definition, task=task, existing=findings)
            for item in extra:
                findings.append(item)
        except Exception:
            pass
    # Final dedupe
    seen: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    for item in findings:
        key = f"{item.get('code')}:{item.get('message')}"
        if key in seen:
            continue
        seen.add(key)
        item.setdefault("id", f"f{len(cleaned)+1}")
        cleaned.append(item)
    errors = sum(1 for item in cleaned if item.get("severity") == "error")
    return {
        "findings": cleaned,
        "error_count": errors,
        "warning_count": sum(1 for item in cleaned if item.get("severity") == "warning"),
        "blocks_publish": errors > 0,
        "action_ending": is_action_ending(definition),
        "usage": usage,
    }


async def _llm_findings(
    definition: WorkflowDefinition,
    *,
    task: str,
    existing: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    compact = {
        "task": (task or definition.description or "")[:400],
        "name": definition.name,
        "inputs": [item.model_dump(exclude_none=True) for item in definition.input_schema or []],
        "steps": [
            {
                "type": step.type,
                "url": step.url,
                "value": step.value,
                "target_text": step.target_text,
                "extractionGoal": step.extractionGoal,
            }
            for step in (definition.steps or [])[:20]
        ],
        "already": [item.get("code") for item in existing],
    }
    message, usage = await chat_complete(
        [
            {
                "role": "system",
                "content": (
                    "Check whether a browser workflow's inputs make sense. "
                    "Reply JSON only: {\"findings\":[{\"severity\":\"error|warning\",\"code\":\"short\",\"message\":\"...\",\"suggestion_label\":\"optional\"}]}. "
                    "Flag unused inputs, missing inputs, hard-coded values that should be inputs, "
                    "bad names/types/examples, and examples that would not fill a URL. "
                    "Do not repeat issues already listed. If everything is fine, return {\"findings\":[]}."
                ),
            },
            {"role": "user", "content": json.dumps(compact, ensure_ascii=False)[:2500]},
        ],
        max_tokens=250,
        response_format={"type": "json_object"},
    )
    findings: list[dict[str, Any]] = []
    raw = message.get("content") or "{}"
    try:
        data = json.loads(raw)
        for item in data.get("findings") or []:
            if not isinstance(item, dict) or not item.get("message"):
                continue
            findings.append(
                {
                    "severity": item.get("severity") if item.get("severity") in {"error", "warning"} else "warning",
                    "code": str(item.get("code") or "llm"),
                    "message": str(item.get("message"))[:300],
                    "suggestion_label": item.get("suggestion_label"),
                    "patch": item.get("patch"),
                    "source": "llm",
                }
            )
    except Exception:
        findings = []
    return findings, usage


def apply_patch(definition: WorkflowDefinition, patch: dict[str, Any] | None) -> WorkflowDefinition:
    """Apply a one-tap suggested fix. Unknown ops are ignored."""

    if not patch:
        return definition
    definition = definition.model_copy(deep=True)
    inputs = list(definition.input_schema or [])
    op = patch.get("op")
    name = str(patch.get("name") or "")
    if op == "remove_param" and name:
        inputs = [item for item in inputs if item.name != name]
    elif op == "add_param" and name:
        if not any(item.name == name for item in inputs):
            inputs.append(
                WorkflowInput(
                    name=name,
                    label=str(patch.get("label") or pretty_label(name)),
                    type=str(patch.get("type") or "string"),  # type: ignore[arg-type]
                    required=patch.get("required") is not False,
                    default=patch.get("example") or patch.get("default"),
                )
            )
    elif op == "set_param" and name:
        for item in inputs:
            if item.name != name:
                continue
            field = patch.get("field")
            if field == "type":
                item.type = patch.get("value") or item.type  # type: ignore[assignment]
            elif field == "label":
                item.label = str(patch.get("value") or item.label)
            elif field in {"example", "default"}:
                item.default = patch.get("value")
    elif op == "bind_url" and name and patch.get("example"):
        example = str(patch.get("example"))
        for step in definition.steps or []:
            if step.url and example in step.url and f"{{{name}" not in step.url:
                step.url = step.url.replace(example, "{" + name + "}")
    definition.input_schema = inputs
    return definition

"""Extract reusable from-task parameters and turn them into hard agent constraints."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from browser_platform.authoring.generalize import placeholders_in_task
from browser_platform.models.workflow import WorkflowInput
from browser_platform.workflow_inputs import pretty_label

_NAME = re.compile(r"^[a-zA-Z_][\w]{0,39}$")


class TaskParameter(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    label: str = ""
    example: str = ""
    required: bool = True
    description: str = ""


def normalize_parameter(raw: Any, *, used: set[str] | None = None) -> TaskParameter | None:
    used = used if used is not None else set()
    if isinstance(raw, TaskParameter):
        rec = raw.model_dump()
    elif isinstance(raw, dict):
        rec = raw
    else:
        return None
    name = re.sub(r"[^a-z0-9_]+", "_", str(rec.get("name") or "").strip().lower()).strip("_")
    if not name or not _NAME.match(name) or name in used:
        return None
    example = str(rec.get("example") or rec.get("value") or rec.get("default") or "").strip()
    label = str(rec.get("label") or "").strip() or pretty_label(name)
    required = rec.get("required")
    if required is None:
        required = True
    return TaskParameter(
        name=name,
        label=label,
        example=example,
        required=bool(required),
        description=str(rec.get("description") or "").strip(),
    )


def parameters_from_placeholders(task: str, provided: dict[str, Any] | None = None) -> list[TaskParameter]:
    provided = {str(k): str(v) for k, v in (provided or {}).items() if v is not None and str(v).strip()}
    out: list[TaskParameter] = []
    used: set[str] = set()
    for name in placeholders_in_task(task):
        param = normalize_parameter({"name": name, "example": provided.get(name, ""), "required": True}, used=used)
        if param:
            used.add(param.name)
            out.append(param)
    for name, value in provided.items():
        if name in used:
            for item in out:
                if item.name == name and not item.example:
                    item.example = value
            continue
        param = normalize_parameter({"name": name, "example": value, "required": True}, used=used)
        if param:
            used.add(param.name)
            out.append(param)
    return out


def merge_parameters(base: list[TaskParameter], extra: list[TaskParameter] | None = None, provided: dict[str, Any] | None = None) -> list[TaskParameter]:
    provided = {str(k): str(v) for k, v in (provided or {}).items() if v is not None and str(v).strip()}
    by_name: dict[str, TaskParameter] = {}
    for item in [*(base or []), *(extra or [])]:
        current = by_name.get(item.name)
        if current is None:
            by_name[item.name] = item
            continue
        if not current.example and item.example:
            current.example = item.example
        if not current.label and item.label:
            current.label = item.label
        if not current.description and item.description:
            current.description = item.description
    for name, value in provided.items():
        if name in by_name:
            if not by_name[name].example:
                by_name[name].example = value
        else:
            param = normalize_parameter({"name": name, "example": value, "required": True}, used=set(by_name))
            if param:
                by_name[param.name] = param
    return list(by_name.values())


def variables_from_parameters(parameters: list[TaskParameter], provided: dict[str, Any] | None = None) -> dict[str, str]:
    provided = {str(k): str(v) for k, v in (provided or {}).items() if v is not None and str(v).strip()}
    out: dict[str, str] = {}
    for item in parameters:
        value = provided.get(item.name) or item.example
        if value:
            out[item.name] = value
    return out


def schema_from_parameters(parameters: list[TaskParameter], resolved: dict[str, str] | None = None) -> list[WorkflowInput]:
    resolved = resolved or {}
    schema: list[WorkflowInput] = []
    for item in parameters:
        schema.append(
            WorkflowInput(
                name=item.name,
                type="string",
                required=item.required if item.required is not None else True,
                default=resolved.get(item.name) or item.example or None,
                label=item.label or pretty_label(item.name),
                description=item.description or None,
            )
        )
    return schema


def constraint_prompt(task: str, parameters: list[TaskParameter], variables: dict[str, Any] | None = None, start_url: str | None = None) -> str:
    """Build the browser-use demonstration prompt with hard parameter constraints."""
    from browser_platform.interpolate import interpolate

    variables = {str(k): str(v) for k, v in (variables or {}).items() if v is not None}
    filled = interpolate(task, variables)
    lines = [
        filled.strip(),
        "",
        "Hard constraints — you must satisfy every one before finishing:",
    ]
    if parameters:
        for item in parameters:
            value = variables.get(item.name) or item.example
            label = item.label or item.name
            if value:
                lines.append(f'- {label} ({item.name}) must be "{value}". Apply this as a real UI filter, chosen option, or entity link.')
            else:
                lines.append(f"- {label} ({item.name}) is a required input. Choose a specific value in the site UI.")
    else:
        lines.append("- If the task names a specific entity (a team, competition, person, or search term), apply that as a filter in the UI.")
    lines += [
        "- Do not stop on a generic unfiltered list that happens to mention the value among many others.",
        "- Typing into a search box or adding ?q= is not enough: you must confirm the matching suggestion, dropdown option, or team/competition link so the page or URL is clearly scoped to that value (for example a /teams/… path, a heading, or a selected chip).",
        "- When you are done, the visible result should be about those parameter values only.",
        "Do not close other tabs. Do not clear cookies or site data. Do not open a new browser window. Prefer the current Chrome session.",
    ]
    prompt = "\n".join(lines)
    if start_url:
        prompt = f"Start at {start_url}. {prompt}"
    return prompt


def retry_constraint_prompt(base: str, missing: list[str], url: str) -> str:
    detail = ", ".join(missing) or "the required parameters"
    return (
        f"{base}\n\n"
        f"Your previous attempt did not actually apply the filter for {detail}. "
        f"The page ended at {url or '(unknown URL)'}. "
        "Go back and select the exact matching option, chip, or entity page so the URL path or heading reflects each value. "
        "Then extract the filtered result."
    )


async def extract_parameters_with_llm(
    task: str,
    *,
    llm: Any,
    provided: dict[str, Any] | None = None,
) -> tuple[list[TaskParameter], dict[str, Any]]:
    """Cheap JSON extraction of reusable inputs. Falls back to placeholders on failure."""
    usage = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": False}
    fallback = parameters_from_placeholders(task, provided)
    api_key = llm.key() if llm is not None and hasattr(llm, "key") else None
    if not api_key:
        return fallback, usage
    try:
        import httpx
    except Exception:
        return fallback, usage
    model = getattr(llm, "model", None) or "qwen/qwen3.7-flash"
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 250,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Extract reusable input parameters from a browser automation task. "
                    'Reply with JSON only: {"parameters":[{"name":"team","label":"Team","example":"Arsenal","required":true,"description":"Club or side to filter by"}]}. '
                    "Include only values the user would change on a later run (search terms, team or competition names, dates, filters). "
                    "name must be a short snake_case identifier. "
                    "If the task already contains {placeholders}, use those names. "
                    "Do not invent a parameter for the site itself. If there are none, return {\"parameters\":[]}."
                ),
            },
            {"role": "user", "content": task},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        usage_block = body.get("usage") or {}
        usage["tokens_in"] = int(usage_block.get("prompt_tokens") or usage_block.get("input_tokens") or 0)
        usage["tokens_out"] = int(usage_block.get("completion_tokens") or usage_block.get("output_tokens") or 0)
        usage["used"] = True
        text = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
        data = json.loads(text)
        raw_list = data.get("parameters") if isinstance(data, dict) else data
        extracted: list[TaskParameter] = []
        used_names: set[str] = set()
        if isinstance(raw_list, list):
            for item in raw_list:
                param = normalize_parameter(item, used=used_names)
                if param:
                    used_names.add(param.name)
                    extracted.append(param)
        merged = merge_parameters(fallback, extracted, provided)
        return merged, usage
    except Exception:
        return fallback, usage

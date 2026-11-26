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
    provided = normalize_variable_map(provided)
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
    provided = normalize_variable_map(provided)
    out: dict[str, str] = {}
    for item in parameters:
        value = provided.get(item.name) or item.example
        if value:
            out[item.name] = value
    return out



_TEAM_TASK = re.compile(r"\b(team|teams|fixture|fixtures|club|football|soccer|premier league)\b", re.I)
_MONTH_TASK = re.compile(r"\b(month|one month|next month)\b", re.I)
_JUNK_VALUE = re.compile(r"UserAgent|HardwareConcurrency|FontString|internal-pdf-viewer")


def normalize_variable_map(values: dict[str, Any] | None) -> dict[str, str]:
    """Lower-case and slug keys so Team and team are the same input."""

    out: dict[str, str] = {}
    for key, raw in (values or {}).items():
        if raw is None or not str(raw).strip():
            continue
        param = normalize_parameter({"name": key, "example": str(raw)})
        name = param.name if param else re.sub(r"[^a-z0-9_]+", "_", str(key).strip().lower()).strip("_")
        if name:
            out[name] = str(raw).strip()
    return out


def seed_parameters_from_task(task: str, existing: list[TaskParameter] | None = None) -> list[TaskParameter]:
    """Deterministic seeds when the task implies a well-known filter but the LLM omitted it."""

    existing = list(existing or [])
    used = {item.name for item in existing}
    seeds: list[dict[str, Any]] = []
    if _TEAM_TASK.search(task or "") and "team" not in used:
        seeds.append({"name": "team", "label": "Team", "example": "Arsenal", "required": True, "description": "Club or side to filter by"})
    if _MONTH_TASK.search(task or "") and "month" not in used:
        seeds.append({"name": "month", "label": "Month", "example": "November", "required": False, "description": "Month to show fixtures for"})
    if re.search(r"saucedemo|sauce labs", task or "", re.I):
        seeds.extend(
            [
                {"name": "product", "label": "Product", "example": "Sauce Labs Backpack", "required": True, "description": "Catalogue item to add to the cart"},
                {"name": "first_name", "label": "First name", "example": "Alex", "required": True, "description": "Checkout first name"},
                {"name": "last_name", "label": "Last name", "example": "Lewis", "required": True, "description": "Checkout last name"},
                {"name": "zip", "label": "Zip", "example": "90210", "required": True, "description": "Checkout postal code"},
            ]
        )
    if re.search(r"demoqa|automation-practice-form", task or "", re.I):
        seeds.extend(
            [
                {"name": "first_name", "label": "First name", "example": "Alex", "required": True},
                {"name": "last_name", "label": "Last name", "example": "Lewis", "required": True},
                {"name": "email", "label": "Email", "example": "alex@example.com", "required": True},
                {"name": "gender", "label": "Gender", "example": "Male", "required": True},
                {"name": "mobile", "label": "Mobile", "example": "9876543210", "required": True},
            ]
        )
    if re.search(r"httpbin|pizza form", task or "", re.I):
        seeds.extend(
            [
                {"name": "customer", "label": "Customer", "example": "John Doe", "required": True},
                {"name": "size", "label": "Size", "example": "large", "required": True},
                {"name": "toppings", "label": "Toppings", "example": "bacon", "required": True},
            ]
        )
    extras: list[TaskParameter] = []
    for raw in seeds:
        param = normalize_parameter(raw, used=used)
        if param:
            used.add(param.name)
            extras.append(param)
    merged = merge_parameters(existing, extras)
    for item in merged:
        if item.name == "team" and not item.example:
            item.example = "Arsenal"
    return merged


def maybe_bind_known_urls(steps: list, parameters: list[TaskParameter] | None = None, *, task: str = "") -> list:
    """If a football-team task ended on the generic fixtures page, bind /teams/{team|slug}/."""

    names = {item.name for item in (parameters or [])}
    if "team" not in names or not steps:
        return steps
    if any("{team" in (getattr(step, "url", None) or "") for step in steps):
        return steps
    generic = None
    for step in steps:
        url = (getattr(step, "url", None) or "").lower()
        if "bbc.com/sport" in url and "/teams/" not in url:
            generic = step
            break
    if generic is None:
        return steps
    generic.url = "https://www.bbc.com/sport/football/teams/{team|slug}/scores-fixtures"
    # The search-box fill is redundant once the URL is scoped to the team.
    kept = []
    for step in steps:
        value = getattr(step, "value", None) or ""
        if getattr(step, "type", None) == "input" and value.lower() in {"{team}", "{team|slug}"}:
            continue
        kept.append(step)
    return kept or steps


def is_junk_suggestion(item: dict[str, Any] | None) -> bool:
    if not item:
        return False
    value = str(item.get("value") or "")
    name = str(item.get("suggested_name") or item.get("name") or "")
    if len(value) > 200 or _JUNK_VALUE.search(value):
        return True
    if name in {"v", "value", "input", "q"} and (len(value) > 80 or value.startswith("{")):
        return True
    return False


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
    from browser_platform.executor.verify import classify_parameter

    if parameters:
        for item in parameters:
            value = variables.get(item.name) or item.example
            label = item.label or item.name
            kind = classify_parameter(item.name)
            if kind == "form":
                if value:
                    lines.append(f'- {label} ({item.name}) must be typed or chosen as "{value}" in the form field, then submitted.')
                else:
                    lines.append(f"- {label} ({item.name}) is a required form field. Fill it before submitting.")
            elif kind == "locator":
                if value:
                    lines.append(f'- {label} ({item.name}) must be "{value}". Click or choose that item in the list, not just search for it.')
                else:
                    lines.append(f"- {label} ({item.name}) must be chosen from the list or catalogue.")
            else:
                if value:
                    lines.append(f'- {label} ({item.name}) must be "{value}". Apply this as a real UI filter, chosen option, or entity link so the URL or heading reflects it.')
                else:
                    lines.append(f"- {label} ({item.name}) is a required filter. Choose a specific value in the site UI.")
    else:
        lines.append("- If the task names a specific entity (a team, competition, person, or search term), apply that as a filter in the UI.")
    lines += [
        "- Finish the whole task. If it includes login, add-to-cart, checkout, or submit, do not stop on an earlier page such as a catalogue or an unsubmitted form.",
        "- Do not stop on a generic unfiltered list that happens to mention a filter value among many others.",
        "- For filters, typing into a search box or adding ?q= is not enough: confirm the matching suggestion, dropdown option, or entity link so the page or URL is clearly scoped to that value.",
        "- Form fields do not need to appear in the URL; they must be filled and then submitted so the confirmation or echo contains them.",
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
    """Cheap JSON extraction of reusable inputs. Falls back to placeholders and task seeds."""
    usage = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": False}
    fallback = seed_parameters_from_task(task, parameters_from_placeholders(task, provided))
    api_key = llm.key() if llm is not None and hasattr(llm, "key") else None
    if not api_key:
        return fallback, usage
    try:
        from browser_platform.llm.openrouter import chat_complete, message_text

        message, usage = await chat_complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Extract reusable input parameters from a browser automation task. "
                        'Reply with JSON only: {"parameters":[{"name":"team","label":"Team","example":"Arsenal","required":true,"description":"Club or side to filter by"}]}. '
                        "Include only values the user would change on a later run (search terms, team or competition names, dates, filters). "
                        "name must be a short snake_case identifier. Always include a realistic example. "
                        "If the task is about football or sports team fixtures and does not name a team, still include a required team input with example Arsenal. "
                        "If the task already contains {placeholders}, use those names. "
                        "Do not invent a parameter for the website itself, cookies, or device info."
                    ),
                },
                {"role": "user", "content": task},
            ],
            max_tokens=400,
            response_format={"type": "json_object"},
            model=getattr(llm, "model", None) or "qwen/qwen3.7-flash",
            api_key=api_key,
        )
        text = message_text(message)
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
        data = json.loads(text or "{}")
        raw_list = data.get("parameters") if isinstance(data, dict) else data
        extracted: list[TaskParameter] = []
        used_names: set[str] = set()
        if isinstance(raw_list, list):
            for item in raw_list:
                param = normalize_parameter(item, used=used_names)
                if param:
                    used_names.add(param.name)
                    extracted.append(param)
        merged = seed_parameters_from_task(task, merge_parameters(fallback, extracted, provided))
        return merged, usage
    except Exception:
        return fallback, usage

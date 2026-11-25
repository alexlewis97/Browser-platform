"""Turn a human Chrome recording into a reviewable workflow-use document."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from browser_platform import DEFAULT_MODEL
from browser_platform.authoring.compact import compact_recording_steps, estimate_cost_usd, maybe_llm_prune, stamp_rec_ids
from browser_platform.authoring.elements import strategies_for_element
from browser_platform.authoring.from_task import attach_parameter_checks, events_to_steps
from browser_platform.authoring.generalize import generalize_events
from browser_platform.authoring.parameters import (
    TaskParameter,
    extract_parameters_with_llm,
    merge_parameters,
    parameters_from_placeholders,
    schema_from_parameters,
    variables_from_parameters,
)
from browser_platform.authoring.recorder import RecordedEvent, compact_events, stabilize_event_order
from browser_platform.models.llm import LLMConfig
from browser_platform.models.workflow import WorkflowDefinition, WorkflowStep
from browser_platform.workflow_inputs import pretty_label

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{2,}")


class ReviewItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kept: bool = True
    reason: str | None = None
    step: dict[str, Any] = Field(default_factory=dict)


class RecordingCleanResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    review: list[ReviewItem] = Field(default_factory=list)
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    variables: dict[str, str] = Field(default_factory=dict)
    definition: WorkflowDefinition | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    stats: dict[str, Any] = Field(default_factory=dict)
    name: str | None = None
    creation: dict[str, Any] = Field(default_factory=dict)
    input_validation: dict[str, Any] | None = None


def events_from_payload(raw: list[Any] | None) -> list[RecordedEvent]:
    events: list[RecordedEvent] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        typ = str(item.get("type") or "").strip()
        if typ == "tab_change":
            typ = "navigation"
        if not typ or typ.startswith("_"):
            continue
        element = item.get("element") if isinstance(item.get("element"), dict) else None
        ev = RecordedEvent(
            type=typ,
            url=item.get("url") or item.get("url_after"),
            value=item.get("value"),
            key=item.get("key"),
            text=item.get("text") or (element or {}).get("text"),
            element=element,
            selectors=strategies_for_element(element) if element else [],
            ts=item.get("ts"),
        )
        # Preserve change flags for noop detection (RecordedEvent is a dataclass; stash on element).
        if element is None:
            ev.element = {}
        assert ev.element is not None
        if "changed" in item:
            ev.element["changed"] = item.get("changed")
        if item.get("url_after"):
            ev.element["url_after"] = item.get("url_after")
        events.append(ev)
    return stabilize_event_order(compact_events(events))


def recording_events_to_steps(events: list[RecordedEvent], *, extract_goal: str = "result") -> list[WorkflowStep]:
    steps = events_to_steps(events, extract_goal=extract_goal)
    # Align extras (changed/element) onto matching click/input steps in order.
    action_events = [ev for ev in events if ev.type in {"click", "input", "select_change", "key_press"}]
    action_steps = [s for s in steps if s.type in {"click", "input", "select_change", "key_press"}]
    for ev, step in zip(action_events, action_steps):
        data = step.model_dump()
        el = ev.element or {}
        if "changed" in el:
            data["changed"] = el.get("changed")
        if el:
            data["element"] = {k: v for k, v in el.items() if k != "changed"}
        # Re-find and replace
        for i, current in enumerate(steps):
            if current is step:
                steps[i] = WorkflowStep.model_validate(data)
                break
    return stamp_rec_ids(steps)


def _slug(value: str) -> str:
    return re.sub(r"\s+", "-", (value or "").strip().lower())


def _urls(events: list[RecordedEvent]) -> list[str]:
    return [ev.url for ev in events if ev.url]


_GENERIC_VALUES = {
    "football", "soccer", "cricket", "rugby", "tennis", "sport", "sports", "home",
    "news", "scores", "fixtures", "scores & fixtures", "scores and fixtures",
    "more", "bbc", "menu", "search", "all", "skip to content", "skip to main content",
    "sign in", "account", "watch", "audio", "weather", "iplayer",
}


def _team_slugs(events: list[RecordedEvent]) -> list[str]:
    found: list[str] = []
    for url in _urls(events):
        path = urlparse(url).path.lower()
        for match in re.finditer(r"/teams/([a-z0-9-]+)", path):
            slug = match.group(1)
            if slug not in found:
                found.append(slug)
    return found


def infer_variables(events: list[RecordedEvent], goal: str = "") -> dict[str, str]:
    """Guess reusable inputs from typed text, chosen options, and URL slugs."""

    provided: dict[str, str] = {}
    urls = " ".join(_urls(events)).lower()
    team_slugs = _team_slugs(events)
    path = " ".join(urlparse(u).path.lower() for u in _urls(events))

    def take(name: str, value: str) -> None:
        value = (value or "").strip()
        if not value or len(value) > 80:
            return
        if value.strip().lower() in _GENERIC_VALUES:
            return
        key = name
        i = 2
        while key in provided and provided[key].lower() != value.lower():
            key = f"{name}_{i}"
            i += 1
        provided[key] = value

    def looks_like_team(text: str) -> bool:
        slug = _slug(text)
        head = _slug(text.split()[0]) if text.split() else ""
        return bool(slug and (slug in team_slugs or head in team_slugs or f"/teams/{slug}" in path or f"/teams/{head}" in path))

    for ev in events:
        if ev.type in {"input", "select_change"} and ev.value and ev.value.strip():
            placeholder = ((ev.element or {}).get("placeholder") or ev.text or "").lower()
            name = "term" if any(w in placeholder for w in ("search", "find", "query")) else "value"
            text = ev.value.strip()
            if looks_like_team(text) or (team_slugs and "team" in (goal or "").lower()):
                name = "team"
            elif "/teams/" in urls or "team" in (goal or "").lower() or "fixture" in (goal or "").lower():
                if looks_like_team(text) or text.lower() not in _GENERIC_VALUES:
                    name = "team" if looks_like_team(text) else name
            take(name, text)
        elif ev.type == "click":
            text = (ev.text or (ev.element or {}).get("text") or "").strip()
            if not text or len(text) > 60:
                continue
            slug = _slug(text)
            head = _slug(text.split()[0]) if text.split() else ""
            if slug and (slug in path or (head and len(head) >= 3 and f"/{head}/" in path + "/")):
                name = "team" if looks_like_team(text) else "value"
                value = text
                if slug not in path and head and f"/{head}/" in path + "/":
                    value = text.split()[0]
                take(name, value)

    if team_slugs and "team" not in provided:
        # Promote a matching value, or fall back to the last /teams/{slug} seen.
        match_name = None
        for key, value in list(provided.items()):
            if looks_like_team(value):
                match_name = key
                break
        if match_name:
            provided["team"] = provided.pop(match_name) if match_name != "team" else provided[match_name]
            if match_name != "team":
                provided.pop(match_name, None)
        else:
            slug = team_slugs[-1]
            take("team", slug.replace("-", " ").title())

    # If both a generic leftover and a real team exist, keep the /teams/ match as team.
    if team_slugs and "team" in provided and not looks_like_team(provided["team"]):
        for key, value in list(provided.items()):
            if key != "team" and looks_like_team(value):
                provided[key], provided["team"] = provided["team"], value
                break
        if not looks_like_team(provided["team"]):
            provided["team"] = team_slugs[-1].replace("-", " ").title()

    for name in re.findall(r"\{([a-zA-Z_][\w]*)\}", goal or ""):
        provided.setdefault(name, provided.get(name, ""))
    return {k: v for k, v in provided.items() if v}


def _extract_goal(goal: str) -> str:
    lower = (goal or "").lower()
    if "first paragraph" in lower or "paragraph" in lower:
        return "first paragraph"
    if "heading" in lower or "title" in lower:
        return "heading"
    if "fixture" in lower or "score" in lower:
        return "fixtures"
    return "result"


def llm_config_from_request(llm: LLMConfig | None) -> LLMConfig | None:
    key = ""
    model = DEFAULT_MODEL
    if llm is not None:
        try:
            key = llm.key() if hasattr(llm, "key") else ""
        except Exception:
            key = ""
        model = getattr(llm, "model", None) or model
    if not key:
        key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        return None
    return LLMConfig(api_key=key, model=model, max_tokens=300)


def _review_items(
    original: list[WorkflowStep],
    kept: list[WorkflowStep],
    removed: list[dict[str, Any]],
    llm_dropped: list[dict[str, Any]] | None = None,
) -> list[ReviewItem]:
    kept_ids = {str(getattr(s, "rec_id", "") or "") for s in kept}
    reason_by_id = {item["id"]: item.get("reason") for item in removed}
    for item in llm_dropped or []:
        reason_by_id[item["id"]] = item.get("reason") or "llm"
    items: list[ReviewItem] = []
    seen: set[str] = set()
    # Show in original order, then any new extract-only steps at the end.
    for step in original:
        ident = str(getattr(step, "rec_id", "") or "")
        if not ident or ident in seen:
            continue
        seen.add(ident)
        kept_step = next((s for s in kept if str(getattr(s, "rec_id", "") or "") == ident), None)
        items.append(
            ReviewItem(
                id=ident,
                kept=kept_step is not None,
                reason=None if kept_step else str(reason_by_id.get(ident) or "pruned"),
                step=(kept_step or step).model_dump(exclude_none=True),
            )
        )
    for step in kept:
        ident = str(getattr(step, "rec_id", "") or "")
        if ident and ident not in seen:
            seen.add(ident)
            items.append(ReviewItem(id=ident, kept=True, reason=None, step=step.model_dump(exclude_none=True)))
    return items



def usage_label(usage: dict[str, Any] | None) -> str:
    """Human-readable token line that does not call extract 'prune'."""
    usage = usage or {}
    tin = int(usage.get("tokens_in") or 0)
    tout = int(usage.get("tokens_out") or 0)
    prune = bool(usage.get("prune_used"))
    extract = bool(usage.get("extract_used"))
    if prune and extract:
        return f"LLM extract + prune used {tin} in / {tout} out tokens."
    if prune:
        return f"LLM prune used {tin} in / {tout} out tokens."
    if extract:
        return f"Parameter extract used {tin} in / {tout} out tokens (deterministic prune only)."
    if usage.get("used"):
        return f"LLM used {tin} in / {tout} out tokens."
    return "Deterministic prune only (no LLM tokens)."


async def clean_recording(
    *,
    events: list[dict[str, Any]] | list[RecordedEvent],
    goal: str = "",
    name: str | None = None,
    variables: dict[str, Any] | None = None,
    llm: LLMConfig | None = None,
) -> RecordingCleanResult:
    raw_events = events if events and isinstance(events[0], RecordedEvent) else events_from_payload(events)  # type: ignore[arg-type]
    if not raw_events:
        raise ValueError("No usable recorded events. Try recording a click, typed value, or navigation.")

    inferred = infer_variables(raw_events, goal)
    provided = {**inferred, **{str(k): str(v) for k, v in (variables or {}).items() if v is not None and str(v).strip()}}
    cfg = llm_config_from_request(llm)
    parameters: list[TaskParameter] = parameters_from_placeholders(goal, provided)
    extract_usage = {"tokens_in": 0, "tokens_out": 0, "used": False}
    if cfg is not None and (goal or provided):
        try:
            guessed, extract_usage = await extract_parameters_with_llm(goal or "Recorded browser task", llm=cfg, provided=provided)
            parameters = merge_parameters(parameters, guessed, provided)
        except Exception:
            parameters = merge_parameters(parameters, [], provided)
    else:
        parameters = merge_parameters(parameters, [], provided)
    resolved = variables_from_parameters(parameters, provided)

    generalized, schema, resolved, _suggestions = generalize_events(
        raw_events, task=goal or "Recorded browser task", variables=resolved
    )
    steps = recording_events_to_steps(generalized, extract_goal=_extract_goal(goal))
    original = list(steps)
    steps, compact_stats, removed = compact_recording_steps(steps)

    llm_usage = {"used": False, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    llm_dropped: list[dict[str, Any]] = []
    if cfg is not None:
        before_llm = list(steps)
        steps, llm_usage = await maybe_llm_prune(steps, task=goal or "Complete the recorded task with as few steps as possible.", llm=cfg)
        kept_ids = {str(getattr(s, "rec_id", "") or "") for s in steps}
        for step in before_llm:
            ident = str(getattr(step, "rec_id", "") or "")
            if ident and ident not in kept_ids:
                llm_dropped.append({"id": ident, "reason": "llm", "step": step.model_dump(exclude_none=True)})
        if llm_usage.get("used"):
            compact_stats = {**compact_stats, "method": "deterministic+llm", "steps_after": len(steps), "llm": llm_usage}
        else:
            compact_stats = {**compact_stats, "llm": llm_usage}

    # Keep pre-extracted parameters in the schema even if a step used a different spelling.
    param_schema = schema_from_parameters(parameters, resolved)
    seen = {item.name for item in param_schema}
    for item in schema:
        if item.name not in seen:
            param_schema.append(item)
            seen.add(item.name)
    steps = attach_parameter_checks(steps, parameters)
    title = (name or "").strip() or (goal.strip()[:80] if goal.strip() else "Recorded workflow")
    definition = WorkflowDefinition(
        name=title,
        description=goal.strip() or "Recorded in Chrome and cleaned into a reusable workflow.",
        version="1",
        input_schema=param_schema,
        steps=steps,
        workflow_analysis=f"Created from a Chrome recording of: {goal.strip() or title}",
        cache_results=False,
    )
    tokens_in = int((extract_usage or {}).get("tokens_in") or 0) + int((llm_usage or {}).get("tokens_in") or 0)
    tokens_out = int((extract_usage or {}).get("tokens_out") or 0) + int((llm_usage or {}).get("tokens_out") or 0)
    review = _review_items(original, steps, removed, llm_dropped)
    input_validation: dict[str, Any] | None = None
    try:
        from browser_platform.authoring.input_validate import validate_inputs
        input_validation = await validate_inputs(definition, task=goal)
        tokens_in += int((input_validation.get("usage") or {}).get("tokens_in") or 0)
        tokens_out += int((input_validation.get("usage") or {}).get("tokens_out") or 0)
    except Exception:
        input_validation = {"findings": [], "blocks_publish": False, "error": "input validation skipped"}
    return RecordingCleanResult(

        review=review,
        parameters=[item.model_dump() for item in parameters],
        variables=resolved,
        definition=definition,
        usage={
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "used": bool((extract_usage or {}).get("used") or (llm_usage or {}).get("used")),
            "extract_used": bool((extract_usage or {}).get("used")),
            "prune_used": bool((llm_usage or {}).get("used")),
            "label": usage_label({
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "used": bool((extract_usage or {}).get("used") or (llm_usage or {}).get("used")),
                "extract_used": bool((extract_usage or {}).get("used")),
                "prune_used": bool((llm_usage or {}).get("used")),
            }),
        },
        stats={**compact_stats, "steps_before": len(original), "steps_after": len(steps)},
        name=title,
        creation={
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": estimate_cost_usd(tokens_in, tokens_out, llm_usage.get("cost_usd")),
            "compaction": compact_stats,
            "events": len(raw_events),
            "steps_recorded": len(original),
            "steps_kept": len(steps),
        },
        input_validation=input_validation,
    )


def definition_from_review(
    *,
    name: str,
    goal: str,
    review: list[dict[str, Any]] | list[ReviewItem],
    parameters: list[dict[str, Any]] | list[TaskParameter] | None = None,
    variables: dict[str, Any] | None = None,
) -> WorkflowDefinition:
    kept: list[WorkflowStep] = []
    for item in review or []:
        rec = item if isinstance(item, dict) else item.model_dump()
        if rec.get("kept") is False:
            continue
        step = rec.get("step") or {}
        if isinstance(step, WorkflowStep):
            kept.append(step)
        elif isinstance(step, dict) and step.get("type"):
            kept.append(WorkflowStep.model_validate(step))
    if not kept:
        raise ValueError("Nothing to save: restore or keep at least one step.")
    if not any(s.type in {"extract", "extract_page_content"} for s in kept):
        kept.append(WorkflowStep(type="extract", extractionGoal=_extract_goal(goal), description="Extract the result"))
    params: list[TaskParameter] = []
    used: set[str] = set()
    from browser_platform.authoring.parameters import normalize_parameter

    for raw in parameters or []:
        item = raw if isinstance(raw, TaskParameter) else normalize_parameter(raw, used=used)
        if item:
            used.add(item.name)
            params.append(item)
    resolved = variables_from_parameters(params, variables or {})
    schema = schema_from_parameters(params, resolved)
    kept = attach_parameter_checks(kept, params)
    title = (name or "").strip() or (goal.strip()[:80] if goal.strip() else "Recorded workflow")
    return WorkflowDefinition(
        name=title,
        description=goal.strip() or "Recorded in Chrome and cleaned into a reusable workflow.",
        version="1",
        input_schema=schema,
        steps=kept,
        workflow_analysis=f"Created from a Chrome recording of: {goal.strip() or title}",
        cache_results=False,
    )

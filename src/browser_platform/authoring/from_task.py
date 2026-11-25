"""Run a browser-use demonstration, save a workflow-use document, and replay it."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from browser_platform.authoring.compact import compact_steps, estimate_cost_usd, maybe_llm_prune, usage_from_history
from browser_platform.authoring.generalize import generalize_events, placeholders_in_task
from browser_platform.authoring.parameters import (
    TaskParameter,
    constraint_prompt,
    extract_parameters_with_llm,
    merge_parameters,
    parameters_from_placeholders,
    retry_constraint_prompt,
    schema_from_parameters,
    variables_from_parameters,
)
from browser_platform.executor.verify import missing_parameters
from browser_platform.models.workflow import ExpectedState
from browser_platform.authoring.recorder import (
    RecordedEvent,
    attach_recorder,
    collect_events,
    detach_recorder,
    poll_field_changes,
)
from browser_platform.config import Settings
from browser_platform.models.llm import LLMConfig
from browser_platform.workflow_inputs import pretty_label
from browser_platform.models.workflow import ExpectedState, WorkflowDefinition, WorkflowInput, WorkflowStep
from browser_platform.orchestrator.runner import WorkflowRunner
from browser_platform.repair.browser_use_agent import build_cdp_browser, build_chat_openai, browser_use_available
from browser_platform.store.file_store import FileStore

AgentRunner = Callable[..., Awaitable[Any]]


class FromTaskUsage(BaseModel):
    model_config = ConfigDict(extra="allow")

    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    latency_ms: float = 0
    steps: int = 0
    steps_before: int = 0
    steps_after: int = 0
    compaction: dict[str, Any] | None = None


class FromTaskResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    status: str = "running"
    task: str
    name: str
    variables: dict[str, Any] = Field(default_factory=dict)
    workflow_id: str | None = None
    definition: WorkflowDefinition | None = None
    creation: FromTaskUsage = Field(default_factory=FromTaskUsage)
    replay: dict[str, Any] | None = None
    suggestions: list[dict[str, Any]] = Field(default_factory=list)
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    confirmation: dict[str, Any] | None = None
    input_validation: dict[str, Any] | None = None
    error: str | None = None



def apply_variable_promotions(
    events: list[RecordedEvent],
    suggestions: list[dict[str, Any]],
    chosen: dict[str, Any],
    resolved: dict[str, str],
    *,
    accept_suggestions: bool = True,
) -> tuple[list[RecordedEvent], list[WorkflowInput], dict[str, str]]:
    """Apply a human's {name: value} or {value: name} mapping to captured input events."""

    name_for_value: dict[str, str] = {}
    if accept_suggestions:
        for item in suggestions:
            if item.get("suggested_name") and item.get("value") is not None:
                name_for_value[str(item["value"])] = str(item["suggested_name"])
    suggestion_values = {str(item.get("value")) for item in suggestions if item.get("value") is not None}
    for key, raw in (chosen or {}).items():
        key, raw = str(key), str(raw)
        if key in suggestion_values:
            name_for_value[key] = raw
        else:
            name_for_value[raw] = key
            resolved[key] = raw
    for ev in events:
        if ev.type not in {"input", "select_change"}:
            continue
        if ev.value is None or str(ev.value).startswith("{"):
            continue
        text = str(ev.value)
        name = name_for_value.get(text)
        if name:
            ev.value = "{" + name + "}"
            resolved[name] = text
    names = list(dict.fromkeys([*placeholders_in_task(""), *resolved.keys()]))
    # placeholders already in resolved keys
    schema = [WorkflowInput(name=n, type="string", required=True, default=resolved.get(n), label=pretty_label(n)) for n in list(dict.fromkeys(list(resolved.keys())))]
    return events, schema, resolved


def events_to_steps(events: list[RecordedEvent], *, extract_goal: str = "result") -> list[WorkflowStep]:
    steps: list[WorkflowStep] = []
    for ev in events:
        if ev.type == "navigation" and ev.url:
            steps.append(WorkflowStep(type="navigation", url=ev.url, description=f"Open {ev.url}"))
        elif ev.type == "click":
            target = ev.text or (ev.element or {}).get("name") or ev.value
            steps.append(
                WorkflowStep(
                    type="click",
                    description=f"Click {target}" if target else "Click",
                    target_text=target,
                    cssSelector=(ev.element or {}).get("css"),
                    xpath=(ev.element or {}).get("xpath"),
                    selectorStrategies=ev.selectors or None,
                )
            )
        elif ev.type == "input":
            steps.append(
                WorkflowStep(
                    type="input",
                    description="Fill field",
                    value=ev.value,
                    target_text=(ev.element or {}).get("placeholder") or ev.text,
                    cssSelector=(ev.element or {}).get("css"),
                    xpath=(ev.element or {}).get("xpath"),
                    selectorStrategies=ev.selectors or None,
                )
            )
        elif ev.type == "select_change":
            steps.append(
                WorkflowStep(
                    type="select_change",
                    description="Choose option",
                    selectedText=ev.value,
                    target_text=ev.value,
                    cssSelector=(ev.element or {}).get("css"),
                    xpath=(ev.element or {}).get("xpath"),
                    selectorStrategies=ev.selectors or None,
                )
            )
        elif ev.type == "key_press" and ev.key:
            steps.append(
                WorkflowStep(
                    type="key_press",
                    description=f"Press {ev.key}",
                    key=ev.key,
                    cssSelector=(ev.element or {}).get("css"),
                    xpath=(ev.element or {}).get("xpath"),
                    selectorStrategies=ev.selectors or None,
                )
            )
        elif ev.type == "extract":
            steps.append(
                WorkflowStep(
                    type="extract",
                    extractionGoal=ev.value or ev.text or extract_goal,
                    cssSelector=(ev.element or {}).get("css"),
                )
            )
    if not steps or steps[-1].type not in {"extract", "extract_page_content"}:
        steps.append(WorkflowStep(type="extract", extractionGoal=extract_goal, description="Extract the result"))
    return steps


def _extract_goal(task: str) -> str:
    lower = task.lower()
    if "first paragraph" in lower or "paragraph" in lower:
        return "first paragraph"
    if "heading" in lower or "title" in lower:
        return "heading"
    return "result"


def demonstration_task(
    task: str,
    variables: dict[str, Any] | None = None,
    start_url: str | None = None,
    parameters: list[TaskParameter] | None = None,
) -> str:
    """Fill {placeholders} and state hard filter constraints the agent must satisfy."""
    return constraint_prompt(task, parameters or [], variables, start_url)


def _parameters_payload(parameters: list[TaskParameter]) -> list[dict[str, Any]]:
    return [item.model_dump() for item in parameters]


def attach_parameter_checks(steps: list[WorkflowStep], parameters: list[TaskParameter]) -> list[WorkflowStep]:
    names = [item.name for item in parameters if item.required is not False]
    if not names or not steps:
        return steps
    extract = next((step for step in reversed(steps) if step.type in {"extract", "extract_page_content"}), None)
    if extract is None:
        return steps
    extract.expected_state = ExpectedState(parameter_values=["{" + name + "}" for name in names])
    return steps


async def _read_page_state(page) -> dict[str, Any] | None:
    try:
        url = page.url or ""
        if url.startswith(("chrome://", "chrome-extension://", "about:")):
            return None
        title = await page.title()
        text = await page.locator("body").inner_text()
        headings = await page.locator("h1, h2, [role=heading]").all_inner_texts()
        selected = await page.locator("[aria-selected=true], [aria-current=page], option:checked").all_inner_texts()
        return {"url": url, "title": title, "text": text, "headings": headings, "selected": selected}
    except Exception:
        return None


async def snapshot_page_state(recorder) -> dict[str, Any]:
    """Prefer the page the demonstration just navigated to, not a leftover long tab."""
    empty = {"url": "", "title": "", "text": "", "headings": [], "selected": []}
    if recorder is None:
        return empty
    try:
        pages = [page for page in list(recorder.context.pages) if not page.is_closed()]
    except Exception:
        return empty
    recent_urls: list[str] = []
    for event in reversed(getattr(recorder, "events", None) or []):
        url = getattr(event, "url", None)
        typ = getattr(event, "type", None)
        if typ in {"navigation", "navigate"} and url:
            recent_urls.append(url)
    for target in recent_urls:
        for page in reversed(pages):
            current = (page.url or "").rstrip("/")
            if current == target.rstrip("/") or current.endswith(target.rstrip("/")):
                state = await _read_page_state(page)
                if state:
                    return state
    for page in reversed(pages):
        state = await _read_page_state(page)
        if state:
            return state
    return empty


async def _open_demo_tab(recorder, start_url: str | None) -> None:
    """Open a fresh tab for the demonstration. Does not close the user's existing tabs."""
    if recorder is None:
        return
    try:
        page = await recorder.context.new_page()
        if start_url:
            await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
        else:
            await page.goto("about:blank")
    except Exception:
        return


def _required_values(parameters: list[TaskParameter], variables: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in parameters:
        if item.required is False:
            continue
        value = variables.get(item.name) or item.example
        if value:
            values[item.name] = str(value)
    return values


async def _run_browser_use_task(
    *,
    task: str,
    cdp_url: str,
    llm: LLMConfig,
    start_url: str | None = None,
    variables: dict[str, Any] | None = None,
    max_steps: int = 25,
    timeout: float = 300,
    parameters: list[TaskParameter] | None = None,
    prompt_override: str | None = None,
) -> Any:
    from browser_use import Agent

    chat = build_chat_openai(llm)
    browser = build_cdp_browser(cdp_url)
    prompt = prompt_override or demonstration_task(task, variables, start_url, parameters)
    agent = Agent(task=prompt, llm=chat, browser=browser, judge=False, web_search=False)
    return await agent.run(max_steps=max_steps, timeout=timeout)


async def create_workflow_from_task(
    store: FileStore,
    settings: Settings,
    *,
    task: str,
    cdp_url: str,
    llm: LLMConfig | None = None,
    variables: dict[str, Any] | None = None,
    start_url: str | None = None,
    name: str | None = None,
    write_back: bool = True,
    replay: bool = True,
    job_id: str | None = None,
    agent_runner: AgentRunner | None = None,
    max_steps: int = 25,
    timeout: float = 300,
    confirm_variables: bool | None = None,
    confirmation_broker=None,
    event_hub=None,
    on_status=None,
    replay_variables: dict[str, Any] | None = None,
) -> FromTaskResult:
    """Demonstrate the task with browser-use, save a workflow, and optionally replay it."""

    job = FromTaskResult(
        id=job_id or uuid.uuid4().hex,
        status="running",
        task=task,
        name=name or (task.strip().split("\n")[0][:80] or "from_task"),
        variables=dict(variables or {}),
    )
    recorder = None
    started = time.perf_counter()
    extract_usage: dict[str, Any] = {"tokens_in": 0, "tokens_out": 0, "used": False}
    try:
        if llm is not None:
            parameters, extract_usage = await extract_parameters_with_llm(task, llm=llm, provided=variables)
        else:
            parameters = parameters_from_placeholders(task, variables)
        resolved_vars = variables_from_parameters(parameters, variables)
        job.parameters = _parameters_payload(parameters)
        job.variables = resolved_vars or dict(variables or {})
        if on_status:
            on_status(job)

        confirm_pre = bool(confirm_variables) and confirmation_broker is not None
        if confirm_pre:
            job.status = "needs_confirmation"
            job.confirmation = {
                "event": "needs_confirmation",
                "kind": "parameters",
                "job_id": job.id,
                "task": task,
                "parameters": job.parameters,
                "placeholders": placeholders_in_task(task),
                "variables": job.variables,
                "message": "Confirm, edit, or add the inputs this workflow should use. The demonstration will treat these as hard constraints.",
            }
            if on_status:
                on_status(job)
            if event_hub is not None:
                await event_hub.publish(job.confirmation)
            timeout_s = float(getattr(settings, "hitl_timeout_s", 300) or 300)
            decision = await confirmation_broker.wait(job.id, job.confirmation, timeout_s=timeout_s)
            if decision.action != "confirm":
                job.status = "cancelled" if decision.action == "cancel" else "failed"
                job.error = decision.reason or decision.action
                job.creation.latency_ms = round((time.perf_counter() - started) * 1000, 1)
                if on_status:
                    on_status(job)
                return job
            chosen = dict(decision.variables or {})
            extra_params = list(getattr(decision, "parameters", None) or [])
            if extra_params:
                from browser_platform.authoring.parameters import normalize_parameter

                parsed: list[TaskParameter] = []
                used: set[str] = set()
                for raw in extra_params:
                    item = normalize_parameter(raw, used=used)
                    if item:
                        if item.name in chosen and not item.example:
                            item.example = str(chosen[item.name])
                        used.add(item.name)
                        parsed.append(item)
                parameters = merge_parameters([], parsed, chosen)
            elif chosen:
                parameters = merge_parameters(parameters, [], chosen)
            resolved_vars = variables_from_parameters(parameters, {**resolved_vars, **chosen})
            job.parameters = _parameters_payload(parameters)
            job.variables = resolved_vars
            job.status = "running"
            job.confirmation = None
            if on_status:
                on_status(job)

        variables = resolved_vars or dict(variables or {})

        recorder = await attach_recorder(cdp_url)
        await _open_demo_tab(recorder, start_url)
        history = None
        stop_poll = asyncio.Event()
        poller = asyncio.create_task(poll_field_changes(recorder, stop_poll))
        try:
            if agent_runner is not None:
                history = await agent_runner(
                    task=task,
                    cdp_url=cdp_url,
                    llm=llm,
                    start_url=start_url,
                    variables=variables or {},
                    parameters=parameters,
                    session=recorder,
                )
            else:
                if not llm:
                    raise RuntimeError("llm config is required to run browser-use")
                if not browser_use_available():
                    raise RuntimeError("browser-use is not installed")
                history = await _run_browser_use_task(
                    task=task,
                    cdp_url=cdp_url,
                    llm=llm,
                    start_url=start_url,
                    variables=variables,
                    max_steps=max_steps,
                    timeout=timeout,
                    parameters=parameters,
                )
                state = await snapshot_page_state(recorder)
                required = _required_values(parameters, variables)
                missing = missing_parameters(
                    url=state["url"],
                    title=state["title"],
                    headings=state["headings"],
                    text=state["text"],
                    selected=state["selected"],
                    values=required,
                )
                if missing:
                    retry_prompt = retry_constraint_prompt(
                        demonstration_task(task, variables, start_url, parameters),
                        missing,
                        state["url"],
                    )
                    history = await _run_browser_use_task(
                        task=task,
                        cdp_url=cdp_url,
                        llm=llm,
                        start_url=start_url,
                        variables=variables,
                        max_steps=max_steps,
                        timeout=timeout,
                        parameters=parameters,
                        prompt_override=retry_prompt,
                    )
                    state = await snapshot_page_state(recorder)
                    missing = missing_parameters(
                        url=state["url"],
                        title=state["title"],
                        headings=state["headings"],
                        text=state["text"],
                        selected=state["selected"],
                        values=required,
                    )
                if missing:
                    tokens_in = tokens_out = 0
                    cost = None
                    if history is not None:
                        tokens_in, tokens_out, cost = usage_from_history(history)
                    tokens_in += int(extract_usage.get("tokens_in") or 0)
                    tokens_out += int(extract_usage.get("tokens_out") or 0)
                    job.creation = FromTaskUsage(
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        cost_usd=estimate_cost_usd(tokens_in, tokens_out, cost or 0),
                        latency_ms=round((time.perf_counter() - started) * 1000, 1),
                    )
                    job.status = "failed"
                    job.error = (
                        "The demonstration finished without applying the required filter(s): "
                        + ", ".join(missing)
                        + f". The page was {state['url'] or 'unknown'}. "
                        "Select the matching option or entity page so the URL or heading reflects each value."
                    )
                    if on_status:
                        on_status(job)
                    return job
        finally:
            stop_poll.set()
            try:
                await asyncio.wait_for(poller, timeout=2)
            except Exception:
                poller.cancel()
        events = await collect_events(recorder)
        events, schema, resolved, suggestions = generalize_events(events, task=task, variables=variables)
        # Keep pre-extracted parameters in the schema even if a step used a different spelling.
        param_schema = schema_from_parameters(parameters, resolved)
        seen = {item.name for item in param_schema}
        for item in schema:
            if item.name not in seen:
                param_schema.append(item)
                seen.add(item.name)
        schema = param_schema
        if confirm_variables is None:
            confirm_variables = bool(suggestions) and not (variables or {})
        if suggestions and confirm_variables and confirmation_broker is not None:
            job.suggestions = suggestions
            job.variables = resolved or job.variables
            job.status = "needs_confirmation"
            job.confirmation = {
                "event": "needs_confirmation",
                "kind": "variables",
                "job_id": job.id,
                "task": task,
                "suggestions": suggestions,
                "placeholders": placeholders_in_task(task),
                "variables": resolved,
            }
            if on_status:
                on_status(job)
            if event_hub is not None:
                await event_hub.publish(job.confirmation)
            timeout_s = float(getattr(settings, "hitl_timeout_s", 300) or 300)
            decision = await confirmation_broker.wait(job.id, job.confirmation, timeout_s=timeout_s)
            if decision.action != "confirm":
                job.status = "cancelled" if decision.action == "cancel" else "failed"
                job.error = decision.reason or decision.action
                job.creation.latency_ms = round((time.perf_counter() - started) * 1000, 1)
                if on_status:
                    on_status(job)
                return job
            chosen = dict(decision.variables or {})
            if decision.text and "=" in decision.text and not chosen:
                for part in decision.text.split(","):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        chosen[k.strip()] = v.strip()
            events, schema, resolved = apply_variable_promotions(
                events, suggestions, chosen, resolved, accept_suggestions=not chosen
            )
            # Always keep explicit placeholders in the schema.
            for name in placeholders_in_task(task):
                if name not in {inp.name for inp in schema}:
                    schema.append(WorkflowInput(name=name, type="string", required=True, default=resolved.get(name), label=pretty_label(name)))
        steps = events_to_steps(events, extract_goal=_extract_goal(task))
        steps_before = len(steps)
        steps, compact_stats = compact_steps(steps)
        llm_prune = {"used": False, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
        if llm is not None:
            steps, llm_prune = await maybe_llm_prune(steps, task=task, llm=llm)
            if llm_prune.get("used"):
                compact_stats = {**compact_stats, "method": "deterministic+llm", "steps_after": len(steps), "llm": llm_prune}
            else:
                compact_stats = {**compact_stats, "llm": llm_prune}
        steps = attach_parameter_checks(steps, parameters)
        steps_after = len(steps)
        definition = WorkflowDefinition(
            name=job.name,
            description=task,
            version="1",
            input_schema=schema,
            steps=steps,
            workflow_analysis=f"Created from a browser-use demonstration of: {task}",
            cache_results=False,
        )
        tokens_in = tokens_out = 0
        cost = None
        if history is not None:
            tokens_in, tokens_out, cost = usage_from_history(history)
            if not job.variables and resolved:
                job.variables = resolved
        prune_in = int((llm_prune or {}).get("tokens_in") or 0)
        prune_out = int((llm_prune or {}).get("tokens_out") or 0)
        prune_cost = (llm_prune or {}).get("cost_usd") or 0.0
        extract_in = int(extract_usage.get("tokens_in") or 0)
        extract_out = int(extract_usage.get("tokens_out") or 0)
        tokens_in += prune_in + extract_in
        tokens_out += prune_out + extract_out
        cost = estimate_cost_usd(tokens_in, tokens_out, (cost or 0) + float(prune_cost or 0))
        job.creation = FromTaskUsage(
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            steps=len([s for s in steps if s.type != "extract"]),
            steps_before=steps_before,
            steps_after=steps_after,
            compaction=compact_stats,
        )
        job.suggestions = suggestions
        job.variables = resolved or job.variables
        if on_status:
            on_status(job)
        try:
            from browser_platform.authoring.input_validate import validate_inputs
            job.input_validation = await validate_inputs(definition, task=task)
            usage = (job.input_validation or {}).get("usage") or {}
            if usage:
                job.creation.tokens_in = int(job.creation.tokens_in or 0) + int(usage.get("tokens_in") or 0)
                job.creation.tokens_out = int(job.creation.tokens_out or 0) + int(usage.get("tokens_out") or 0)
        except Exception:
            job.input_validation = {"findings": [], "blocks_publish": False, "error": "input validation skipped"}
        stored = store.create_workflow(definition)
        job.workflow_id = stored.id
        job.definition = stored.definition

        if replay:
            # Drop the demonstration CDP session first, then replay in a fresh tab
            # opened by WorkflowRunner.connect (never the already-filled page).
            if recorder is not None:
                await detach_recorder(recorder)
                recorder = None
            runner = WorkflowRunner(store, settings, use_browser_use=False)
            replay_started = time.perf_counter()
            replay_vars = dict(replay_variables) if replay_variables is not None else dict(job.variables or {})
            run = await runner.run(
                stored,
                cdp_url=cdp_url,
                variables=replay_vars,
                llm=None,
                write_back_repairs=write_back,
            )
            job.replay = {
                "run_id": run.id,
                "status": run.status,
                "error": run.error,
                "variables": replay_vars,
                "latency_ms": round((time.perf_counter() - replay_started) * 1000, 1),
                "tokens_in": sum(int(s.tokens_in or 0) for s in run.steps),
                "tokens_out": sum(int(s.tokens_out or 0) for s in run.steps),
                "steps": [
                    {"type": s.step_type, "tier": s.tier, "success": s.success, "latency_ms": s.latency_ms}
                    for s in run.steps
                ],
                "outputs": run.outputs,
            }
            job.status = "succeeded" if run.status == "succeeded" else "failed"
            if run.status != "succeeded":
                job.error = run.error or "deterministic replay failed"
        else:
            job.status = "succeeded"
        if on_status:
            on_status(job)
        return job
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)
        job.creation.latency_ms = round((time.perf_counter() - started) * 1000, 1)
        if on_status:
            on_status(job)
        return job
    finally:
        if recorder is not None:
            await detach_recorder(recorder)

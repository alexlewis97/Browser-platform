"""Run a browser-use demonstration, save a workflow-use document, and replay it."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from browser_platform.authoring.compact import compact_steps, estimate_cost_usd, maybe_llm_prune, usage_from_history
from browser_platform.authoring.elements import resolve_fill_element, strategies_for_element
from browser_platform.authoring.generalize import apply_variables_to_steps, generalize_events, placeholders_in_task
from browser_platform.authoring.parameters import (
    TaskParameter,
    constraint_prompt,
    extract_parameters_with_llm,
    is_junk_suggestion,
    maybe_bind_known_urls,
    merge_parameters,
    normalize_variable_map,
    parameters_from_placeholders,
    retry_constraint_prompt,
    schema_from_parameters,
    seed_parameters_from_task,
    variables_from_parameters,
)
from browser_platform.executor.verify import classify_parameter, missing_parameters
from browser_platform.models.workflow import ExpectedState
from browser_platform.authoring.recorder import (
    RecordedEvent,
    attach_recorder,
    collect_events,
    harvest_field_snapshots,
    detach_recorder,
    poll_field_changes,
)
from browser_platform.config import Settings
from browser_platform.models.llm import LLMConfig, resolve_llm
from browser_platform.workflow_inputs import pretty_label
from browser_platform.interpolate import is_navigable_url
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
    explore: dict[str, Any] | None = None
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
        elif ev.type in {"input", "select_change"}:
            element = resolve_fill_element(ev.element) or ev.element or {}
            typ = str((element or {}).get("type") or "").lower()
            choice = ev.type == "select_change" or typ in {"radio", "checkbox"}
            selectors = ev.selectors or strategies_for_element(element, for_fill=not choice, include_text=True)
            if choice:
                css = element.get("css")
                for strategy in selectors or []:
                    raw = strategy.value if hasattr(strategy, "value") else (strategy.get("value") if isinstance(strategy, dict) else None)
                    typ_s = strategy.type if hasattr(strategy, "type") else (strategy.get("type") if isinstance(strategy, dict) else None)
                    if typ_s == "css" and raw and ("label[" in str(raw) or "name=" in str(raw)):
                        css = raw
                        break
                steps.append(
                    WorkflowStep(
                        type="select_change",
                        description="Choose option",
                        value=ev.value,
                        selectedText=ev.value,
                        target_text=ev.value or ev.text,
                        cssSelector=css,
                        xpath=element.get("xpath"),
                        selectorStrategies=selectors or None,
                    )
                )
            else:
                steps.append(
                    WorkflowStep(
                        type="input",
                        description="Fill field",
                        value=ev.value,
                        target_text=element.get("placeholder") or ev.text,
                        cssSelector=element.get("css"),
                        xpath=element.get("xpath"),
                        selectorStrategies=selectors or None,
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
    from browser_platform.executor.verify import classify_parameter

    if not parameters or not steps:
        return steps
    extract = next((step for step in reversed(steps) if step.type in {"extract", "extract_page_content"}), None)
    if extract is None:
        return steps
    filters: list[str] = []
    echos: list[str] = []
    for item in parameters:
        if item.required is False:
            continue
        kind = classify_parameter(item.name, steps=steps)
        if kind == "form":
            echos.append(item.name)
        elif kind == "filter":
            filters.append(item.name)
        # locators are enforced by the click/select step that binds them, not by
        # the confirmation page (checkout receipts often omit the product name).
    extract.expected_state = ExpectedState(
        parameter_values=["{" + name + "}" for name in filters] or None,
        echo_values=["{" + name + "}" for name in echos] or None,
    )
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
        if start_url and is_navigable_url(start_url):
            await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
        else:
            await page.goto("about:blank")
    except Exception:
        return


def _required_values(parameters: list[TaskParameter], variables: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    mapped = normalize_variable_map(variables)
    for item in parameters:
        if item.required is False:
            continue
        value = mapped.get(item.name) or (variables or {}).get(item.name) or item.example
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
    max_steps: int = 50,
    timeout: float = 300,
    parameters: list[TaskParameter] | None = None,
    prompt_override: str | None = None,
) -> Any:
    from browser_use import Agent

    chat = build_chat_openai(llm)
    browser = build_cdp_browser(cdp_url)
    prompt = prompt_override or demonstration_task(task, variables, start_url, parameters)
    guide = (
        " Navigate only to http(s) URLs, never to a CSS selector or id=...]. "
        "If a CDP or DOM call fails, retry with fill or click. "
        "Finish the terminal goal (submit, checkout confirmation, or echoed result)."
    )
    agent = Agent(task=prompt, llm=chat, browser=browser, judge=False, web_search=False)
    try:
        return await agent.run(max_steps=max_steps, timeout=timeout)
    except Exception as exc:
        message = str(exc).lower()
        transient = any(
            token in message
            for token in ("ipv4", "ipv6", "does not appear to be", "cdp", "websocket", "target closed", "node is not", "wasn't found")
        )
        if not transient:
            raise
        agent = Agent(task=prompt + guide, llm=chat, browser=build_cdp_browser(cdp_url), judge=False, web_search=False)
        return await agent.run(max_steps=max_steps, timeout=timeout)



_SUBMIT_NAMES = ("finish", "continue", "submit", "place-order", "place_order")


async def maybe_complete_visible_form(recorder, values: dict[str, str]) -> bool:
    """If the demo stalled on a filled checkout/form, click Continue/Finish/Submit.

    Playwright fills and clicks go through the page so the in-page recorder can
    capture them as real events. Returns True when the URL or visible text changed.
    """
    if not values:
        return False
    try:
        pages = [page for page in list(recorder.context.pages) if not page.is_closed()]
    except Exception:
        return False
    if not pages:
        return False
    page = pages[-1]
    advanced = False
    try:
        start_url = page.url or ""
        start_text = ""
        try:
            start_text = (await page.locator("body").inner_text())[:400]
        except Exception:
            pass
        # Fill empty visible text fields from provided values when the control
        # name/id/placeholder mentions the parameter.
        for name, value in values.items():
            if not value:
                continue
            tokens = {name.lower(), name.lower().replace("_", "-"), name.lower().replace("_", "")}
            if name.lower() in {"zip", "postal_code", "postal"}:
                tokens.update({"zip", "postal", "postalcode", "postcode"})
            if name.lower() in {"first_name", "firstname"}:
                tokens.update({"first-name", "firstname", "first_name"})
            if name.lower() in {"last_name", "lastname"}:
                tokens.update({"last-name", "lastname", "last_name"})
            locator = page.locator("input:visible, textarea:visible, select:visible")
            count = 0
            try:
                count = await locator.count()
            except Exception:
                count = 0
            for i in range(min(count, 20)):
                loc = locator.nth(i)
                typ = (await loc.get_attribute("type") or "").lower()
                if typ in {"hidden", "submit", "button", "checkbox", "radio", "file"}:
                    continue
                ident = " ".join(
                    filter(
                        None,
                        [
                            await loc.get_attribute("id"),
                            await loc.get_attribute("name"),
                            await loc.get_attribute("placeholder"),
                            await loc.get_attribute("data-test"),
                            await loc.get_attribute("aria-label"),
                        ],
                    )
                ).lower().replace(" ", "")
                if not any(tok.replace("_", "") in ident or tok in ident for tok in tokens):
                    continue
                current = ""
                try:
                    current = await loc.input_value()
                except Exception:
                    pass
                if current and current.strip() == str(value).strip():
                    break
                try:
                    await loc.fill(str(value), timeout=3000)
                    advanced = True
                    break
                except Exception:
                    continue
        # Click a visible continue/finish/submit control, up to two times
        # (checkout information -> overview -> complete).
        for _ in range(2):
            clicked = False
            css_candidates = [
                "#finish",
                "#continue",
                "[data-test='finish']",
                "[data-test='continue']",
                "[data-test='submit']",
                "input#finish",
                "input#continue",
                "input[type=submit]",
                "button[type=submit]",
            ]
            for sel in css_candidates:
                loc = page.locator(sel)
                try:
                    if await loc.count() and await loc.first.is_visible():
                        await loc.first.click(timeout=3000)
                        clicked = True
                        advanced = True
                        await page.wait_for_timeout(800)
                        break
                except Exception:
                    continue
            if not clicked:
                for label in ("Finish", "Continue", "Submit", "Place Order"):
                    loc = page.get_by_role("button", name=label)
                    try:
                        if await loc.count() and await loc.first.is_visible():
                            await loc.first.click(timeout=3000)
                            clicked = True
                            advanced = True
                            await page.wait_for_timeout(800)
                            break
                    except Exception:
                        continue
            if not clicked:
                break
        end_url = page.url or ""
        return advanced or end_url != start_url
    except Exception:
        return False


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
    max_steps: int = 50,
    timeout: float = 300,
    confirm_variables: bool | None = None,
    confirmation_broker=None,
    event_hub=None,
    on_status=None,
    replay_variables: dict[str, Any] | None = None,
    explore: bool = True,
) -> FromTaskResult:
    """Demonstrate the task with browser-use, save a workflow, and optionally replay it."""

    llm = resolve_llm(llm)
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
        parameters = seed_parameters_from_task(task, parameters)
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
            chosen = normalize_variable_map(decision.variables or {})
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
                demo_error = None
                try:
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
                except Exception as exc:
                    demo_error = f"{type(exc).__name__}: {exc}"
                    history = None
                state = await snapshot_page_state(recorder)
                try:
                    await harvest_field_snapshots(recorder)
                except Exception:
                    pass
                preview_events = list(getattr(recorder, "events", None) or [])
                try:
                    # Peek without draining the in-page buffer used by collect_events later.
                    for page in list(getattr(recorder, "context").pages):
                        raw = await page.evaluate("() => (window.__bpRecorder && window.__bpRecorder.snapshot) ? window.__bpRecorder.snapshot() : []")
                        preview_events.extend(raw or [])
                except Exception:
                    pass
                required = _required_values(parameters, variables)
                missing = missing_parameters(
                    url=state["url"],
                    title=state["title"],
                    headings=state["headings"],
                    text=state["text"],
                    selected=state["selected"],
                    values=required,
                    events=preview_events,
                    task=task,
                )
                if missing and not demo_error:
                    retry_prompt = retry_constraint_prompt(
                        demonstration_task(task, variables, start_url, parameters),
                        missing,
                        state["url"],
                    )
                    try:
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
                    except Exception as exc:
                        demo_error = f"{type(exc).__name__}: {exc}"
                    state = await snapshot_page_state(recorder)
                    try:
                        await harvest_field_snapshots(recorder)
                    except Exception:
                        pass
                    preview_events = list(getattr(recorder, "events", None) or [])
                    try:
                        for page in list(getattr(recorder, "context").pages):
                            raw = await page.evaluate("() => (window.__bpRecorder && window.__bpRecorder.snapshot) ? window.__bpRecorder.snapshot() : []")
                            preview_events.extend(raw or [])
                    except Exception:
                        pass
                    missing = missing_parameters(
                        url=state["url"],
                        title=state["title"],
                        headings=state["headings"],
                        text=state["text"],
                        selected=state["selected"],
                        values=required,
                        events=preview_events,
                        task=task,
                    )
                if missing:
                    try:
                        await maybe_complete_visible_form(recorder, required)
                        await harvest_field_snapshots(recorder)
                    except Exception:
                        pass
                    state = await snapshot_page_state(recorder)
                    preview_events = list(getattr(recorder, "events", None) or [])
                    try:
                        for page in list(getattr(recorder, "context").pages):
                            raw = await page.evaluate("() => (window.__bpRecorder && window.__bpRecorder.snapshot) ? window.__bpRecorder.snapshot() : []")
                            preview_events.extend(raw or [])
                    except Exception:
                        pass
                    missing = missing_parameters(
                        url=state["url"],
                        title=state["title"],
                        headings=state["headings"],
                        text=state["text"],
                        selected=state["selected"],
                        values=required,
                        events=preview_events,
                        task=task,
                    )
                if missing or demo_error:
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
                    parts = []
                    if demo_error:
                        parts.append(demo_error)
                    if missing:
                        parts.append(
                            "The demonstration did not finish the task: "
                            + "; ".join(missing)
                            + f" The page was {state['url'] or 'unknown'}."
                        )
                    job.error = " ".join(parts)
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
        suggestions = [item for item in (suggestions or []) if not is_junk_suggestion(item)]
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
            chosen = normalize_variable_map(decision.variables or {})
            if decision.text and "=" in decision.text and not chosen:
                for part in decision.text.split(","):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        chosen[k.strip()] = v.strip()
                chosen = normalize_variable_map(chosen)
            events, schema, resolved = apply_variable_promotions(
                events, suggestions, chosen, resolved, accept_suggestions=not chosen
            )
            resolved = normalize_variable_map(resolved)
            # Re-base the schema on the confirmed parameters so names stay snake_case.
            param_schema = schema_from_parameters(parameters, resolved)
            seen = {item.name for item in param_schema}
            for item in schema:
                raw = {"name": item.name, "label": item.label, "example": item.default, "required": item.required}
                from browser_platform.authoring.parameters import normalize_parameter
                parsed = normalize_parameter(raw)
                if parsed and parsed.name not in seen:
                    item.name = parsed.name
                    param_schema.append(item)
                    seen.add(parsed.name)
            schema = param_schema
            # Always keep explicit placeholders in the schema.
            for name in placeholders_in_task(task):
                if name not in {inp.name for inp in schema}:
                    schema.append(WorkflowInput(name=name, type="string", required=True, default=resolved.get(name), label=pretty_label(name)))
        steps = events_to_steps(events, extract_goal=_extract_goal(task))
        steps_before = len(steps)
        steps, compact_stats = compact_steps(steps)
        steps = apply_variables_to_steps(steps, {str(v): k for k, v in (resolved or {}).items() if v is not None})
        form_names = [item.name for item in (parameters or []) if classify_parameter(item.name, steps=steps) == "form"]
        bound_values = " ".join(str(step.value or "") for step in steps if step.type in {"input", "select_change"})
        missing_fills = [name for name in form_names if "{" + name + "}" not in bound_values]
        if missing_fills:
            tokens_in, tokens_out, cost = (0, 0, None)
            if history is not None:
                tokens_in, tokens_out, cost = usage_from_history(history)
            job.creation = FromTaskUsage(
                tokens_in=int(tokens_in or 0) + int(extract_usage.get("tokens_in") or 0),
                tokens_out=int(tokens_out or 0) + int(extract_usage.get("tokens_out") or 0),
                cost_usd=estimate_cost_usd(int(tokens_in or 0), int(tokens_out or 0), cost or 0),
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                steps=len(steps),
                steps_before=steps_before,
                steps_after=len(steps),
                compaction=compact_stats,
            )
            job.status = "failed"
            job.error = (
                "The recording did not capture form fills for "
                + ", ".join(missing_fills)
                + ". Values were probably set without input events and then lost on navigation."
            )
            if on_status:
                on_status(job)
            return job
        llm_prune = {"used": False, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
        if llm is not None:
            steps, llm_prune = await maybe_llm_prune(steps, task=task, llm=llm)
            if llm_prune.get("used"):
                compact_stats = {**compact_stats, "method": "deterministic+llm", "steps_after": len(steps), "llm": llm_prune}
            else:
                compact_stats = {**compact_stats, "llm": llm_prune}
        steps = maybe_bind_known_urls(steps, parameters, task=task)
        steps = attach_parameter_checks(steps, parameters)
        steps_after = len(steps)
        resolved = normalize_variable_map(resolved)
        from browser_platform.authoring.parameters import normalize_parameter
        merged_schema = schema_from_parameters(parameters, resolved)
        seen = {item.name for item in merged_schema}
        for item in schema or []:
            parsed = normalize_parameter({"name": item.name, "label": item.label, "example": item.default, "required": item.required})
            if not parsed or parsed.name in seen:
                continue
            item.name = parsed.name
            if not item.default:
                item.default = resolved.get(item.name)
            seen.add(item.name)
            merged_schema.append(item)
        schema = merged_schema or schema
        definition = WorkflowDefinition(
            name=job.name,
            description=task,
            version="1",
            input_schema=schema,
            steps=steps,
            workflow_analysis=f"Created from a browser-use demonstration of: {task}",
            cache_results=False,
        )
        try:
            from browser_platform.authoring.explore import explore_definition

            page_state = None
            if recorder is not None:
                try:
                    page_state = await snapshot_page_state(recorder)
                except Exception:
                    page_state = None
            explore_result = await explore_definition(
                definition,
                task=task,
                variables=resolved,
                llm=llm if explore else None,
                page_state=page_state,
                enabled=bool(explore),
            )
            job.explore = {k: v for k, v in explore_result.items() if k != "definition"}
            if explore_result.get("definition"):
                definition = WorkflowDefinition.model_validate(explore_result["definition"])
        except Exception as exc:
            job.explore = {"enabled": bool(explore), "error": type(exc).__name__, "proposals": [], "verified": [], "dropped": []}
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
        explore_usage = ((job.explore or {}).get("usage") or {}) if isinstance(job.explore, dict) else {}
        explore_in = int(explore_usage.get("tokens_in") or 0)
        explore_out = int(explore_usage.get("tokens_out") or 0)
        tokens_in += prune_in + extract_in + explore_in
        tokens_out += prune_out + extract_out + explore_out
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
                llm=llm,
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

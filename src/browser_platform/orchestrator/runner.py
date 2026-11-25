"""Run a workflow with the platform tier order.

  1. Pomerado minted tool (if the workflow or step names an artifact)
  2. workflow-use replay: action cache, selector chain, heuristics, then fuzzy
     target resolution (name order, typo, Hebrew/RTL). If the match is not
     confident, pause for human confirmation or fall through to the LLM tier
     according to hitl_mode.
  3. browser-use / OpenRouter LLM repair of the failed step, then write-back

AI is used only when deterministic (including confident fuzzy) resolution fails.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Awaitable

from playwright.async_api import Page

from browser_platform.a11y import compress_ax_tree
from browser_platform.cache.action_cache import ActionCache
from browser_platform.cache.result_cache import ResultCache
from browser_platform.config import Settings
from browser_platform.executor import browser as browser_mod
from browser_platform.executor.browser import CdpSession
from browser_platform.executor.deterministic import StepFailed, run_step as run_deterministic_step
from browser_platform.executor.output_validate import collect_sample, validate_output as validate_run_output
from browser_platform.executor.selectors import SelectorAttempt, build_selector_plan
from browser_platform.fuzzy.candidates import Candidate, apply_candidate, collect_candidates
from browser_platform.fuzzy.config import find_alias, fuzzy_applies, make_alias, merge_fuzzy, merge_hitl
from browser_platform.fuzzy.hitl import ConfirmationBroker, ConfirmationDecision, EventHub
from browser_platform.fuzzy.score import query_for_step, score_candidates
from browser_platform.models.llm import LLMConfig
from browser_platform.models.run import RunRecord, StepTelemetry
from browser_platform.models.workflow import SelectorStrategy, StoredWorkflow, WorkflowDefinition, WorkflowStep
from browser_platform.pomerado.adapter import PomeradoAdapter, McpError
from browser_platform.repair.browser_use_agent import run_browser_use_repair
from browser_platform.repair.llm import OpenRouterClient, RepairClient, browser_use_available, repair_messages
from browser_platform.repair.writeback import write_back
from browser_platform.store.file_store import FileStore

ConnectFn = Callable[[str], Awaitable[CdpSession]]
DisconnectFn = Callable[[CdpSession], Awaitable[None]]



async def _page_meta(session: CdpSession | None) -> dict[str, Any]:
    if session is None:
        return {}
    page = getattr(session, "page", None)
    if page is None:
        return {}
    meta: dict[str, Any] = {}
    try:
        meta["url"] = page.url
    except Exception:
        pass
    try:
        meta["title"] = await page.title()
    except Exception:
        pass
    try:
        meta["text"] = await page.evaluate(
            "() => (document.body && document.body.innerText ? document.body.innerText.slice(0, 2000) : '')"
        )
    except Exception:
        pass
    return meta


class WorkflowRunner:
    def __init__(
        self,
        store: FileStore,
        settings: Settings,
        *,
        action_cache: ActionCache | None = None,
        result_cache: ResultCache | None = None,
        repair_client: RepairClient | None = None,
        pomerado: PomeradoAdapter | None = None,
        connect: ConnectFn = browser_mod.connect,
        disconnect: DisconnectFn = browser_mod.disconnect,
        browser_use_repair: Callable[..., Any] | None = None,
        use_browser_use: bool | None = None,
        event_hub: EventHub | None = None,
        confirmation_broker: ConfirmationBroker | None = None,
    ):
        self.store = store
        self.settings = settings
        self.action_cache = action_cache or ActionCache()
        self.result_cache = result_cache or ResultCache(ttl_seconds=settings.result_cache_ttl_seconds)
        self.repair_client = repair_client or OpenRouterClient(settings.openrouter_base_url)
        self.pomerado = pomerado
        self._connect = connect
        self._disconnect = disconnect
        # None = auto (real Playwright page + browser-use installed). A callable overrides the agent.
        self.browser_use_repair = browser_use_repair
        self.use_browser_use = use_browser_use
        self.event_hub = event_hub or EventHub()
        self.confirmation_broker = confirmation_broker

    async def run(
        self,
        stored: StoredWorkflow,
        *,
        cdp_url: str,
        variables: dict[str, Any] | None = None,
        llm: LLMConfig | None = None,
        write_back_repairs: bool = True,
        run_id: str | None = None,
        request_text: str | None = None,
        validate_output: bool = True,
        validation_attempt: int = 0,
    ) -> RunRecord:
        run = RunRecord(
            id=run_id or uuid.uuid4().hex,
            workflow_id=stored.id,
            workflow_name=stored.definition.name,
            workflow_version=stored.definition.version,
            status="running",
            cdp_url=cdp_url,
            model=llm.model if llm else None,
            do_write_back=write_back_repairs,
        )
        self.store.save_run(run)
        session: CdpSession | None = None
        definition = stored.definition
        outputs: dict[str, Any] = {}
        original_version = definition.version
        definition = definition.model_copy(deep=True)
        try:
            # Tier 1 (workflow-level): a minted Pomerado tool can satisfy the whole task.
            if definition.pomerado_artifact and self.pomerado:
                started = time.perf_counter()
                try:
                    pomerado_input = {**(definition.pomerado_input or {}), **(variables or {})}
                    result = await self.pomerado.run_artifact(
                        definition.pomerado_artifact,
                        input_values=pomerado_input,
                        tool=definition.pomerado_tool,
                        llm=llm,
                        url=pomerado_input.get("url"),
                        effect=pomerado_input.get("effect") or "read",
                        intent=pomerado_input.get("intent") or definition.description,
                    )
                    if _pomerado_ok(result):
                        latency = (time.perf_counter() - started) * 1000
                        run.steps.append(
                            StepTelemetry(
                                step_index=-1,
                                step_type="pomerado",
                                description="workflow-level minted tool",
                                tier="pomerado",
                                success=True,
                                latency_ms=latency,
                                output=result,
                            )
                        )
                        run.outputs = {"pomerado": result}
                        run.status = "succeeded"
                        run.finished_at = datetime.now(timezone.utc)
                        self.store.save_run(run)
                        return run
                except Exception as exc:
                    latency = (time.perf_counter() - started) * 1000
                    run.steps.append(
                        StepTelemetry(
                            step_index=-1,
                            step_type="pomerado",
                            description="workflow-level minted tool",
                            tier="pomerado",
                            success=False,
                            latency_ms=latency,
                            error=str(exc),
                        )
                    )
                    self.store.save_run(run)

            session = await self._connect(cdp_url)
            page = session.page
            for index, step in enumerate(definition.steps):
                tel, definition, step_output = await self._run_one_step(
                    page,
                    stored.id,
                    definition,
                    index,
                    step,
                    variables=variables or {},
                    llm=llm,
                    write_back_repairs=write_back_repairs,
                    cdp_url=cdp_url,
                    run=run,
                )
                run.steps.append(tel)
                self.store.save_run(run)
                if step.output and step_output is not None:
                    outputs[step.output] = step_output
                    variables = {**(variables or {}), step.output: step_output}
                if tel.success and step.type in {"extract", "extract_page_content"}:
                    outputs.setdefault("extract", step_output)
                if not tel.success:
                    if (tel.error or "").lower().startswith("cancelled"):
                        run.status = "cancelled"
                    else:
                        run.status = "failed"
                    run.error = tel.error
                    break
            else:
                run.status = "succeeded"
            run.outputs = outputs
            if write_back_repairs and definition.version != original_version:
                stored = self.store.update_workflow(stored.id, definition)
                run.workflow_version = stored.definition.version
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc)
        finally:
            page_meta: dict[str, Any] = {}
            if session is not None and run.status == "succeeded" and validate_output:
                page_meta = await _page_meta(session)
            if session is not None:
                await self._disconnect(session)
            if run.status == "succeeded" and validate_output:
                await self._attach_output_validation(
                    run,
                    stored,
                    variables=variables or {},
                    request_text=request_text,
                    page_meta=page_meta,
                    validation_attempt=validation_attempt,
                )
            run.finished_at = datetime.now(timezone.utc)
            self.store.save_run(run)
        if getattr(run, "retry_output_validation", False) and validation_attempt == 0:
            return await self.run(
                stored,
                cdp_url=cdp_url,
                variables=variables,
                llm=llm,
                write_back_repairs=write_back_repairs,
                request_text=request_text,
                validate_output=True,
                validation_attempt=1,
            )
        return run

    async def _attach_output_validation(
        self,
        run: RunRecord,
        stored: StoredWorkflow,
        *,
        variables: dict[str, Any],
        request_text: str | None,
        page_meta: dict[str, Any],
        validation_attempt: int = 0,
    ) -> None:
        from browser_platform.authoring.tools import is_action_ending

        sample = collect_sample(run.outputs, page_meta.get("text") or "")
        request = (request_text or stored.definition.description or stored.definition.name or "").strip()
        if variables:
            request = f"{request} Inputs: {variables}".strip()
        try:
            result = await validate_run_output(
                request=request,
                inputs=variables,
                outputs=run.outputs,
                sample=sample,
                url=str(page_meta.get("url") or "") ,
                title=str(page_meta.get("title") or ""),
                action_ending=is_action_ending(stored.definition),
            )
        except Exception as exc:
            result = {"verdict": "suspect", "reason": f"Output validation failed: {exc}", "usage": {"used": False}}
        run.validation = result
        if result.get("verdict") == "wrong":
            action = is_action_ending(stored.definition)
            # Never re-run clicks/submits. Surface the flag on action workflows;
            # only retry (once) and fail extract/read workflows.
            if action:
                return
            if validation_attempt == 0:
                run.retry_output_validation = True
            else:
                run.status = "failed"
                run.error = f"Output validation: {result.get('reason') or 'result did not match the request'}"



    async def _run_one_step(
        self,
        page: Page,
        workflow_id: str,
        definition: WorkflowDefinition,
        index: int,
        step: WorkflowStep,
        *,
        variables: dict[str, Any],
        llm: LLMConfig | None,
        write_back_repairs: bool,
        cdp_url: str | None = None,
        run: RunRecord | None = None,
    ) -> tuple[StepTelemetry, WorkflowDefinition, Any]:
        timeout_ms = self.settings.action_timeout_ms

        # --- Tier 1: Pomerado (step-level) ---
        if (step.pomerado_artifact or step.pomerado_tool) and self.pomerado:
            started = time.perf_counter()
            try:
                step_input = {**(step.pomerado_input or {}), **variables}
                result = await self.pomerado.run_artifact(
                    step.pomerado_artifact or definition.pomerado_artifact or "",
                    input_values=step_input,
                    tool=step.pomerado_tool,
                    llm=llm,
                    url=step_input.get("url"),
                    effect=step_input.get("effect") or "read",
                )
                if _pomerado_ok(result):
                    return (
                        _tel(index, step, "pomerado", True, started, output=result),
                        definition,
                        result,
                    )
                pomerado_error = f"pomerado tool returned unsuccessful result: {result!r}"
            except Exception as exc:
                pomerado_error = str(exc)
            # Fall through; record the miss only if later tiers also fail? We keep going silently
            # after a failed pomerado attempt, but include it if we want telemetry of the miss.
            _ = pomerado_error

        # --- Tier 2a: result cache for read-only extracts ---
        if (
            definition.cache_results
            and step.type in {"extract", "extract_page_content"}
            and page.url
        ):
            goal = step.extractionGoal or step.goal or ""
            cached_value = self.result_cache.get(workflow_id, index, page.url, goal, variables)
            if cached_value is not None:
                started = time.perf_counter()
                return (
                    _tel(index, step, "result_cache", True, started, output=cached_value),
                    definition,
                    cached_value,
                )

        # --- Tier 2: workflow-use deterministic replay (cache, selectors, heuristics) ---
        url_for_cache = page.url
        cached = self.action_cache.get(workflow_id, index, url_for_cache)
        plan = build_selector_plan(step, cached=cached)
        started = time.perf_counter()
        try:
            success = await run_deterministic_step(
                page,
                step,
                variables=variables,
                plan=plan,
                timeout_ms=timeout_ms,
            )
            tier = "workflow_use"
            if success.attempt:
                if success.attempt.kind == "cached":
                    tier = "action_cache"
                elif success.attempt.kind == "heuristic":
                    tier = "heuristic"
                else:
                    tier = "selector"
                self.action_cache.put(workflow_id, index, page.url, success.attempt.strategy)
            if step.type in {"extract", "extract_page_content"} and definition.cache_results:
                goal = step.extractionGoal or step.goal or ""
                self.result_cache.put(workflow_id, index, page.url, goal, success.output, variables)
            # Navigation / extract have no attempt — still workflow_use deterministic.
            if success.attempt is None and step.type in {
                "navigation",
                "go_back",
                "go_forward",
                "scroll",
                "extract",
                "extract_page_content",
            }:
                tier = "workflow_use"
            return (
                _tel(
                    index,
                    step,
                    tier,  # type: ignore[arg-type]
                    True,
                    started,
                    attempt=success.attempt,
                    output=success.output,
                ),
                definition,
                success.output,
            )
        except StepFailed as exc:
            det_error = exc.last_error or str(exc)

        # Still in workflow-use: resolve the step target with aliases + fuzzy match.
        pending_hitl: dict[str, Any] | None = None
        fuzzy_outcome = await self._workflow_use_fuzzy(
            page,
            workflow_id,
            definition,
            index,
            step,
            variables=variables,
            write_back_repairs=write_back_repairs,
            run=run,
            started=started,
            timeout_ms=timeout_ms,
        )
        if fuzzy_outcome is not None:
            kind, payload = fuzzy_outcome
            if kind == "done":
                return payload
            if kind == "needs_hitl":
                pending_hitl = payload

        if pending_hitl and pending_hitl["hitl"].mode == "always_on_uncertain":
            hitl_result = await self._await_hitl(
                page,
                workflow_id,
                definition,
                index,
                step,
                pending_hitl,
                write_back_repairs=write_back_repairs,
                run=run,
                started=started,
                timeout_ms=timeout_ms,
                det_error=det_error,
            )
            if hitl_result is not None:
                return hitl_result

        # --- LLM repair (browser-use, then OpenRouter selector repair) ---
        if not llm:
            if pending_hitl:
                hitl_result = await self._await_hitl(
                    page,
                    workflow_id,
                    definition,
                    index,
                    step,
                    pending_hitl,
                    write_back_repairs=write_back_repairs,
                    run=run,
                    started=started,
                    timeout_ms=timeout_ms,
                    det_error=det_error,
                )
                if hitl_result is not None:
                    return hitl_result
            return (
                _tel(index, step, "llm", False, started, error=f"{det_error} (no LLM config; repair skipped)"),
                definition,
                None,
            )

        repair_started = time.perf_counter()
        tokens_in = tokens_out = 0

        if self._should_use_browser_use(page) and cdp_url:
            try:
                fn = self.browser_use_repair or run_browser_use_repair
                repaired_bu = await fn(
                    cdp_url=cdp_url,
                    step=step,
                    error=det_error,
                    url=page.url,
                    llm=llm,
                )
                tokens_in += int(getattr(repaired_bu, "tokens_in", 0) or 0)
                tokens_out += int(getattr(repaired_bu, "tokens_out", 0) or 0)
                strategies = list(getattr(repaired_bu, "strategies", None) or [])
                value = getattr(repaired_bu, "value", None)
                agent_ok = bool(getattr(repaired_bu, "success", False))
                if value and step.type == "input":
                    step = step.model_copy(update={"value": value})
                output = getattr(repaired_bu, "text", None)
                attempt = None
                if strategies:
                    if value and step.type == "input":
                        pass
                    repair_plan = [SelectorAttempt(kind="selector", strategy=s) for s in strategies]
                    try:
                        success = await run_deterministic_step(
                            page,
                            step,
                            variables=variables,
                            plan=repair_plan,
                            timeout_ms=timeout_ms,
                        )
                        winner = success.attempt.strategy if success.attempt else strategies[0]
                        attempt = success.attempt
                        output = success.output
                        agent_ok = True
                        if write_back_repairs:
                            definition = write_back(
                                definition,
                                step_index=index,
                                strategy=winner,
                                reason=det_error,
                            )
                        self.action_cache.put(workflow_id, index, page.url, winner)
                    except StepFailed:
                        # Do not write back a selector that failed deterministic replay.
                        if not agent_ok:
                            raise
                        # Agent claimed success but selector did not replay — treat as success
                        # for this run only (no cache / write-back pollution).
                        return (
                            _tel(
                                index,
                                step,
                                "browser_use",
                                True,
                                repair_started,
                                output=output,
                                tokens_in=tokens_in,
                                tokens_out=tokens_out,
                            ),
                            definition,
                            output,
                        )
                if agent_ok:
                    return (
                        _tel(
                            index,
                            step,
                            "browser_use",
                            True,
                            repair_started,
                            attempt=attempt,
                            output=output,
                            tokens_in=tokens_in,
                            tokens_out=tokens_out,
                        ),
                        definition,
                        output,
                    )
            except Exception:
                # Fall through to the cheaper OpenRouter selector repair.
                pass

        try:
            snapshot = await page.accessibility.snapshot()
            ax = compress_ax_tree(snapshot if isinstance(snapshot, dict) else None)
            messages = repair_messages(step, error=det_error, ax_tree=ax, url=page.url)
            repaired = await self.repair_client.complete(messages=messages, llm=llm)
            tokens_in += repaired.tokens_in
            tokens_out += repaired.tokens_out
            if not repaired.strategies:
                raise StepFailed("LLM repair returned no selectorStrategies")
            if repaired.value and step.type == "input":
                step = step.model_copy(update={"value": repaired.value})
            repair_plan = [
                SelectorAttempt(kind="selector", strategy=s) for s in repaired.strategies
            ]
            success = await run_deterministic_step(
                page,
                step,
                variables=variables,
                plan=repair_plan,
                timeout_ms=timeout_ms,
            )
            winner = success.attempt.strategy if success.attempt else repaired.strategies[0]
            if write_back_repairs:
                definition = write_back(
                    definition,
                    step_index=index,
                    strategy=winner,
                    reason=det_error,
                )
            self.action_cache.put(workflow_id, index, page.url, winner)
            return (
                _tel(
                    index,
                    step,
                    "llm",
                    True,
                    repair_started,
                    attempt=success.attempt,
                    output=success.output,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                ),
                definition,
                success.output,
            )
        except Exception as exc:
            if pending_hitl and pending_hitl["hitl"].mode == "llm_then_human":
                hitl_result = await self._await_hitl(
                    page,
                    workflow_id,
                    definition,
                    index,
                    step,
                    pending_hitl,
                    write_back_repairs=write_back_repairs,
                    run=run,
                    started=started,
                    timeout_ms=timeout_ms,
                    det_error=det_error,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
                if hitl_result is not None:
                    return hitl_result
            return (
                _tel(
                    index,
                    step,
                    "llm",
                    False,
                    repair_started,
                    error=f"{det_error}; LLM repair failed: {exc}",
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                ),
                definition,
                None,
            )



    async def _workflow_use_fuzzy(
        self,
        page: Page,
        workflow_id: str,
        definition: WorkflowDefinition,
        index: int,
        step: WorkflowStep,
        *,
        variables: dict[str, Any],
        write_back_repairs: bool,
        run: RunRecord | None,
        started: float,
        timeout_ms: int,
    ):
        """Alias lookup + fuzzy target resolution. Still the workflow-use tier.

        Returns ("done", (tel, definition, output)), ("needs_hitl", context), or None
        to continue to the LLM tier.
        """
        fuzzy_cfg = merge_fuzzy(self.settings, definition, step)
        if not fuzzy_applies(step, fuzzy_cfg):
            return None
        query = query_for_step(step, variables)
        if not query:
            return None

        alias = find_alias(definition.aliases, index, query)
        if alias:
            candidate = Candidate(
                id=alias.chosen_id or "alias",
                text=alias.chosen_text,
                role=alias.role,
                selector=alias.cssSelector or alias.xpath,
                selector_type="xpath" if alias.xpath and not alias.cssSelector else "css",
            )
            try:
                await apply_candidate(page, step, candidate, timeout_ms=timeout_ms)
                definition = self._remember_target(
                    definition,
                    workflow_id,
                    index,
                    page,
                    step,
                    candidate,
                    query,
                    do_write_back=write_back_repairs,
                    source="workflow_use_alias",
                    persist_alias=False,
                )
                return (
                    "done",
                    (
                        _tel(
                            index,
                            step,
                            "workflow_use",
                            True,
                            started,
                            attempt=SelectorAttempt(
                                kind="cached",
                                strategy=SelectorStrategy(
                                    type=candidate.selector_type,
                                    value=candidate.selector or candidate.text,
                                ),
                            ),
                            output={"query": query, "matched": candidate.text, "reason": "alias"},
                            resolution="alias",
                        ),
                        definition,
                        {"query": query, "matched": candidate.text},
                    ),
                )
            except Exception:
                pass

        include_screenshot = bool(fuzzy_cfg.include_screenshot)
        try:
            candidates = await collect_candidates(page, step, include_screenshot=include_screenshot)
        except Exception:
            candidates = []
        decision = score_candidates(query, candidates, threshold=fuzzy_cfg.threshold, margin=fuzzy_cfg.margin)
        if decision.accepted and decision.winner is not None:
            try:
                await apply_candidate(page, step, decision.winner, timeout_ms=timeout_ms)
            except Exception:
                # Fall through to LLM repair rather than failing the whole run.
                return None
            definition = self._remember_target(
                definition,
                workflow_id,
                index,
                page,
                step,
                decision.winner,
                query,
                do_write_back=write_back_repairs,
                source="workflow_use_fuzzy",
                persist_alias=False,
            )
            return (
                "done",
                (
                    _tel(
                        index,
                        step,
                        "workflow_use",
                        True,
                        started,
                        attempt=SelectorAttempt(
                            kind="selector",
                            strategy=SelectorStrategy(
                                type=decision.winner.selector_type,
                                value=decision.winner.selector or decision.winner.text,
                            ),
                        ),
                        output={
                            "query": query,
                            "matched": decision.winner.text,
                            "score": decision.winner.score,
                            "reason": "accepted",
                        },
                        resolution="fuzzy",
                    ),
                    definition,
                    {"query": query, "matched": decision.winner.text, "score": decision.winner.score},
                ),
            )
        if decision.reason == "no_candidates":
            return None
        hitl_cfg = merge_hitl(self.settings, definition, step)
        if hitl_cfg.mode == "llm_only":
            return None
        return (
            "needs_hitl",
            {
                "query": query,
                "decision": decision,
                "fuzzy": fuzzy_cfg,
                "hitl": hitl_cfg,
                "candidates": list(decision.ranked),
            },
        )

    def _remember_target(
        self,
        definition: WorkflowDefinition,
        workflow_id: str,
        index: int,
        page: Page,
        step: WorkflowStep,
        candidate: Candidate,
        query: str,
        *,
        do_write_back: bool,
        source: str,
        persist_alias: bool,
    ) -> WorkflowDefinition:
        strategy = SelectorStrategy(
            type=candidate.selector_type or "css",
            value=candidate.selector or candidate.text,
            priority=1,
            metadata={"source": source, "role": candidate.role or "", "text": candidate.text},
        )
        if not candidate.selector:
            strategy = SelectorStrategy(
                type="text_exact",
                value=candidate.text,
                priority=1,
                metadata={"source": source, "role": candidate.role or ""},
            )
        try:
            self.action_cache.put(workflow_id, index, getattr(page, "url", "") or "", strategy)
        except Exception:
            pass
        if do_write_back:
            definition = write_back(definition, step_index=index, strategy=strategy, reason=source)
        if persist_alias:
            alias = make_alias(index, query, candidate)
            others = [a for a in definition.aliases if not (a.step_index == index and a.query_normalized == alias.query_normalized)]
            definition = definition.model_copy(update={"aliases": [alias, *others]})
            if workflow_id:
                try:
                    self.store.update_workflow(workflow_id, definition)
                except Exception:
                    pass
        return definition

    async def _await_hitl(
        self,
        page: Page,
        workflow_id: str,
        definition: WorkflowDefinition,
        index: int,
        step: WorkflowStep,
        pending: dict[str, Any],
        *,
        write_back_repairs: bool,
        run: RunRecord | None,
        started: float,
        timeout_ms: int,
        det_error: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ):
        """Pause for a human choice, or apply the HITL timeout policy.

        Returns a (tel, definition, output) tuple, or None to fall through to the LLM.
        """
        hitl_cfg = pending["hitl"]
        decision = pending["decision"]
        query = pending["query"]
        fuzzy_cfg = pending["fuzzy"]

        if self.confirmation_broker is None or run is None:
            # Cannot pause (unit tests, no broker): defer to LLM when possible.
            return None

        timeout_at = datetime.now(timezone.utc) + timedelta(seconds=float(hitl_cfg.timeout_s or 0))
        event = {
            "type": "needs_confirmation",
            "run_id": run.id,
            "workflow_id": workflow_id,
            "workflow_name": run.workflow_name,
            "step_index": index,
            "step_type": step.type,
            "step_description": step.description,
            "query": query,
            "reason": decision.reason,
            "threshold": decision.threshold,
            "margin": decision.margin,
            "winner_score": decision.winner.score if decision.winner else None,
            "runner_up_score": decision.runner_up.score if decision.runner_up else None,
            "top": [
                c.public_dict(include_screenshot=bool(fuzzy_cfg.include_screenshot))
                for c in decision.top_n(fuzzy_cfg.top_n)
            ],
            "timeout_s": hitl_cfg.timeout_s,
            "timeout_at": timeout_at.isoformat(),
            "timeout_policy": hitl_cfg.timeout_policy,
            "hitl_mode": hitl_cfg.mode,
            "resume": {
                "method": "POST",
                "path": f"/api/runs/{run.id}/confirmation",
                "body": {"action": "confirm", "candidate_id": "<id from top>"},
            },
            "cancel": {
                "method": "POST",
                "path": f"/api/runs/{run.id}/confirmation",
                "body": {"action": "cancel"},
            },
        }
        run.status = "needs_confirmation"
        run.confirmation = event
        self.store.save_run(run)

        choice = await self.confirmation_broker.wait(run.id, event, timeout_s=float(hitl_cfg.timeout_s or 0))

        if choice.action == "cancel":
            run.status = "cancelled"
            run.confirmation = None
            run.error = "cancelled by operator"
            self.store.save_run(run)
            await self.event_hub.publish({"type": "run_cancelled", "run_id": run.id, "workflow_id": workflow_id})
            return (
                _tel(index, step, "workflow_use", False, started, error="cancelled by operator", resolution="hitl"),
                definition,
                None,
            )

        if choice.action == "timeout":
            run.status = "running"
            run.confirmation = None
            self.store.save_run(run)
            await self.event_hub.publish(
                {"type": "confirmation_timeout", "run_id": run.id, "policy": hitl_cfg.timeout_policy}
            )
            if hitl_cfg.timeout_policy == "fall_to_llm":
                return None
            return (
                _tel(
                    index,
                    step,
                    "workflow_use",
                    False,
                    started,
                    error=f"{det_error}; confirmation timed out",
                    resolution="hitl",
                ),
                definition,
                None,
            )

        ranked: list[Candidate] = list(pending.get("candidates") or decision.ranked)
        picked = _pick_candidate(ranked, choice)
        if picked is None:
            return (
                _tel(
                    index,
                    step,
                    "workflow_use",
                    False,
                    started,
                    error="confirmation did not match a candidate",
                    resolution="hitl",
                ),
                definition,
                None,
            )

        await apply_candidate(page, step, picked, timeout_ms=timeout_ms)
        definition = self._remember_target(
            definition,
            workflow_id,
            index,
            page,
            step,
            picked,
            query,
            do_write_back=write_back_repairs,
            source="workflow_use_hitl",
            persist_alias=True,
        )
        run.status = "running"
        run.confirmation = None
        self.store.save_run(run)
        await self.event_hub.publish(
            {
                "type": "run_resumed",
                "run_id": run.id,
                "workflow_id": workflow_id,
                "step_index": index,
                "chosen_text": picked.text,
                "candidate_id": picked.id,
            }
        )
        return (
            _tel(
                index,
                step,
                "workflow_use",
                True,
                started,
                attempt=SelectorAttempt(
                    kind="selector",
                    strategy=SelectorStrategy(
                        type=picked.selector_type,
                        value=picked.selector or picked.text,
                    ),
                ),
                output={"query": query, "matched": picked.text, "score": picked.score, "reason": "human"},
                resolution="hitl",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            ),
            definition,
            {"query": query, "matched": picked.text, "reason": "human"},
        )

    def _should_use_browser_use(self, page: Page) -> bool:
        if self.browser_use_repair is not None:
            return True
        if self.use_browser_use is False:
            return False
        if self.use_browser_use is True:
            return browser_use_available()
        # Auto: only drive a real Playwright page so unit FakePage tests stay on OpenRouter mocks.
        return browser_use_available() and page.__class__.__module__.startswith("playwright")


def _pomerado_ok(result: Any) -> bool:
    """Only terminal published results count as success (not running/incomplete)."""

    if result is None:
        return False
    if isinstance(result, dict):
        if result.get("ok") is False:
            return False
        if result.get("isError") or result.get("error"):
            return False
        status = str(result.get("status") or "").lower()
        if status in {"failed", "error", "incomplete", "running", "pending", "queued", "waiting"}:
            return False
        if result.get("ok") is True and status in {"", "ok", "succeeded", "success", "published", "complete", "completed"}:
            return True
        if status in {"succeeded", "success", "published", "complete", "completed"}:
            return True
        # Explicit terminal payload without a running status.
        if status == "" and (result.get("output") is not None or result.get("result") is not None):
            return True
        return False
    return False


def _tel(
    index: int,
    step: WorkflowStep,
    tier,
    success: bool,
    started: float,
    *,
    attempt: SelectorAttempt | None = None,
    output: Any = None,
    error: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    resolution: str | None = None,
) -> StepTelemetry:
    return StepTelemetry(
        step_index=index,
        step_type=step.type,
        description=step.description,
        tier=tier,
        success=success,
        latency_ms=(time.perf_counter() - started) * 1000,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        selector_type=attempt.type if attempt else None,
        selector_value=attempt.value if attempt else None,
        error=error,
        output=output,
        resolution=resolution,
    )


def _pick_candidate(candidates: list[Candidate], choice: ConfirmationDecision) -> Candidate | None:
    from browser_platform.fuzzy.normalize import normalize_text

    cid = (choice.candidate_id or "").strip()
    text = (choice.text or "").strip()
    for cand in candidates:
        if cid and cand.id == cid:
            return cand
    if text:
        want = normalize_text(text)
        for cand in candidates:
            if cand.text == text or normalize_text(cand.text) == want:
                return cand
    return None

"""Plan workflow tool calls, run them as a batch, then adapt the results to the question."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any, Awaitable, Callable

from browser_platform.authoring.tools import (
    is_action_ending,
    lookup_tool,
    openai_tools,
    tools_from_workflows,
)
from browser_platform.executor.isolated import IsolatedBrowser, close_isolated, launch_isolated
from browser_platform.executor.output_validate import collect_sample, is_nav_footer_only
from browser_platform.llm.openrouter import chat_complete, env_api_key, merge_usage, message_text
from browser_platform.models.workflow import StoredWorkflow
from browser_platform.orchestrator.normalize_inputs import normalize_prepared_calls
from browser_platform.store.file_store import FileStore
from browser_platform.workflow_inputs import missing_required_inputs

BATCH_CONFIRM_AFTER = 5
CONCURRENCY = 2
MAX_JOBS = 50
Emit = Callable[[dict[str, Any]], Awaitable[None] | None]


def _now() -> float:
    return time.perf_counter()


def compact_item(item: dict[str, Any], *, fact_limit: int = 800) -> dict[str, Any]:
    """Shrink a per-call result so the synthesis model stays cheap."""

    validation = item.get("validation") or {}
    facts = item.get("facts") or item.get("sample") or ""
    if not facts:
        facts = collect_sample(item.get("outputs") or {})
    facts = " ".join(str(facts).split())
    for noise in ("Skip to content", "Scroll left", "Scroll right", "Back to top"):
        facts = facts.replace(noise, " ")
    facts = " ".join(facts.split())
    if len(facts) > fact_limit:
        facts = facts[: fact_limit - 1].rstrip() + "…"
    return {
        "id": item.get("id"),
        "workflow": item.get("workflow_name") or item.get("tool"),
        "workflow_id": item.get("workflow_id"),
        "inputs": item.get("inputs") or item.get("arguments") or {},
        "raw_inputs": item.get("raw_inputs") or item.get("raw_arguments") or {},
        "status": item.get("status"),
        "validation": {
            "verdict": validation.get("verdict"),
            "reason": validation.get("reason"),
        }
        if validation
        else None,
        "error": item.get("error"),
        "facts": facts,
        "url": item.get("url") or "",
    }


def is_action_call(stored: StoredWorkflow | None) -> bool:
    return bool(stored and is_action_ending(stored.definition))


def _missing(stored: StoredWorkflow, arguments: dict[str, Any]) -> list[str]:
    return missing_required_inputs(stored.definition, arguments or {})


class ChatJobs:
    def __init__(self, cap: int = MAX_JOBS):
        self.cap = cap
        self._jobs: dict[str, dict[str, Any]] = {}

    def put(self, job: dict[str, Any]) -> dict[str, Any]:
        self._jobs[job["id"]] = job
        while len(self._jobs) > self.cap:
            oldest = next(iter(self._jobs))
            self._jobs.pop(oldest, None)
        return job

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self._jobs.get(job_id)


def _status_from_items(compact: list[dict[str, Any]], flagged: list[dict[str, Any]]) -> str:
    """A chat job is not succeeded when any workflow item failed or was flagged."""

    if not compact:
        return "failed"
    ok = [
        item
        for item in compact
        if item.get("status") == "succeeded" and (item.get("validation") or {}).get("verdict") not in {"wrong", "suspect"}
    ]
    if ok and len(ok) == len(compact) and not flagged:
        return "succeeded"
    if ok:
        return "partial"
    return "failed"


class ChatOrchestrator:
    def __init__(
        self,
        store: FileStore,
        *,
        runner: Any = None,
        jobs: ChatJobs | None = None,
        concurrency: int = CONCURRENCY,
    ):
        self.store = store
        self.runner = runner
        self.jobs = jobs or ChatJobs()
        self.concurrency = concurrency

    def catalog(self) -> list[dict[str, Any]]:
        return tools_from_workflows(self.store.list_workflows())

    def stored_for(self, tool: dict[str, Any] | None) -> StoredWorkflow | None:
        if not tool:
            return None
        try:
            return self.store.get_workflow(str(tool.get("workflow_id")))
        except Exception:
            return None

    async def handle(
        self,
        *,
        message: str,
        history: list[dict[str, Any]] | None = None,
        target: str = "cloud",
        confirm: bool = False,
        job_id: str | None = None,
        results: list[dict[str, Any]] | None = None,
        emit: Emit | None = None,
    ) -> dict[str, Any]:
        started = _now()
        if job_id:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError("Unknown or expired chat job.")
            if results is not None:
                job["items"] = _normalize_client_results(job, results)
                await self.synthesize(job, emit=emit)
                job["elapsed_ms"] = round((_now() - started) * 1000, 1)
                return job
            if confirm and job.get("status") in {"needs_confirmation", "planned"}:
                job["confirmed"] = True
                if target == "extension" or job.get("target") == "extension":
                    job["status"] = "awaiting_results"
                    job["target"] = "extension"
                    await _emit(emit, {"type": "planned", "job": public_job(job)})
                    return job
                await self.execute(job, emit=emit)
                await self.synthesize(job, emit=emit)
                job["elapsed_ms"] = round((_now() - started) * 1000, 1)
                return job
        job = await self.plan(message, history=history or [], target=target, emit=emit)
        needs = job.get("status")
        if needs == "needs_confirmation" and confirm:
            job["confirmed"] = True
            if target == "extension" or job.get("target") == "extension":
                job["status"] = "awaiting_results"
                job["target"] = "extension"
                await _emit(emit, {"type": "planned", "job": public_job(job)})
                job["elapsed_ms"] = round((_now() - started) * 1000, 1)
                return job
            await self.execute(job, emit=emit)
            await self.synthesize(job, emit=emit)
            job["elapsed_ms"] = round((_now() - started) * 1000, 1)
            return job
        if needs in {"needs_input", "needs_confirmation"}:
            job["elapsed_ms"] = round((_now() - started) * 1000, 1)
            return job
        if job.get("target") == "extension":
            job["status"] = "awaiting_results"
            await _emit(emit, {"type": "planned", "job": public_job(job)})
            job["elapsed_ms"] = round((_now() - started) * 1000, 1)
            return job
        await self.execute(job, emit=emit)
        await self.synthesize(job, emit=emit)
        job["elapsed_ms"] = round((_now() - started) * 1000, 1)
        return job

    async def plan(
        self,
        message: str,
        *,
        history: list[dict[str, Any]],
        target: str = "cloud",
        emit: Emit | None = None,
    ) -> dict[str, Any]:
        tools = self.catalog()
        job: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "status": "planning",
            "message": message,
            "target": "extension" if target == "extension" else "cloud",
            "calls": [],
            "items": [],
            "answer": None,
            "usage": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": False},
            "question": None,
            "confirm_reason": None,
            "flagged": [],
        }
        self.jobs.put(job)
        await _emit(emit, {"type": "planning", "job_id": job["id"]})
        if not tools:
            job["status"] = "failed"
            job["error"] = "No saved workflows are available to use as tools."
            await _emit(emit, {"type": "error", "error": job["error"]})
            return job
        if not env_api_key():
            job["status"] = "failed"
            job["error"] = "Chat planning needs OPENROUTER_API_KEY on the server."
            await _emit(emit, {"type": "error", "error": job["error"]})
            return job
        tool_lines = []
        for item in tools:
            fn = item["function"]
            req = ", ".join((fn.get("parameters") or {}).get("required") or []) or "none"
            tool_lines.append(f"- {fn['name']} (workflow {item.get('workflow_name')}; required: {req})")
        system = (
            "You are a workflow tool router. Call one or more saved browser workflows to answer the user. "
            "Fill required arguments from values the user already stated (teams, names, terms, dates). Only ask a short question when a required input is not present in the user's message. Never invent values the user did not state. "
            "When the same workflow should run for several values (a list of teams, terms, dates), emit one function call per value, in parallel. "
            "Do not pack several values into one argument. Prefer the workflow that matches the request. "
            "Do not answer from memory when a tool can fetch the live result."
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for turn in history[-8:]:
            role = turn.get("role")
            content = turn.get("content")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": str(content)[:1500]})
        messages.append({"role": "user", "content": message[:4000]})
        assistant, usage = await chat_complete(
            messages,
            tools=openai_tools(tools),
            max_tokens=800,
        )
        job["usage"] = merge_usage(job.get("usage"), usage)
        job["plan_usage"] = usage
        content = message_text(assistant)
        calls = _parse_tool_calls(assistant, tools)
        if not calls:
            job["status"] = "succeeded" if content else "failed"
            job["answer"] = content or "I need a bit more detail to pick a workflow."
            job["question"] = None if content else job["answer"]
            if not content:
                job["status"] = "needs_input"
            await _emit(emit, {"type": "message", "text": job["answer"], "job_id": job["id"]})
            return job
        missing: list[str] = []
        prepared: list[dict[str, Any]] = []
        action = False
        for index, call in enumerate(calls, 1):
            tool = lookup_tool(tools, call["name"])
            stored = self.stored_for(tool)
            if stored is None:
                missing.append(f"Unknown workflow tool '{call['name']}'.")
                continue
            args = call.get("arguments") or {}
            absent = _missing(stored, args)
            if absent:
                missing.append(
                    f"{stored.definition.name} is missing required input(s): {', '.join(absent)}."
                )
            ends = is_action_ending(stored.definition)
            action = action or ends
            prepared.append(
                {
                    "id": f"c{index}",
                    "tool": call["name"],
                    "workflow_id": stored.id,
                    "workflow_name": stored.definition.name,
                    "arguments": args,
                    "action_ending": ends,
                    "definition": stored.definition.model_dump(exclude_none=True) if job["target"] == "extension" else None,
                }
            )
        job["calls"] = prepared
        if prepared:
            definitions = {}
            for call in prepared:
                try:
                    stored = self.store.get_workflow(call["workflow_id"])
                except Exception:
                    stored = None
                if stored is not None:
                    definitions[stored.id] = stored.definition
            try:
                norm_usage = await normalize_prepared_calls(prepared, definitions=definitions)
                job["usage"] = merge_usage(job.get("usage"), norm_usage)
            except Exception:
                pass
        if missing:
            job["status"] = "needs_input"
            job["question"] = " ".join(missing) + " Please provide the missing value(s)."
            job["answer"] = job["question"]
            await _emit(emit, {"type": "needs_input", "question": job["question"], "job_id": job["id"]})
            return job
        reason = None
        if action:
            names = [c["workflow_name"] for c in prepared if c.get("action_ending")]
            reason = (
                "This will run "
                + ", ".join(names)
                + ", which ends in a click or submit. Confirm before I continue."
            )
        if len(prepared) > BATCH_CONFIRM_AFTER:
            extra = f"This batch will run {len(prepared)} workflow calls."
            reason = f"{reason} {extra}".strip() if reason else extra + " Confirm before I continue."
        if reason:
            job["status"] = "needs_confirmation"
            job["confirm_reason"] = reason
            job["answer"] = reason
            await _emit(emit, {"type": "needs_confirmation", "reason": reason, "calls": public_calls(prepared), "job_id": job["id"]})
            return job
        job["status"] = "planned"
        await _emit(emit, {"type": "planned", "calls": public_calls(prepared), "job_id": job["id"]})
        return job

    async def execute(self, job: dict[str, Any], *, emit: Emit | None = None) -> dict[str, Any]:
        job["status"] = "running"
        await _emit(emit, {"type": "running", "job_id": job["id"], "total": len(job.get("calls") or [])})
        sem = asyncio.Semaphore(self.concurrency)
        items: list[dict[str, Any] | None] = [None] * len(job["calls"])

        async def _one(index: int, call: dict[str, Any]) -> None:
            async with sem:
                item = await self._run_call(job, call, emit=emit)
                items[index] = item

        await asyncio.gather(*[_one(i, call) for i, call in enumerate(job["calls"])], return_exceptions=False)
        job["items"] = [item for item in items if item is not None]
        return job

    async def _run_call(self, job: dict[str, Any], call: dict[str, Any], *, emit: Emit | None = None) -> dict[str, Any]:
        if not call.get("normalized"):
            try:
                stored = self.store.get_workflow(call["workflow_id"])
                usage = await normalize_prepared_calls(
                    [call],
                    definitions={stored.id: stored.definition},
                )
                job["usage"] = merge_usage(job.get("usage"), usage)
            except Exception:
                call.setdefault("arguments", dict(call.get("raw_arguments") or call.get("arguments") or {}))
        item: dict[str, Any] = {
            "id": call["id"],
            "tool": call["tool"],
            "workflow_id": call["workflow_id"],
            "workflow_name": call["workflow_name"],
            "inputs": dict(call.get("arguments") or {}),
            "raw_inputs": dict(call.get("raw_arguments") or call.get("arguments") or {}),
            "action_ending": bool(call.get("action_ending")),
            "status": "running",
            "attempts": 0,
        }
        await _emit(emit, {"type": "item_start", "item": public_item(item), "job_id": job["id"]})
        last_error = "Workflow run failed."
        tried: list[str] = []
        for attempt in (1, 2):
            item["attempts"] = attempt
            item["inputs"] = dict(call.get("arguments") or item.get("inputs") or {})
            signature = repr(sorted((item.get("inputs") or {}).items()))
            isolated: IsolatedBrowser | None = None
            try:
                isolated = await launch_isolated()
                stored = self.store.get_workflow(call["workflow_id"])
                run = await self.runner.run(
                    stored,
                    cdp_url=isolated.cdp_url,
                    variables=item["inputs"],
                    llm=None,
                    write_back_repairs=False,
                    request_text=(
                        f"Check that this {call['workflow_name']} result matches the inputs {item['inputs']}. "
                        "Do not require this single call to answer a comparison across other teams or workflows."
                    ),
                    validate_output=True,
                )
                item["run_id"] = run.id
                item["outputs"] = run.outputs or {}
                item["url"] = _url_from_run(run)
                item["validation"] = getattr(run, "validation", None) or (run.model_dump().get("validation") if hasattr(run, "model_dump") else None)
                if isinstance(run, dict):
                    item["validation"] = run.get("validation")
                usage = _run_validation_usage(item.get("validation"))
                job["usage"] = merge_usage(job.get("usage"), usage)
                sample = collect_sample(item.get("outputs"))
                chrome_only = is_nav_footer_only(sample, action_ending=bool(item.get("action_ending")))
                if chrome_only:
                    reason = "The extract contains only navigation or footer text, not the requested content."
                    validation = dict(item.get("validation") or {})
                    validation.update({"verdict": "wrong", "reason": reason, "source": "deterministic"})
                    item["validation"] = validation
                    last_error = reason
                if run.status == "succeeded" and not chrome_only:
                    item["status"] = "succeeded"
                    item["facts"] = sample
                    if item.get("action_ending"):
                        item["facts"] = "Action completed. " + (item.get("facts") or "")
                    break
                if not chrome_only:
                    last_error = run.error or f"Workflow ended with status {run.status}."
                tried.append(signature)
                # Retry once only when a different normalised value is available.
                should_retry = chrome_only or (item.get("validation") or {}).get("verdict") == "wrong"
                if should_retry and attempt == 1:
                    alt = await self._retry_normalized_arguments(call, stored, complete_failed=chrome_only)
                    alt_sig = repr(sorted((alt or {}).items()))
                    if alt and alt_sig not in tried and alt_sig != signature:
                        call["arguments"] = alt
                        item["inputs"] = dict(alt)
                        continue
                item["status"] = "failed"
                item["error"] = last_error
                break
            except Exception as exc:
                last_error = str(exc)
                if attempt == 2:
                    item["status"] = "failed"
                    item["error"] = last_error
            finally:
                await close_isolated(isolated)
        if item.get("status") != "succeeded" and not item.get("error"):
            item["status"] = "failed"
            item["error"] = last_error
        verdict = (item.get("validation") or {}).get("verdict")
        if verdict in {"suspect", "wrong"} or item.get("status") == "failed":
            job.setdefault("flagged", []).append(
                {
                    "id": item["id"],
                    "workflow": item["workflow_name"],
                    "inputs": item["inputs"],
                    "verdict": verdict or item.get("status"),
                    "reason": (item.get("validation") or {}).get("reason") or item.get("error"),
                }
            )
        await _emit(emit, {"type": "item_done", "item": public_item(item), "job_id": job["id"]})
        return item

    async def _retry_normalized_arguments(self, call: dict[str, Any], stored: StoredWorkflow, *, complete_failed: bool) -> dict[str, Any] | None:
        """Return a different canonical argument map to retry, or None."""

        raw = dict(call.get("raw_arguments") or call.get("arguments") or {})
        current = dict(call.get("arguments") or raw)
        # Re-run normalisation from the original user values.
        probe = {
            "id": call.get("id") or "c1",
            "arguments": dict(raw),
            "raw_arguments": dict(raw),
            "workflow_id": call.get("workflow_id"),
        }
        try:
            await normalize_prepared_calls([probe], definitions={stored.id: stored.definition})
        except Exception:
            return None
        alt = dict(probe.get("arguments") or {})
        if alt and alt != current:
            return alt
        if complete_failed and alt == current and raw != current:
            return None
        return alt if alt != current else None


    async def synthesize(self, job: dict[str, Any], *, emit: Emit | None = None) -> dict[str, Any]:
        compact = [compact_item(item) for item in job.get("items") or []]
        job["compact"] = compact
        flagged = [
            item
            for item in compact
            if item.get("status") != "succeeded" or (item.get("validation") or {}).get("verdict") in {"suspect", "wrong"}
        ]
        job["flagged"] = [
            {
                "id": item["id"],
                "workflow": item["workflow"],
                "inputs": item["inputs"],
                "verdict": (item.get("validation") or {}).get("verdict") or item.get("status"),
                "reason": (item.get("validation") or {}).get("reason") or item.get("error"),
            }
            for item in flagged
        ]
        fallback = _fallback_answer(job, compact)
        if not env_api_key():
            job["answer"] = fallback
            job["status"] = _status_from_items(compact, job["flagged"])
            if job["status"] != "succeeded" and not job.get("error"):
                job["error"] = (job["flagged"][0].get("reason") if job["flagged"] else "One or more workflow items failed.")
            await _emit(emit, {"type": "answer", "text": job["answer"], "job_id": job["id"]})
            return job
        try:
            message, usage = await chat_complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "Adapt the workflow outputs to the user's actual question. "
                            "Merge, dedupe, sort and compare across calls. "
                            "Lead with a direct answer, then a compact markdown table when comparing or listing several items. "
                            "Use the user's language and format. "
                            "Cite which workflow and input each fact came from (for example football_fixtures / team=Arsenal). "
                            "Clearly mention any item the output validator flagged as suspect or wrong, or that failed. "
                            "Do not dump raw extracts. Keep the answer tight."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"question": job.get("message"), "results": compact, "flagged": job.get("flagged") or []},
                            ensure_ascii=False,
                        )[:6000],
                    },
                ],
                max_tokens=500,
            )
            job["usage"] = merge_usage(job.get("usage"), usage)
            job["synthesis_usage"] = usage
            text = message_text(message)
            job["answer"] = text or fallback
        except Exception as exc:
            job["answer"] = fallback
            job["synthesis_error"] = type(exc).__name__
        job["status"] = _status_from_items(compact, job["flagged"])
        if job["status"] != "succeeded" and not job.get("error"):
            job["error"] = (job["flagged"][0].get("reason") if job["flagged"] else "One or more workflow items failed.")
        await _emit(emit, {"type": "answer", "text": job["answer"], "usage": job.get("usage"), "job_id": job["id"]})
        return job


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job.get("id"),
        "status": job.get("status"),
        "message": job.get("message"),
        "target": job.get("target"),
        "calls": public_calls(job.get("calls") or []),
        "items": [public_item(item) for item in job.get("items") or []],
        "answer": job.get("answer"),
        "question": job.get("question"),
        "confirm_reason": job.get("confirm_reason"),
        "flagged": job.get("flagged") or [],
        "usage": job.get("usage"),
        "elapsed_ms": job.get("elapsed_ms"),
        "error": job.get("error"),
    }


def public_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for call in calls:
        item = {
            "id": call.get("id"),
            "tool": call.get("tool"),
            "workflow_id": call.get("workflow_id"),
            "workflow_name": call.get("workflow_name"),
            "arguments": call.get("arguments") or {},
            "raw_arguments": call.get("raw_arguments") or {},
            "action_ending": bool(call.get("action_ending")),
        }
        if call.get("definition") is not None:
            item["definition"] = call["definition"]
        out.append(item)
    return out


def public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "tool": item.get("tool"),
        "workflow_id": item.get("workflow_id"),
        "workflow_name": item.get("workflow_name"),
        "inputs": item.get("inputs") or {},
        "raw_inputs": item.get("raw_inputs") or item.get("raw_arguments") or {},
        "status": item.get("status"),
        "error": item.get("error"),
        "validation": item.get("validation"),
        "facts": item.get("facts") or item.get("sample"),
        "outputs": item.get("outputs") or {},
        "url": item.get("url"),
        "run_id": item.get("run_id"),
        "action_ending": bool(item.get("action_ending")),
        "attempts": item.get("attempts"),
    }


def _parse_args(args_raw: Any) -> dict[str, Any]:
    if isinstance(args_raw, dict):
        return args_raw
    if isinstance(args_raw, str):
        try:
            parsed = json.loads(args_raw) if args_raw.strip() else {}
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_tool_calls(assistant: dict[str, Any], tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw = assistant.get("tool_calls") or []
    calls: list[dict[str, Any]] = []
    for item in raw:
        fn = item.get("function") if isinstance(item, dict) else None
        if not isinstance(fn, dict):
            continue
        name = fn.get("name") or ""
        args = _parse_args(fn.get("arguments") or "{}")
        if name:
            calls.append({"name": name, "arguments": args})
    if calls:
        return calls
    # Some models write tool calls into the text instead of the tool_calls field.
    text = message_text(assistant)
    known = {((item.get("function") or {}).get("name") or "") for item in tools}
    for match in re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, re.S):
        try:
            data = json.loads(match.group(1))
        except Exception:
            continue
        name = str(data.get("name") or data.get("function") or "")
        args = data.get("arguments") or data.get("parameters") or {}
        if name and name in known:
            calls.append({"name": name, "arguments": _parse_args(args)})
    if not calls:
        for match in re.finditer(
            r'"name"\s*:\s*"([^"]+)"\s*,\s*"arguments"\s*:\s*(\{.*?\})',
            text,
            re.S,
        ):
            name, raw_args = match.group(1), match.group(2)
            if name in known:
                calls.append({"name": name, "arguments": _parse_args(raw_args)})
    return calls


def _url_from_run(run: Any) -> str:
    outputs = getattr(run, "outputs", None) or {}
    for key in ("url", "final_url", "page_url"):
        if outputs.get(key):
            return str(outputs[key])
    for step in reversed(getattr(run, "steps", None) or []):
        out = getattr(step, "output", None)
        if isinstance(out, dict) and out.get("url"):
            return str(out["url"])
        if isinstance(out, str) and out.startswith("http"):
            return out
    return ""


def _run_validation_usage(validation: dict[str, Any] | None) -> dict[str, Any] | None:
    if not validation:
        return None
    return validation.get("usage")


def _fallback_answer(job: dict[str, Any], compact: list[dict[str, Any]]) -> str:
    lines = []
    ok = [item for item in compact if item.get("status") == "succeeded"]
    if len(ok) == 1:
        lines.append(ok[0].get("facts") or "Action completed." if ok[0].get("status") == "succeeded" else "")
    elif ok:
        lines.append("Combined results:")
        for item in ok:
            inputs = ", ".join(f"{k}={v}" for k, v in (item.get("inputs") or {}).items())
            lines.append(f"- {item.get('workflow')} ({inputs}): {item.get('facts') or 'ok'}")
    flagged = job.get("flagged") or []
    if flagged:
        lines.append("Flagged or failed:")
        for item in flagged:
            lines.append(
                f"- {item.get('workflow')} {item.get('inputs')}: {item.get('verdict')} — {item.get('reason')}"
            )
    return "\n".join(line for line in lines if line).strip() or "The workflow calls finished, but there was nothing to summarise."


def _normalize_client_results(job: dict[str, Any], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {call["id"]: call for call in job.get("calls") or []}
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(results or [], 1):
        call = by_id.get(str(raw.get("id") or "")) or (job.get("calls") or [None])[index - 1] if job.get("calls") else {}
        call = call or {}
        item = {
            "id": raw.get("id") or call.get("id") or f"c{index}",
            "tool": raw.get("tool") or call.get("tool"),
            "workflow_id": raw.get("workflow_id") or call.get("workflow_id"),
            "workflow_name": raw.get("workflow_name") or call.get("workflow_name"),
            "inputs": raw.get("inputs") or raw.get("arguments") or call.get("arguments") or {},
            "status": raw.get("status") or ("succeeded" if raw.get("ok") else "failed"),
            "error": raw.get("error"),
            "outputs": raw.get("outputs") or {},
            "facts": raw.get("facts") or collect_sample(raw.get("outputs") or {}),
            "url": raw.get("url") or "",
            "validation": raw.get("validation"),
            "action_ending": bool(raw.get("action_ending") or call.get("action_ending")),
            "run_id": raw.get("run_id"),
        }
        items.append(item)
    return items


async def _emit(emit: Emit | None, event: dict[str, Any]) -> None:
    if emit is None:
        return
    result = emit(event)
    if asyncio.iscoroutine(result):
        await result

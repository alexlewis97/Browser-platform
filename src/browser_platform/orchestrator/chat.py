"""Plan workflow tool calls, run them as a batch, then adapt the results to the question."""

from __future__ import annotations

import asyncio
import json
import re
import time
import logging
import uuid
from pathlib import Path
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
from browser_platform.orchestrator.grounding import data_status, date_context, ungrounded_claims
from browser_platform.orchestrator.normalize_inputs import normalize_prepared_calls
from browser_platform.orchestrator.planner import (
    TOP_K,
    check_step,
    is_ref,
    parse_plan,
    planner_messages,
    repair_calls,
    resolve_refs,
    retrieval_query,
    sub_queries,
    uncovered_intents,
    workspace_context,
    waves,
)
from browser_platform.skills.registry import SkillRegistry
from browser_platform.workspaces import Workspace, WorkspaceStore, resolve_scope
from browser_platform.skills.retrieve import rank_skills
from browser_platform.redaction import redact
from browser_platform.store.file_store import FileStore
from browser_platform.workflow_inputs import (
    inputs_for_output_validation,
    invalid_typed_inputs,
    input_grain,
    missing_required_inputs,
    resolve_input_values,
)

logger = logging.getLogger("browser_platform.chat")
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
        "data_status": data_status(facts, status=item.get("status")),
        "needs_repair": bool(item.get("needs_repair")),
        "url": item.get("url") or "",
    }


def is_action_call(stored: StoredWorkflow | None) -> bool:
    return bool(stored and is_action_ending(stored.definition))



MAX_FAN_OUT = 5
FACTS_LIMIT = 4000  # chars of extract kept per chat item
SYNTH_FACTS_BUDGET = 5000  # chars of facts shared by all items in the synthesis prompt


def fan_out_calls(prepared: list[dict[str, Any]], store: Any, notes: list[str] | None = None) -> list[dict[str, Any]]:
    """Several values for a one-value input become one call per value (a list input keeps them).

    Only the first multi-valued argument fans out, capped at MAX_FAN_OUT; extra values are noted."""

    out: list[dict[str, Any]] = []
    for call in prepared:
        args = call.get("arguments") or {}
        try:
            definition = store.get_workflow(call["workflow_id"]).definition
        except Exception:
            definition = None
        types = {i.name: i.type for i in (getattr(definition, "input_schema", None) or [])}
        multi = next((k for k, v in args.items() if isinstance(v, list) and types.get(k) != "list"), None)
        if multi is None:
            out.append(call)
            continue
        values = [v for v in args[multi] if str(v).strip()]
        if len(values) > MAX_FAN_OUT and notes is not None:
            notes.append(f"{call.get('tool')}: only the first {MAX_FAN_OUT} of {len(values)} {multi} values were run")
        for k, value in enumerate(values[:MAX_FAN_OUT] or [""]):
            copy = {**call, "arguments": {**args, multi: value}}
            if k:
                copy["id"] = f"{call['id']}_{k + 1}"
            out.append(copy)
    return out


def default_view_companions(
    calls: list[dict[str, Any]], definitions: dict[str, Any] | None, message: str
) -> list[dict[str, Any]]:
    """For a question about *today*, a call that sets a page control whose omitted value is the
    page's own default view (results|fixtures, upcoming|past...) also gets a twin without it.

    Date-partitioned views split at "now": today's in-progress or remaining items can be in
    neither the past nor the future view, only in the default one. Other days are not doubled."""

    from browser_platform import dates

    today = dates.now_value("date")
    if not any(dates.resolve(expr, "date") == today for expr in dates.find_expressions(message or "")):
        return []
    out: list[dict[str, Any]] = []
    for call in calls:
        definition = (definitions or {}).get(call.get("workflow_id") or "")
        args = call.get("arguments") or {}
        controls = [
            i.name for i in (getattr(definition, "input_schema", None) or [])
            if getattr(i, "default_view", None) and args.get(i.name) not in (None, "")
        ]
        if not controls:
            continue
        twin = {**call, "id": f"{call['id']}_default", "arguments": {k: v for k, v in args.items() if k not in controls}}
        twin["raw_arguments"] = {k: v for k, v in (call.get("raw_arguments") or {}).items() if k not in controls}
        out.append(twin)
    return out


def resolve_prepared_defaults(prepared: list[dict[str, Any]], definitions: dict[str, Any] | None) -> None:
    """Resolve optional runtime defaults and drop placeholders before a tool is called."""

    definitions = definitions or {}
    for call in prepared or []:
        args = dict(call.get("arguments") or {})
        call.setdefault("raw_arguments", dict(call.get("raw_arguments") or args))
        definition = definitions.get(call.get("workflow_id"))
        if definition is None and call.get("workflow_id"):
            # definitions is keyed by workflow id; also try identity
            definition = definitions.get(call["workflow_id"])
        call["arguments"] = resolve_input_values(definition, args)


def anchor_relative_dates(calls: list[dict[str, Any]], definitions: dict[str, Any], message: str, notes: list[str]) -> None:
    """Deterministic dates: if the user said "yesterday" and the planner computed a date that
    disagrees, code's resolution (server clock, user time zone) wins."""

    from browser_platform.dates import anchor_to_message

    for call in calls or []:
        definition = (definitions or {}).get(call.get("workflow_id"))
        if definition is None:
            continue
        args = dict(call.get("arguments") or {})
        for item in definition.input_schema or []:
            grain = input_grain(item, definition)
            if not grain or item.name not in args or is_ref(args[item.name]):
                continue
            fixed = anchor_to_message(args[item.name], grain, message)
            if fixed and fixed != args[item.name]:
                notes.append(f"date anchored: {call.get('tool')}.{item.name} {args[item.name]!r} -> {fixed} (from the user's words)")
                args[item.name] = fixed
        call["arguments"] = args


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

    dir: Any = None
    keep: int = 200

    def save(self, job: dict[str, Any]) -> None:
        """Persist a redacted record of the job so bad runs can be inspected later."""

        if not self.dir:
            return
        from pathlib import Path

        folder = Path(self.dir)
        folder.mkdir(parents=True, exist_ok=True)
        record = public_job(job)
        record.update(
            {
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "plan_raw": job.get("plan_raw"),
                "intents": job.get("intents"),
                "uncovered": job.get("uncovered"),
                "retrieval_scores": (job.get("retrieval") or {}).get("scores"),
                "all_calls": public_calls(job.get("all_calls") or []),
                "items": [{k: v for k, v in public_item(i).items() if k != "outputs"} for i in job.get("items") or []],
            }
        )
        for call in record.get("calls") or []:
            call.pop("definition", None)
        for call in record.get("all_calls") or []:
            call.pop("definition", None)
        path = folder / f"{job['id']}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(redact(record), ensure_ascii=False, default=str, indent=1), encoding="utf-8")
        tmp.replace(path)
        logger.info(
            "chat job %s status=%s skills=%s calls=%s uncovered=%s tokens_in=%s",
            job.get("id"), job.get("status"), record.get("skills_considered"),
            [(c.get("tool"), c.get("arguments")) for c in record.get("all_calls") or record.get("calls") or []],
            job.get("uncovered"), (job.get("usage") or {}).get("tokens_in"),
        )
        files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[: max(0, len(files) - self.keep)]:
            try:
                old.unlink()
            except OSError:
                pass

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.dir:
            return []
        from pathlib import Path

        files = sorted(Path(self.dir).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
        out = []
        for path in files:
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self._jobs.get(job_id)


def _status_from_items(compact: list[dict[str, Any]], flagged: list[dict[str, Any]]) -> str:
    """A chat job is not succeeded when any workflow item failed or was flagged."""

    if not compact:
        return "failed"
    ok = [
        item
        for item in compact
        if item.get("status") == "succeeded"
        and (item.get("validation") or {}).get("verdict") not in {"wrong", "suspect"}
        and item.get("data_status") != "no_data"
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
        self.skills = SkillRegistry(store)
        if getattr(self.jobs, "dir", None) is None and getattr(store, "data_dir", None) is not None:
            self.jobs.dir = store.data_dir / "chat_jobs"
        self.workspaces = WorkspaceStore(Path(getattr(store, "data_dir", "data")))
        hooks = getattr(store, "on_change", None)
        if isinstance(hooks, list):
            hooks.append(self.skills.on_store_change)
            hooks.append(self.workspaces.on_store_change)

    def scope(self, workspace: str | None) -> Workspace | None:
        """Resolve a workspace id/slug; None or 'all' is the whole library. Unknown -> KeyError."""

        return resolve_scope(self.workspaces, workspace)

    def catalog(self, workspace: str | None = None) -> list[dict[str, Any]]:
        ws = self.scope(workspace)
        tools = tools_from_workflows(self.store.list_workflows())
        if ws is None:
            return tools
        allowed = set(ws.workflow_ids)
        return [t for t in tools if t.get("workflow_id") in allowed]

    def stored_for(self, tool: dict[str, Any] | None) -> StoredWorkflow | None:
        if not tool:
            return None
        try:
            return self.store.get_workflow(str(tool.get("workflow_id")))
        except Exception:
            return None

    async def _retrieve(self, query: str, pool: list[dict[str, Any]], parts: list[str] | None = None) -> dict[str, Any]:
        """Top-k for the whole request, plus the best two for each clause, capped at 8 cards.

        A multi-part request ("X results and Y fixtures") can rank one part's skill out of a
        whole-query top-k in a large library; per-clause retrieval keeps every part represented.
        """

        prior = self.skills.reliability()
        ranked = await rank_skills(query, pool, k=TOP_K, embedder=self.skills.embedder, prior=prior)
        skills = list(ranked["skills"])
        scores = list(ranked["scores"])
        tokens = ranked["embed_tokens"]
        for part in parts or []:
            sub = await rank_skills(part, pool, k=2, embedder=self.skills.embedder, prior=prior)
            tokens += sub["embed_tokens"]
            for manifest, score in zip(sub["skills"], sub["scores"]):
                if manifest not in skills and len(skills) < 8 and ((score.get("bm25") or 0) > 0 or (score.get("dense") or 0) >= 0.3):
                    skills.append(manifest)
                    scores.append({**score, "via": part[:60]})
        return {"skills": skills, "scores": scores, "method": ranked["method"], "embed_tokens": tokens}

    async def _next_extension_wave(self, job: dict[str, Any], *, emit: Emit | None = None) -> bool:
        """Queue the next dependency layer for the extension. True when more calls were queued."""

        done = {item["id"]: item for item in job.get("items") or []}
        pending = [c for c in job.get("all_calls") or [] if c["id"] not in done]
        if not pending:
            return False
        ready = [c for c in pending if all(a in done for a in c.get("after") or [])]
        if not ready:
            ready = pending[:1]
        api_key = (job.get("llm_api_key") or "").strip() or env_api_key()
        wave = []
        for call in ready:
            failed = [a for a in call.get("after") or [] if (done.get(a) or {}).get("status") != "succeeded"]
            if failed:
                job["items"].append(
                    {
                        "id": call["id"], "tool": call["tool"], "workflow_id": call["workflow_id"], "workflow_name": call["workflow_name"],
                        "inputs": {k: v for k, v in (call.get("arguments") or {}).items() if not is_ref(v)},
                        "status": "failed", "error": f"Skipped because {', '.join(failed)} did not succeed.",
                        "action_ending": bool(call.get("action_ending")),
                    }
                )
                continue
            if any(is_ref(v) for v in (call.get("arguments") or {}).values()):
                try:
                    args, usage = await resolve_refs(call, done, api_key=api_key, model=job.get("llm_model") or None)
                    job["usage"] = merge_usage(job.get("usage"), usage)
                except Exception:
                    args = {k: v for k, v in (call.get("arguments") or {}).items() if not is_ref(v)}
                call["arguments"] = args
                call["raw_arguments"] = dict(args)
                try:
                    stored = self.store.get_workflow(call["workflow_id"])
                    await normalize_prepared_calls([call], definitions={stored.id: stored.definition})
                    resolve_prepared_defaults([call], {stored.id: stored.definition})
                except Exception:
                    pass
            wave.append(call)
        if not wave:
            return await self._next_extension_wave(job, emit=emit)
        job["calls"] = wave
        job["status"] = "awaiting_results"
        await _emit(emit, {"type": "planned", "job": public_job(job)})
        return True

    async def handle(self, **kwargs: Any) -> dict[str, Any]:
        job = await self._handle(**kwargs)
        try:
            self.jobs.save(job)
        except Exception:
            pass
        return job

    async def _handle(
        self,
        *,
        message: str,
        history: list[dict[str, Any]] | None = None,
        target: str = "cloud",
        confirm: bool = False,
        job_id: str | None = None,
        results: list[dict[str, Any]] | None = None,
        emit: Emit | None = None,
        llm_api_key: str = "",
        llm_model: str = "",
        workspace: str | None = None,
    ) -> dict[str, Any]:
        started = _now()
        if job_id:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError("Unknown or expired chat job.")
            if llm_api_key:
                job["llm_api_key"] = llm_api_key
            if llm_model:
                job["llm_model"] = llm_model
            if results is not None:
                fresh = _normalize_client_results(job, results)
                done = {item["id"]: item for item in job.get("items") or []}
                done.update({item["id"]: item for item in fresh})
                order = [c["id"] for c in job.get("all_calls") or job.get("calls") or []]
                job["items"] = [done[i] for i in order if i in done] + [v for k, v in done.items() if k not in order]
                if await self._next_extension_wave(job, emit=emit):
                    job["elapsed_ms"] = round((_now() - started) * 1000, 1)
                    return job
                await self.synthesize(job, emit=emit)
                job["elapsed_ms"] = round((_now() - started) * 1000, 1)
                return job
            if confirm and job.get("status") in {"needs_confirmation", "planned"}:
                job["confirmed"] = True
                if target == "extension" or job.get("target") == "extension":
                    if not (job.get("calls") or []):
                        job["elapsed_ms"] = round((_now() - started) * 1000, 1)
                        return job
                    job["status"] = "awaiting_results"
                    job["target"] = "extension"
                    await _emit(emit, {"type": "planned", "job": public_job(job)})
                    return job
                await self.execute(job, emit=emit)
                await self.synthesize(job, emit=emit)
                job["elapsed_ms"] = round((_now() - started) * 1000, 1)
                return job
        job = await self.plan(
            message,
            history=history or [],
            target=target,
            emit=emit,
            llm_api_key=llm_api_key,
            llm_model=llm_model,
            workspace=workspace,
        )
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
        if needs in {"needs_input", "needs_confirmation"} or job.get("declined"):
            job["elapsed_ms"] = round((_now() - started) * 1000, 1)
            return job
        if job.get("target") == "extension":
            # Only hand off to the extension when there are tool calls to run.
            if job.get("status") in {"failed", "succeeded"} or not (job.get("calls") or []):
                job["elapsed_ms"] = round((_now() - started) * 1000, 1)
                return job
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
        llm_api_key: str = "",
        llm_model: str = "",
        workspace: str | None = None,
    ) -> dict[str, Any]:
        ws = self.scope(workspace)
        tools, manifests = self.skills.catalog(set(ws.workflow_ids) if ws else None)
        job: dict[str, Any] = {
            "workspace": {"id": ws.id, "slug": ws.slug, "name": ws.name, "description": ws.description, "instructions": ws.instructions} if ws else None,
            "id": uuid.uuid4().hex,
            "status": "planning",
            "message": message,
            "target": "extension" if target == "extension" else "cloud",
            "calls": [],
            "items": [],
            "answer": None,
            "usage": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": False},
            "token_breakdown": {},
            "skills_considered": [],
            "question": None,
            "confirm_reason": None,
            "flagged": [],
            "notes": [],
            "llm_api_key": (llm_api_key or "").strip(),
            "llm_model": (llm_model or "").strip(),
        }
        self.jobs.put(job)
        await _emit(emit, {"type": "planning", "job_id": job["id"]})
        if not tools:
            job["status"] = "failed"
            job["error"] = (
                f"The workspace '{ws.name}' has no workflows yet." if ws else "No saved workflows are available to use as tools."
            )
            await _emit(emit, {"type": "error", "error": job["error"]})
            return job
        api_key = job["llm_api_key"] or env_api_key()
        if not api_key:
            job["status"] = "failed"
            job["error"] = "Chat planning needs an OpenRouter key (send llm.api_key or set OPENROUTER_API_KEY)."
            await _emit(emit, {"type": "error", "error": job["error"]})
            return job
        model = job["llm_model"] or None
        breakdown = job["token_breakdown"]

        # 1) Retrieve a short slate of skills; never the whole library.
        pool = self.skills.selection_pool(manifests) or manifests
        ranked = await self._retrieve(retrieval_query(message, history), pool, sub_queries(message))
        candidates = ranked["skills"]
        job["skills_considered"] = [m.get("slug") for m in candidates]
        for manifest in candidates:
            if not manifest.get("enriched") or manifest.get("stale"):
                self.skills.schedule_enrich(manifest["id"])
        job["retrieval"] = {"method": ranked["method"], "embed_tokens": ranked["embed_tokens"], "library": len(manifests), "scores": ranked["scores"]}
        await _emit(emit, {"type": "skills", "skills": job["skills_considered"], "method": ranked["method"], "job_id": job["id"]})

        # 2) Plan over compact cards.
        assistant, usage = await chat_complete(
            planner_messages(message, history, candidates, workspace=job["workspace"]),
            max_tokens=600,
            response_format={"type": "json_object"},
            api_key=api_key,
            model=model,
        )
        breakdown["plan"] = usage
        job["plan_usage"] = usage
        job["usage"] = merge_usage(job.get("usage"), usage)
        content = message_text(assistant)
        job["plan_raw"] = content[:3000]
        plan = parse_plan(content)
        steps: list[dict[str, Any]] = list((plan or {}).get("steps") or [])
        intents: list[dict[str, Any]] = list((plan or {}).get("intents") or [])
        reply = (plan or {}).get("answer")
        if plan is None:
            native = _parse_tool_calls(assistant, tools)
            steps = [{"id": f"s{i}", "skill": c["name"], "args": c.get("arguments") or {}, "after": []} for i, c in enumerate(native, 1)]
            reply = None if steps else content

        # 3) Check arguments against the full schemas of only the chosen skills; repair if needed.
        good: list[dict[str, Any]] = []
        bad: list[dict[str, Any]] = []
        chosen: list[dict[str, Any]] = []
        for step in steps:
            tool, issues = check_step(step, tools)
            (bad if issues else good).append(step)
            if tool is not None and tool not in chosen:
                chosen.append(tool)
        relevant = any((row.get("bm25") or 0) > 0 or (row.get("dense") or 0) >= 0.3 for row in ranked.get("scores") or [])
        # Repair broken steps, and never let the model answer from memory when a skill matches.
        if bad or (not steps and (plan is None or relevant)):
            slate = [lookup_tool(tools, b["skill"]) for b in bad] if bad else []
            slate = [t for t in slate if t] or [lookup_tool(tools, m.get("slug")) for m in candidates[:3]]
            slate = [t for t in slate if t][:4]
            if slate:
                try:
                    repaired, rusage, rtext = await repair_calls(
                        message, history, slate, api_key=api_key, model=model, parse_tool_calls=_parse_tool_calls, workspace=job["workspace"]
                    )
                    breakdown["repair"] = rusage
                    job["usage"] = merge_usage(job.get("usage"), rusage)
                    fixed = []
                    for index, step in enumerate(repaired, 1):
                        step["id"] = f"p{index}"
                        tool, issues = check_step(step, tools)
                        if tool is not None and not issues:
                            fixed.append(step)
                    if bad:
                        # Keep the planner's good steps; only replace the broken ones.
                        keep_skills = {g["skill"] for g in good}
                        fixed = [f for f in fixed if f["skill"] not in keep_skills or f["skill"] in {b["skill"] for b in bad}]
                    steps = good + fixed
                    if not steps and rtext:
                        reply = rtext
                except Exception as exc:
                    job["notes"].append(f"repair pass failed: {type(exc).__name__}")
                    steps = good
            else:
                steps = good
        steps = [s for s in steps if lookup_tool(tools, s["skill"]) is not None]

        # 3b) Intent coverage: every distinct ask must map to a call; re-plan only the gaps.
        missing_intents = uncovered_intents(intents, steps)
        if missing_intents:
            focus_ranked = await self._retrieve(" ".join(missing_intents), pool, missing_intents)
            slate = list(candidates)
            for manifest in focus_ranked["skills"]:
                if manifest not in slate:
                    slate.append(manifest)
            try:
                extra_msg, extra_usage = await chat_complete(
                    planner_messages(message, history, slate[:8], focus=missing_intents, workspace=job["workspace"]),
                    max_tokens=500,
                    response_format={"type": "json_object"},
                    api_key=api_key,
                    model=model,
                )
                breakdown["coverage"] = extra_usage
                job["usage"] = merge_usage(job.get("usage"), extra_usage)
                extra = parse_plan(message_text(extra_msg)) or {}
                seen = {(s["skill"], json.dumps(s.get("args") or {}, sort_keys=True, default=str)) for s in steps}
                added = []
                for index, step in enumerate(extra.get("steps") or [], 1):
                    old_id = step["id"]
                    step["id"] = f"r{index}"
                    step["after"] = [f"r{a[1:]}" if a.startswith("s") else a for a in step.get("after") or []]
                    tool, issues = check_step(step, tools)
                    key = (step["skill"], json.dumps(step.get("args") or {}, sort_keys=True, default=str))
                    if tool is None or issues or key in seen:
                        continue
                    seen.add(key)
                    step["_plan_id"] = old_id
                    added.append(step)
                steps.extend(added)
                added_ids = {s.pop("_plan_id") for s in added}
                extra_intents = extra.get("intents") or []
                if extra_intents:
                    covered = sum(1 for i in extra_intents if any(x in added_ids for x in i.get("steps") or []))
                else:
                    covered = len({s["skill"] for s in added})
                remaining = missing_intents[covered:]
                job["coverage_added"] = [s["skill"] for s in added]
            except Exception as exc:
                job["notes"].append(f"coverage re-plan failed: {type(exc).__name__}")
                remaining = missing_intents
            job["uncovered"] = remaining
        job["intents"] = intents
        if not steps:
            text = (reply or "").strip()
            if text and relevant:
                job["notes"].append("Answered without a tool call although a matching skill was retrieved.")
                job["answered_without_tools"] = True
            job["status"] = "succeeded" if text else "needs_input"
            job["answer"] = text or "I need a bit more detail to pick a workflow."
            job["question"] = None if text else job["answer"]
            await _emit(emit, {"type": "message", "text": job["answer"], "job_id": job["id"]})
            return job

        # 4) Bind to calls (c1..cn), keep dependencies.
        id_map = {s["id"]: f"c{i}" for i, s in enumerate(steps, 1)}
        missing: list[str] = []
        prepared: list[dict[str, Any]] = []
        action = False
        for index, step in enumerate(steps, 1):
            tool = lookup_tool(tools, step["skill"])
            stored = self.stored_for(tool)
            if stored is None:
                missing.append(f"Unknown workflow tool '{step['skill']}'.")
                continue
            args = dict(step.get("args") or {})
            for key, value in list(args.items()):
                if is_ref(value):
                    args[key] = {"from": id_map.get(str(value.get("from")), str(value.get("from"))), "pick": value.get("pick") or key}
            after = [id_map[a] for a in step.get("after") or [] if a in id_map]
            after += [v["from"] for v in args.values() if is_ref(v) and v["from"] not in after]
            ends = is_action_ending(stored.definition)
            action = action or ends
            prepared.append(
                {
                    "id": f"c{index}",
                    "tool": tool["function"]["name"],
                    "workflow_id": stored.id,
                    "workflow_name": stored.definition.name,
                    "arguments": args,
                    "after": after,
                    "action_ending": ends,
                    "definition": stored.definition.model_dump(exclude_none=True) if job["target"] == "extension" else None,
                    "_plan_step": step["id"],
                }
            )
        prepared = fan_out_calls(prepared, self.store, job["notes"])
        # The extension runs one dependency layer per round trip; the server keeps the rest.
        job["calls"] = [c for c in prepared if not c.get("after")] if job["target"] == "extension" else prepared
        if prepared:
            definitions = {}
            for call in prepared:
                try:
                    stored = self.store.get_workflow(call["workflow_id"])
                except Exception:
                    stored = None
                if stored is not None:
                    definitions[stored.id] = stored.definition
            ready = [c for c in prepared if not any(is_ref(v) for v in (c.get("arguments") or {}).values())]
            try:
                user_words = " ".join(
                    [message]
                    + [str(t.get("content") or "")[:300] for t in (history or [])[-4:] if t.get("role") in {"user", "assistant"}]
                    + [str((job.get("workspace") or {}).get("instructions") or "")[:300]]
                )
                norm_usage = await normalize_prepared_calls(ready, definitions=definitions, user_words=user_words)
                breakdown["normalize"] = norm_usage
                job["usage"] = merge_usage(job.get("usage"), norm_usage)
            except Exception:
                pass
            anchor_relative_dates(ready, definitions, message, job["notes"])
            resolve_prepared_defaults(ready, definitions)
            extra_views = default_view_companions(ready, definitions, message)
            if extra_views:
                prepared.extend(extra_views)
                ready.extend(extra_views)
                job["notes"].append("Also fetched the page's default view for today: a filtered view can leave out today's items.")
            # Entity-kind mismatch (a competition sent to a club input): never call the tool with
            # the wrong kind of value; the intent is reported as not covered instead.
            dropped = [c for c in prepared if c.get("kind_mismatch") or c.get("unstated")]
            if dropped:
                step_intent = {sid: i.get("ask") for i in intents for sid in i.get("steps") or []}
                uncovered = list(job.get("uncovered") or [])
                for call in dropped:
                    ask = step_intent.get(call.get("_plan_step")) or message
                    if call.get("kind_mismatch"):
                        mm = call["kind_mismatch"][0]
                        reason = (
                            f"{ask} (not covered: '{mm['value']}' is {mm['value_kind'] or 'a different kind of thing'}, "
                            f"but {call['tool']}.{mm['param']} expects {mm['expects']})"
                        )
                    else:
                        mm = call["unstated"][0]
                        reason = (
                            f"{ask} (not covered: {call['tool']} needs a specific {mm['param']} ({mm['expects']}) "
                            f"and the request did not name one; '{mm['value']}' would have been a guess)"
                        )
                    uncovered.append(reason)
                    job["notes"].append(("kind mismatch: " if call.get("kind_mismatch") else "unstated value: ") + reason)
                job["uncovered"] = uncovered
                gone = {c["id"] for c in dropped}
                prepared = [c for c in prepared if c["id"] not in gone and not (set(c.get("after") or []) & gone)]
            # Ask only after normalisation and resolved defaults (runtime tokens, trusted defaults).
            for call in prepared:
                if any(is_ref(v) for v in (call.get("arguments") or {}).values()):
                    continue
                definition = definitions.get(call["workflow_id"])
                if definition is None:
                    continue
                absent = missing_required_inputs(definition, call.get("arguments") or {})
                if absent:
                    missing.append(f"{call['workflow_name']} is missing required input(s): {', '.join(absent)}.")
                problems = invalid_typed_inputs(definition, call.get("arguments") or {})
                if problems:
                    missing.append(f"{call['workflow_name']}: " + "; ".join(problems) + ".")
        for call in prepared:
            call.pop("_plan_step", None)
        job["all_calls"] = prepared
        # The extension runs one dependency layer per round trip; the server keeps the rest.
        job["calls"] = [c for c in prepared if not c.get("after")] if job["target"] == "extension" else prepared
        if not prepared and not missing:
            job["status"] = "succeeded"
            job["declined"] = True
            job["answer"] = "That isn't covered by the tools in this workspace: " + "; ".join(job.get("uncovered") or [message]) + "."
            if reply:
                job["answer"] = f"{reply.strip()}\n\n{job['answer']}"
            await _emit(emit, {"type": "message", "text": job["answer"], "job_id": job["id"]})
            return job
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
        """Run the plan in dependency layers; calls inside a layer fan out in parallel."""

        job["status"] = "running"
        await _emit(emit, {"type": "running", "job_id": job["id"], "total": len(job.get("calls") or [])})
        sem = asyncio.Semaphore(self.concurrency)
        results: dict[str, dict[str, Any]] = {}
        api_key = (job.get("llm_api_key") or "").strip() or env_api_key()

        async def _one(call: dict[str, Any]) -> None:
            async with sem:
                failed = [a for a in call.get("after") or [] if a in results and results[a].get("status") != "succeeded"]
                if failed:
                    item = {
                        "id": call["id"],
                        "tool": call["tool"],
                        "workflow_id": call["workflow_id"],
                        "workflow_name": call["workflow_name"],
                        "inputs": {k: v for k, v in (call.get("arguments") or {}).items() if not is_ref(v)},
                        "status": "failed",
                        "error": f"Skipped because {', '.join(failed)} did not succeed.",
                        "action_ending": bool(call.get("action_ending")),
                    }
                    job.setdefault("flagged", []).append({"id": item["id"], "workflow": item["workflow_name"], "inputs": item["inputs"], "verdict": "failed", "reason": item["error"]})
                    await _emit(emit, {"type": "item_done", "item": public_item(item), "job_id": job["id"]})
                    results[call["id"]] = item
                    return
                if any(is_ref(v) for v in (call.get("arguments") or {}).values()):
                    try:
                        args, usage = await resolve_refs(call, results, api_key=api_key, model=job.get("llm_model") or None)
                        job["usage"] = merge_usage(job.get("usage"), usage)
                        job["token_breakdown"]["refs"] = merge_usage((job.get("token_breakdown") or {}).get("refs"), usage)
                    except Exception:
                        args = {k: v for k, v in (call.get("arguments") or {}).items() if not is_ref(v)}
                    call["arguments"] = args
                    call["raw_arguments"] = dict(args)
                    call["normalized"] = False
                results[call["id"]] = await self._run_call(job, call, emit=emit)

        for layer in waves(job.get("calls") or []):
            await asyncio.gather(*[_one(call) for call in layer], return_exceptions=False)
        job["items"] = [results[c["id"]] for c in job.get("calls") or [] if c["id"] in results]
        return job

    async def _run_call(self, job: dict[str, Any], call: dict[str, Any], *, emit: Emit | None = None) -> dict[str, Any]:
        stored = None
        try:
            stored = self.store.get_workflow(call["workflow_id"])
        except Exception:
            stored = None
        if not call.get("normalized"):
            try:
                if stored is not None:
                    usage = await normalize_prepared_calls(
                        [call],
                        definitions={stored.id: stored.definition},
                    )
                    job["usage"] = merge_usage(job.get("usage"), usage)
            except Exception:
                call.setdefault("arguments", dict(call.get("raw_arguments") or call.get("arguments") or {}))
        if stored is not None:
            resolve_prepared_defaults([call], {stored.id: stored.definition})
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
                check_inputs = inputs_for_output_validation(
                    item["inputs"],
                    definition=stored.definition,
                    raw_inputs=item.get("raw_inputs"),
                )
                run = await self.runner.run(
                    stored,
                    cdp_url=isolated.cdp_url,
                    variables=item["inputs"],
                    llm=None,
                    write_back_repairs=False,
                    request_text=(
                        f"Check that this {call['workflow_name']} result matches the inputs {check_inputs}. "
                        "Do not require omitted optional inputs, placeholders such as current, or runtime defaults "
                        "(for example the current month) to appear in the extract. "
                        "Do not require this single call to answer a comparison across other teams or workflows."
                    ),
                    validate_output=True,
                    raw_inputs=item.get("raw_inputs"),
                    # Nobody can answer a confirmation inside a chat turn: fail fast, never hang.
                    allow_hitl=False,
                )
                item["run_id"] = run.id
                if getattr(run, "fallbacks", None):
                    item["needs_repair"] = True
                    item["fallbacks"] = list(run.fallbacks)
                item["outputs"] = run.outputs or {}
                item["url"] = _url_from_run(run)
                item["validation"] = getattr(run, "validation", None) or (run.model_dump().get("validation") if hasattr(run, "model_dump") else None)
                if isinstance(run, dict):
                    item["validation"] = run.get("validation")
                usage = _run_validation_usage(item.get("validation"))
                job["usage"] = merge_usage(job.get("usage"), usage)
                tb = job.setdefault("token_breakdown", {})
                tb["validation"] = merge_usage(tb.get("validation"), usage)
                sample = collect_sample(item.get("outputs"), limit=FACTS_LIMIT)
                chrome_only = is_nav_footer_only(sample, action_ending=bool(item.get("action_ending")))
                if chrome_only:
                    reason = "The extract contains only navigation or footer text, not the requested content."
                    validation = dict(item.get("validation") or {})
                    validation.update({"verdict": "wrong", "reason": reason, "source": "deterministic"})
                    item["validation"] = validation
                    last_error = reason
                if run.status in {"succeeded", "succeeded_with_fallback"} and not chrome_only:
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
                        if stored is not None:
                            resolve_prepared_defaults([call], {stored.id: stored.definition})
                            alt = dict(call.get("arguments") or alt)
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
        items_all = job.get("items") or []
        # Share one facts budget across items: a single month listing is not cut to 800 chars,
        # several items still fit the synthesis prompt.
        per_item = max(600, min(3000, SYNTH_FACTS_BUDGET // max(1, len(items_all))))
        compact = [compact_item(item, fact_limit=per_item) for item in items_all]
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
        for ask in job.get("uncovered") or []:
            job["flagged"].append({"id": None, "workflow": None, "inputs": {}, "verdict": "not_run", "reason": f"No skill call was planned for: {ask}"})
        for item in compact:
            if item.get("status") == "succeeded" and item.get("data_status") == "no_data":
                job["flagged"].append(
                    {
                        "id": item["id"],
                        "workflow": item["workflow"],
                        "inputs": item["inputs"],
                        "verdict": "no_data",
                        "reason": "The page showed no data for this request, so nothing could be verified.",
                    }
                )
            if item.get("needs_repair"):
                job.setdefault("notes", []).append(
                    f"{item.get('workflow')}: a step only worked through a fallback selector; the workflow needs repair."
                )
        fallback = _fallback_answer(job, compact)
        api_key = (job.get("llm_api_key") or "").strip() or env_api_key()
        has_data = any(item.get("status") == "succeeded" and item.get("data_status") == "ok" for item in compact)
        if not has_data:
            # Nothing to summarise from: never let a model fill the gap.
            api_key = ""
        if not api_key:
            job["answer"] = fallback
            job["status"] = _status_from_items(compact, job["flagged"])
            if job["status"] == "succeeded" and job.get("uncovered"):
                job["status"] = "partial"
            if job["status"] != "succeeded" and not job.get("error"):
                job["error"] = (job["flagged"][0].get("reason") if job["flagged"] else "One or more workflow items failed.")
            await _emit(emit, {"type": "answer", "text": job["answer"], "job_id": job["id"]})
            return job
        try:
            synth_messages = [
                    {
                        "role": "system",
                        "content": (
                            "Adapt the workflow outputs to the user's actual question. "
                            "Merge, dedupe, sort and compare across calls. "
                            "Lead with a direct answer, then a compact markdown table when comparing or listing several items. "
                            "Use the user's language and format. "
                            "Cite which workflow and input each fact came from (for example football_fixtures / team=Arsenal). "
                            "Clearly mention any item the output validator flagged as suspect or wrong, that failed, or that was not run (verdict not_run). "
                            "State only facts present in results[].facts: inputs are what was requested, not what the page showed. "
                            "data_status no_data means the page showed nothing; say the data was not available or could not be verified, "
                            "never conclude that something does not exist. "
                            "Dates: use today and dates_in_question as given; never assume another current date. "
                            "Report the page's data as it is; never second-guess it from your own knowledge (for example who plays in a league). "
                            "When the question asks about one day and the results cover a longer period, answer for that day only. "
                            "Do not dump raw extracts. Keep the answer tight."
                            + workspace_context(job.get("workspace"))
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"question": job.get("message"), **date_context(str(job.get("message") or "")),
                             "results": compact, "flagged": job.get("flagged") or []},
                            ensure_ascii=False,
                        )[:9000],
                    },
                ]
            message, usage = await chat_complete(
                synth_messages,
                max_tokens=500,
                api_key=api_key,
                model=(job.get("llm_model") or None),
            )
            job["usage"] = merge_usage(job.get("usage"), usage)
            job["synthesis_usage"] = usage
            job.setdefault("token_breakdown", {})["synthesis"] = usage
            text = message_text(message)
            full_facts = "\n".join(str(i.get("facts") or "") for i in job.get("items") or [])
            problems = ungrounded_claims(text, compact, str(job.get("message") or ""), full_facts)
            if problems and text:
                # One rewrite, told exactly what was unsupported; then re-check.
                try:
                    retry_msg, retry_usage = await chat_complete(
                        [
                            synth_messages[0],
                            synth_messages[1],
                            {"role": "assistant", "content": text[:1500]},
                            {
                                "role": "user",
                                "content": "These are not in results[].facts: " + "; ".join(problems[:8])
                                + ". Rewrite the answer using only facts that appear in results[].facts.",
                            },
                        ],
                        max_tokens=500,
                        api_key=api_key,
                        model=(job.get("llm_model") or None),
                    )
                    job["usage"] = merge_usage(job.get("usage"), retry_usage)
                    job["token_breakdown"]["synthesis_retry"] = retry_usage
                    retry_text = message_text(retry_msg)
                    if retry_text and not ungrounded_claims(retry_text, compact, str(job.get("message") or ""), full_facts):
                        text, problems = retry_text, []
                except Exception:
                    pass
            if problems:
                logger.warning("grounding rejected draft for job %s: %s | draft=%r", job.get("id"), problems[:10], (text or "")[:800])
                job["grounding"] = {"ok": False, "ungrounded": problems[:10]}
                job.setdefault("notes", []).append(
                    "The summary was replaced with the raw results: it stated things not found in the extracts ("
                    + ", ".join(problems[:5]) + ")."
                )
                text = ""
            else:
                job["grounding"] = {"ok": True}
            job["answer"] = text or fallback
        except Exception as exc:
            job["answer"] = fallback
            job["synthesis_error"] = type(exc).__name__
        job["status"] = _status_from_items(compact, job["flagged"])
        if job["status"] == "succeeded" and job.get("uncovered"):
            job["status"] = "partial"
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
        "token_breakdown": job.get("token_breakdown") or {},
        "skills_considered": job.get("skills_considered") or [],
        "retrieval": {k: v for k, v in (job.get("retrieval") or {}).items() if k != "scores"} or None,
        "notes": job.get("notes") or [],
        "intents": job.get("intents") or [],
        "workspace": {k: v for k, v in (job.get("workspace") or {}).items() if k in {"id", "slug", "name"}} or None,
        "uncovered": job.get("uncovered") or [],
        "grounding": job.get("grounding"),
        "declined": bool(job.get("declined")),
        "needs_repair": any(bool(item.get("needs_repair")) for item in job.get("items") or []),
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
            "after": call.get("after") or [],
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
        "needs_repair": bool(item.get("needs_repair")),
        "fallbacks": item.get("fallbacks") or [],
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
    ok = [item for item in compact if item.get("status") == "succeeded" and item.get("data_status") != "no_data"]
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

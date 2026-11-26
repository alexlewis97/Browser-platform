"""Talk to official stock Pomerado MCP (`pomerado-mcp mint`) and run published artifacts.

Minting uses Pomerado's default OpenAI models and hosted tools. The key is the
server's OPENAI_API_KEY (never taken from the request, never logged). Runs make
no model request. Browser sessions attach to a dedicated persistent Chromium,
not the user's Chrome.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from browser_platform import DEFAULT_MODEL, OPENROUTER_BASE_URL
from browser_platform.models.llm import LLMConfig
from browser_platform.pomerado.inputs import auto_answers_for_request
from browser_platform.pomerado.keys import looks_like_live_key
from browser_platform.pomerado.mcp_client import McpClient, McpError
from browser_platform.pomerado.register import integration_slug, register_published_tool
from browser_platform.pomerado.trace import (
    MintJobStore,
    is_live,
    is_terminal,
    looks_unknown_job,
    make_step,
    notification_step,
    stderr_step,
)
from browser_platform.pomerado.usage import OFFICIAL_GUARDIAN_MODEL, OFFICIAL_MINTER_MODEL
from browser_platform.redaction import redact

STOCK_MCP = "stock-mcp.mjs"
STOCK_RUN = "stock-run.mjs"
OPENAI_API_BASE = "https://api.openai.com/v1"


class PomeradoAdapter:
    def __init__(
        self,
        host_path: Path,
        *,
        root: Path,
        cdp_url: str | None = None,
        endpoint: str | None = None,
        model: str = DEFAULT_MODEL,
        store=None,
        openai_base_url: str | None = None,
        usage_tracker=None,
        event_hub=None,
        jobs_dir: Path | None = None,
    ):
        self.host_path = Path(host_path)
        self.root = Path(root)
        # Official Pomerado refuses CDP; sessions attach via Playwright --endpoint.
        self.endpoint = endpoint or cdp_url
        self.cdp_url = None
        self.model = model
        self.store = store
        self.openai_base_url = openai_base_url
        self.usage_tracker = usage_tracker
        self.event_hub = event_hub
        self.jobs = MintJobStore(Path(jobs_dir) if jobs_dir else self.root.parent / "pomerado_jobs")
        self._client: McpClient | None = None
        self._lock = asyncio.Lock()
        self._requests: dict[str, dict[str, Any]] = {}
        self._monitors: dict[str, asyncio.Task] = {}

    def _stock_mcp_path(self) -> Path:
        path = self.host_path
        if path.name == STOCK_MCP and path.is_file():
            return path
        candidate = path.parent / STOCK_MCP
        if candidate.is_file():
            return candidate
        # Fall back to the given path (tests may point at a stub).
        return path

    def _stock_run_path(self) -> Path:
        return self._stock_mcp_path().with_name(STOCK_RUN)

    def _jail_directory(self, directory: str | Path | None) -> Path:
        from browser_platform.pomerado.register import jail_integration_directory

        return Path(jail_integration_directory(str(directory or ""), self.root))

    def _command(self) -> list[str]:
        return ["node", str(self._stock_mcp_path()), "mint", "--root", str(self.root)]

    def _use_server_openai(self) -> bool:
        pref = (os.environ.get("POMERADO_PROVIDER") or "").strip().lower()
        if pref == "openrouter":
            return False
        return looks_like_live_key(os.environ.get("OPENAI_API_KEY") or "")

    def _env(self, llm: LLMConfig | None = None) -> dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if "KEY" not in k.upper() and "TOKEN" not in k.upper() and "SECRET" not in k.upper()
        }
        env["PATH"] = os.environ.get("PATH", "")
        env["HOME"] = os.environ.get("HOME", "")
        env["POMERADO_ROOT"] = str(self.root)
        if self.endpoint:
            env["POMERADO_BROWSER_ENDPOINT"] = self.endpoint
        if self._use_server_openai():
            env["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
            env["OPENAI_BASE_URL"] = self.openai_base_url or OPENAI_API_BASE
            env["POMERADO_PROVIDER"] = os.environ.get("POMERADO_PROVIDER") or "openai"
        else:
            # Stock Pomerado minting requires a server OpenAI key. Do not copy a
            # request-scoped OpenRouter key into OPENAI_API_KEY.
            env["OPENROUTER_BASE_URL"] = OPENROUTER_BASE_URL
            if llm is not None:
                env["OPENROUTER_API_KEY"] = llm.key()
        cap = os.environ.get("POMERADO_OPENAI_SPEND_CAP")
        if cap:
            env["POMERADO_OPENAI_SPEND_CAP"] = cap
        return env

    async def close(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.close()
                self._client = None

    async def _ensure_client(self, llm: LLMConfig | None) -> McpClient:
        if self._client is not None:
            return self._client
        cwd = str(self._stock_mcp_path().parent.parent)  # node_host/
        self.root.mkdir(parents=True, exist_ok=True)
        client = McpClient(self._command(), env=self._env(llm), cwd=cwd)
        client.add_notification_listener(self._on_mcp_notification)
        client.add_stderr_listener(self._on_mcp_stderr)
        await client.start()
        self._client = client
        return client

    async def _call(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        llm: LLMConfig | None = None,
        timeout: float = 120,
    ) -> Any:
        async with self._lock:
            client = await self._ensure_client(llm)
            return await client.call_tool(name, arguments, timeout=timeout)

    async def capabilities(self) -> dict[str, Any]:
        configured = self._use_server_openai()
        cdp = {
            "dedicated": True,
            "mode": "playwright-endpoint",
            "endpoint_configured": bool(self.endpoint),
            "healthy": bool(self.endpoint and self.endpoint.startswith("ws")),
            "passed_to_createPomerado": bool(self.endpoint),
            "user_chrome": False,
            "cdp_attach": False,
            "closeOtherTabs": False,
            "resetSiteData": "pomerado-managed (keep on run; clear only when the mint plan says so)",
        }
        version = _pomerado_version(self._stock_mcp_path())
        return {
            "pomerado": {
                "available": True,
                "createPomerado": True,
                "stock": True,
                "host": "pomerado-mcp",
                "version": version,
            },
            "provider": "openai" if configured else "unconfigured",
            "openai": {
                "configured": configured,
                "base_url": OPENAI_API_BASE,
                "minter_model": OFFICIAL_MINTER_MODEL,
                "guardian_model": OFFICIAL_GUARDIAN_MODEL,
                "responses_api": True,
                "hosted_tools": True,
                "key_source": "server_env",
            },
            "openrouter": {
                "base_url": OPENROUTER_BASE_URL,
                "used_for": ["browser-use", "repair", "from-task", "hitl"],
                "used_for_pomerado_mint": False,
            },
            "cdp": cdp,
            "host": "browser-platform-stock-pomerado",
        }

    async def cdp_health(self, cdp: str | None = None) -> dict[str, Any]:
        return {"ok": bool(self.endpoint), "endpoint_configured": bool(self.endpoint), "dedicated": True, "user_chrome": False, "cdp_attach": False}

    async def mint(
        self,
        *,
        name: str,
        url: str,
        intent: str,
        effect: str = "read",
        input_values: dict[str, Any] | None = None,
        llm: LLMConfig | None = None,
        wait_s: float = 0,
    ) -> dict[str, Any]:
        slug = integration_slug(name)
        authority = "write" if effect == "write" else "read"
        if not self._use_server_openai():
            return {
                "job_id": None,
                "status": "incomplete",
                "ok": False,
                "pomerado": True,
                "provider": "openai",
                "output": {
                    "build": "incomplete",
                    "reason": "no live server OPENAI_API_KEY; official Pomerado mint was not started",
                },
            }
        arguments: dict[str, Any] = {
            "name": slug,
            "url": url,
            "intent": intent,
            "effect": authority,
            "input": input_values or {},
        }
        # Never pass a request API key to stock Pomerado.
        started = await self._call("mint", arguments, llm=None)
        request = {"name": slug, "url": url, "intent": intent, "effect": authority, "input": input_values or {}}
        job = normalize_job(started, request=request)
        if job.get("job_id"):
            self._requests[str(job["job_id"])] = request
            self._begin_trace(str(job["job_id"]), request, job)
        self._maybe_register(job)
        job_id = job.get("job_id")
        if not job_id or wait_s <= 0:
            return self._with_trace(job, job_id)
        return await self.wait_for_job(job_id, wait_s=wait_s, known_inputs=input_values or {})

    async def wait_for_job(
        self,
        job_id: str,
        *,
        wait_s: float,
        llm: LLMConfig | None = None,
        known_inputs: dict[str, Any] | None = None,
        on_needs_input=None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"job_id": job_id, "status": "running"}
        deadline = time.monotonic() + wait_s
        last_request_id = None
        while time.monotonic() < deadline:
            result = await self.get_job(job_id)
            status = result.get("status") if isinstance(result, dict) else None
            if status in {"needs_input", "input_required"}:
                request = result.get("pending_input") or result.get("input_request") or {}
                request_id = request.get("id") or request.get("request_id")
                answers = None
                if request_id and request_id != last_request_id:
                    answers = auto_answers_for_request(request, known_inputs or {})
                    if answers is None and on_needs_input is not None:
                        maybe = on_needs_input(result)
                        if hasattr(maybe, "__await__"):
                            maybe = await maybe
                        answers = maybe if isinstance(maybe, dict) else None
                    if answers:
                        last_request_id = request_id
                        await self.provide_input(job_id, answers, request_id=str(request_id))
                await asyncio.sleep(1)
                continue
            if status and status not in {"running", "queued"}:
                return result
            await asyncio.sleep(2)
        return result

    async def get_job(self, job_id: str, llm: LLMConfig | None = None) -> dict[str, Any]:
        stored = self.jobs.get(job_id)
        if stored and is_terminal(stored.get("status")) and stored.get("steps"):
            return self.jobs.as_response(stored)
        if self._client is None and not (stored and is_live(stored.get("status"))):
            if stored:
                return self.jobs.as_response(stored)
            raise KeyError(job_id)
        try:
            wait_seconds = 20 if not (stored and is_terminal(stored.get("status"))) else 1
            raw = await self._call("get_job", {"job_id": job_id, "wait_seconds": wait_seconds}, llm=None, timeout=40)
        except McpError as exc:
            if stored:
                if is_live(stored.get("status")):
                    self._trace_event(
                        job_id,
                        make_step(
                            phase="error",
                            title="Lost contact with the mint process",
                            error=str(exc),
                            success=False,
                            source="live",
                        ),
                    )
                    self.jobs.merge_job(job_id, {"status": "failed", "ok": False, "error": "mint process is no longer available; trace may be partial"})
                    record = self.jobs.get(job_id)
                    if record:
                        record["partial"] = True
                        self.jobs.save(record)
                return self.jobs.as_response(self.jobs.get(job_id) or stored)
            raise KeyError(job_id) from exc
        if looks_unknown_job(raw, job_id):
            if stored:
                return self.jobs.as_response(stored)
            raise KeyError(job_id)
        job = normalize_job(raw, request=self._requests.get(str(job_id)) or ((stored or {}).get("request") if stored else None))
        if self.usage_tracker is not None and is_live(job.get("status")):
            job["usage"] = self.usage_tracker.snapshot()
        self._maybe_register(job)
        self._sync_trace(job_id, job, stored)
        return self._with_trace(job, job_id)

    def list_jobs(self) -> list[dict[str, Any]]:
        return self.jobs.list_jobs()

    async def provide_input(
        self,
        job_id: str,
        answers: dict[str, Any],
        llm: LLMConfig | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"job_id": job_id, "answers": answers}
        if request_id:
            arguments["request_id"] = request_id
        return normalize_job(await self._call("provide_input", arguments, llm=None))

    async def run_artifact(
        self,
        artifact: str,
        *,
        input_values: dict[str, Any] | None = None,
        tool: str | None = None,
        llm: LLMConfig | None = None,
        url: str | None = None,
        effect: str | None = None,
        intent: str | None = None,
        timeout: float = 180,
    ) -> dict[str, Any]:
        run_js = self._stock_run_path()
        if not run_js.is_file():
            return {"ok": False, "status": "failed", "error": f"missing {run_js}", "tokens_in": 0, "tokens_out": 0}
        try:
            artifact_path = self._jail_directory(artifact)
        except ValueError as exc:
            return {"ok": False, "status": "failed", "error": str(exc), "tokens_in": 0, "tokens_out": 0}
        env = self._env(None)
        # A run makes no model request; drop the mint key from the child just in case.
        env.pop("OPENAI_API_KEY", None)
        args = ["node", str(run_js), "--artifact", str(artifact_path), "--timeout-seconds", str(int(timeout))]
        if url:
            args.extend(["--url", url])
        if intent:
            args.extend(["--intent", intent])
        if input_values:
            args.extend(["--input", json.dumps(input_values)])
        started = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(run_js.parent.parent),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 30)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "status": "failed", "error": "pomerado run timed out", "latency_ms": (time.perf_counter() - started) * 1000, "tokens_in": 0, "tokens_out": 0}
        text = stdout.decode("utf-8", "replace").strip()
        try:
            result = json.loads(text.splitlines()[-1]) if text else {}
        except json.JSONDecodeError:
            err = stderr.decode("utf-8", "replace")[-500:]
            return {"ok": False, "status": "failed", "error": err or "pomerado run produced no JSON", "tokens_in": 0, "tokens_out": 0}
        if not isinstance(result, dict):
            result = {"output": result}
        result.setdefault("tokens_in", 0)
        result.setdefault("tokens_out", 0)
        result.setdefault("latency_ms", (time.perf_counter() - started) * 1000)
        result.setdefault("pomerado", True)
        return result

    def _maybe_register(self, job: dict[str, Any]) -> None:
        if self.store is None or not isinstance(job, dict):
            return
        output = job.get("output") or {}
        if output.get("build") != "published":
            return
        integration = output.get("integration") or {}
        directory = integration.get("directory") or job.get("directory")
        if not directory:
            return
        request = job.get("request") or {}
        try:
            jailed = str(self._jail_directory(directory))
            registered = register_published_tool(
                self.store,
                name=request.get("name") or Path(jailed).name,
                url=request.get("url") or (job.get("pomerado_input") or {}).get("url") or "",
                intent=request.get("intent") or "",
                effect=request.get("effect") or "read",
                directory=jailed,
                input_values=request.get("input") or {},
                mint_job_id=str(job.get("job_id") or "") or None,
                integrations_root=self.root,
            )
            job["workflow"] = registered
            job["directory"] = jailed
        except Exception:
            # Registration failure must not fail the mint job itself.
            job.setdefault("workflow_error", "failed to register workflow")



    def _begin_trace(self, job_id: str, request: dict[str, Any], job: dict[str, Any]) -> None:
        record = self.jobs.create(
            job_id,
            name=str(request.get("name") or job_id),
            url=str(request.get("url") or ""),
            intent=str(request.get("intent") or ""),
            effect=str(request.get("effect") or "read"),
            status=str(job.get("status") or "running"),
            extra={"request": request},
        )
        if not record.get("steps"):
            self.jobs.append_step(
                job_id,
                make_step(
                    phase="start",
                    title=f"Mint started for {request.get('name') or job_id}",
                    detail=f"URL {request.get('url') or ''}. Official Pomerado mint on the dedicated Chromium.",
                    source="live",
                    tier="pomerado",
                ),
                dedupe=False,
            )
        self._sync_trace(job_id, job, record)
        self._ensure_monitor(job_id)
        self._publish_event(job_id, "mint_updated")

    def _with_trace(self, job: dict[str, Any] | None, job_id: str | None) -> dict[str, Any]:
        payload = dict(job or {})
        if not job_id:
            return payload
        stored = self.jobs.get(job_id)
        if stored:
            return {**self.jobs.as_response(stored), **{k: v for k, v in payload.items() if k not in {"steps", "trace"} and v is not None}}
        return payload

    def _sync_trace(self, job_id: str, job: dict[str, Any], previous: dict[str, Any] | None) -> None:
        before = previous or self.jobs.get(job_id)
        merged = self.jobs.merge_job(job_id, job)
        prev_status = (before or {}).get("status")
        status = merged.get("status")
        if status and status != prev_status:
            phase = "publish" if status == "completed" or (job.get("output") or {}).get("build") == "published" else "phase"
            if status in {"failed", "error"}:
                phase = "error"
            elif status in {"needs_input", "input_required"}:
                phase = "phase"
            title = {
                "completed": "Mint completed",
                "needs_input": "Mint paused for input",
                "running": "Mint is running",
                "failed": "Mint failed",
                "error": "Mint failed",
            }.get(str(status), f"Status: {status}")
            request = job.get("input_request") or job.get("pending_input")
            detail = None
            if isinstance(request, dict):
                detail = request.get("prompt") or request.get("message") or request.get("question")
            error = job.get("error") or (job.get("output") or {}).get("publicationDenial") or job.get("publicationDenial")
            self.jobs.append_step(
                job_id,
                make_step(
                    phase=phase,
                    title=title,
                    detail=str(detail) if detail else None,
                    success=status not in {"failed", "error"},
                    error=str(error) if error and status in {"failed", "error"} else None,
                    source="live",
                    tier="guardian" if "guardian" in title.lower() or job.get("publicationDenial") else "pomerado",
                ),
            )
        output = job.get("output") or {}
        if output.get("build") == "published":
            last_phases = [s.get("phase") for s in (merged.get("steps") or [])]
            if "publish" not in last_phases:
                directory = output.get("integration", {}).get("directory") if isinstance(output.get("integration"), dict) else job.get("directory")
                workflow = job.get("workflow") or {}
                wf_bit = workflow.get("name") or workflow.get("id") or ""
                self.jobs.append_step(
                    job_id,
                    make_step(
                        phase="publish",
                        title="Build published and registered as a workflow",
                        detail=f"Artifact {directory or ''}{(' · ' + wf_bit) if wf_bit else ''}",
                        source="live",
                        tier="pomerado",
                    ),
                )
            denial = output.get("publicationDenial") or job.get("publicationDenial")
            if denial:
                self.jobs.append_step(
                    job_id,
                    make_step(phase="guardian", title="Guardian publication denial", error=str(denial), success=False, source="live", tier="guardian"),
                )
        self._ingest_artifacts(job_id, job)
        self._publish_event(job_id, "mint_finished" if is_terminal(status) else "mint_updated")

    def _ingest_artifacts(self, job_id: str, job: dict[str, Any]) -> None:
        directory = job.get("directory") or ((job.get("output") or {}).get("integration") or {}).get("directory")
        if not directory:
            name = (job.get("request") or {}).get("name") or job.get("name")
            if name:
                candidate = self.root / str(name)
                if candidate.is_dir():
                    directory = str(candidate)
        if not directory:
            return
        root = Path(directory)
        if not root.is_dir():
            return
        record = self.jobs.get(job_id) or {}
        seen = set(record.get("seen_artifacts") or [])
        interesting = [
            "pomerado.json",
            "deployment.json",
            "mcp.json",
            "README.md",
            "src/tool.mjs",
        ]
        new_files: list[str] = []
        for rel in interesting:
            path = root / rel
            key = str(path)
            if path.is_file() and key not in seen:
                seen.add(key)
                new_files.append(rel)
        if not new_files:
            return
        record = self.jobs.get(job_id) or record
        record["seen_artifacts"] = sorted(seen)
        record["directory"] = str(root)
        self.jobs.save(record)
        self.jobs.append_step(
            job_id,
            make_step(
                phase="artifact",
                title="Pomerado wrote integration files",
                detail=", ".join(new_files),
                source="artifact",
                tier="pomerado",
            ),
        )

    def _on_mcp_notification(self, message: dict[str, Any]) -> None:
        job_id, step = notification_step(message)
        self._trace_event(job_id, step)

    def _on_mcp_stderr(self, line: str) -> None:
        self._trace_event(None, stderr_step(line))

    def _trace_event(self, job_id: str | None, step: dict[str, Any] | None) -> None:
        if not step:
            return
        targets = [job_id] if job_id else [jid for jid, rec in self._running_records()]
        if not targets:
            return
        for target in targets:
            if not target:
                continue
            try:
                self.jobs.append_step(target, step)
                self._publish_event(target, "mint_step")
            except Exception:
                continue

    def _running_records(self) -> list[tuple[str, dict[str, Any]]]:
        out = []
        for row in self.jobs.list_jobs():
            if is_live(row.get("status")):
                full = self.jobs.get(str(row["job_id"]))
                if full:
                    out.append((str(row["job_id"]), full))
        return out

    def _ensure_monitor(self, job_id: str) -> None:
        task = self._monitors.get(job_id)
        if task is not None and not task.done():
            return
        try:
            self._monitors[job_id] = asyncio.create_task(self._monitor_job(job_id))
        except RuntimeError:
            return

    async def _monitor_job(self, job_id: str) -> None:
        try:
            while True:
                job = await self.get_job(job_id)
                if is_terminal(job.get("status")):
                    return
                await asyncio.sleep(2)
        except Exception:
            return

    def _publish_event(self, job_id: str, typ: str) -> None:
        hub = self.event_hub
        if hub is None:
            return
        record = self.jobs.get(job_id)
        event = {
            "type": typ,
            "job_id": job_id,
            "run_id": job_id,
            "status": (record or {}).get("status"),
            "partial": bool((record or {}).get("partial")),
            "step_count": len((record or {}).get("steps") or []),
        }
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(hub.publish(event))



def normalize_job(raw: Any, request: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"status": "failed", "error": "invalid job payload", "pomerado": True}
    job = dict(raw)
    job["pomerado"] = True
    if request:
        job.setdefault("request", request)
    status = job.get("status")
    if status == "input_required":
        job["status"] = "needs_input"
    if job.get("pending_input") and not job.get("input_request"):
        job["input_request"] = job["pending_input"]
    output = job.get("output") or {}
    if isinstance(output, dict):
        integration = output.get("integration") or {}
        if integration.get("directory"):
            job["directory"] = integration["directory"]
        if output.get("build") == "published":
            job["ok"] = True
        elif "ok" not in job and output.get("build"):
            job["ok"] = False
    return job


def _pomerado_version(host_path: Path) -> str:
    pkg = host_path.parent.parent / "node_modules" / "pomerado" / "package.json"
    try:
        return json.loads(pkg.read_text()).get("version") or "installed"
    except Exception:
        return "installed"


__all__ = ["PomeradoAdapter", "McpError", "redact", "normalize_job"]

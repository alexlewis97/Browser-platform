"""Per-job Pomerado mint traces: persist, redact, backfill, and summarize."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from browser_platform.redaction import redact, redact_text

JERUSALEM = ZoneInfo("Asia/Jerusalem")

LIVE_STATUSES = frozenset({"running", "queued", "needs_input", "input_required"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "error", "cancelled", "canceled", "incomplete"})

WIKIPEDIA_JOB_ID = "ea496208-20c5-49fa-8326-90c59803f7bd"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_NOISY_STDERR = re.compile(
    r"^(npm |node:|DeprecationWarning|ExperimentalWarning|\s*$)",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_live(status: str | None) -> bool:
    return (status or "").lower() in LIVE_STATUSES


def is_terminal(status: str | None) -> bool:
    return (status or "").lower() in TERMINAL_STATUSES


def safe_job_id(job_id: str) -> str:
    text = (job_id or "").strip()
    if not text or not _SAFE_ID.match(text) or len(text) > 120:
        raise ValueError("invalid job id")
    return text


def make_step(
    *,
    phase: str,
    title: str,
    detail: str | None = None,
    success: bool | None = True,
    error: str | None = None,
    source: str = "live",
    ts: str | None = None,
    tool: str | None = None,
    tier: str | None = None,
    latency_ms: float | None = None,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "ts": ts or utc_now(),
        "phase": phase,
        "step_type": phase,
        "title": redact_text(title or phase),
        "description": redact_text(title or phase),
        "success": False if error else (True if success is None else bool(success)),
        "source": source,
    }
    if detail:
        step["detail"] = redact_text(str(detail))[:2000]
    if error:
        step["error"] = redact_text(str(error))[:2000]
        step["success"] = False
    if tool:
        step["tool"] = redact_text(str(tool))[:200]
    if tier:
        step["tier"] = tier
    if latency_ms is not None:
        step["latency_ms"] = float(latency_ms)
    return redact(step)


def _fingerprint(step: dict[str, Any]) -> tuple[Any, ...]:
    return (
        step.get("phase"),
        step.get("title") or step.get("description"),
        step.get("detail"),
        step.get("error"),
        step.get("tool"),
    )


class MintJobStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path(self, job_id: str) -> Path:
        return self.root / f"{safe_job_id(job_id)}.json"

    def get(self, job_id: str) -> dict[str, Any] | None:
        path = self.path(job_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def list_jobs(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in self.root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and data.get("job_id"):
                items.append(self.summary(data))
        items.sort(key=lambda row: str(row.get("started_at") or ""), reverse=True)
        return items

    def save(self, record: dict[str, Any]) -> dict[str, Any]:
        job_id = safe_job_id(str(record.get("job_id") or ""))
        clean = redact(dict(record))
        clean["job_id"] = job_id
        path = self.path(job_id)
        tmp = path.with_suffix(".json.tmp")
        with self._lock:
            tmp.write_text(json.dumps(clean, indent=2, default=str) + "\n", encoding="utf-8")
            tmp.replace(path)
        return clean

    def create(
        self,
        job_id: str,
        *,
        name: str,
        url: str,
        intent: str,
        effect: str = "read",
        status: str = "running",
        source: str = "live",
        started_at: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = self.get(job_id)
        if existing is not None:
            return existing
        record: dict[str, Any] = {
            "job_id": job_id,
            "name": name,
            "url": url,
            "intent": intent,
            "effect": effect,
            "status": status,
            "ok": None,
            "directory": None,
            "workflow": None,
            "started_at": started_at or utc_now(),
            "finished_at": None,
            "partial": source == "backfill",
            "trace_source": source,
            "error": None,
            "request": {"name": name, "url": url, "intent": intent, "effect": effect},
            "steps": [],
        }
        if extra:
            record.update(extra)
        return self.save(record)

    def append_step(self, job_id: str, step: dict[str, Any], *, dedupe: bool = True) -> dict[str, Any]:
        record = self.get(job_id)
        if record is None:
            record = self.create(job_id, name=job_id, url="", intent="", source=str(step.get("source") or "live"))
        steps = list(record.get("steps") or [])
        clean = make_step(
            phase=str(step.get("phase") or step.get("step_type") or "log"),
            title=str(step.get("title") or step.get("description") or step.get("phase") or "event"),
            detail=step.get("detail"),
            success=step.get("success", True if not step.get("error") else False),
            error=step.get("error"),
            source=str(step.get("source") or record.get("trace_source") or "live"),
            ts=step.get("ts"),
            tool=step.get("tool"),
            tier=step.get("tier"),
            latency_ms=step.get("latency_ms"),
        )
        if dedupe and steps and _fingerprint(steps[-1]) == _fingerprint(clean):
            return record
        steps.append(clean)
        record["steps"] = steps
        return self.save(record)

    def merge_job(self, job_id: str, job: dict[str, Any]) -> dict[str, Any]:
        record = self.get(job_id) or self.create(
            job_id,
            name=str((job.get("request") or {}).get("name") or job.get("name") or job_id),
            url=str((job.get("request") or {}).get("url") or job.get("url") or ""),
            intent=str((job.get("request") or {}).get("intent") or job.get("intent") or ""),
            effect=str((job.get("request") or {}).get("effect") or job.get("effect") or "read"),
        )
        request = job.get("request") if isinstance(job.get("request"), dict) else {}
        for key in ("name", "url", "intent", "effect", "directory"):
            value = job.get(key) or request.get(key)
            if value and not record.get(key):
                record[key] = value
        status = job.get("status")
        if status:
            record["status"] = status
        if "ok" in job:
            record["ok"] = job.get("ok")
        if job.get("workflow"):
            record["workflow"] = job["workflow"]
        if job.get("error"):
            record["error"] = redact_text(str(job["error"]))
        if job.get("output"):
            record["output"] = redact(job["output"])
        if is_terminal(record.get("status")) and not record.get("finished_at"):
            record["finished_at"] = utc_now()
        return self.save(record)

    def summary(self, record: dict[str, Any]) -> dict[str, Any]:
        steps = record.get("steps") or []
        workflow = record.get("workflow") or {}
        return {
            "job_id": record.get("job_id"),
            "name": record.get("name"),
            "url": record.get("url"),
            "intent": record.get("intent"),
            "status": record.get("status"),
            "ok": record.get("ok"),
            "partial": bool(record.get("partial")),
            "trace_source": record.get("trace_source"),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "step_count": len(steps),
            "workflow_id": workflow.get("id") if isinstance(workflow, dict) else None,
            "workflow_name": workflow.get("name") if isinstance(workflow, dict) else None,
            "error": record.get("error"),
        }

    def as_response(self, record: dict[str, Any]) -> dict[str, Any]:
        steps = list(record.get("steps") or [])
        payload = dict(record)
        payload["trace"] = {
            "steps": steps,
            "partial": bool(record.get("partial")),
            "source": record.get("trace_source") or "live",
        }
        payload["pomerado"] = True
        return redact(payload)


def looks_unknown_job(raw: Any, job_id: str) -> bool:
    if not isinstance(raw, dict):
        return True
    err = str(raw.get("error") or raw.get("message") or raw.get("hostFailure") or "").lower()
    if "not found" in err or "unknown job" in err:
        return True
    returned_id = str(raw.get("job_id") or raw.get("id") or "")
    if returned_id and returned_id != job_id:
        return True
    status = raw.get("status")
    if status in {None, ""} and not raw.get("output") and not raw.get("directory"):
        return True
    return False


def notification_step(message: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    if not isinstance(message, dict):
        return None, None
    method = str(message.get("method") or "")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    job_id = params.get("job_id") or params.get("jobId")
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    job_id = job_id or meta.get("job_id") or meta.get("jobId")
    if method in {"notifications/progress", "notifications/progress/update"}:
        title = str(params.get("message") or params.get("status") or "MCP progress")
        total = params.get("total")
        progress = params.get("progress")
        detail = None
        if progress is not None:
            detail = f"{progress}/{total}" if total is not None else str(progress)
        return job_id, make_step(phase="mcp", title=title, detail=detail, source="mcp")
    if method in {"notifications/message", "logging/message", "notifications/stderr"}:
        data = params.get("data") if "data" in params else params.get("message") or params
        text = data if isinstance(data, str) else json.dumps(data, default=str)
        level = str(params.get("level") or "info")
        phase = "error" if level.lower() in {"error", "err"} else "log"
        return job_id, make_step(phase=phase, title=text[:240], source="mcp", success=phase != "error")
    if method.startswith("notifications/"):
        title = method.split("/", 1)[-1].replace("_", " ")
        detail = json.dumps(params, default=str)[:500] if params else None
        return job_id, make_step(phase="mcp", title=title, detail=detail, source="mcp")
    return job_id, None


def stderr_step(line: str) -> dict[str, Any] | None:
    text = redact_text((line or "").rstrip())
    if not text or _NOISY_STDERR.match(text) or len(text) < 4:
        return None
    lower = text.lower()
    phase = "log"
    success: bool | None = True
    error = None
    if any(word in lower for word in ("guardian", "publicationdenial", "publication denial")):
        phase = "guardian"
        if any(word in lower for word in ("denial", "denied", "reject")):
            success = False
            error = text
    elif any(word in lower for word in ("publish", "published", "artifact")):
        phase = "publish"
    elif any(word in lower for word in ("error", "exception", "failed", "fatal")):
        phase = "error"
        success = False
        error = text
    elif any(word in lower for word in ("click", "navigate", "goto", "fill", "type ", "scroll", "browser")):
        phase = "browser"
    elif any(word in lower for word in ("tool", "apply_patch", "hosted")):
        phase = "tool"
    elif any(word in lower for word in ("gpt-", "model", "minter")):
        phase = "model"
    return make_step(phase=phase, title=text[:240], success=success, error=error, source="stderr")


def parse_jerusalem(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    for suffix in (" IST", " IDT"):
        if text.endswith(suffix):
            try:
                dt = datetime.strptime(text[: -len(suffix)], "%Y-%m-%d %H:%M:%S").replace(tzinfo=JERUSALEM)
                return dt.astimezone(timezone.utc).isoformat()
            except ValueError:
                return None
    return None


def _tool_browser_actions(tool_js: str) -> list[str]:
    actions: list[str] = []
    if "Special:Search" in tool_js or "/wiki/Special:Search" in tool_js:
        actions.append("Opened English Wikipedia's dedicated Search page (/wiki/Special:Search).")
    if "getByRole(\"combobox\")" in tool_js or "box.fill(term" in tool_js:
        actions.append("Filled the search combobox with the requested term.")
    if 'name: "Search"' in tool_js or "Search" in tool_js:
        actions.append("Clicked the Search button and waited for results or a no-results message.")
    if "mw-search-results" in tool_js and "first.click" in tool_js.lower() or "first.click" in tool_js:
        actions.append("Opened the first search-result heading.")
    if "first_paragraph" in tool_js or "lead paragraph" in tool_js:
        actions.append("Read the first visible lead paragraph of the article.")
    if "Search link" in tool_js:
        actions.insert(0, "The mint observed Wikipedia's own Search link to reach the dedicated search page.")
    return actions


def backfill_wikipedia_job(
    store: MintJobStore,
    *,
    data_dir: Path,
    workflow_store=None,
    overwrite: bool = False,
) -> dict[str, Any] | None:
    """Reconstruct a partial trace for the known live Wikipedia mint."""

    data_dir = Path(data_dir)
    integration = data_dir / "integrations" / "wikipedia_search_reader"
    report_path = data_dir / "live_mint_pomerado_openai_report.json"
    if not integration.is_dir() or not report_path.is_file():
        return None
    existing = store.get(WIKIPEDIA_JOB_ID)
    if existing is not None and existing.get("steps") and not overwrite:
        _attach_mint_job(workflow_store, existing)
        return existing

    report = json.loads(report_path.read_text(encoding="utf-8"))
    started = parse_jerusalem(report.get("started_at_ist")) or utc_now()
    mint_s = float(report.get("mint_time_s") or 0)
    finished = started
    try:
        finished = (datetime.fromisoformat(started) + timedelta(seconds=mint_s)).isoformat()
    except ValueError:
        finished = parse_jerusalem(report.get("finished_at_ist")) or started

    deployment = {}
    dep_path = integration / "deployment.json"
    if dep_path.is_file():
        try:
            deployment = json.loads(dep_path.read_text(encoding="utf-8"))
        except Exception:
            deployment = {}
    request = deployment.get("request") if isinstance(deployment.get("request"), dict) else {}
    intent = str(request.get("intent") or deployment.get("description") or "")
    url = str(request.get("url") or "https://en.wikipedia.org/wiki/Special:Search")
    models = report.get("models") or {}
    minter = models.get("minter") or "gpt-6.1-sol"
    guardian = models.get("guardian") or "gpt-6-luna"
    workflow = report.get("workflow") or (report.get("mint") or {}).get("workflow") or {}
    directory = str(integration.resolve())

    tool_js = ""
    tool_path = integration / "src" / "tool.mjs"
    if tool_path.is_file():
        tool_js = tool_path.read_text(encoding="utf-8")

    steps: list[dict[str, Any]] = [
        make_step(
            phase="start",
            title="Mint started for wikipedia_search_reader",
            detail=f"URL {url}. Stock Pomerado (official models, hosted tools).",
            source="backfill",
            ts=started,
            tier="pomerado",
        ),
        make_step(
            phase="model",
            title=f"Minter {minter}, Guardian {guardian}",
            detail="Official Pomerado defaults. Mid-mint model/tool-call events were not retained.",
            source="backfill",
            ts=started,
            tier="pomerado",
        ),
        make_step(
            phase="browser",
            title="Attached to the dedicated Playwright Chromium",
            detail="playwright-endpoint mode. Alex's Chrome was not used.",
            source="backfill",
            ts=started,
            tier="pomerado",
        ),
    ]
    for action in _tool_browser_actions(tool_js):
        steps.append(
            make_step(
                phase="browser",
                title=action,
                detail="Reconstructed from the published src/tool.mjs (times unknown).",
                source="backfill",
                ts=None,
                tier="pomerado",
            )
        )
        steps[-1].pop("ts", None)

    denial = (report.get("mint") or {}).get("publicationDenial") or report.get("guardian_issues")
    if denial:
        steps.append(
            make_step(
                phase="guardian",
                title="Guardian reported an issue",
                detail=str(denial),
                success=False,
                error=str(denial),
                source="backfill",
                ts=finished,
                tier="guardian",
            )
        )
    else:
        steps.append(
            make_step(
                phase="guardian",
                title=f"Guardian ({guardian}) approved publication",
                detail="No publicationDenial, stopReason, or Guardian issues were recorded in the mint result.",
                source="backfill",
                ts=finished,
                tier="guardian",
            )
        )

    wf_name = workflow.get("name") or "wikipedia_search_reader"
    wf_id = workflow.get("id")
    steps.append(
        make_step(
            phase="publish",
            title="Build published and registered as a workflow",
            detail=f"Artifact {directory}. Workflow {wf_name}" + (f" ({wf_id})" if wf_id else "") + ".",
            source="backfill",
            ts=finished,
            tier="pomerado",
            latency_ms=mint_s * 1000 if mint_s else None,
        )
    )
    steps.append(
        make_step(
            phase="note",
            title="This trace is partial",
            detail=(
                "Reconstructed after the mint finished from the published integration "
                "(pomerado.json, src/tool.mjs, deployment.json) and data/live_mint_pomerado_openai_report.json. "
                "Live MCP progress, stdout/stderr, mid-mint tool calls, and Guardian step logs were not captured."
            ),
            source="backfill",
            ts=finished,
        )
    )

    record = {
        "job_id": WIKIPEDIA_JOB_ID,
        "name": "wikipedia_search_reader",
        "url": url,
        "intent": intent,
        "effect": "read",
        "status": "completed",
        "ok": True,
        "directory": directory,
        "workflow": workflow if workflow else {"id": wf_id, "name": wf_name},
        "started_at": started,
        "finished_at": finished,
        "partial": True,
        "trace_source": "backfill",
        "error": None,
        "request": {"name": "wikipedia_search_reader", "url": url, "intent": intent, "effect": "read"},
        "output": {"build": "published", "integration": {"directory": directory}},
        "steps": steps,
    }
    saved = store.save(record)
    _attach_mint_job(workflow_store, saved)
    return saved


def backfill_known_jobs(store: MintJobStore, *, data_dir: Path, workflow_store=None) -> list[str]:
    done: list[str] = []
    job = backfill_wikipedia_job(store, data_dir=data_dir, workflow_store=workflow_store)
    if job:
        done.append(str(job.get("job_id")))
    return done


def _attach_mint_job(workflow_store, record: dict[str, Any]) -> None:
    if workflow_store is None or not record:
        return
    try:
        from browser_platform.pomerado.register import attach_mint_job_id

        attach_mint_job_id(
            workflow_store,
            directory=str(record.get("directory") or ""),
            mint_job_id=str(record.get("job_id") or ""),
            workflow_id=(record.get("workflow") or {}).get("id") if isinstance(record.get("workflow"), dict) else None,
            name=str(record.get("name") or ((record.get("workflow") or {}) if isinstance(record.get("workflow"), dict) else {}).get("name") or ""),
        )
    except Exception:
        return


__all__ = [
    "MintJobStore",
    "WIKIPEDIA_JOB_ID",
    "backfill_known_jobs",
    "backfill_wikipedia_job",
    "is_live",
    "is_terminal",
    "looks_unknown_job",
    "make_step",
    "notification_step",
    "stderr_step",
]

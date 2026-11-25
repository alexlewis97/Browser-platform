"""REST routes: workflows, runs, from-task authoring, Pomerado mint, recorder ingest."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from browser_platform.api.schemas import FromRecordingRequest, FromRecordingSaveRequest, ConfirmationRequest, FromTaskConfirmation, FromTaskRequest, MintRequest, RunRequest, WorkflowCreate, WorkflowReplace
from browser_platform.fuzzy.hitl import ConfirmationDecision
from browser_platform.models.workflow import WorkflowDefinition
from browser_platform.workflow_inputs import missing_required_inputs
from browser_platform.local_secrets import key_status, save_openai_key
from browser_platform.workflowuse.adapter import ingest_payload, recorder_help

router = APIRouter()


def _state(request: Request):
    return request.app.state


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "browser-platform"}


@router.get("/api/settings/keys")
async def get_key_status() -> dict[str, Any]:
    """Report whether a local OpenAI key is configured. Never returns the key."""
    return key_status()


@router.put("/api/settings/openai")
async def put_openai_key(payload: dict[str, Any], request: Request) -> dict[str, Any]:
    """Save or clear the machine-local OpenAI key used for Pomerado minting."""
    raw = payload.get("api_key") if isinstance(payload, dict) else None
    try:
        save_openai_key("" if raw is None else str(raw))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    from browser_platform.app import ensure_openai_runtime

    ensure_openai_runtime(request.app)
    return key_status()


@router.get("/api/pomerado/capabilities")
async def pomerado_capabilities(request: Request) -> dict[str, Any]:
    state = _state(request)
    if state.pomerado is None:
        raise HTTPException(status_code=503, detail="Pomerado host is not configured")
    return await state.pomerado.capabilities()


@router.get("/api/recorder")
async def recorder_info() -> dict[str, str]:
    return recorder_help()


@router.post("/api/workflows")
async def create_workflow(payload: WorkflowCreate, request: Request) -> dict[str, Any]:
    if payload.definition is not None:
        definition = payload.definition
    else:
        data = payload.model_dump(exclude_none=True)
        data.pop("definition", None)
        try:
            definition = ingest_payload(data)
        except Exception as exc:
            try:
                definition = WorkflowDefinition.model_validate(data)
            except Exception:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
    stored = _state(request).store.create_workflow(definition)
    return {"id": stored.id, "path": stored.path, "definition": stored.definition.model_dump(exclude_none=True)}


@router.get("/api/workflows")
async def list_workflows(request: Request) -> dict[str, Any]:
    items = [
        {"id": w.id, "name": w.definition.name, "version": w.definition.version, "path": w.path}
        for w in _state(request).store.list_workflows()
    ]
    return {"workflows": items}



@router.post("/api/workflows/from-task")
async def create_workflow_from_task_endpoint(payload: FromTaskRequest, request: Request) -> dict[str, Any]:
    """Demonstrate a natural-language task with browser-use and save a workflow-use document."""

    from browser_platform.authoring.from_task import FromTaskResult, create_workflow_from_task

    state = _state(request)
    cdp_url = payload.cdp_url or state.settings.default_cdp_url
    job_id = uuid.uuid4().hex
    jobs = getattr(state, "from_task_jobs", None)
    if jobs is None:
        jobs = {}
        state.from_task_jobs = jobs
    seed = FromTaskResult(
        id=job_id,
        status="running",
        task=payload.task,
        name=payload.name or (payload.task.strip().split("\n")[0][:80] or "from_task"),
        variables=dict(payload.variables or {}),
    )
    jobs[job_id] = seed

    def _remember(job: FromTaskResult) -> None:
        jobs[job.id] = job

    async def _run() -> FromTaskResult:
        return await create_workflow_from_task(
            state.store,
            state.settings,
            task=payload.task,
            cdp_url=cdp_url,
            llm=payload.llm,
            variables=payload.variables,
            start_url=payload.start_url,
            name=payload.name,
            write_back=payload.write_back,
            replay=payload.replay,
            job_id=job_id,
            max_steps=payload.max_steps,
            timeout=payload.timeout,
            confirm_variables=payload.confirm_variables,
            replay_variables=getattr(payload, "replay_variables", None),
            confirmation_broker=getattr(state, "confirmation_broker", None),
            event_hub=getattr(state, "event_hub", None),
            on_status=_remember,
        )

    if payload.wait:
        job = await _run()
        jobs[job_id] = job
        return job.model_dump(mode="json")

    async def _bg() -> None:
        try:
            job = await _run()
            jobs[job_id] = job
        except Exception as exc:
            seed.status = "failed"
            seed.error = str(exc)
            jobs[job_id] = seed

    asyncio.create_task(_bg())
    return seed.model_dump(mode="json")


@router.get("/api/workflows/from-task/{job_id}")
async def get_from_task_job(job_id: str, request: Request) -> dict[str, Any]:
    jobs = getattr(_state(request), "from_task_jobs", None) or {}
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="from-task job not found")
    return job.model_dump(mode="json") if hasattr(job, "model_dump") else job


@router.post("/api/workflows/from-task/{job_id}/confirmation")
async def post_from_task_confirmation(job_id: str, payload: FromTaskConfirmation, request: Request) -> dict[str, Any]:
    state = _state(request)
    broker = getattr(state, "confirmation_broker", None)
    if broker is None:
        raise HTTPException(status_code=503, detail="confirmation broker is not configured")
    action = (payload.action or "confirm").strip().lower()
    if action == "cancel":
        decision = ConfirmationDecision(action="cancel")
    elif action in {"confirm", "choose", "select"}:
        decision = ConfirmationDecision(
            action="confirm",
            text=payload.text,
            variables=payload.variables,
            parameters=getattr(payload, "parameters", None),
        )
    else:
        raise HTTPException(status_code=422, detail="action must be confirm or cancel")
    if not broker.resolve(job_id, decision):
        raise HTTPException(status_code=409, detail="job is not waiting for confirmation")
    return {"ok": True, "job_id": job_id, "action": decision.action}



@router.post("/api/workflows/from-recording")
async def clean_recording_endpoint(payload: FromRecordingRequest, request: Request) -> dict[str, Any]:
    """Prune a human Chrome recording and propose labelled inputs for confirmation."""

    from browser_platform.authoring.from_recording import clean_recording

    try:
        result = await clean_recording(
            events=payload.events,
            goal=payload.goal or "",
            name=payload.name,
            variables=payload.variables or {},
            llm=payload.llm,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result.model_dump(exclude_none=True)


@router.post("/api/workflows/from-recording/save")
async def save_recording_endpoint(payload: FromRecordingSaveRequest, request: Request) -> dict[str, Any]:
    """Save the builder's reviewed recording as a normal library workflow."""

    from browser_platform.authoring.from_recording import definition_from_review

    try:
        definition = definition_from_review(
            name=payload.name,
            goal=payload.goal or "",
            review=payload.review,
            parameters=payload.parameters,
            variables=payload.variables,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    stored = _state(request).store.create_workflow(definition)
    return {"id": stored.id, "path": stored.path, "definition": stored.definition.model_dump(exclude_none=True)}


@router.get("/api/workflows/{workflow_id}")
async def get_workflow(workflow_id: str, request: Request) -> dict[str, Any]:
    try:
        stored = _state(request).store.get_workflow(workflow_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="workflow not found")
    return {"id": stored.id, "path": stored.path, "definition": stored.definition.model_dump(exclude_none=True)}


@router.put("/api/workflows/{workflow_id}")
async def replace_workflow(workflow_id: str, payload: WorkflowReplace, request: Request) -> dict[str, Any]:
    try:
        stored = _state(request).store.update_workflow(workflow_id, payload.definition)
    except KeyError:
        raise HTTPException(status_code=404, detail="workflow not found")
    return {"id": stored.id, "path": stored.path, "definition": stored.definition.model_dump(exclude_none=True)}


@router.post("/api/workflows/{workflow_id}/run")
async def run_workflow(workflow_id: str, payload: RunRequest, request: Request) -> dict[str, Any]:
    state = _state(request)
    try:
        stored = state.store.get_workflow(workflow_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="workflow not found")
    missing = missing_required_inputs(stored.definition, payload.variables)
    if missing:
        raise HTTPException(
            status_code=400,
            detail="Missing required inputs: " + ", ".join(missing),
        )
    cdp_url = payload.cdp_url or state.settings.default_cdp_url
    if payload.wait:
        run = await state.runner.run(
            stored,
            cdp_url=cdp_url,
            variables=payload.variables,
            llm=payload.llm,
            write_back_repairs=payload.write_back,
        )
        return run.model_dump(mode="json")

    run_id = uuid.uuid4().hex

    async def _run_in_background() -> None:
        await state.runner.run(
            stored,
            cdp_url=cdp_url,
            variables=payload.variables,
            llm=payload.llm,
            write_back_repairs=payload.write_back,
            run_id=run_id,
        )

    asyncio.create_task(_run_in_background())
    return {
        "id": run_id,
        "workflow_id": stored.id,
        "workflow_name": stored.definition.name,
        "workflow_version": stored.definition.version,
        "status": "running",
        "cdp_url": cdp_url,
        "model": payload.llm.model if payload.llm else None,
        "steps": [],
        "outputs": {},
        "write_back": payload.write_back,
    }


@router.get("/api/runs")
async def list_runs(request: Request) -> dict[str, Any]:
    return {"runs": [r.model_dump(mode="json") for r in _state(request).store.list_runs()]}


@router.get("/api/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    try:
        return _state(request).store.get_run(run_id).model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found")


@router.post("/api/pomerado/mint")
async def mint(payload: MintRequest, request: Request) -> dict[str, Any]:
    state = _state(request)
    if state.pomerado is None:
        raise HTTPException(status_code=503, detail="Pomerado host is not configured")
    try:
        result = await state.pomerado.mint(
            name=payload.name,
            url=payload.url,
            intent=payload.intent,
            effect=payload.effect,
            input_values=payload.input,
            llm=payload.llm,
            wait_s=getattr(payload, "wait_s", 0) or 0,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"result": result}


@router.get("/api/pomerado/jobs")
async def list_jobs(request: Request) -> dict[str, Any]:
    state = _state(request)
    if state.pomerado is None:
        raise HTTPException(status_code=503, detail="Pomerado host is not configured")
    return {"jobs": state.pomerado.list_jobs()}


@router.get("/api/pomerado/jobs/{job_id}")
async def get_job(job_id: str, request: Request) -> dict[str, Any]:
    state = _state(request)
    if state.pomerado is None:
        raise HTTPException(status_code=503, detail="Pomerado host is not configured")
    try:
        return await state.pomerado.get_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found")



@router.get("/api/runs/{run_id}/confirmation")
async def get_confirmation(run_id: str, request: Request) -> dict[str, Any]:
    state = _state(request)
    try:
        run = state.store.get_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="run not found")
    payload = run.confirmation or (state.confirmation_broker.pending(run_id) if getattr(state, "confirmation_broker", None) else None)
    if not payload and run.status != "needs_confirmation":
        raise HTTPException(status_code=404, detail="no pending confirmation")
    return {"run_id": run_id, "status": run.status, "confirmation": payload}


@router.post("/api/runs/{run_id}/confirmation")
async def post_confirmation(run_id: str, payload: ConfirmationRequest, request: Request) -> dict[str, Any]:
    state = _state(request)
    broker = getattr(state, "confirmation_broker", None)
    if broker is None:
        raise HTTPException(status_code=503, detail="confirmation broker is not configured")
    action = (payload.action or "confirm").strip().lower()
    if action == "cancel":
        decision = ConfirmationDecision(action="cancel")
    elif action in {"confirm", "choose", "select"}:
        if not payload.candidate_id and not payload.text:
            raise HTTPException(status_code=422, detail="candidate_id or text is required")
        decision = ConfirmationDecision(action="confirm", candidate_id=payload.candidate_id, text=payload.text)
    else:
        raise HTTPException(status_code=422, detail="action must be confirm or cancel")
    if not broker.resolve(run_id, decision):
        raise HTTPException(status_code=409, detail="run is not waiting for confirmation")
    return {"ok": True, "run_id": run_id, "action": decision.action}

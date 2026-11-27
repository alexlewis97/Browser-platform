"""REST routes: workflows, runs, from-task authoring, Pomerado mint, recorder ingest."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from browser_platform.api.schemas import FromRecordingRequest, FromRecordingSaveRequest, ConfirmationRequest, FromTaskConfirmation, FromTaskRequest, MintRequest, RunRequest, WorkflowCreate, WorkflowReplace, WorkflowVisibility
from browser_platform.fuzzy.hitl import ConfirmationDecision
from browser_platform.models.llm import resolve_llm
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
        data.pop("workspace_ids", None)
        try:
            definition = ingest_payload(data)
        except Exception as exc:
            try:
                definition = WorkflowDefinition.model_validate(data)
            except Exception:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        stored = _state(request).store.create_workflow(definition)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    workspaces = _assign(request, stored.id, getattr(payload, "workspace_ids", None))
    return {"id": stored.id, "path": stored.path, "workspaces": workspaces, "definition": stored.definition.model_dump(exclude_none=True)}


def _assign(request: Request, workflow_id: str, keys: Any) -> list[str]:
    """Optional ``workspace_ids`` on create: add the new workflow to those workspaces."""

    if not keys:
        return []
    from browser_platform.api.workspaces import assign_new_workflow

    return assign_new_workflow(request, workflow_id, [str(k) for k in (keys if isinstance(keys, list) else [keys])])


def _scope_or_404(request: Request, workspace: str | None):
    from browser_platform.api.chat import _orch

    try:
        return _orch(request).scope(workspace)
    except KeyError:
        raise HTTPException(status_code=404, detail="workspace not found")


def _workflow_summary(w) -> dict[str, Any]:
    definition = w.definition
    return {
        "id": w.id,
        "name": definition.name,
        "description": definition.description or "",
        "version": definition.version,
        "path": w.path,
        "published": bool(getattr(definition, "published", True)),
        "input_schema": [item.model_dump(exclude_none=True) for item in (definition.input_schema or [])],
    }


def _catalog_item(w) -> dict[str, Any]:
    item = _workflow_summary(w)
    item.pop("path", None)
    return item


@router.get("/api/workflows")
async def list_workflows(request: Request, workspace: str | None = None) -> dict[str, Any]:
    from browser_platform.api.chat import _orch

    ws = _scope_or_404(request, workspace)
    member: dict[str, list[str]] = {}
    for space in _orch(request).workspaces.list():
        for wid in space.workflow_ids:
            member.setdefault(wid, []).append(space.slug)
    items = [
        {**_workflow_summary(w), "workspaces": member.get(w.id, [])}
        for w in _state(request).store.list_workflows()
        if ws is None or w.id in set(ws.workflow_ids)
    ]
    return {"workflows": items}


@router.get("/api/catalog")
async def list_catalog(request: Request, workspace: str | None = None) -> dict[str, Any]:
    """Public list of published workflows for the end-user run site. No access token.
    ``workspace`` (id or slug) narrows it; omitted or 'all' lists every published workflow."""
    ws = _scope_or_404(request, workspace)
    allowed = set(ws.workflow_ids) if ws else None
    items = [
        _catalog_item(w)
        for w in _state(request).store.list_workflows()
        if getattr(w.definition, "published", True) and (allowed is None or w.id in allowed)
    ]
    return {"workflows": items, "workspace": ws.slug if ws else None}


@router.get("/api/catalog/workspaces")
async def list_catalog_workspaces(request: Request) -> dict[str, Any]:
    """Public: workspaces with at least one published workflow (no instructions or ids leaked)."""
    from browser_platform.api.chat import _orch

    published = {w.id for w in _state(request).store.list_workflows() if getattr(w.definition, "published", True)}
    out = []
    for ws in _orch(request).workspaces.list():
        count = sum(1 for i in ws.workflow_ids if i in published)
        if count:
            out.append({"id": ws.id, "slug": ws.slug, "name": ws.name, "description": ws.description, "workflow_count": count})
    return {"workspaces": out}


@router.get("/api/catalog/{workflow_id}")
async def get_catalog_workflow(workflow_id: str, request: Request) -> dict[str, Any]:
    """Public definition of a published workflow, for running in the user's extension."""
    try:
        stored = _state(request).store.get_workflow(workflow_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="workflow not found")
    if not getattr(stored.definition, "published", True):
        raise HTTPException(status_code=404, detail="workflow not found")
    return {
        "id": stored.id,
        "definition": stored.definition.model_dump(exclude_none=True),
        **_catalog_item(stored),
    }



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
            llm=resolve_llm(payload.llm),
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
            explore=getattr(payload, "explore", True),
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

    # Cap in-memory from-task jobs and track the background task.
    while len(jobs) > 50:
        oldest = next(iter(jobs))
        jobs.pop(oldest, None)
    _track_task(request, asyncio.create_task(_bg()))
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




@router.post("/api/workflows/from-task/{job_id}/explore")
async def post_from_task_explore(job_id: str, payload: dict, request: Request) -> dict[str, Any]:
    """Accept or reject verified explore-mode optional inputs in the review UI."""

    from browser_platform.authoring.explore import apply_proposals, strip_proposals

    state = _state(request)
    jobs = getattr(state, "from_task_jobs", None) or {}
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="from-task job not found")
    if not job.workflow_id or job.definition is None:
        raise HTTPException(status_code=409, detail="job has no saved workflow yet")
    explore = dict(job.explore or {})
    verified = list(explore.get("verified") or [])
    accepted = [str(n) for n in (payload or {}).get("accepted") or [item.get("name") for item in verified]]
    rejected = [str(n) for n in (payload or {}).get("rejected") or []]
    keep = [item for item in verified if item.get("name") in accepted and item.get("name") not in rejected]
    drop = [name for name in {*(item.get("name") for item in verified), *rejected} if name not in {i.get("name") for i in keep}]
    definition = apply_proposals(job.definition, keep) if keep else job.definition
    if drop:
        definition = strip_proposals(definition, drop)
    stored = state.store.update_workflow(job.workflow_id, definition)
    job.definition = stored.definition
    if job.explore is not None:
        job.explore = {**explore, "accepted": [item["name"] for item in keep], "rejected": drop, "applied": bool(keep)}
    jobs[job_id] = job
    return {"ok": True, "job_id": job_id, "workflow_id": stored.id, "accepted": [item["name"] for item in keep], "rejected": drop, "definition": stored.definition.model_dump(exclude_none=True)}


def _explore_sample_variables(definition: Any) -> dict[str, Any]:
    """Values used only to open the page while exploring: the default, else the first example.

    Required inputs often have no default (no silent entity defaults); exploring still needs a
    real page, so a sample example is used for the visit and never saved as a default."""

    out: dict[str, Any] = {}
    for item in definition.input_schema or []:
        if item.default not in (None, ""):
            out[item.name] = item.default
        elif getattr(item, "examples", None):
            out[item.name] = item.examples[0]
    return out


@router.post("/api/workflows/{workflow_id}/explore")
async def post_workflow_explore(workflow_id: str, request: Request) -> dict[str, Any]:
    """Run bounded explore-mode on an existing library workflow and apply verified options."""

    from browser_platform.authoring.explore import explore_definition

    state = _state(request)
    try:
        stored = state.store.get_workflow(workflow_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="workflow not found")
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    apply = bool((body or {}).get("apply", True))
    result = await explore_definition(
        stored.definition,
        task=(body or {}).get("task") or stored.definition.description or stored.definition.name,
        variables=(body or {}).get("variables") or _explore_sample_variables(stored.definition),
        enabled=True,
    )
    if apply and result.get("definition"):
        from browser_platform.models.workflow import WorkflowDefinition

        stored = state.store.update_workflow(workflow_id, WorkflowDefinition.model_validate(result["definition"]))
        result["workflow_id"] = stored.id
        result["definition"] = stored.definition.model_dump(exclude_none=True)
    else:
        result["workflow_id"] = workflow_id
    return result



@router.post("/api/workflows/{workflow_id}/derive")
async def post_workflow_derive(workflow_id: str, request: Request) -> dict[str, Any]:
    """Create a new tool from a sibling view explore found, then explore it for parameters.

    Body: {"view": {...one item of explore's sibling_views...}, "apply": false}. With apply
    false (default) nothing is saved and the proposed definition is returned."""

    from browser_platform.authoring.explore import derive_view_definition, explore_definition
    from browser_platform.models.workflow import WorkflowDefinition

    state = _state(request)
    try:
        stored = state.store.get_workflow(workflow_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="workflow not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    view = (body or {}).get("view") or {}
    try:
        derived = derive_view_definition(stored.definition, view)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    explored = await explore_definition(derived, task=derived.description or derived.name,
                                        variables=_explore_sample_variables(derived), enabled=True)
    if explored.get("definition"):
        derived = WorkflowDefinition.model_validate(explored["definition"])
    result: dict[str, Any] = {"definition": derived.model_dump(exclude_none=True), "explore": {
        k: explored.get(k) for k in ("accepted", "dropped", "usage")}, "saved": False}
    if bool((body or {}).get("apply")):
        saved = state.store.create_workflow(derived)
        result.update({"saved": True, "workflow_id": saved.id})
    return result


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
    from browser_platform.workflow_inputs import finalize_authored_inputs

    stored = _state(request).store.create_workflow(finalize_authored_inputs(definition))
    workspaces = _assign(request, stored.id, getattr(payload, "workspace_ids", None))
    return {"id": stored.id, "path": stored.path, "workspaces": workspaces, "definition": stored.definition.model_dump(exclude_none=True)}


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
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": stored.id, "path": stored.path, "definition": stored.definition.model_dump(exclude_none=True)}


@router.put("/api/workflows/{workflow_id}/published")
async def set_workflow_published(workflow_id: str, payload: WorkflowVisibility, request: Request) -> dict[str, Any]:
    """Admin toggle: show or hide a workflow on the end-user run site."""
    state = _state(request)
    try:
        stored = state.store.get_workflow(workflow_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="workflow not found")
    definition = stored.definition
    definition.published = bool(payload.published)
    try:
        stored = state.store.update_workflow(workflow_id, definition)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": stored.id,
        "published": bool(stored.definition.published),
        "definition": stored.definition.model_dump(exclude_none=True),
    }


@router.delete("/api/workflows/{workflow_id}")
async def delete_workflow(workflow_id: str, request: Request) -> dict[str, Any]:
    """Owner-scoped delete: drop the workflow and caches, keep flagged run history."""
    state = _state(request)
    try:
        runs_kept = state.store.delete_workflow(workflow_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="workflow not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    caches_cleared = 0
    runner = getattr(state, "runner", None)
    if runner is not None:
        caches_cleared += runner.result_cache.drop_workflow(workflow_id)
        caches_cleared += runner.action_cache.drop_workflow(workflow_id)
    return {"ok": True, "id": workflow_id, "runs_kept": runs_kept, "caches_cleared": caches_cleared}


def _track_task(request: Request, task: asyncio.Task) -> None:
    tasks: set[asyncio.Task] = getattr(request.app.state, "background_tasks", None) or set()
    request.app.state.background_tasks = tasks
    tasks.add(task)

    def _done(done: asyncio.Task) -> None:
        tasks.discard(done)

    task.add_done_callback(_done)


@router.post("/api/workflows/{workflow_id}/run")
async def run_workflow(workflow_id: str, payload: RunRequest, request: Request) -> dict[str, Any]:
    state = _state(request)
    try:
        stored = state.store.get_workflow(workflow_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="workflow not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    missing = missing_required_inputs(stored.definition, payload.variables)
    if missing:
        raise HTTPException(
            status_code=400,
            detail="Missing required inputs: " + ", ".join(missing),
        )
    cdp_url = payload.cdp_url or state.settings.default_cdp_url
    run_id = uuid.uuid4().hex
    # Persist a running record immediately: every run has a run_id even if it hangs or the
    # caller disconnects, and GET /api/runs/{id} never 404s.
    from datetime import datetime, timezone

    from browser_platform.models.run import RunRecord

    pending = RunRecord(
        id=run_id,
        workflow_id=stored.id,
        workflow_name=stored.definition.name,
        workflow_version=stored.definition.version,
        status="running",
        cdp_url=cdp_url,
        model=payload.llm.model if payload.llm else None,
        write_back=payload.write_back,
        started_at=datetime.now(timezone.utc),
    )
    state.store.save_run(pending)

    async def _run_in_background() -> Any:
        return await state.runner.run(
            stored,
            cdp_url=cdp_url,
            variables=payload.variables,
            llm=payload.llm,
            write_back_repairs=payload.write_back,
            run_id=run_id,
            strict=payload.strict,
        )

    task = asyncio.create_task(_run_in_background())
    _track_task(request, task)
    if not payload.wait:
        return pending.model_dump(mode="json")
    # wait=true: block until the run finishes, or return early (with its run_id and the
    # confirmation prompt) when it pauses for a human, instead of holding the request open.
    while not task.done():
        await asyncio.wait({task}, timeout=0.5)
        if task.done():
            break
        try:
            current = state.store.get_run(run_id)
        except Exception:
            continue
        if current.status == "needs_confirmation":
            return current.model_dump(mode="json")
    try:
        return task.result().model_dump(mode="json")
    except Exception:
        return state.store.get_run(run_id).model_dump(mode="json")


@router.get("/api/runs")
async def list_runs(request: Request, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    runs = _state(request).store.list_runs()
    page = runs[offset : offset + limit]
    return {"runs": [r.model_dump(mode="json") for r in page], "total": len(runs), "limit": limit, "offset": offset}


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

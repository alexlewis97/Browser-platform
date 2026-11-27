"""Workspace admin API (token-protected).

    GET    /api/workspaces                      list (with workflow_count)
    POST   /api/workspaces                      {name, description?, instructions?, workflow_ids?}
    GET    /api/workspaces/{key}                one workspace + its workflows (key = id or slug)
    PATCH  /api/workspaces/{key}                rename / describe / instructions / workflow_ids (replace)
    DELETE /api/workspaces/{key}                delete the workspace (workflows are kept)
    POST   /api/workspaces/{key}/workflows      {workflow_ids} add
    DELETE /api/workspaces/{key}/workflows      {workflow_ids} remove
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from browser_platform.workspaces import Workspace, WorkspaceStore

router = APIRouter()


class WorkspaceCreate(BaseModel):
    name: str
    description: str = ""
    instructions: str = ""
    workflow_ids: list[str] = []
    slug: str | None = None


class WorkspacePatch(BaseModel):
    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    workflow_ids: list[str] | None = None


class WorkflowIds(BaseModel):
    workflow_ids: list[str]


def workspace_store(request: Request) -> WorkspaceStore:
    from browser_platform.api.chat import _orch

    return _orch(request).workspaces


def _known_ids(request: Request, ids: list[str]) -> list[str]:
    store = request.app.state.store
    missing = []
    for wid in ids:
        try:
            store.get_workflow(wid)
        except Exception:
            missing.append(wid)
    if missing:
        raise HTTPException(status_code=400, detail="Unknown workflow ids: " + ", ".join(missing))
    return ids


def _get(request: Request, key: str) -> Workspace:
    ws = workspace_store(request).get(key)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return ws


def _detail(request: Request, ws: Workspace) -> dict[str, Any]:
    store = request.app.state.store
    workflows = []
    for wid in ws.workflow_ids:
        try:
            d = store.get_workflow(wid).definition
            workflows.append({"id": wid, "name": d.name, "description": d.description or "", "published": bool(getattr(d, "published", True))})
        except Exception:
            continue
    return {**ws.summary(), "workflows": workflows}


def assign_new_workflow(request: Request, workflow_id: str, keys: list[str] | None) -> list[str]:
    """Add a freshly created workflow to workspaces (used by the create endpoints)."""

    store = workspace_store(request)
    added = []
    for key in keys or []:
        ws = store.get(str(key))
        if ws is None:
            raise HTTPException(status_code=404, detail=f"workspace '{key}' not found")
        added.append(store.add(ws.id, [workflow_id]).slug)
    return added


@router.get("/api/workspaces")
async def list_workspaces(request: Request) -> dict[str, Any]:
    return {"workspaces": [ws.summary() for ws in workspace_store(request).list()]}


@router.post("/api/workspaces")
async def create_workspace(payload: WorkspaceCreate, request: Request) -> dict[str, Any]:
    ids = _known_ids(request, list(dict.fromkeys(payload.workflow_ids)))
    try:
        ws = workspace_store(request).create(
            payload.name, description=payload.description, instructions=payload.instructions, workflow_ids=ids, slug=payload.slug
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _detail(request, ws)


@router.get("/api/workspaces/{key}")
async def get_workspace(key: str, request: Request) -> dict[str, Any]:
    return _detail(request, _get(request, key))


@router.patch("/api/workspaces/{key}")
async def patch_workspace(key: str, payload: WorkspacePatch, request: Request) -> dict[str, Any]:
    ws = _get(request, key)
    if payload.workflow_ids is not None:
        _known_ids(request, payload.workflow_ids)
    try:
        ws = workspace_store(request).update(ws.id, **payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _detail(request, ws)


@router.delete("/api/workspaces/{key}")
async def delete_workspace(key: str, request: Request) -> dict[str, Any]:
    ws = _get(request, key)
    workspace_store(request).delete(ws.id)
    return {"ok": True, "deleted": ws.id}


@router.post("/api/workspaces/{key}/workflows")
async def add_workflows(key: str, payload: WorkflowIds, request: Request) -> dict[str, Any]:
    ws = _get(request, key)
    return _detail(request, workspace_store(request).add(ws.id, _known_ids(request, payload.workflow_ids)))


@router.delete("/api/workspaces/{key}/workflows")
async def remove_workflows(key: str, payload: WorkflowIds, request: Request) -> dict[str, Any]:
    ws = _get(request, key)
    return _detail(request, workspace_store(request).remove(ws.id, payload.workflow_ids))

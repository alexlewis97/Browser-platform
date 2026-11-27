"""Chat, tool catalog, MCP, and validation endpoints."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from browser_platform.authoring.input_validate import apply_patch, validate_inputs
from browser_platform.authoring.tools import mcp_tools, tools_from_workflows
from browser_platform.executor.output_validate import validate_output
from browser_platform.models.workflow import WorkflowDefinition
from browser_platform.orchestrator.chat import ChatJobs, ChatOrchestrator, public_job
from browser_platform.skills.manifest import card
from browser_platform.skills.registry import mcp_tool

router = APIRouter()
from browser_platform.api.workspaces import router as _workspaces_router  # noqa: E402

router.include_router(_workspaces_router)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    message: str = ""
    history: list[dict[str, Any]] = Field(default_factory=list)
    target: str = "cloud"
    stream: bool = True
    confirm: bool = False
    job_id: str | None = None
    results: list[dict[str, Any]] | None = None
    llm: dict[str, Any] | None = None
    workspace: str | None = None


class ValidateInputsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    definition: dict[str, Any] | None = None
    task: str = ""
    patch: dict[str, Any] | None = None
    use_llm: bool = True


class ValidateOutputRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    request: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    sample: str | None = None
    url: str = ""
    title: str = ""
    action_ending: bool = False
    use_llm: bool = True


def _orch(request: Request) -> ChatOrchestrator:
    state = request.app.state
    orch = getattr(state, "chat", None)
    if orch is None:
        jobs = getattr(state, "chat_jobs", None) or ChatJobs()
        state.chat_jobs = jobs
        orch = ChatOrchestrator(state.store, runner=state.runner, jobs=jobs)
        state.chat = orch
    return orch


def _scope_ids(request: Request, workspace: str | None) -> set[str] | None:
    """Workflow ids for a workspace (None = whole library). Unknown workspace -> 404."""

    try:
        ws = _orch(request).scope(workspace)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown workspace '{workspace}'.")
    return set(ws.workflow_ids) if ws else None


@router.get("/api/tools")
async def list_tools(request: Request, workspace: str | None = None) -> dict[str, Any]:
    _scope_ids(request, workspace)
    tools = _orch(request).catalog(workspace)
    return {"tools": tools, "openai": [{"type": "function", "function": t["function"]} for t in tools], "mcp": mcp_tools(tools)}


@router.get("/api/skills")
async def list_skills(request: Request, full: bool = False, include_duplicates: bool = False, workspace: str | None = None) -> dict[str, Any]:
    """Skill manifests (one per workflow). Compact by default; full=true adds schemas.
    ``workspace`` (id or slug) scopes the list; omitted or 'all' is the whole library."""

    manifests = _orch(request).skills.manifests(_scope_ids(request, workspace))
    if not include_duplicates:
        manifests = [m for m in manifests if m.get("primary", True)]
    if full:
        items = manifests
    else:
        keys = ("id", "slug", "name", "summary", "site", "tags", "action_ending", "enriched", "primary")
        items = [{**{k: m.get(k) for k in keys}, "card": card(m)} for m in manifests]
    return {"count": len(items), "workspace": workspace or None, "skills": items}


@router.get("/api/skills/search")
async def search_skills(request: Request, q: str = "", k: int = 6, workspace: str | None = None) -> dict[str, Any]:
    result = await _orch(request).skills.search(q, k=max(1, min(k, 20)), workflow_ids=_scope_ids(request, workspace))
    return {
        "query": q,
        "workspace": workspace or None,
        "method": result["method"],
        "embed_tokens": result["embed_tokens"],
        "scores": result["scores"],
        "skills": [{"id": m.get("id"), "slug": m.get("slug"), "card": card(m)} for m in result["skills"]],
    }


@router.post("/api/skills/backfill")
async def backfill_skills(request: Request, force: bool = False) -> dict[str, Any]:
    """Write (or refresh) LLM-enriched manifests for every workflow."""

    return await _orch(request).skills.backfill(force=force)


@router.get("/api/skills/{key}")
async def get_skill(key: str, request: Request) -> dict[str, Any]:
    manifest = _orch(request).skills.get(key)
    if manifest is None:
        raise HTTPException(status_code=404, detail="Unknown skill.")
    return manifest


@router.post("/api/workflows/validate-inputs")
async def validate_inputs_endpoint(payload: ValidateInputsRequest) -> dict[str, Any]:
    if not payload.definition:
        raise HTTPException(status_code=422, detail="definition is required")
    definition = WorkflowDefinition.model_validate(payload.definition)
    if payload.patch:
        definition = apply_patch(definition, payload.patch)
    result = await validate_inputs(definition, task=payload.task, use_llm=payload.use_llm)
    result["definition"] = definition.model_dump(exclude_none=True)
    return result


@router.post("/api/runs/validate-output")
async def validate_output_endpoint(payload: ValidateOutputRequest) -> dict[str, Any]:
    return await validate_output(
        request=payload.request,
        inputs=payload.inputs,
        outputs=payload.outputs,
        sample=payload.sample,
        url=payload.url,
        title=payload.title,
        action_ending=payload.action_ending,
        use_llm=payload.use_llm,
    )


@router.get("/api/chat-jobs")
async def recent_chat_jobs(request: Request, limit: int = 20) -> dict[str, Any]:
    """Recent chat jobs (persisted, redacted): plan JSON, candidates, calls, items, tokens."""

    return {"jobs": _orch(request).jobs.recent(max(1, min(limit, 100)))}


@router.get("/api/chat/{job_id}")
async def get_chat_job(job_id: str, request: Request) -> dict[str, Any]:
    job = _orch(request).jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired chat job.")
    return public_job(job)


@router.post("/api/chat")
async def post_chat(payload: ChatRequest, request: Request):
    orch = _orch(request)
    if not payload.message and not payload.job_id:
        raise HTTPException(status_code=422, detail="message or job_id is required")
    if payload.workspace and not payload.job_id:
        _scope_ids(request, payload.workspace)
    llm = payload.llm if isinstance(payload.llm, dict) else {}
    kwargs = dict(
        message=payload.message,
        history=payload.history,
        target=payload.target or "cloud",
        confirm=payload.confirm,
        job_id=payload.job_id,
        results=payload.results,
        llm_api_key=(llm.get("api_key") or "") if llm else "",
        llm_model=(llm.get("model") or "") if llm else "",
        workspace=payload.workspace,
    )
    if payload.stream and payload.results is None:
        queue: list[dict[str, Any]] = []

        async def emit(event: dict[str, Any]) -> None:
            queue.append(event)

        async def gen():
            # Drive the orchestrator and flush events as they arrive.
            import asyncio

            done: list[dict[str, Any] | BaseException] = []

            async def run() -> None:
                try:
                    job = await orch.handle(emit=emit, **kwargs)
                    done.append(job)
                except KeyError as exc:
                    done.append(exc)
                except Exception as exc:
                    done.append(exc)

            task = asyncio.create_task(run())
            try:
                while not task.done() or queue:
                    if queue:
                        event = queue.pop(0)
                        yield _sse(event)
                        await asyncio.sleep(0)
                        continue
                    if not task.done():
                        await asyncio.sleep(0.05)
                result = done[0] if done else None
                if isinstance(result, KeyError):
                    yield _sse({"type": "error", "error": "Unknown or expired chat job."})
                    return
                if isinstance(result, Exception):
                    yield _sse({"type": "error", "error": str(result)})
                    return
                yield _sse({"type": "done", "job": public_job(result)})  # type: ignore[arg-type]
            finally:
                if not task.done():
                    task.cancel()

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    try:
        job = await orch.handle(emit=None, **kwargs)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown or expired chat job.") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return public_job(job)


@router.post("/mcp/{workspace}")
async def mcp_workspace_endpoint(workspace: str, payload: dict[str, Any], request: Request) -> dict[str, Any]:
    """One MCP server per workspace: only that workspace's workflows are listed or callable."""

    return await mcp_endpoint(payload, request, workspace=workspace)


@router.post("/mcp")
async def mcp_endpoint(payload: dict[str, Any], request: Request, workspace: str | None = None) -> dict[str, Any]:
    """Minimal JSON-RPC MCP: initialize, tools/list, tools/call, ping.

    Scope with /mcp/{workspace}, ?workspace=, or params.workspace; none or 'all' = whole library.
    """

    method = str(payload.get("method") or "")
    req_id = payload.get("id")
    params = payload.get("params") or {}
    orch = _orch(request)
    workspace = workspace or request.query_params.get("workspace") or (params.get("workspace") if isinstance(params, dict) else None)
    try:
        ws = orch.scope(workspace)
    except KeyError:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"Unknown workspace '{workspace}'."}}
    scope_ids = set(ws.workflow_ids) if ws else None
    tools = orch.catalog(ws.id if ws else None)

    def ok(result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    if method in {"initialize", "notifications/initialized"}:
        return ok(
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": f"browser-platform/{ws.slug}" if ws else "browser-platform", "version": "0.2.0"},
                **({"instructions": "\n".join(x for x in (ws.description, ws.instructions) if x)} if ws and (ws.description or ws.instructions) else {}),
            }
        )
    if method == "ping":
        return ok({})
    if method == "tools/list":
        # MCP-style catalog from skill manifests. Optional extensions: params.query
        # returns the top-k matches, params.cursor / limit page through the library.
        if params.get("query"):
            found = await orch.skills.search(str(params["query"]), k=int(params.get("limit") or 6), workflow_ids=scope_ids)
            return ok({"tools": [mcp_tool(m) for m in found["skills"]]})
        manifests = orch.skills.manifests(scope_ids)
        start = int(params.get("cursor") or 0)
        limit = int(params.get("limit") or 100)
        page = manifests[start : start + limit]
        result: dict[str, Any] = {"tools": [mcp_tool(m) for m in page]}
        if start + limit < len(manifests):
            result["nextCursor"] = str(start + limit)
        return ok(result)
    if method == "tools/call":
        name = str(params.get("name") or "")
        raw_arguments = params.get("arguments") or {}
        arguments = dict(raw_arguments) if isinstance(raw_arguments, dict) else {}
        confirm = bool(params.get("confirm") or arguments.pop("confirm", False))
        # Reuse a one-shot planned call via the orchestrator job shape.
        from browser_platform.authoring.tools import lookup_tool
        from browser_platform.workflow_inputs import missing_required_inputs

        tool = lookup_tool(tools, name)
        if tool is None:
            return err(-32602, f"Unknown tool '{name}'.")
        stored = orch.stored_for(tool)
        if stored is None:
            return err(-32602, f"Unknown workflow for '{name}'.")
        missing = missing_required_inputs(stored.definition, arguments)
        if missing:
            return err(-32602, "Missing required inputs: " + ", ".join(missing))
        if is_action_tool(tool) and not confirm:
            return err(-32000, "This workflow ends in an action (click/submit). Retry with confirm=true.")
        job = {
            "id": "mcp",
            "message": f"MCP tools/call {name}",
            "usage": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": False},
            "flagged": [],
            "calls": [
                {
                    "id": "c1",
                    "tool": name,
                    "workflow_id": stored.id,
                    "workflow_name": stored.definition.name,
                    "arguments": arguments,
                    "action_ending": is_action_tool(tool),
                }
            ],
        }
        await orch.execute(job, emit=None)
        item = (job.get("items") or [{}])[0]
        text = item.get("facts") or item.get("error") or json.dumps(item.get("outputs") or {}, ensure_ascii=False)
        return ok(
            {
                "content": [{"type": "text", "text": text}],
                "isError": item.get("status") != "succeeded",
                "structuredContent": item,
            }
        )
    return err(-32601, f"Unsupported method '{method}'.")


def is_action_tool(tool: dict[str, Any]) -> bool:
    return bool(tool.get("action_ending"))


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str, ensure_ascii=False)}\n\n"

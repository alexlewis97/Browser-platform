"""REST + WebSocket endpoints for website remote-browser recording."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from browser_platform.authoring.remote_record import SessionBusy, manager_from
from browser_platform.local_access import access_token_ok

router = APIRouter()


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    start_url: str = ""
    goal: str = ""
    name: str = ""


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    x: float | None = None
    y: float | None = None
    text: str | None = None
    key: str | None = None
    url: str | None = None
    deltaX: float | None = None
    deltaY: float | None = None


class LocateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    selector: str | None = None
    text: str | None = None
    role: str | None = None


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    goal: str = ""
    name: str = ""


def _manager(request: Request):
    return manager_from(request.app)


@router.post("/api/record/sessions")
async def create_session(payload: SessionCreate, request: Request) -> dict[str, Any]:
    try:
        session = await _manager(request).create(start_url=payload.start_url, goal=payload.goal, name=payload.name)
    except SessionBusy as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not start a remote browser: {exc}") from exc
    return {"session": session.snapshot()}


@router.get("/api/record/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> dict[str, Any]:
    session = _manager(request).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Recording session not found or already closed.")
    return {"session": session.snapshot()}


@router.get("/api/record/sessions/{session_id}/frame")
async def get_frame(session_id: str, request: Request) -> dict[str, Any]:
    session = _manager(request).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Recording session not found or already closed.")
    return session.frame_payload()


@router.post("/api/record/sessions/{session_id}/input")
async def post_input(session_id: str, payload: SessionInput, request: Request) -> dict[str, Any]:
    session = _manager(request).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Recording session not found or already closed.")
    try:
        await session.handle_input(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"session": session.snapshot()}


@router.post("/api/record/sessions/{session_id}/locate")
async def post_locate(session_id: str, payload: LocateRequest, request: Request) -> dict[str, Any]:
    session = _manager(request).get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Recording session not found or already closed.")
    box = await session.locate(selector=payload.selector, text=payload.text, role=payload.role)
    return {"match": box, "session": session.snapshot()}


@router.post("/api/record/sessions/{session_id}/stop")
async def stop_session(session_id: str, payload: StopRequest, request: Request) -> dict[str, Any]:
    manager = _manager(request)
    session = manager.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Recording session not found or already closed.")
    try:
        result = await session.stop_and_clean(goal=payload.goal, name=payload.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await manager.drop(session_id)
    return result


@router.delete("/api/record/sessions/{session_id}")
async def cancel_session(session_id: str, request: Request) -> dict[str, Any]:
    await _manager(request).drop(session_id)
    return {"ok": True}


@router.websocket("/ws/record/{session_id}")
async def record_socket(ws: WebSocket, session_id: str) -> None:
    settings = getattr(ws.app.state, "settings", None)
    if not access_token_ok(ws, settings):
        await ws.close(code=1008, reason="invalid or missing access token")
        return
    manager = manager_from(ws.app)
    session = manager.get(session_id)
    if session is None:
        await ws.close(code=1008, reason="unknown session")
        return
    await ws.accept()

    async def push(message: dict[str, Any]) -> None:
        await ws.send_json(message)

    session.add_client(push)
    session.touch()
    await ws.send_json({"type": "hello", "session": session.snapshot()})
    if session.latest_frame:
        await ws.send_json(session.frame_payload())
    try:
        while True:
            message = await ws.receive_json()
            typ = message.get("type")
            if typ == "ping":
                await ws.send_json({"type": "pong", "session": session.snapshot()})
                session.touch()
            elif typ in {"click", "down", "up", "move", "scroll", "type", "key", "navigate"}:
                try:
                    await session.handle_input(message)
                    await ws.send_json({"type": "ack", "session": session.snapshot()})
                except Exception as exc:
                    await ws.send_json({"type": "error", "error": str(exc)})
            else:
                await ws.send_json({"type": "ack", "received": typ})
    except WebSocketDisconnect:
        pass
    finally:
        session.drop_client(push)

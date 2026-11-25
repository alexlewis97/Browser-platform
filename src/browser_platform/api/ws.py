"""WebSocket endpoints for run events and the future Chrome extension bridge."""

from __future__ import annotations

import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from browser_platform.local_access import access_token_ok, origin_allowed

router = APIRouter()

PROTOCOL = 1


async def _accept_if_authorized(ws: WebSocket) -> bool:
    settings = getattr(ws.app.state, "settings", None)
    origin = ws.headers.get("origin")
    if not origin_allowed(origin, settings):
        await ws.close(code=1008, reason="origin not allowed")
        return False
    # Browser WebSocket API cannot set custom headers — query token is allowed here only.
    if not access_token_ok(ws, settings, allow_query=True):
        await ws.close(code=1008, reason="invalid or missing access token")
        return False
    await ws.accept()
    return True


@router.websocket("/ws/extension")
async def extension_socket(ws: WebSocket) -> None:
    if not await _accept_if_authorized(ws):
        return
    await ws.send_json(
        {
            "type": "hello_ack",
            "server": "browser-platform",
            "protocol": PROTOCOL,
            # Only advertise features that are actually wired.
            "features": ["questions", "run_events", "needs_confirmation", "workflow_use_recorder"],
            "notes": {
                "cdp_relay": "Not implemented; use --remote-debugging-port / chrome://inspect/#remote-debugging.",
                "playwright_mcp_bridge": "Reserved; not wired.",
            },
        }
    )
    try:
        while True:
            message = await ws.receive_json()
            typ = message.get("type")
            if typ == "hello":
                await ws.send_json({"type": "hello_ack", "protocol": PROTOCOL})
            elif typ == "ping":
                await ws.send_json({"type": "pong"})
            elif typ in {"cdp_relay", "cdp_relay_ready", "cdp_message", "playwright_mcp_bridge", "mcp_extension"}:
                await ws.send_json(
                    {
                        "type": "error",
                        "for": typ,
                        "message": "Feature not implemented in this build.",
                    }
                )
            else:
                # Do not echo arbitrary client types as a control channel.
                await ws.send_json({"type": "error", "message": f"unsupported message type '{typ}'"})
    except WebSocketDisconnect:
        return


@router.websocket("/ws/runs")
async def runs_socket(ws: WebSocket) -> None:
    """Live run events for the mobile/web client, including needs_confirmation."""

    if not await _accept_if_authorized(ws):
        return
    run_filter = (ws.query_params.get("run_id") or ws.query_params.get("job_id") or "").strip()
    await ws.send_json(
        {
            "type": "hello_ack",
            "server": "browser-platform",
            "protocol": PROTOCOL,
            "features": ["run_events", "needs_confirmation", "mint_events"],
            "run_id": run_filter or None,
            "job_id": (ws.query_params.get("job_id") or "").strip() or None,
        }
    )
    hub = getattr(ws.app.state, "event_hub", None)
    if hub is None:
        try:
            while True:
                message = await ws.receive_json()
                if message.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
        except WebSocketDisconnect:
            return
        return

    queue = hub.subscribe()

    async def pump() -> None:
        while True:
            event = await queue.get()
            if run_filter:
                eid = str(event.get("run_id") or event.get("job_id") or "")
                if eid != run_filter:
                    continue
            await ws.send_json(event)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            message = await ws.receive_json()
            typ = message.get("type")
            if typ == "ping":
                await ws.send_json({"type": "pong"})
            elif typ == "hello":
                await ws.send_json(
                    {
                        "type": "hello_ack",
                        "protocol": PROTOCOL,
                        "features": ["run_events", "needs_confirmation", "mint_events"],
                    }
                )
            else:
                await ws.send_json({"type": "error", "message": f"unsupported message type '{typ}'"})
    except WebSocketDisconnect:
        return
    finally:
        pump_task.cancel()
        hub.unsubscribe(queue)

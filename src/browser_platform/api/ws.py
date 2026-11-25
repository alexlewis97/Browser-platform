"""WebSocket stub for the future Chrome extension (and Playwright MCP extension bridge)."""

from __future__ import annotations

import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from browser_platform.local_access import access_token_ok

router = APIRouter()

PROTOCOL = 1


def _ws_token(ws: WebSocket) -> str:
    header = ws.headers.get("x-browser-platform-token") or ws.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    if header:
        return header.strip()
    return (ws.query_params.get("access_token") or ws.query_params.get("token") or "").strip()


@router.websocket("/ws/extension")
async def extension_socket(ws: WebSocket) -> None:
    settings = getattr(ws.app.state, "settings", None)
    if not access_token_ok(ws, settings):
        await ws.close(code=1008, reason="invalid or missing access token")
        return
    await ws.accept()
    await ws.send_json(
        {
            "type": "hello_ack",
            "server": "browser-platform",
            "protocol": PROTOCOL,
            "features": [
                "cdp_relay",
                "playwright_mcp_bridge",
                "questions",
                "run_events",
                "needs_confirmation",
                "workflow_use_recorder",
            ],
            "notes": {
                "cdp_relay": "The extension will attach chrome.debugger and relay CDP to this socket. Not implemented in v1; use --remote-debugging-port or chrome://inspect/#remote-debugging for now.",
                "playwright_mcp_bridge": "Reserved for connecting Playwright MCP --extension to the same user Chrome session (SSO / existing tabs).",
            },
        }
    )
    try:
        while True:
            message = await ws.receive_json()
            typ = message.get("type")
            if typ == "hello":
                await ws.send_json({"type": "hello_ack", "protocol": PROTOCOL})
            elif typ in {"cdp_relay", "cdp_relay_ready", "cdp_message"}:
                await ws.send_json(
                    {
                        "type": "stub",
                        "for": typ,
                        "message": "CDP relay is stubbed in v1. Point the backend at the browser's existing CDP endpoint instead.",
                    }
                )
            elif typ in {"playwright_mcp_bridge", "mcp_extension"}:
                await ws.send_json(
                    {
                        "type": "stub",
                        "for": typ,
                        "message": "Playwright MCP extension bridge is reserved; not wired in v1.",
                    }
                )
            elif typ == "ping":
                await ws.send_json({"type": "pong"})
            else:
                await ws.send_json({"type": "ack", "received": typ})
    except WebSocketDisconnect:
        return



@router.websocket("/ws/runs")
async def runs_socket(ws: WebSocket) -> None:
    """Live run events for the mobile/web client, including needs_confirmation."""

    settings = getattr(ws.app.state, "settings", None)
    if not access_token_ok(ws, settings):
        await ws.close(code=1008, reason="invalid or missing access token")
        return
    await ws.accept()
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
                await ws.send_json({"type": "hello_ack", "protocol": PROTOCOL, "features": ["run_events", "needs_confirmation", "mint_events"]})
            else:
                await ws.send_json({"type": "ack", "received": typ})
    except WebSocketDisconnect:
        return
    finally:
        pump_task.cancel()
        hub.unsubscribe(queue)

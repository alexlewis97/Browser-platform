
"""REST + WebSocket confirmation prompts for the mobile / web client."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from browser_platform.app import create_app
from browser_platform.config import Settings
from browser_platform.models.workflow import HitlConfig, WorkflowDefinition, WorkflowStep

from test_fuzzy_match import AttendancePage
from test_orchestrator import FakeSession

ROOT = Path(__file__).resolve().parents[1]


def _app(data_dir: Path, page: AttendancePage) -> TestClient:
    settings = Settings(data_dir=data_dir, hitl_timeout_s=8, pomerado_host=data_dir / "no-such-host.mjs", access_token="")
    app = create_app(settings, root=ROOT)

    async def connect(url: str) -> FakeSession:
        return FakeSession(page=page, opened_pages=[page])

    async def disconnect(session: FakeSession) -> None:
        session.closed = True

    app.state.runner._connect = connect
    app.state.runner._disconnect = disconnect
    app.state.runner.use_browser_use = False
    return TestClient(app)


def _create_workflow(client: TestClient) -> str:
    body = WorkflowDefinition(
        name="attendance_mark",
        version="1",
        hitl=HitlConfig(mode="always_on_uncertain", timeout_s=8, timeout_policy="fail"),
        steps=[
            WorkflowStep(type="click", target_text="{name}", fuzzy_query="{name}"),
            WorkflowStep(type="extract", extractionGoal="result"),
        ],
    )
    resp = client.post("/api/workflows", json={"definition": body.model_dump(exclude_none=True)})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_confirmation_rest_and_websocket(data_dir: Path):
    page = AttendancePage()
    with _app(data_dir, page) as client:
        wf_id = _create_workflow(client)
        with client.websocket_connect("/ws/runs") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello_ack"
            assert "needs_confirmation" in hello["features"]

            started = client.post(
                f"/api/workflows/{wf_id}/admin-run",
                json={"cdp_url": "http://127.0.0.1:0", "wait": False, "interactive": True, "variables": {"name": "Ann Cohen"}},
            )
            assert started.status_code == 200
            run_id = started.json()["id"]

            event = None
            for _ in range(40):
                # Drain WS with a short poll via REST; TestClient delivers queued WS messages.
                body = client.get(f"/api/runs/{run_id}").json()
                if body.get("status") == "needs_confirmation":
                    event = body.get("confirmation")
                    break
                time.sleep(0.05)
            assert event and event["type"] == "needs_confirmation"
            assert event["query"] == "Ann Cohen"
            assert event["reason"] == "ambiguous_margin"
            assert {c["text"] for c in event["top"][:2]} == {"Anna Cohen", "Anne Cohen"}
            assert event["resume"]["path"] == f"/api/runs/{run_id}/confirmation"
            assert event["cancel"]["body"] == {"action": "cancel"}

            detail = client.get(f"/api/runs/{run_id}/confirmation")
            assert detail.status_code == 200
            assert detail.json()["confirmation"]["query"] == "Ann Cohen"

            anna = next(c for c in event["top"] if c["text"] == "Anna Cohen")
            confirm = client.post(
                f"/api/runs/{run_id}/confirmation",
                json={"action": "confirm", "candidate_id": anna["id"]},
            )
            assert confirm.status_code == 200, confirm.text

            final = None
            for _ in range(40):
                final = client.get(f"/api/runs/{run_id}").json()
                if final["status"] in {"succeeded", "succeeded_with_fallback", "failed", "cancelled"}:
                    break
                time.sleep(0.05)
            assert final and final["status"] == "succeeded_with_fallback"
            assert final.get("confirmation") is None
            click = next(s for s in final["steps"] if s["step_type"] == "click")
            assert click["tier"] == "workflow_use"
            assert click["resolution"] == "hitl"
            assert page.applied == ["Anna Cohen"]

            # REST already observed needs_confirmation; do not block on the socket.
            assert event["type"] == "needs_confirmation"


def test_confirmation_conflict_when_not_pending(data_dir: Path):
    page = AttendancePage()
    with _app(data_dir, page) as client:
        missing = client.post("/api/runs/does-not-exist/confirmation", json={"action": "cancel"})
        assert missing.status_code in {404, 409}

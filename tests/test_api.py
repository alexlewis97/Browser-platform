from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from browser_platform.app import create_app
from browser_platform.config import Settings
from browser_platform.models.workflow import WorkflowDefinition, WorkflowStep


def _app(data_dir: Path):
    settings = Settings(data_dir=data_dir, pomerado_host=Path(__file__).resolve().parents[1] / "node_host" / "src" / "stock-mcp.mjs")
    return create_app(settings, root=Path(__file__).resolve().parents[1])


def test_health_and_create_list_get(data_dir: Path):
    client = TestClient(_app(data_dir))
    assert client.get("/health").json()["ok"] is True
    payload = {
        "name": "example",
        "description": "read heading",
        "version": "1",
        "steps": [
            {"type": "navigation", "url": "https://example.com"},
            {"type": "extract", "extractionGoal": "heading"},
        ],
    }
    created = client.post("/api/workflows", json=payload)
    assert created.status_code == 200, created.text
    wf_id = created.json()["id"]
    listed = client.get("/api/workflows").json()["workflows"]
    assert any(w["id"] == wf_id for w in listed)
    got = client.get(f"/api/workflows/{wf_id}")
    assert got.status_code == 200
    assert got.json()["definition"]["name"] == "example"


def test_run_record_does_not_echo_api_key(data_dir: Path):
    client = TestClient(_app(data_dir))
    # Creating is enough; a failed run still must not persist the key.
    created = client.post(
        "/api/workflows",
        json={
            "name": "x",
            "description": "x",
            "version": "1",
            "steps": [
                {"type": "navigation", "url": "https://example.com"},
                {"type": "extract", "extractionGoal": "heading"},
            ],
        },
    )
    wf_id = created.json()["id"]
    # CDP will fail (nothing listening) — that's fine.
    response = client.post(
        f"/api/workflows/{wf_id}/run",
        json={"cdp_url": "http://127.0.0.1:1", "llm": {"api_key": "sk-or-v1-should-never-be-stored", "model": "qwen/qwen3.7-flash"}},
    )
    assert response.status_code == 200
    body = response.text
    assert "sk-or-v1-should-never-be-stored" not in body
    run_id = response.json()["id"]
    raw = (data_dir / "runs" / f"{run_id}.json").read_text()
    assert "sk-or-v1-should-never-be-stored" not in raw


def test_extension_websocket_stub(data_dir: Path):
    client = TestClient(_app(data_dir))
    with client.websocket_connect("/ws/extension") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello_ack"
        assert "cdp_relay" in hello["features"]
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        ws.send_json({"type": "cdp_relay"})
        stub = ws.receive_json()
        assert stub["type"] == "stub"


def test_pomerado_mint_via_node_host(data_dir: Path):
    client = TestClient(_app(data_dir))
    response = client.post(
        "/api/pomerado/mint",
        json={
            "name": "example_reader",
            "url": "https://example.com",
            "intent": "Read the main heading",
            "effect": "read",
            "llm": {"api_key": "sk-or-v1-not-used-in-stub", "model": "qwen/qwen3.7-flash"},
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result.get("status") == "incomplete"
    assert result.get("ok") is False
    assert "sk-or-v1-not-used-in-stub" not in response.text
    assert (result.get("output") or {}).get("build") == "incomplete"


def test_access_token_required_when_configured(data_dir: Path):
    settings = Settings(
        data_dir=data_dir,
        pomerado_host=Path(__file__).resolve().parents[1] / "node_host" / "src" / "stock-mcp.mjs",
        access_token="test-token-not-for-production",
        cors_origins="https://example-app.vercel.app",
    )
    client = TestClient(create_app(settings, root=Path(__file__).resolve().parents[1]))
    assert client.get("/health").status_code == 200
    denied = client.get("/api/workflows")
    assert denied.status_code == 401
    allowed = client.get("/api/workflows", headers={"X-Browser-Platform-Token": "test-token-not-for-production"})
    assert allowed.status_code == 200
    bearer = client.get("/api/workflows", headers={"Authorization": "Bearer test-token-not-for-production"})
    assert bearer.status_code == 200
    wrong = client.get("/api/workflows", headers={"X-Browser-Platform-Token": "nope"})
    assert wrong.status_code == 401

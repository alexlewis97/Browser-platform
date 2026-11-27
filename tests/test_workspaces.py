from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from browser_platform.app import create_app
from browser_platform.config import Settings
from browser_platform.orchestrator.chat import ChatOrchestrator
from browser_platform.store.file_store import FileStore
from browser_platform.workspaces import WorkspaceStore, resolve_scope

from test_skills import USAGE, _library

ROOT = Path(__file__).resolve().parents[1]


def _app(data_dir: Path):
    app = create_app(Settings(data_dir=data_dir, pomerado_host=ROOT / "node_host" / "src" / "stock-mcp.mjs"), root=ROOT)
    _library(app.state.store)
    return app


def _ids(store: FileStore) -> dict[str, str]:
    return {w.definition.name: w.id for w in store.list_workflows()}


def test_store_crud_slug_and_forget(data_dir: Path):
    ws = WorkspaceStore(data_dir)
    a = ws.create("Sports", description="BBC Sport", workflow_ids=["x", "y", "x"])
    assert a.slug == "sports" and a.workflow_ids == ["x", "y"]
    assert ws.create("Sports").slug == "sports-2"
    assert ws.create("All").slug == "all-workspace"
    assert resolve_scope(ws, None) is None and resolve_scope(ws, "all") is None
    assert resolve_scope(ws, "sports").id == a.id
    with pytest.raises(KeyError):
        resolve_scope(ws, "nope")
    ws.update("sports", name="Sport", instructions="Answer briefly.")
    ws.add(a.id, ["z"])
    ws.remove(a.id, ["y"])
    ws.forget_workflow("x")
    got = ws.get(a.id)
    assert got.name == "Sport" and got.slug == "sports" and got.workflow_ids == ["z"]
    ws.delete("sports")
    assert ws.get(a.id) is None and (data_dir / "workspaces.json").is_file()


def test_workspace_api_scopes_skills_mcp_and_catalog(data_dir: Path):
    app = _app(data_dir)
    client = TestClient(app)
    ids = _ids(app.state.store)
    sports = client.post("/api/workspaces", json={"name": "Sports", "description": "BBC Sport", "workflow_ids": [ids["football_fixtures"]]}).json()
    other = client.post("/api/workspaces", json={"name": "Forms", "workflow_ids": [ids["httpbin_form_post"], ids["books_category"]]}).json()
    assert sports["slug"] == "sports" and sports["workflows"][0]["name"] == "football_fixtures"
    assert client.post("/api/workspaces", json={"name": "Bad", "workflow_ids": ["missing"]}).status_code == 400
    assert [w["slug"] for w in client.get("/api/workspaces").json()["workspaces"]] == ["sports", "forms"]

    # Skills / search / tools
    assert [s["slug"] for s in client.get("/api/skills", params={"workspace": "sports"}).json()["skills"]] == ["football_fixtures"]
    assert client.get("/api/skills").json()["count"] == 33  # no workspace = whole library
    assert client.get("/api/skills", params={"workspace": "all"}).json()["count"] == 33
    assert client.get("/api/skills", params={"workspace": "nope"}).status_code == 404
    found = client.get("/api/skills/search", params={"q": "books prices form", "workspace": "sports"}).json()
    assert {s["slug"] for s in found["skills"]} <= {"football_fixtures"}
    assert [t["name"] for t in client.get("/api/tools", params={"workspace": "forms"}).json()["mcp"]] == ["books_category", "httpbin_form_post"]

    # MCP per workspace: path, query and params all scope; out-of-scope calls are unknown.
    for url, params in (("/mcp/sports", {}), ("/mcp?workspace=sports", {}), ("/mcp", {"workspace": sports["id"]})):
        res = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": params}).json()["result"]
        assert [t["name"] for t in res["tools"]] == ["football_fixtures"]
    init = client.post("/mcp/sports", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}).json()["result"]
    assert init["serverInfo"]["name"] == "browser-platform/sports" and "BBC Sport" in init["instructions"]
    call = client.post("/mcp/sports", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "books_category", "arguments": {}}}).json()
    assert "Unknown tool" in call["error"]["message"]
    assert "Unknown workspace" in client.post("/mcp/nope", json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"}).json()["error"]["message"]

    # Public catalogue
    assert [w["name"] for w in client.get("/api/catalog", params={"workspace": "sports"}).json()["workflows"]] == ["football_fixtures"]
    pub = client.get("/api/catalog/workspaces").json()["workspaces"]
    assert {w["slug"] for w in pub} == {"sports", "forms"} and "instructions" not in pub[0] and "workflow_ids" not in pub[0]

    # Membership edits, rename, workflow listing, assign at creation, delete cascade.
    client.post(f"/api/workspaces/{sports['id']}/workflows", json={"workflow_ids": [ids["books_category"]]})
    assert len(client.get("/api/workspaces/sports").json()["workflows"]) == 2
    client.request("DELETE", "/api/workspaces/sports/workflows", json={"workflow_ids": [ids["books_category"]]})
    assert client.patch("/api/workspaces/sports", json={"name": "Sport", "instructions": "Prefer BBC"}).json()["name"] == "Sport"
    listed = {w["name"]: w for w in client.get("/api/workflows").json()["workflows"]}
    assert listed["books_category"]["workspaces"] == ["forms"]
    assert [w["name"] for w in client.get("/api/workflows", params={"workspace": "sports"}).json()["workflows"]] == ["football_fixtures"]
    created = client.post(
        "/api/workflows",
        json={"name": "rugby_scores", "version": "1", "steps": [{"type": "navigation", "url": "https://www.bbc.com/sport/rugby-union"}, {"type": "extract", "extractionGoal": "scores", "output": "extract"}], "workspace_ids": ["sports"]},
    ).json()
    assert created.get("workspaces") == ["sports"], created
    assert created["id"] in client.get("/api/workspaces/sports").json()["workflow_ids"]
    client.delete(f"/api/workflows/{created['id']}")
    assert created["id"] not in client.get("/api/workspaces/sports").json()["workflow_ids"]
    assert client.delete("/api/workspaces/forms").json()["ok"] is True
    assert client.get("/api/workspaces/forms").status_code == 404
    assert ids["books_category"] in _ids(app.state.store).values()  # deleting a workspace keeps workflows


@pytest.mark.asyncio
async def test_chat_in_workspace_never_sees_or_calls_outside_tools(data_dir: Path, monkeypatch):
    store = FileStore(data_dir)
    _library(store)
    orch = ChatOrchestrator(store)
    ids = _ids(store)
    orch.workspaces.create("Sports", description="BBC Sport for one club", instructions="Always answer in one sentence.", workflow_ids=[ids["football_fixtures"]])
    prompts: list[str] = []

    async def fake(messages, **kwargs):
        prompts.append(json.dumps(messages))
        # A misbehaving planner that also asks for an out-of-scope tool.
        plan = {
            "intents": ["fixtures", "books"],
            "steps": [
                {"id": "c1", "tool": "football_fixtures", "arguments": {"team": "Arsenal"}, "intent": 0},
                {"id": "c2", "tool": "books_category", "arguments": {"category": "travel_2"}, "intent": 1},
            ],
        }
        return {"role": "assistant", "content": json.dumps(plan)}, USAGE

    monkeypatch.setattr("browser_platform.orchestrator.chat.chat_complete", fake)
    monkeypatch.setattr("browser_platform.orchestrator.planner.chat_complete", fake)
    monkeypatch.setattr("browser_platform.orchestrator.chat.env_api_key", lambda: "sk-test")
    job = await orch.plan("Arsenal fixtures and cheap travel books", history=[], workspace="sports")
    assert job["workspace"]["slug"] == "sports"
    assert [c["tool"] for c in job["calls"]] == ["football_fixtures"]
    assert "football_fixtures" in json.dumps(job["skills_considered"])
    assert "books_category" not in json.dumps(job["skills_considered"])
    joined = "\n".join(prompts)
    # No out-of-scope skill card is ever shown to the model (cards are rendered as "name(").
    assert "books_category(" not in joined and "decoy_widget" not in joined and "httpbin_form_post" not in joined
    assert any("not available" in str(n) or "books_category" in str(n) for n in job.get("notes") or []) or job.get("uncovered")
    assert "Always answer in one sentence." in prompts[0]

    with pytest.raises(KeyError):
        await orch.plan("x", history=[], workspace="missing")
    empty = orch.workspaces.create("Empty")
    job = await orch.plan("anything", history=[], workspace=empty.slug)
    assert job["status"] == "failed" and "no workflows" in job["error"]

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from browser_platform.app import create_app
from browser_platform.authoring.input_validate import apply_patch, deterministic_findings
from browser_platform.authoring.tools import is_action_ending, tools_from_workflows
from browser_platform.config import Settings
from browser_platform.executor.output_validate import deterministic_verdict
from browser_platform.models.workflow import StoredWorkflow, WorkflowDefinition, WorkflowInput, WorkflowStep
from browser_platform.orchestrator.chat import (
    ChatOrchestrator,
    compact_item,
    public_job,
    _fallback_answer,
)
from browser_platform.store.file_store import FileStore


def _app(data_dir: Path):
    settings = Settings(
        data_dir=data_dir,
        pomerado_host=Path(__file__).resolve().parents[1] / "node_host" / "src" / "stock-mcp.mjs",
    )
    return create_app(settings, root=Path(__file__).resolve().parents[1])


def _fixtures_def(**kwargs) -> WorkflowDefinition:
    data = dict(
        name="football_fixtures",
        description="Upcoming fixtures for a team",
        version="1",
        input_schema=[WorkflowInput(name="team", type="string", required=True, label="Team", default="Arsenal")],
        steps=[
            WorkflowStep(type="navigation", url="https://www.bbc.com/sport/football/teams/{team}/scores-fixtures"),
            WorkflowStep(type="extract", extractionGoal="upcoming fixtures", output="extract"),
        ],
    )
    data.update(kwargs)
    return WorkflowDefinition.model_validate(data)


def test_tools_expose_required_inputs(data_dir: Path):
    store = FileStore(data_dir)
    stored = store.create_workflow(_fixtures_def())
    tools = tools_from_workflows([stored])
    assert len(tools) == 1
    fn = tools[0]["function"]
    assert "team" in fn["parameters"]["required"]
    assert tools[0]["workflow_id"] == stored.id
    assert tools[0]["action_ending"] is False


def test_action_ending_detects_final_click():
    definition = WorkflowDefinition(
        name="mark_attendance",
        version="1",
        steps=[
            WorkflowStep(type="navigation", url="https://example.com/form"),
            WorkflowStep(type="click", target_text="Submit"),
            WorkflowStep(type="extract", extractionGoal="confirmation"),
        ],
    )
    assert is_action_ending(definition) is True
    assert is_action_ending(_fixtures_def()) is False


def test_validate_inputs_flags_unused_and_hardcoded():
    definition = WorkflowDefinition(
        name="bad_fixtures",
        version="1",
        description="Find fixtures for a football team",
        input_schema=[WorkflowInput(name="color", type="string", required=True, default="blue")],
        steps=[
            WorkflowStep(type="navigation", url="https://www.bbc.com/sport/football/teams/Arsenal/scores-fixtures"),
            WorkflowStep(type="extract", extractionGoal="result", output="extract"),
        ],
    )
    findings = deterministic_findings(definition, task="Find fixtures for a football team")
    codes = {item["code"] for item in findings}
    assert "unused_input" in codes
    assert "missing_team_input" in codes or "hardcoded_example" in codes
    assert any(item["severity"] == "error" for item in findings)


def test_good_schema_has_no_errors():
    findings = deterministic_findings(_fixtures_def())
    assert not any(item["severity"] == "error" for item in findings)


def test_apply_patch_adds_and_binds_team():
    definition = WorkflowDefinition(
        name="bad",
        version="1",
        input_schema=[WorkflowInput(name="color", type="string", required=True, default="blue")],
        steps=[
            WorkflowStep(type="navigation", url="https://www.bbc.com/sport/football/teams/Arsenal/scores-fixtures"),
            WorkflowStep(type="extract", extractionGoal="result", output="extract"),
        ],
    )
    definition = apply_patch(definition, {"op": "remove_param", "name": "color"})
    definition = apply_patch(definition, {"op": "add_param", "name": "team", "label": "Team", "example": "Arsenal"})
    definition = apply_patch(definition, {"op": "bind_url", "name": "team", "example": "Arsenal"})
    assert [i.name for i in definition.input_schema] == ["team"]
    assert "{team}" in (definition.steps[0].url or "")


def test_output_validation_wrong_team_is_deterministic():
    result = deterministic_verdict(
        inputs={"team": "Chelsea"},
        sample="Arsenal fixtures November 2026. Arsenal vs Liverpool, 29 November.",
        url="https://www.bbc.com/sport/football/teams/arsenal/scores-fixtures",
        title="Arsenal scores & fixtures",
    )
    assert result["verdict"] == "wrong"
    ok = deterministic_verdict(
        inputs={"team": "Arsenal"},
        sample="Arsenal fixtures November 2026. Arsenal vs Liverpool, 29 November.",
        url="https://www.bbc.com/sport/football/teams/arsenal/scores-fixtures",
        title="Arsenal scores & fixtures",
    )
    assert ok["verdict"] == "ok"


def test_compact_item_and_fallback_mentions_flagged():
    item = compact_item(
        {
            "id": "c1",
            "workflow_name": "football_fixtures",
            "inputs": {"team": "Arsenal"},
            "status": "succeeded",
            "outputs": {"extract": "Arsenal vs Leeds, 1 December 2026, 20:00"},
            "validation": {"verdict": "ok", "reason": "matches"},
        }
    )
    assert item["workflow"] == "football_fixtures"
    assert "Leeds" in item["facts"]
    failed = compact_item(
        {
            "id": "c2",
            "workflow_name": "football_fixtures",
            "inputs": {"team": "Spurs"},
            "status": "failed",
            "error": "timeout",
            "outputs": {},
        }
    )
    answer = _fallback_answer({"flagged": [{"workflow": "football_fixtures", "inputs": {"team": "Spurs"}, "verdict": "failed", "reason": "timeout"}]}, [item, failed])
    assert "Spurs" in answer
    assert "timeout" in answer


def test_api_tools_validate_and_mcp(data_dir: Path):
    client = TestClient(_app(data_dir))
    created = client.post(
        "/api/workflows",
        json={
            "name": "football_fixtures",
            "description": "Upcoming fixtures for a team",
            "version": "1",
            "input_schema": [{"name": "team", "type": "string", "required": True, "label": "Team", "default": "Arsenal"}],
            "steps": [
                {"type": "navigation", "url": "https://www.bbc.com/sport/football/teams/{team}/scores-fixtures"},
                {"type": "extract", "extractionGoal": "fixtures", "output": "extract"},
            ],
        },
    )
    assert created.status_code == 200, created.text
    tools = client.get("/api/tools").json()["tools"]
    assert any("team" in (t["function"]["parameters"].get("required") or []) for t in tools)

    bad = client.post(
        "/api/workflows/validate-inputs",
        json={
            "task": "Find fixtures for a football team",
            "use_llm": False,
            "definition": {
                "name": "bad_fixtures",
                "version": "1",
                "input_schema": [{"name": "color", "type": "string", "required": True, "default": "blue"}],
                "steps": [
                    {"type": "navigation", "url": "https://www.bbc.com/sport/football/teams/Arsenal/scores-fixtures"},
                    {"type": "extract", "extractionGoal": "result", "output": "extract"},
                ],
            },
        },
    )
    assert bad.status_code == 200, bad.text
    body = bad.json()
    assert body["blocks_publish"] is True
    assert body["error_count"] >= 1

    out = client.post(
        "/api/runs/validate-output",
        json={
            "request": "fixtures for Chelsea",
            "inputs": {"team": "Chelsea"},
            "sample": "Arsenal fixtures November 2026. Next: Arsenal vs Liverpool.",
            "url": "https://www.bbc.com/sport/football/teams/arsenal/scores-fixtures",
            "title": "Arsenal scores & fixtures",
            "use_llm": False,
        },
    )
    assert out.status_code == 200, out.text
    assert out.json()["verdict"] == "wrong"

    mcp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert mcp.status_code == 200
    listed = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listed.status_code == 200
    names = [t["name"] for t in listed.json()["result"]["tools"]]
    assert any("football" in n for n in names)


@pytest.mark.asyncio
async def test_plan_asks_for_missing_inputs_and_confirms_action(data_dir: Path, monkeypatch):
    store = FileStore(data_dir)
    fixtures = store.create_workflow(_fixtures_def())
    action = store.create_workflow(
        WorkflowDefinition(
            name="submit_form",
            version="1",
            input_schema=[WorkflowInput(name="name", type="string", required=True, default="Alex")],
            steps=[
                WorkflowStep(type="navigation", url="https://example.com/form"),
                WorkflowStep(type="input", value="{name}", cssSelector="#name"),
                WorkflowStep(type="click", target_text="Submit"),
            ],
        )
    )
    orch = ChatOrchestrator(store, runner=None)

    async def fake_complete(messages, **kwargs):
        tools = kwargs.get("tools") or []
        names = [t["function"]["name"] for t in tools]
        user = messages[-1]["content"]
        if "missing" in user.lower() or "fixtures for a team" in user.lower():
            # call football without team
            fname = next(n for n in names if "football" in n)
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": fname, "arguments": "{}"}}]}, {"tokens_in": 10, "tokens_out": 5, "cost_usd": 0, "used": True}
        if "submit" in user.lower():
            fname = next(n for n in names if "submit" in n)
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": fname, "arguments": "{\"name\":\"Alex\"}"}}]}, {"tokens_in": 8, "tokens_out": 4, "cost_usd": 0, "used": True}
        if "first" in user.lower():
            fname = next(n for n in names if "football" in n)
            calls = [
                {"function": {"name": fname, "arguments": '{"team":"Arsenal"}'}},
                {"function": {"name": fname, "arguments": '{"team":"Chelsea"}'}},
                {"function": {"name": fname, "arguments": '{"team":"Spurs"}'}},
            ]
            return {"role": "assistant", "content": "", "tool_calls": calls}, {"tokens_in": 20, "tokens_out": 12, "cost_usd": 0, "used": True}
        return {"role": "assistant", "content": "ok"}, {"tokens_in": 1, "tokens_out": 1, "cost_usd": 0, "used": True}

    monkeypatch.setattr("browser_platform.orchestrator.chat.chat_complete", fake_complete)
    monkeypatch.setattr("browser_platform.orchestrator.chat.env_api_key", lambda: "sk-test")

    missing = await orch.plan("Get fixtures for a team", history=[], target="cloud")
    assert missing["status"] == "needs_input"
    assert "team" in (missing["question"] or "").lower()

    confirm = await orch.plan("Please submit the form", history=[], target="cloud")
    assert confirm["status"] == "needs_confirmation"
    assert "click" in (confirm["confirm_reason"] or "").lower() or "submit" in (confirm["confirm_reason"] or "").lower()

    fan = await orch.plan("Which of Arsenal, Chelsea and Spurs plays first?", history=[], target="cloud")
    assert fan["status"] == "planned"
    assert len(fan["calls"]) == 3
    teams = {c["arguments"]["team"] for c in fan["calls"]}
    assert teams == {"Arsenal", "Chelsea", "Spurs"}
    public = public_job(fan)
    assert "definition" not in (public["calls"][0] or {}) or public["calls"][0].get("definition") in (None, {})

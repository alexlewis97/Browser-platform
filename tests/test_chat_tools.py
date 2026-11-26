from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from browser_platform.app import create_app
from browser_platform.authoring.input_validate import apply_patch, deterministic_findings
from browser_platform.authoring.tools import is_action_ending, tools_from_workflows
from browser_platform.config import Settings
from browser_platform.executor.output_validate import deterministic_verdict, is_nav_footer_only
from browser_platform.models.workflow import StoredWorkflow, WorkflowDefinition, WorkflowInput, WorkflowStep
from browser_platform.orchestrator.chat import (
    ChatOrchestrator,
    compact_item,
    public_job,
    _fallback_answer,
)
from browser_platform.orchestrator.normalize_inputs import (
    is_entity_parameter,
    match_canonical_example,
    normalize_arguments,
    normalize_prepared_calls,
)
from types import SimpleNamespace
import json
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

    async def fake_normalize(messages, **kwargs):
        user = messages[-1]["content"]
        values = []
        try:
            payload = json.loads(user)
            for item in payload.get("inputs") or []:
                raw = str(item.get("value") or "")
                canonical = {
                    "Spurs": "Tottenham Hotspur",
                    "Man Utd": "Manchester United",
                    "Man United": "Manchester United",
                }.get(raw, raw)
                values.append({
                    "call": item.get("call"),
                    "param": item.get("param") or item.get("name"),
                    "canonical": canonical,
                    "slug": canonical.lower().replace(" ", "-"),
                })
        except Exception:
            pass
        return {"role": "assistant", "content": json.dumps({"values": values})}, {"tokens_in": 2, "tokens_out": 8, "cost_usd": 0, "used": True}

    monkeypatch.setattr("browser_platform.orchestrator.chat.chat_complete", fake_complete)
    monkeypatch.setattr("browser_platform.orchestrator.chat.env_api_key", lambda: "sk-test")
    monkeypatch.setattr("browser_platform.orchestrator.normalize_inputs.chat_complete", fake_normalize)
    monkeypatch.setattr("browser_platform.orchestrator.normalize_inputs.env_api_key", lambda: "sk-test")

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
    assert teams == {"Arsenal", "Chelsea", "Tottenham Hotspur"}
    raw_teams = {c["raw_arguments"]["team"] for c in fan["calls"]}
    assert raw_teams == {"Arsenal", "Chelsea", "Spurs"}
    public = public_job(fan)
    assert "definition" not in (public["calls"][0] or {}) or public["calls"][0].get("definition") in (None, {})


def test_nav_footer_only_extract_is_wrong_even_when_slug_is_in_url():
    chrome = (
        "Skip to content Home News Sport Weather Menu Search Sign in "
        "Cookie settings Privacy policy Terms of use Copyright BBC "
        "All rights reserved Scores & Fixtures Football Premier League"
    )
    assert is_nav_footer_only(chrome) is True
    result = deterministic_verdict(
        inputs={"team": "Spurs"},
        sample=chrome,
        url="https://www.bbc.com/sport/football/teams/spurs/scores-fixtures",
        title="Scores & Fixtures - Football - BBC Sport",
    )
    assert result["verdict"] == "wrong"
    fixtures = "Tottenham Hotspur vs Chelsea, 29 November 2026, 16:30. Kick-off 16:30."
    assert is_nav_footer_only(fixtures) is False
    assert is_nav_footer_only("You have selected Yes") is False
    assert is_nav_footer_only("Thank you for your order", action_ending=True) is False
    ok = deterministic_verdict(
        inputs={"team": "Tottenham Hotspur"},
        sample=fixtures,
        url="https://www.bbc.com/sport/football/teams/tottenham-hotspur/scores-fixtures",
        title="Tottenham Hotspur - Scores & Fixtures",
    )
    assert ok["verdict"] == "ok"


def test_entity_parameter_and_example_match():
    definition = _fixtures_def(
        steps=[
            WorkflowStep(type="navigation", url="https://www.bbc.com/sport/football/teams/{team|slug}/scores-fixtures"),
            WorkflowStep(type="extract", extractionGoal="upcoming fixtures", output="extract"),
        ]
    )
    assert is_entity_parameter("team", definition) is True
    assert is_entity_parameter("first_name", definition) is False
    assert is_entity_parameter("month", definition) is False
    assert is_entity_parameter("product", definition) is False
    named = WorkflowDefinition(
        name="club_page",
        version="1",
        steps=[
            WorkflowStep(type="navigation", url="https://example.com/clubs/{name|slug}"),
            WorkflowStep(type="extract", extractionGoal="club page", output="extract"),
        ],
        input_schema=[WorkflowInput(name="name", type="string", required=True)],
    )
    assert is_entity_parameter("name", named) is True
    assert match_canonical_example("arsenal", ["Arsenal", "Chelsea"]) == "Arsenal"
    assert match_canonical_example("tottenham-hotspur", ["Tottenham Hotspur"]) == "Tottenham Hotspur"


@pytest.mark.asyncio
async def test_llm_normalizes_nicknames_to_official_names():
    definition = _fixtures_def(
        steps=[
            WorkflowStep(type="navigation", url="https://www.bbc.com/sport/football/teams/{team|slug}/scores-fixtures"),
            WorkflowStep(type="extract", extractionGoal="upcoming fixtures", output="extract"),
        ]
    )

    async def fake_complete(messages, **kwargs):
        return {
            "role": "assistant",
            "content": json.dumps({
                "values": [
                    {"call": "c1", "param": "team", "canonical": "Tottenham Hotspur", "slug": "tottenham-hotspur"},
                    {"call": "c2", "param": "team", "canonical": "Manchester United", "slug": "manchester-united"},
                ]
            }),
        }, {"tokens_in": 3, "tokens_out": 10, "cost_usd": 0, "used": True}

    spurs, _ = await normalize_arguments(definition, {"team": "Spurs"}, call_id="c1", complete=fake_complete)
    assert spurs["team"] == "Tottenham Hotspur"
    man, _ = await normalize_arguments(definition, {"team": "Man Utd"}, call_id="c2", complete=fake_complete)
    assert man["team"] == "Manchester United"
    arsenal, _ = await normalize_arguments(definition, {"team": "Arsenal", "month": ""}, call_id="c3", complete=fake_complete)
    assert arsenal["team"] == "Arsenal"
    form = WorkflowDefinition(
        name="checkout",
        version="1",
        input_schema=[
            WorkflowInput(name="first_name", type="string", required=True),
            WorkflowInput(name="product", type="string", required=True),
        ],
        steps=[
            WorkflowStep(type="navigation", url="https://www.saucedemo.com/"),
            WorkflowStep(type="input", cssSelector="#first-name", value="{first_name}"),
            WorkflowStep(type="click", cssSelector="#add-to-cart-{product|slug}"),
        ],
    )
    kept, _ = await normalize_arguments(form, {"first_name": "Sam", "product": "Sauce Labs Bike Light"}, call_id="c9", complete=fake_complete)
    assert kept == {"first_name": "Sam", "product": "Sauce Labs Bike Light"}


@pytest.mark.asyncio
async def test_run_call_retries_chrome_only_extract_with_canonical_name(data_dir: Path, monkeypatch):
    store = FileStore(data_dir)
    stored = store.create_workflow(_fixtures_def(
        steps=[
            WorkflowStep(type="navigation", url="https://www.bbc.com/sport/football/teams/{team|slug}/scores-fixtures"),
            WorkflowStep(type="extract", extractionGoal="upcoming fixtures", output="extract"),
        ]
    ))

    class FakeRunner:
        def __init__(self):
            self.seen: list[str] = []

        async def run(self, _stored, **kwargs):
            team = (kwargs.get("variables") or {}).get("team")
            self.seen.append(team)
            if team == "Spurs":
                outputs = {"extract": "Home News Sport Menu Cookie settings Privacy Terms Copyright All rights reserved"}
                validation = {"verdict": "suspect", "reason": "only navigation and footer text", "usage": {"used": False}}
            else:
                outputs = {"extract": "Tottenham Hotspur vs Fulham, 29 November 2026, 16:30. Kick-off 16:30."}
                validation = {"verdict": "ok", "reason": "fixtures match", "usage": {"used": False}}
            return SimpleNamespace(
                id="run-" + str(len(self.seen)),
                status="succeeded",
                outputs=outputs,
                validation=validation,
                error=None,
                model_dump=lambda: {"validation": validation},
            )

    runner = FakeRunner()
    orch = ChatOrchestrator(store, runner=runner)

    async def fake_launch():
        return SimpleNamespace(cdp_url="http://127.0.0.1:9")

    async def fake_close(_isolated):
        return None

    async def fake_normalize(messages, **kwargs):
        return {
            "role": "assistant",
            "content": json.dumps({"values": [{"call": "c3", "param": "team", "canonical": "Tottenham Hotspur", "slug": "tottenham-hotspur"}]}),
        }, {"tokens_in": 1, "tokens_out": 4, "cost_usd": 0, "used": True}

    monkeypatch.setattr("browser_platform.orchestrator.chat.launch_isolated", fake_launch)
    monkeypatch.setattr("browser_platform.orchestrator.chat.close_isolated", fake_close)
    monkeypatch.setattr("browser_platform.orchestrator.normalize_inputs.chat_complete", fake_normalize)
    monkeypatch.setattr("browser_platform.orchestrator.normalize_inputs.env_api_key", lambda: "sk-test")

    job = {"id": "job1", "usage": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0, "used": False}, "flagged": []}
    # Simulate a planned call that was not normalised yet (raw nickname).
    call = {
        "id": "c3",
        "tool": "football_fixtures",
        "workflow_id": stored.id,
        "workflow_name": stored.definition.name,
        "arguments": {"team": "Spurs"},
        "raw_arguments": {"team": "Spurs"},
        "action_ending": False,
    }
    item = await orch._run_call(job, call)
    assert runner.seen[0] == "Tottenham Hotspur" or runner.seen == ["Spurs", "Tottenham Hotspur"]
    # Because _run_call normalises before the first attempt when not marked normalized,
    # the first run should already use Tottenham Hotspur.
    assert item["status"] == "succeeded"
    assert item["inputs"]["team"] == "Tottenham Hotspur"
    assert (item.get("validation") or {}).get("verdict") == "ok"
    assert not job.get("flagged")

    # And if the first attempt already ran with the raw nickname, retry once.
    runner.seen.clear()
    job2 = {"id": "job2", "usage": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0, "used": False}, "flagged": []}
    call2 = {
        "id": "c3",
        "tool": "football_fixtures",
        "workflow_id": stored.id,
        "workflow_name": stored.definition.name,
        "arguments": {"team": "Spurs"},
        "raw_arguments": {"team": "Spurs"},
        "action_ending": False,
        "normalized": True,  # skip pre-call rewrite so the first run uses Spurs
    }
    item2 = await orch._run_call(job2, call2)
    assert runner.seen == ["Spurs", "Tottenham Hotspur"]
    assert item2["status"] == "succeeded"
    assert item2["inputs"]["team"] == "Tottenham Hotspur"
    assert item2["attempts"] == 2
    assert not job2.get("flagged")


def test_collect_sample_prefers_extract_over_page_chrome():
    from browser_platform.executor.output_validate import collect_sample, trim_sample, has_strong_content

    extract = (
        "Tottenham Hotspur Scores & Fixtures\n"
        "Sunday 29th November\nPremier League\n"
        "Sunderland versus Tottenham Hotspur kick off 14:05\n"
        "Sunderland 14:05 plays Spurs Tottenham Hotspur"
    )
    chrome = ("BBC Homepage Skip to content Home News Sport " * 80) + "Nov 2025 Dec 2025 Jan 2026"
    sample = collect_sample({"extract": extract}, chrome)
    assert "versus Tottenham Hotspur kick off 14:05" in sample
    assert has_strong_content(sample)
    # Head-only trim of chrome+extract would drop the fixtures; head+tail keeps them.
    dumped = chrome + " " + extract
    trimmed = trim_sample(dumped, limit=400)
    assert "kick off 14:05" in trimmed


@pytest.mark.asyncio
async def test_validate_output_does_not_downgrade_fixture_extract(monkeypatch):
    from browser_platform.executor.output_validate import validate_output

    extract = "Sunday 29th November Premier League Sunderland versus Tottenham Hotspur kick off 14:05"
    chrome = ("BBC Homepage Skip to content Home News Sport Menu Cookie settings " * 40) + "Nov 2025 Dec 2025"

    async def fake_llm(**kwargs):
        return {"verdict": "suspect", "reason": "only navigation and header text"}, {"tokens_in": 1, "tokens_out": 1, "cost_usd": 0, "used": True}

    monkeypatch.setattr("browser_platform.executor.output_validate.env_api_key", lambda: "sk-test")
    monkeypatch.setattr("browser_platform.executor.output_validate._llm_verdict", fake_llm)
    result = await validate_output(
        request="fixtures for Tottenham Hotspur",
        inputs={"team": "Tottenham Hotspur"},
        outputs={"extract": extract},
        sample=chrome + " Tottenham Hotspur Scores & Fixtures " + extract,
        url="https://www.bbc.com/sport/football/teams/tottenham-hotspur/scores-fixtures/2026-11",
        title="Tottenham Hotspur - Scores & Fixtures",
        use_llm=True,
    )
    assert result["verdict"] == "ok"

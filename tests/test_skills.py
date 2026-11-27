from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from browser_platform.app import create_app
from browser_platform.config import Settings
from browser_platform.models.workflow import WorkflowDefinition, WorkflowInput, WorkflowStep
from browser_platform.orchestrator.chat import ChatOrchestrator
from browser_platform.orchestrator.planner import check_step, parse_plan, planner_messages, waves
from browser_platform.skills.manifest import card
from browser_platform.skills.registry import SkillRegistry
from browser_platform.store.file_store import FileStore

USAGE = {"tokens_in": 10, "tokens_out": 5, "cost_usd": 0, "used": True}


def _wf(name, url, inputs=(), extract="results", action=False, description=""):
    steps = [WorkflowStep(type="navigation", url=url)]
    if action:
        steps += [WorkflowStep(type="input", value="{name}", cssSelector="#name"), WorkflowStep(type="click", target_text="Submit")]
    else:
        steps.append(WorkflowStep(type="extract", extractionGoal=extract, output="extract"))
    return WorkflowDefinition(name=name, version="1", description=description, input_schema=list(inputs), steps=steps)


def _library(store: FileStore) -> None:
    store.create_workflow(
        _wf(
            "football_fixtures",
            "https://www.bbc.com/sport/football/teams/{team}/scores-fixtures/{month}",
            [WorkflowInput(name="team", required=True, default="Arsenal"), WorkflowInput(name="month", required=False, format="ym")],
            extract="upcoming fixtures and kick-off times",
            description="Upcoming football fixtures for a team on BBC Sport",
        )
    )
    store.create_workflow(_wf("books_category", "https://books.toscrape.com/catalogue/category/books/{category}/index.html", [WorkflowInput(name="category", required=True, default="travel_2")], extract="book titles and prices"))
    store.create_workflow(_wf("httpbin_form_post", "https://httpbin.org/forms/post", [WorkflowInput(name="name", required=True)], action=True))
    for i in range(30):
        store.create_workflow(_wf(f"decoy_widget_{i}", f"https://example{i}.com/widgets", [WorkflowInput(name="q", required=True)], extract="widget list"))


def test_manifest_and_card_are_compact(data_dir: Path):
    store = FileStore(data_dir)
    _library(store)
    reg = SkillRegistry(store)
    m = reg.get("football_fixtures")
    assert m and m["site"] == "bbc.com" and m["read_only"] is True
    month = next(i for i in m["inputs"] if i["name"] == "month")
    assert month["required"] is False and month["default"].startswith("runtime")
    line = card(m)
    assert line.startswith("football_fixtures(team*, month?=YYYY-MM)")
    assert (data_dir / "skills" / f"{m['id']}.skill.json").is_file()
    action = reg.get("httpbin_form_post")
    assert action["action_ending"] is True and "(action)" in card(action)


@pytest.mark.asyncio
async def test_retrieval_top_k_and_prompt_size(data_dir: Path):
    store = FileStore(data_dir)
    _library(store)
    reg = SkillRegistry(store)
    found = await reg.search("When do Arsenal play their next football match?", k=6)
    assert found["skills"][0]["slug"] == "football_fixtures"
    assert len(found["skills"]) <= 6
    msgs = planner_messages("When do Arsenal play next?", [], found["skills"])
    full = json.dumps([t["function"] for t in reg.tools()])
    assert len(msgs[0]["content"]) < 2500 < len(full)
    books = await reg.search("cheap travel books price", k=3)
    assert books["skills"][0]["slug"] == "books_category"


def test_check_step_coerces_and_flags():
    tools = [
        {
            "type": "function",
            "function": {"name": "sort_items", "parameters": {"type": "object", "properties": {"order": {"type": "string", "enum": ["az", "za"]}, "page": {"type": "number"}}}},
            "workflow_id": "w1",
        }
    ]
    step = {"id": "s1", "skill": "Sort-Items", "args": {"Order": "AZ", "page": "2"}, "after": []}
    tool, problems = check_step(step, tools)
    assert tool and not problems and step["args"] == {"order": "az", "page": 2}
    bad = {"id": "s2", "skill": "sort_items", "args": {"colour": "red"}, "after": []}
    _, problems = check_step(bad, tools)
    assert problems == ["unknown argument colour"]


def test_parse_plan_and_waves():
    plan = parse_plan('```json\n{"steps":[{"id":"a","skill":"x","args":{}},{"id":"b","skill":"y","args":{"q":{"from":"a","pick":"title"}},"after":["a"]},{"id":"c","skill":"x","args":{}}]}\n```')
    assert [s["id"] for s in plan["steps"]] == ["a", "b", "c"]
    layers = waves([{"id": "a"}, {"id": "b", "after": ["a"]}, {"id": "c"}])
    assert [[c["id"] for c in layer] for layer in layers] == [["a", "c"], ["b"]]


@pytest.mark.asyncio
async def test_plan_uses_cards_then_repairs_with_only_chosen_schema(data_dir: Path, monkeypatch):
    store = FileStore(data_dir)
    _library(store)
    orch = ChatOrchestrator(store, runner=None)
    seen = []

    async def fake_complete(messages, **kwargs):
        seen.append(kwargs.get("tools"))
        if kwargs.get("tools"):
            names = [t["function"]["name"] for t in kwargs["tools"]]
            assert names == ["football_fixtures"]
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "football_fixtures", "arguments": '{"team":"Arsenal"}'}}]}, USAGE
        assert "decoy_widget_29" not in messages[0]["content"] or len(re.findall(r"^- ", messages[0]["content"], re.M)) <= 6
        return {"role": "assistant", "content": json.dumps({"steps": [{"id": "s1", "skill": "football_fixtures", "args": {"club": "Arsenal"}}]})}, USAGE

    monkeypatch.setattr("browser_platform.orchestrator.chat.chat_complete", fake_complete)
    monkeypatch.setattr("browser_platform.orchestrator.planner.chat_complete", fake_complete)
    monkeypatch.setattr("browser_platform.orchestrator.chat.env_api_key", lambda: "sk-test")
    job = await orch.plan("Arsenal fixtures please", history=[], target="cloud")
    assert job["status"] == "planned", job
    assert job["calls"][0]["arguments"]["team"] == "Arsenal"
    assert seen[0] is None and seen[1]
    assert set(job["token_breakdown"]) >= {"plan", "repair"}
    assert "football_fixtures" in job["skills_considered"]


@pytest.mark.asyncio
async def test_dependent_step_resolves_reference(data_dir: Path, monkeypatch):
    store = FileStore(data_dir)
    _library(store)
    orch = ChatOrchestrator(store, runner=None)

    async def fake_complete(messages, **kwargs):
        if "Extract each requested value" in messages[0]["content"]:
            return {"role": "assistant", "content": '{"values": {"team": "Chelsea"}}'}, USAGE
        plan = {
            "steps": [
                {"id": "s1", "skill": "books_category", "args": {"category": "travel_2"}},
                {"id": "s2", "skill": "football_fixtures", "args": {"team": {"from": "s1", "pick": "club named in the first title"}}, "after": ["s1"]},
            ]
        }
        return {"role": "assistant", "content": json.dumps(plan)}, USAGE

    ran = []

    async def fake_run(job, call, emit=None):
        ran.append((call["id"], dict(call["arguments"])))
        return {"id": call["id"], "status": "succeeded", "facts": "Chelsea: a travel guide", "workflow_name": call["workflow_name"], "inputs": call["arguments"]}

    monkeypatch.setattr("browser_platform.orchestrator.chat.chat_complete", fake_complete)
    monkeypatch.setattr("browser_platform.orchestrator.planner.chat_complete", fake_complete)
    monkeypatch.setattr("browser_platform.orchestrator.chat.env_api_key", lambda: "sk-test")
    monkeypatch.setattr(orch, "_run_call", fake_run)
    job = await orch.plan("Find a travel book and then that club's fixtures", history=[], target="cloud")
    assert job["status"] == "planned" and job["calls"][1]["after"] == ["c1"]
    await orch.execute(job)
    assert ran == [("c1", {"category": "travel_2"}), ("c2", {"team": "Chelsea"})]


def test_skills_api_and_mcp_list(data_dir: Path):
    settings = Settings(data_dir=data_dir, pomerado_host=Path(__file__).resolve().parents[1] / "node_host" / "src" / "stock-mcp.mjs")
    app = create_app(settings, root=Path(__file__).resolve().parents[1])
    _library(app.state.store)
    client = TestClient(app)
    listed = client.get("/api/skills").json()
    assert listed["count"] == 33 and all("card" in s for s in listed["skills"])
    one = client.get("/api/skills/football_fixtures").json()
    assert one["input_schema"]["properties"]["team"]
    found = client.get("/api/skills/search", params={"q": "Arsenal football fixtures", "k": 3}).json()
    assert found["skills"][0]["slug"] == "football_fixtures"
    mcp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"limit": 10}}).json()["result"]
    assert len(mcp["tools"]) == 10 and mcp["nextCursor"] == "10"
    tool = next(t for t in client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"query": "httpbin pizza form"}}).json()["result"]["tools"] if t["name"] == "httpbin_form_post")
    assert tool["annotations"]["destructiveHint"] is True
    wf_id = one["id"]
    assert client.delete(f"/api/workflows/{wf_id}").status_code == 200
    assert not (data_dir / "skills" / f"{wf_id}.skill.json").exists()


@pytest.mark.asyncio
async def test_enrichment_is_stored_and_survives_definition_change(data_dir: Path, monkeypatch):
    store = FileStore(data_dir)
    _library(store)
    reg = SkillRegistry(store)
    target = reg.get("books_category")

    async def fake(messages, **kwargs):
        return {"role": "assistant", "content": json.dumps({"summary": "List books and prices in a category", "tags": ["books", "prices"], "example_request": "travel books under £20"})}, USAGE

    monkeypatch.setattr("browser_platform.skills.registry.chat_complete", fake)
    monkeypatch.setattr("browser_platform.skills.registry.env_api_key", lambda: "sk-test")
    result = await reg.backfill()
    assert result["enriched"] == 33 and not result["failed"]
    assert SkillRegistry(store).get("books_category")["summary"] == "List books and prices in a category"
    stored = store.get_workflow(target["id"])
    stored.definition.input_schema.append(WorkflowInput(name="page", required=False, default="1"))
    store.update_workflow(stored.id, stored.definition)
    again = SkillRegistry(store).get("books_category")
    assert again["summary"] == "List books and prices in a category" and again.get("stale") is True
    assert any(i["name"] == "page" for i in again["inputs"])
    assert SkillRegistry(store).get("books_category")["summary"] == "List books and prices in a category"

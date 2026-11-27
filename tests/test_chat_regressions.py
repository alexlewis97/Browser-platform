"""Regressions generalised from two bad live chat runs (27 Nov 2026).

What happened: a site had two skills with the same page and inputs, one for upcoming
items and one for past items (same URL plus a ?filter= query). Duplicate detection
collapsed them, so the planner only ever saw one and a "past items" request ran the
"upcoming" skill, or nothing at all, while the job still said succeeded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from browser_platform.app import create_app
from browser_platform.authoring.tools import is_action_ending
from browser_platform.config import Settings
from browser_platform.models.workflow import WorkflowDefinition, WorkflowInput, WorkflowStep
from browser_platform.orchestrator.chat import ChatOrchestrator
from browser_platform.orchestrator.planner import sub_queries, uncovered_intents
from browser_platform.skills.registry import SkillRegistry
from browser_platform.store.file_store import FileStore
from browser_platform.workflow_inputs import ensure_definition_inputs, input_is_required

USAGE = {"tokens_in": 10, "tokens_out": 5, "cost_usd": 0, "used": True}


def _wf(name, url, inputs, description, extra_steps=()):
    return WorkflowDefinition(
        name=name,
        version="1",
        description=description,
        input_schema=list(inputs),
        steps=[WorkflowStep(type="navigation", url=url), *extra_steps, WorkflowStep(type="extract", extractionGoal="result", output="extract")],
    )


def _site(store: FileStore) -> dict[str, str]:
    entity = [WorkflowInput(name="entity", required=True, default="Alpha"), WorkflowInput(name="month", required=False, format="ym")]
    ids = {}
    ids["upcoming"] = store.create_workflow(_wf("site_entity_upcoming", "https://example.org/items/{entity|slug}/list/{month|ym}", entity, "Upcoming items for an entity.")).id
    ids["past"] = store.create_workflow(_wf("site_entity_past", "https://example.org/items/{entity|slug}/list/{month|ym}?filter=past", entity, "Past items for an entity.")).id
    ids["ranking"] = store.create_workflow(_wf("site_group_ranking", "https://example.org/groups/{group}/ranking", [WorkflowInput(name="group", required=True)], "Ranking table for a group.")).id
    ids["leaders"] = store.create_workflow(
        _wf(
            "site_group_leaders",
            "https://example.org/groups/{group}/leaders",
            [WorkflowInput(name="group", required=True), WorkflowInput(name="tab", required=False, default="Points")],
            "Leaders list for a group; tab defaults to Points.",
            extra_steps=[WorkflowStep(type="click", target_text="{tab}")],
        )
    ).id
    return ids


def test_same_page_different_query_are_not_duplicates(data_dir: Path):
    store = FileStore(data_dir)
    _site(store)
    reg = SkillRegistry(store)
    slugs = {m["slug"] for m in reg.selection_pool(reg.manifests())}
    assert {"site_entity_upcoming", "site_entity_past"} <= slugs


def test_true_duplicates_still_collapse(data_dir: Path):
    store = FileStore(data_dir)
    _site(store)
    store.create_workflow(_wf("site_entity_upcoming_copy", "https://example.org/items/{entity|slug}/list/{month|ym}", [WorkflowInput(name="entity", required=True), WorkflowInput(name="month", required=False, format="ym")], "copy"))
    reg = SkillRegistry(store)
    pool = reg.selection_pool(reg.manifests())
    upcoming = [m for m in pool if m["slug"].startswith("site_entity_upcoming")]
    assert len(upcoming) == 1 and upcoming[0].get("aliases")


def test_declared_optional_input_stays_optional():
    definition = _wf("x", "https://example.org/{group}", [WorkflowInput(name="group", required=True), WorkflowInput(name="tab", required=False, default="Points")], "", extra_steps=[WorkflowStep(type="click", target_text="{tab}")])
    tab = next(i for i in ensure_definition_inputs(definition).input_schema if i.name == "tab")
    assert input_is_required(tab) is False


def test_optional_view_switch_click_is_read_only_but_required_pick_is_not():
    view = _wf("v", "https://example.org/{group}", [WorkflowInput(name="group", required=True), WorkflowInput(name="tab", required=False, default="Points")], "", extra_steps=[WorkflowStep(type="click", target_text="{tab}")])
    assert is_action_ending(view) is False
    pick = _wf("p", "https://example.org/list", [WorkflowInput(name="name", required=True)], "", extra_steps=[WorkflowStep(type="click", target_text="{name}")])
    assert is_action_ending(pick) is True


def test_sub_queries_and_uncovered():
    assert sub_queries("Alpha's past items and next items") == ["Alpha's past items", "next items"]
    assert sub_queries("just one thing") == []
    intents = [{"ask": "Alpha past", "steps": ["s1"]}, {"ask": "Alpha upcoming", "steps": ["s2"]}]
    assert uncovered_intents(intents, [{"id": "s2"}]) == ["Alpha past"]
    assert uncovered_intents(intents, [{"id": "s1"}, {"id": "s2"}]) == []


def _patch(monkeypatch, fake):
    monkeypatch.setattr("browser_platform.orchestrator.chat.chat_complete", fake)
    monkeypatch.setattr("browser_platform.orchestrator.planner.chat_complete", fake)
    monkeypatch.setattr("browser_platform.orchestrator.chat.env_api_key", lambda: "sk-test")


def _plan(body):
    return {"role": "assistant", "content": json.dumps(body)}, USAGE


@pytest.mark.asyncio
async def test_uncovered_intent_triggers_focused_replan(data_dir: Path, monkeypatch):
    """Live run 1: two asks for one entity, only one call planned."""

    store = FileStore(data_dir)
    _site(store)
    orch = ChatOrchestrator(store, runner=None)
    prompts = []

    async def fake(messages, **kwargs):
        prompts.append(messages)
        if "not covered yet" in messages[-1]["content"]:
            assert "site_entity_past" in messages[0]["content"]
            return _plan({"intents": [{"ask": "Alpha past items", "steps": ["s1"]}], "steps": [{"id": "s1", "skill": "site_entity_past", "args": {"entity": "Alpha"}}]})
        return _plan(
            {
                "intents": [{"ask": "Alpha past items", "steps": []}, {"ask": "Alpha upcoming items", "steps": ["s1"]}],
                "steps": [{"id": "s1", "skill": "site_entity_upcoming", "args": {"entity": "Alpha"}}],
            }
        )

    _patch(monkeypatch, fake)
    job = await orch.plan("Alpha's past items and next items", history=[], target="cloud")
    assert job["status"] == "planned"
    assert sorted(c["tool"] for c in job["calls"]) == ["site_entity_past", "site_entity_upcoming"]
    assert job["uncovered"] == [] and "coverage" in job["token_breakdown"]
    assert {"site_entity_past", "site_entity_upcoming"} <= set(job["skills_considered"])


@pytest.mark.asyncio
async def test_still_uncovered_intent_makes_the_job_partial(data_dir: Path, monkeypatch):
    store = FileStore(data_dir)
    _site(store)
    orch = ChatOrchestrator(store, runner=None)

    async def fake(messages, **kwargs):
        if messages[0]["content"].startswith("Adapt the workflow outputs"):
            return {"role": "assistant", "content": "Upcoming: ... (past items were not fetched)"}, USAGE
        if "not covered yet" in messages[-1]["content"]:
            return _plan({"steps": []})
        return _plan({"intents": [{"ask": "Alpha past items", "steps": []}, {"ask": "Alpha upcoming", "steps": ["s1"]}], "steps": [{"id": "s1", "skill": "site_entity_upcoming", "args": {"entity": "Alpha"}}]})

    async def fake_run(job, call, emit=None):
        return {"id": call["id"], "status": "succeeded", "facts": "Alpha next: 1 Dec", "workflow_name": call["workflow_name"], "inputs": call["arguments"], "validation": {"verdict": "ok"}}

    _patch(monkeypatch, fake)
    monkeypatch.setattr(orch, "_run_call", fake_run)
    job = await orch.handle(message="Alpha's past items and next items", history=[], target="cloud")
    assert job["uncovered"] == ["Alpha past items"]
    assert job["status"] == "partial"
    assert any(f["verdict"] == "not_run" for f in job["flagged"])


@pytest.mark.asyncio
async def test_no_tool_answer_is_repaired_when_a_skill_matches(data_dir: Path, monkeypatch):
    """Never answer from memory when a retrieved skill can fetch it."""

    store = FileStore(data_dir)
    _site(store)
    orch = ChatOrchestrator(store, runner=None)

    async def fake(messages, **kwargs):
        if kwargs.get("tools"):
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "site_group_ranking", "arguments": '{"group":"north"}'}}]}, USAGE
        return _plan({"steps": [], "answer": "From memory, Alpha is first."})

    _patch(monkeypatch, fake)
    job = await orch.plan("Show the ranking table for group north", history=[], target="cloud")
    assert job["status"] == "planned" and job["calls"][0]["tool"] == "site_group_ranking"


@pytest.mark.asyncio
async def test_extension_runs_dependent_steps_in_rounds(data_dir: Path, monkeypatch):
    store = FileStore(data_dir)
    _site(store)
    orch = ChatOrchestrator(store, runner=None)

    async def fake(messages, **kwargs):
        system = messages[0]["content"]
        if "Extract each requested value" in system:
            return {"role": "assistant", "content": '{"values": {"entity": "Beta"}}'}, USAGE
        if system.startswith("Adapt the workflow outputs"):
            return {"role": "assistant", "content": "Beta leads; next: 3 Dec"}, USAGE
        return _plan(
            {
                "intents": [{"ask": "leader of north", "steps": ["s1"]}, {"ask": "leader's upcoming items", "steps": ["s2"]}],
                "steps": [
                    {"id": "s1", "skill": "site_group_ranking", "args": {"group": "north"}},
                    {"id": "s2", "skill": "site_entity_upcoming", "args": {"entity": {"from": "s1", "pick": "the top entity"}}, "after": ["s1"]},
                ],
            }
        )

    _patch(monkeypatch, fake)
    job = await orch.handle(message="Who leads group north and what's next for them?", history=[], target="extension")
    assert job["status"] == "awaiting_results" and [c["id"] for c in job["calls"]] == ["c1"]
    job = await orch.handle(message="", job_id=job["id"], results=[{"id": "c1", "ok": True, "status": "succeeded", "outputs": {"extract": "1. Beta 30 pts"}}], target="extension")
    assert job["status"] == "awaiting_results" and [c["id"] for c in job["calls"]] == ["c2"]
    assert job["calls"][0]["arguments"]["entity"] == "Beta" and job["calls"][0].get("definition")
    job = await orch.handle(message="", job_id=job["id"], results=[{"id": "c2", "ok": True, "status": "succeeded", "outputs": {"extract": "Beta v Gamma 3 Dec"}}], target="extension")
    assert job["status"] == "succeeded" and len(job["items"]) == 2
    assert "Beta" in job["answer"]


def test_chat_jobs_are_persisted_and_listed(data_dir: Path, monkeypatch):
    settings = Settings(data_dir=data_dir, pomerado_host=Path(__file__).resolve().parents[1] / "node_host" / "src" / "stock-mcp.mjs")
    app = create_app(settings, root=Path(__file__).resolve().parents[1])
    _site(app.state.store)

    async def fake(messages, **kwargs):
        return _plan({"intents": [{"ask": "north ranking", "steps": ["s1"]}], "steps": [{"id": "s1", "skill": "site_group_ranking", "args": {"group": "north"}}]})

    _patch(monkeypatch, fake)
    client = TestClient(app)
    res = client.post("/api/chat", json={"message": "ranking for north", "target": "extension", "stream": False})
    assert res.status_code == 200, res.text
    jobs = client.get("/api/chat-jobs").json()["jobs"]
    assert jobs and jobs[0]["message"] == "ranking for north"
    assert jobs[0]["plan_raw"] and jobs[0]["all_calls"][0]["tool"] == "site_group_ranking"
    assert "llm_api_key" not in json.dumps(jobs[0])


@pytest.mark.asyncio
async def test_output_validator_is_told_not_to_fact_check_live_data(monkeypatch):
    """A correct live extract was marked suspect because the model 'knew' an older squad."""

    from browser_platform.executor import output_validate

    seen = {}

    async def fake(messages, **kwargs):
        seen["system"] = messages[0]["content"]
        return {"role": "assistant", "content": '{"verdict":"ok","reason":"right page"}'}, USAGE

    monkeypatch.setattr(output_validate, "chat_complete", fake)
    await output_validate._llm_verdict(request="leaders", inputs={"group": "north"}, sample="North leaders: Beta 9", url="", title="", action_ending=False)
    assert "own knowledge" in seen["system"]

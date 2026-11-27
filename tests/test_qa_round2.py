"""Round-2 generic fixes. Cases are split into TRAIN (used while implementing) and HELD-OUT
(not used to tune). None of these tests encode a site-, league- or entity-specific rule."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from browser_platform.app import create_app
from browser_platform.authoring.explore import apply_proposals
from browser_platform.authoring.explore_links import propose_from_link_families
from browser_platform.authoring.from_task import _extract_goal
from browser_platform.cache.action_cache import ActionCache
from browser_platform.config import Settings
from browser_platform.executor.deterministic import _wants_page_heading
from browser_platform.interpolate import interpolate_url
from browser_platform.models.llm import LLMConfig
from browser_platform.models.workflow import SelectorStrategy, WorkflowDefinition, WorkflowInput, WorkflowStep
from browser_platform.orchestrator.runner import WorkflowRunner
from browser_platform.store.file_store import FileStore
from browser_platform.workflow_inputs import (
    invalid_typed_inputs,
    match_known_value,
    missing_required_inputs,
    resolve_input_values,
)

from test_fuzzy_match import AttendancePage, ConfirmationBroker, FakeSession, _definition
from test_orchestrator import FakePage, FakeRepair, FakeSession as OrchFakeSession

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "node_host" / "src" / "stock-mcp.mjs"
LLM = LLMConfig(api_key="sk-or-v1-should-never-be-stored", model="qwen/qwen3.7-flash")


def _settings(data_dir: Path, **kwargs) -> Settings:
    return Settings(data_dir=data_dir, pomerado_host=HOST, access_token="", **kwargs)


def _runner(data_dir: Path, page=None, *, broker=None, repair=None, allow_in_run_repair=False, use_browser_use=False):
    settings = _settings(data_dir, allow_in_run_repair=allow_in_run_repair, hitl_timeout_s=2)
    store = FileStore(data_dir)
    page = page if page is not None else FakePage()

    async def connect(url: str):
        return OrchFakeSession(page=page, opened_pages=[page]) if isinstance(page, FakePage) else FakeSession(page=page, opened_pages=[page])

    async def disconnect(session) -> None:
        session.closed = True

    runner = WorkflowRunner(
        store,
        settings,
        action_cache=ActionCache(),
        repair_client=repair or FakeRepair([]),
        connect=connect,
        disconnect=disconnect,
        confirmation_broker=broker,
        use_browser_use=use_browser_use,
    )
    runner._fake_page = page
    return runner, store


def _slug_defn(examples, *, name="competition"):
    return WorkflowDefinition.model_validate({
        "name": "Listing",
        "steps": [
            {"type": "navigation", "url": "https://example.org/{%s|slug}" % name},
            {"type": "extract", "extractionGoal": "result"},
        ],
        "input_schema": [{"name": name, "type": "string", "required": True, "format": "slug", "examples": examples}],
    })


# --- TRAIN -----------------------------------------------------------------

def test_train_known_slug_is_mapped_in_code_unknown_is_invalid():
    d = _slug_defn(["premier-league", "spanish-la-liga", "championship", "french-ligue-one"])
    item = d.input_schema[0]
    assert match_known_value(item, "la-liga") == "spanish-la-liga"
    assert match_known_value(item, "La Liga") == "spanish-la-liga"
    assert resolve_input_values(d, {"competition": "la-liga"})["competition"] == "spanish-la-liga"
    assert invalid_typed_inputs(d, resolve_input_values(d, {"competition": "la-liga"})) == []
    assert invalid_typed_inputs(d, {"competition": "serie-x"})
    assert "known values" in invalid_typed_inputs(d, {"competition": "serie-x"})[0]


@pytest.mark.asyncio
async def test_train_unknown_slug_is_needs_input_and_does_not_repair(data_dir: Path):
    repair = FakeRepair([SelectorStrategy(type="css", value="h1", priority=1)])
    runner, store = _runner(data_dir, repair=repair)
    stored = store.create_workflow(_slug_defn(["spanish-la-liga", "premier-league"]))
    run = await runner.run(stored, cdp_url="http://127.0.0.1:0", variables={"competition": "serie-x"}, llm=LLM)
    assert run.status == "needs_input"
    assert run.failure_kind == "needs_input"
    assert "serie-x" in (run.error or "")
    assert repair.calls == 0
    assert run.steps == []
    assert not getattr(runner._fake_page, "gone_to", None)


@pytest.mark.asyncio
async def test_train_missing_required_input_is_needs_input(data_dir: Path):
    runner, store = _runner(data_dir)
    stored = store.create_workflow(_slug_defn(["spanish-la-liga"]))
    assert missing_required_inputs(stored.definition, {}) == ["competition"]
    run = await runner.run(stored, cdp_url="http://127.0.0.1:0", variables={})
    assert run.status == "needs_input"
    assert "competition" in (run.error or "")
    assert run.steps == []


@pytest.mark.asyncio
async def test_train_default_config_never_calls_llm_or_browser_use_on_replay(data_dir: Path):
    """Alex: workflow replay must not escalate to LLM or browser_use with the default config."""
    page = FakePage()
    page.broken_click = True
    repair = FakeRepair([SelectorStrategy(type="css", value="h1", priority=1)])
    runner, store = _runner(data_dir, page, repair=repair, use_browser_use=True)
    assert runner.settings.allow_in_run_repair is False
    stored = store.create_workflow(WorkflowDefinition(
        name="form",
        steps=[
            WorkflowStep(type="navigation", url="https://example.com"),
            WorkflowStep(
                type="click",
                description="Submit the form",
                cssSelector="button.missing",
                selectorStrategies=[SelectorStrategy(type="css", value="button.missing", priority=1)],
            ),
            WorkflowStep(type="extract", extractionGoal="heading", cssSelector="h1"),
        ],
    ))
    run = await runner.run(stored, cdp_url="http://127.0.0.1:0", llm=LLM)
    assert run.status == "failed"
    assert repair.calls == 0
    click = next(s for s in run.steps if s.step_type == "click")
    assert click.success is False
    assert click.tier not in {"llm", "browser_use"}
    assert (click.tokens_in, click.tokens_out) == (0, 0)
    assert "Submit the form" in (run.error or click.error or "")
    assert "llm" not in (click.tier or "")


@pytest.mark.asyncio
async def test_train_strict_broken_click_fails_by_name_not_fuzzy(data_dir: Path):
    runner, store = _runner(data_dir, AttendancePage())
    stored = store.create_workflow(_definition())
    # Break the recorded target so the primary selector cannot hit.
    stored.definition.steps[1].target_text = "#this-selector-does-not-exist"
    stored.definition.steps[1].cssSelector = "#this-selector-does-not-exist"
    stored.definition.steps[1].selectorStrategies = [
        SelectorStrategy(type="css", value="#this-selector-does-not-exist", priority=1)
    ]
    run = await runner.run(
        stored, cdp_url="http://127.0.0.1:0", variables={"name": "Lewis, Alex"}, strict=True
    )
    assert run.status == "failed"
    assert run.failure_kind == "strict_selector"
    assert "strict selectors" in (run.error or "")
    click = next(s for s in run.steps if s.step_type == "click")
    assert click.success is False
    assert click.resolution != "fuzzy"
    assert run.status != "succeeded_with_fallback"


def test_train_explore_reuses_unused_required_input_and_extract_goal_is_not_heading():
    html = "".join(
        f'<a href="/catalogue/category/books/{slug}/index.html">{label}</a>'
        for slug, label in (("travel_2", "Travel"), ("mystery_3", "Mystery"), ("poetry_23", "Poetry"))
    )
    url = "https://shop.example.org/catalogue/category/books/travel_2/index.html"
    props = propose_from_link_families(html, url, step_template=url)
    definition = WorkflowDefinition.model_validate({
        "name": "Catalogue",
        "steps": [{"type": "navigation", "url": url}, {"type": "extract", "extractionGoal": "result"}],
        "input_schema": [{"name": "category", "type": "string", "required": True, "examples": ["Travel"]}],
    })
    applied = apply_proposals(definition, props)
    names = [item.name for item in applied.input_schema]
    assert names == ["category"]
    item = applied.input_schema[0]
    assert item.required is True
    assert item.default in (None, "")
    assert "{category|slug}" in (applied.steps[0].url or "")
    resolved = resolve_input_values(applied, {"category": "Poetry"})
    assert resolved["category"] == "poetry_23"
    assert interpolate_url(applied.steps[0].url, resolved).endswith("/poetry_23/index.html")

    task = "Open Travel, extract titles and prices"
    assert _extract_goal(task) != "heading"
    assert "titles" in _extract_goal(task).lower()
    assert _wants_page_heading(task) is False
    assert _extract_goal("extract the page title") == "heading"
    assert _wants_page_heading("extract the page title") is True


@pytest.mark.asyncio
async def test_train_unattended_run_does_not_pause_on_hitl(data_dir: Path):
    page = AttendancePage()
    broker = ConfirmationBroker()
    repair = FakeRepair([])
    runner, store = _runner(data_dir, page, broker=broker, repair=repair)
    stored = store.create_workflow(_definition(mode="always_on_uncertain", timeout_s=8, timeout_policy="fail"))
    run = await runner.run(
        stored, cdp_url="http://127.0.0.1:0", variables={"name": "Ann Cohen"}, allow_hitl=False, llm=LLM
    )
    assert run.status == "failed"
    assert run.status != "needs_confirmation"
    assert repair.calls == 0
    assert not any(s.resolution == "hitl" for s in run.steps)
    assert not any(s.tier in {"llm", "browser_use"} for s in run.steps)


def test_train_from_task_job_survives_memory_loss(data_dir: Path):
    from browser_platform.store.from_task_jobs import FromTaskJobStore
    from browser_platform.authoring.from_task import FromTaskResult

    jobs = FromTaskJobStore(data_dir / "from_task_jobs")
    job = FromTaskResult(id="abc123", status="succeeded", task="demo", name="demo", workflow_id="wf-1")
    jobs.put(job)
    jobs._jobs.clear()  # process "restart"
    got = jobs.get("abc123")
    assert got is not None
    assert got.status == "succeeded"
    assert got.workflow_id == "wf-1"


def test_train_repair_has_no_per_run_or_api_override():
    from browser_platform.api.schemas import RunRequest
    from browser_platform.models.workflow import WorkflowDefinition as WD

    assert Settings().allow_in_run_repair is False
    assert "allow_in_run_repair" not in RunRequest.model_fields
    assert "use_browser_use" not in RunRequest.model_fields
    assert "allow_in_run_repair" not in WD.model_fields
    text = Path("src/browser_platform/orchestrator/runner.py").read_text(encoding="utf-8")
    assert "_in_run_repair_allowed" in text
    assert "BROWSER_PLATFORM_ALLOW_IN_RUN_REPAIR" in Path("src/browser_platform/config.py").read_text(encoding="utf-8") or (
        "allow_in_run_repair" in Path("src/browser_platform/config.py").read_text(encoding="utf-8")
    )


# --- HELD-OUT (not used to tune the implementation) -----------------------

def test_held_out_identifier_examples_map_and_reject_unknown():
    d = _slug_defn(["north-river-cup", "east-coast-open", "grand-prix-one"], name="kind")
    item = d.input_schema[0]
    assert match_known_value(item, "East Coast Open") == "east-coast-open"
    assert match_known_value(item, "grand prix 1") == "grand-prix-one"
    assert invalid_typed_inputs(d, {"kind": "west-bay-trophy"})


def test_held_out_value_map_labels_and_free_text_untouched():
    item = WorkflowInput(
        name="section",
        format="slug",
        examples=["news_1", "sport_4"],
        value_map={"News": "news_1", "Sport": "sport_4", "news_1": "news_1"},
    )
    assert match_known_value(item, "News") == "news_1"
    free = WorkflowInput(name="q", examples=["wireless mouse pad"])
    assert match_known_value(free, "wireless mouse") == "wireless mouse"
    assert invalid_typed_inputs(
        WorkflowDefinition.model_validate({
            "name": "Search",
            "steps": [{"type": "navigation", "url": "https://ex.org"}, {"type": "extract", "extractionGoal": "x"}],
            "input_schema": [free.model_dump()],
        }),
        {"q": "wireless mouse"},
    ) == []


def test_held_out_extract_goal_for_product_list_not_page_title():
    goal = _extract_goal("List the products and their prices")
    assert goal != "heading"
    assert _wants_page_heading(goal) is False
    assert _wants_page_heading("the main heading") is True
    assert _extract_goal("read the first paragraph") == "first paragraph"


def test_held_out_explore_binds_required_topic_not_a_second_option():
    html = "".join(f'<a href="/topics/{slug}/">{label}</a>' for slug, label in (
        ("garden-tools", "Garden tools"), ("kitchen-ware", "Kitchen ware"), ("office-supplies", "Office supplies"),
    ))
    url = "https://shop.example.org/topics/garden-tools/"
    props = propose_from_link_families(html, url, step_template=url)
    definition = WorkflowDefinition.model_validate({
        "name": "Topics",
        "steps": [{"type": "navigation", "url": url}, {"type": "extract", "extractionGoal": "items"}],
        "input_schema": [{"name": "topic", "type": "string", "required": True, "examples": ["Garden tools"]}],
    })
    applied = apply_proposals(definition, props)
    assert [i.name for i in applied.input_schema] == ["topic"]
    resolved = resolve_input_values(applied, {"topic": "Kitchen ware"})
    assert "kitchen-ware" in interpolate_url(applied.steps[0].url, resolved)


@pytest.mark.asyncio
async def test_held_out_api_run_is_not_interactive_by_default(data_dir: Path):
    from browser_platform.api.schemas import RunRequest
    assert RunRequest().interactive is False


def test_held_out_from_task_poll_after_new_store(data_dir: Path):
    """A second store pointed at the same directory still serves the job (new process)."""
    from browser_platform.store.from_task_jobs import FromTaskJobStore
    from browser_platform.authoring.from_task import FromTaskResult

    first = FromTaskJobStore(data_dir / "from_task_jobs")
    first.put(FromTaskResult(id="job-held", status="running", task="t", name="t"))
    second = FromTaskJobStore(data_dir / "from_task_jobs")
    got = second.get("job-held")
    assert got is not None and got.status == "running"

"""Admin-only in-run repair, identity by token, version snapshots, and needs_repair flag."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from browser_platform.app import create_app
from browser_platform.cache.action_cache import ActionCache
from browser_platform.config import Settings
from browser_platform.local_access import access_token_ok, caller_role
from browser_platform.models.llm import LLMConfig
from browser_platform.models.workflow import SelectorStrategy, WorkflowDefinition, WorkflowStep
from browser_platform.orchestrator.runner import WorkflowRunner, _safe_repair_variables
from browser_platform.store.file_store import FileStore

from test_orchestrator import FakePage, FakeRepair, FakeSession

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "node_host" / "src" / "stock-mcp.mjs"
LLM = LLMConfig(api_key="sk-or-v1-should-never-be-stored", model="qwen/qwen3.7-flash")
ADMIN = "admin-token-for-tests"
USER = "user-token-for-tests"


def _settings(data_dir: Path, **kwargs) -> Settings:
    kwargs.setdefault("access_token", "")
    return Settings(data_dir=data_dir, pomerado_host=HOST, **kwargs)


def _runner(data_dir: Path, page=None, *, repair=None, allow_in_run_repair=True):
    settings = _settings(data_dir, allow_in_run_repair=allow_in_run_repair)
    store = FileStore(data_dir)
    page = page if page is not None else FakePage()
    page.broken_click = True

    async def connect(url: str):
        return FakeSession(page=page, opened_pages=[page])

    async def disconnect(session) -> None:
        session.closed = True

    runner = WorkflowRunner(
        store,
        settings,
        action_cache=ActionCache(),
        repair_client=repair or FakeRepair([SelectorStrategy(type="css", value="h1", priority=1)]),
        connect=connect,
        disconnect=disconnect,
        use_browser_use=False,
    )
    runner._fake_page = page
    return runner, store


def _broken_click_wf() -> WorkflowDefinition:
    return WorkflowDefinition(
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
    )


class _TokenReq:
    def __init__(self, token: str | None):
        self.headers = {"authorization": f"Bearer {token}"} if token else {}
        self.query_params = {}
        self.scope = {"client": ("127.0.0.1", 9), "scheme": "http", "server": ("127.0.0.1", 8745)}


def test_caller_role_is_admin_only_for_access_token():
    settings = Settings(access_token=ADMIN, user_token=USER)
    assert caller_role(_TokenReq(ADMIN), settings) == "admin"
    assert caller_role(_TokenReq(USER), settings) == "user"
    assert caller_role(_TokenReq(None), settings) == "user"
    assert caller_role(_TokenReq("nope"), settings) == "user"
    assert access_token_ok(_TokenReq(ADMIN), settings) is True
    assert access_token_ok(_TokenReq(USER), settings) is True
    assert access_token_ok(_TokenReq("nope"), settings) is False


def test_identical_tokens_are_treated_as_admin(caplog):
    settings = Settings(access_token=ADMIN, user_token=ADMIN)
    assert caller_role(_TokenReq(ADMIN), settings) == "admin"
    assert "identical" in caplog.text.lower()


def test_safe_repair_variables_drop_secrets():
    got = _safe_repair_variables({"team": "Liverpool", "password": "secret", "api_key": "x", "count": 2})
    assert got == {"team": "Liverpool", "count": 2}


@pytest.mark.asyncio
async def test_user_path_never_repairs_even_when_flag_is_on(data_dir: Path):
    repair = FakeRepair([SelectorStrategy(type="css", value="h1", priority=1)])
    runner, store = _runner(data_dir, repair=repair, allow_in_run_repair=True)
    stored = store.create_workflow(_broken_click_wf())
    run = await runner.run(stored, cdp_url="http://127.0.0.1:0", llm=LLM, variables={"password": "n0t-saved", "q": "ok"})
    assert run.status == "failed"
    assert run.repaired is False
    assert repair.calls == 0
    assert not any(s.tier in {"llm", "browser_use"} for s in run.steps)
    flagged = store.get_workflow(stored.id)
    assert flagged.definition.needs_repair is True
    assert flagged.definition.version == "1"
    assert flagged.definition.repair_from["run_id"] == run.id
    assert flagged.definition.repair_from["variables"] == {"q": "ok"}
    assert "password" not in flagged.definition.repair_from["variables"]


@pytest.mark.asyncio
async def test_admin_repair_snapshots_writes_back_and_clears_flag(data_dir: Path):
    repair = FakeRepair([SelectorStrategy(type="css", value="h1", priority=1)])
    runner, store = _runner(data_dir, repair=repair, allow_in_run_repair=True)
    stored = store.create_workflow(_broken_click_wf())
    stored.definition.needs_repair = True
    stored.definition.repair_from = {"run_id": "old", "variables": {"q": "ok"}}
    store.update_workflow(stored.id, stored.definition)
    run = await runner.run(stored, cdp_url="http://127.0.0.1:0", llm=LLM, caller="admin")
    assert run.status in {"succeeded", "succeeded_with_fallback"}
    assert run.repaired is True
    assert repair.calls == 1
    click = next(s for s in run.steps if s.step_type == "click")
    assert click.tier == "llm"
    refreshed = store.get_workflow(stored.id)
    assert refreshed.definition.version != "1"
    assert refreshed.definition.needs_repair is False
    assert not refreshed.definition.repair_from
    assert run.repair["previous_version"] == "1"
    assert run.repair["new_version"] == refreshed.definition.version
    snaps = list((data_dir / "workflow_versions").glob(f"{stored.id}.1*.json"))
    assert snaps
    raw = snaps[0].read_text(encoding="utf-8")
    assert '"version": "1"' in raw or '"version":"1"' in raw


@pytest.mark.asyncio
async def test_admin_kill_switch_disables_repair(data_dir: Path):
    repair = FakeRepair([SelectorStrategy(type="css", value="h1", priority=1)])
    runner, store = _runner(data_dir, repair=repair, allow_in_run_repair=False)
    stored = store.create_workflow(_broken_click_wf())
    run = await runner.run(stored, cdp_url="http://127.0.0.1:0", llm=LLM, caller="admin")
    assert run.status == "failed"
    assert repair.calls == 0
    assert store.get_workflow(stored.id).definition.version == "1"


def test_repair_endpoint_requires_admin_token(data_dir: Path):
    settings = _settings(data_dir, access_token=ADMIN, user_token=USER, require_token_on_localhost=True)
    app = create_app(settings, root=ROOT)
    store: FileStore = app.state.store
    stored = store.create_workflow(_broken_click_wf())
    stored.definition.needs_repair = True
    stored.definition.repair_from = {"run_id": "r1", "variables": {"q": "ok"}}
    store.update_workflow(stored.id, stored.definition)
    client = TestClient(app)
    url = f"/api/workflows/{stored.id}/repair"
    assert client.post(url).status_code == 401
    denied = client.post(url, headers={"X-Browser-Platform-Token": USER})
    assert denied.status_code == 403
    # Admin is accepted; the run itself may fail later without a browser, but auth passed.
    allowed = client.post(url, headers={"X-Browser-Platform-Token": ADMIN}, json={"wait": False})
    assert allowed.status_code == 200
    listed = client.get("/api/workflows", headers={"X-Browser-Platform-Token": USER})
    assert listed.status_code == 200
    item = next(w for w in listed.json()["workflows"] if w["id"] == stored.id)
    assert item["needs_repair"] is True
    catalog = client.get("/api/catalog")
    assert catalog.status_code == 200
    cat = next((w for w in catalog.json()["workflows"] if w["id"] == stored.id), None)
    if cat:
        assert "needs_repair" not in cat
        assert "repair_from" not in cat


def test_repair_endpoint_400_without_repair_from(data_dir: Path):
    settings = _settings(data_dir, access_token=ADMIN, require_token_on_localhost=True)
    app = create_app(settings, root=ROOT)
    store: FileStore = app.state.store
    stored = store.create_workflow(_broken_click_wf())
    client = TestClient(app)
    res = client.post(f"/api/workflows/{stored.id}/repair", headers={"X-Browser-Platform-Token": ADMIN})
    assert res.status_code == 400

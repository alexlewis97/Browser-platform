"""Admin-only in-run repair, identity by token, version snapshots, and needs_repair flag."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from browser_platform.app import create_app
from browser_platform.cache.action_cache import ActionCache
from browser_platform.config import Settings
from browser_platform.local_access import access_token_ok, caller_role, require_admin
from browser_platform.models.llm import LLMConfig
from browser_platform.models.workflow import HitlConfig, SelectorStrategy, WorkflowDefinition, WorkflowStep
from browser_platform.orchestrator.runner import WorkflowRunner, _safe_repair_variables
from browser_platform.store.file_store import FileStore

from test_orchestrator import FakeLocator, FakePage, FakeRepair, FakeSession

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
    def __init__(self, token: str | None, *, forwarded_for: str | None = None, client_host: str = "127.0.0.1"):
        self.headers = {}
        if token:
            self.headers["authorization"] = f"Bearer {token}"
        if forwarded_for:
            self.headers["x-forwarded-for"] = forwarded_for
        self.query_params = {}
        self.client = (client_host, 9)
        self.scope = {"client": (client_host, 9), "scheme": "http", "server": ("127.0.0.1", 8745)}


def test_caller_role_loopback_without_tokens_is_admin():
    settings = Settings(access_token="", user_token="")
    req = _TokenReq(None)
    assert require_admin(req, settings) is True
    assert caller_role(req, settings) == "admin"


def test_proxied_request_without_tokens_is_not_admin():
    """Caddy / X-Forwarded-For traffic must not be treated as loopback admin."""
    settings = Settings(access_token="", user_token="")
    req = _TokenReq(None, forwarded_for="203.0.113.9")
    assert require_admin(req, settings) is False
    assert caller_role(req, settings) == "user"


def test_caller_role_is_admin_only_for_access_token():
    settings = Settings(access_token=ADMIN, user_token=USER)
    assert caller_role(_TokenReq(ADMIN), settings) == "admin"
    assert caller_role(_TokenReq(USER), settings) == "user"
    assert caller_role(_TokenReq(None), settings) == "user"
    assert caller_role(_TokenReq("nope"), settings) == "user"
    assert access_token_ok(_TokenReq(ADMIN), settings) is True
    assert access_token_ok(_TokenReq(USER), settings) is True
    assert access_token_ok(_TokenReq("nope"), settings) is False


def test_identical_tokens_still_unlock_admin_endpoints_only(caplog):
    """The same token may be accepted as the access token on /admin-run and /repair,
    but that does not change /run (always user). See the HTTP tests below."""
    settings = Settings(access_token=ADMIN, user_token=ADMIN)
    assert require_admin(_TokenReq(ADMIN), settings) is True
    assert caller_role(_TokenReq(ADMIN), settings) == "admin"
    assert "identical" in caplog.text.lower()
    assert "/run stays user" in caplog.text or "admin only" in caplog.text.lower()


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



def _broken_click_wf_llm_only() -> WorkflowDefinition:
    definition = _broken_click_wf()
    definition.hitl = HitlConfig(mode="llm_only", timeout_s=2)
    return definition


def _http_app(data_dir: Path, page, **settings_kw):
    settings = _settings(data_dir, allow_in_run_repair=True, require_token_on_localhost=True, **settings_kw)
    app = create_app(settings, root=ROOT)
    repair = FakeRepair([SelectorStrategy(type="css", value="h1", priority=1)])

    async def connect(url: str):
        return FakeSession(page=page, opened_pages=[page])

    async def disconnect(session) -> None:
        session.closed = True

    app.state.runner._connect = connect
    app.state.runner._disconnect = disconnect
    app.state.runner.use_browser_use = False
    app.state.runner.repair_client = repair
    app.state.runner.settings.allow_in_run_repair = True
    return TestClient(app), repair


def _wait_run(client: TestClient, path: str, workflow_id: str, headers: dict, extra: dict | None = None):
    body = {
        "wait": True,
        "cdp_url": "http://127.0.0.1:0",
        "llm": {"api_key": "sk-or-v1-should-never-be-stored", "model": "qwen/qwen3.7-flash"},
        **(extra or {}),
    }
    return client.post(f"/api/workflows/{workflow_id}{path}", headers=headers, json=body)


def test_http_run_is_user_mode_even_with_the_access_token(data_dir: Path):
    page = FakePage()
    page.broken_click = True
    client, repair = _http_app(data_dir, page, access_token=ADMIN, user_token=USER)
    stored = client.app.state.store.create_workflow(_broken_click_wf_llm_only())
    headers = {"X-Browser-Platform-Token": ADMIN}
    res = _wait_run(client, "/run", stored.id, headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "failed"
    assert body.get("repaired") is not True
    assert "Submit the form" in (body.get("error") or "")
    assert "strict selectors" in (body.get("error") or "")
    assert repair.calls == 0
    assert client.app.state.store.get_workflow(stored.id).definition.version == "1"


def test_admin_run_requires_access_token_and_may_repair(data_dir: Path):
    page = FakePage()
    page.broken_click = True
    client, repair = _http_app(data_dir, page, access_token=ADMIN, user_token=USER)
    stored = client.app.state.store.create_workflow(_broken_click_wf_llm_only())
    url = f"/api/workflows/{stored.id}/admin-run"
    assert client.post(url, json={"wait": False, "cdp_url": "http://127.0.0.1:0"}).status_code == 401
    denied = client.post(url, headers={"X-Browser-Platform-Token": USER}, json={"wait": False, "cdp_url": "http://127.0.0.1:0"})
    assert denied.status_code == 403
    res = _wait_run(client, "/admin-run", stored.id, {"X-Browser-Platform-Token": ADMIN})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] in {"succeeded", "succeeded_with_fallback"}
    assert body.get("repaired") is True
    assert repair.calls == 1
    assert client.app.state.store.get_workflow(stored.id).definition.version != "1"


def test_identical_tokens_do_not_make_run_an_admin_path(data_dir: Path):
    """Even when access and user tokens match, POST /run stays user mode."""
    page = FakePage()
    page.broken_click = True
    client, repair = _http_app(data_dir, page, access_token=ADMIN, user_token=ADMIN)
    store = client.app.state.store
    stored = store.create_workflow(_broken_click_wf_llm_only())
    headers = {"X-Browser-Platform-Token": ADMIN}
    userish = _wait_run(client, "/run", stored.id, headers)
    assert userish.status_code == 200, userish.text
    assert userish.json()["status"] == "failed"
    assert userish.json().get("repaired") is not True
    assert repair.calls == 0
    assert store.get_workflow(stored.id).definition.version == "1"

    page2 = FakePage()
    page2.broken_click = True
    client2, repair2 = _http_app(data_dir, page2, access_token=ADMIN, user_token=ADMIN)
    stored2 = client2.app.state.store.create_workflow(_broken_click_wf_llm_only())
    admin = _wait_run(client2, "/admin-run", stored2.id, headers)
    assert admin.status_code == 200, admin.text
    assert admin.json()["status"] in {"succeeded", "succeeded_with_fallback"}
    assert admin.json().get("repaired") is True
    assert repair2.calls == 1



class ExpandPage(FakePage):
    """A page whose Expand all control is only reachable via the live selector."""

    def __init__(self):
        super().__init__()
        self.body = "Expand all\nNotes\nReact\nPublic"
        self.expanded = False

    def locator(self, sel: str):
        if sel == "button.expand-all":
            page = self
            loc = FakeLocator(text="Expand all")

            async def click(**kwargs) -> None:
                loc.clicked = True
                page.expanded = True
                page.body = "Expand all\nNotes\nReact\nPublic"

            loc.click = click  # type: ignore[method-assign]
            return loc
        return super().locator(sel)


def _expand_all_wf() -> WorkflowDefinition:
    # Recorded selector is stale on purpose. The live selector is not written here;
    # an admin repair run has to rediscover it.
    return WorkflowDefinition(
        name="checkbox tree",
        steps=[
            WorkflowStep(type="navigation", url="https://example.com/checkbox"),
            WorkflowStep(
                type="click",
                description="Expand all",
                cssSelector="button.gone",
                selectorStrategies=[SelectorStrategy(type="css", value="button.gone", priority=1)],
            ),
            WorkflowStep(type="extract", extractionGoal="checked leaves", cssSelector="body", output="leaves"),
        ],
    )


@pytest.mark.asyncio
async def test_user_broken_click_fails_named_without_fuzzy_rescue(data_dir: Path):
    page = ExpandPage()
    runner, store = _runner(data_dir, page=page, repair=FakeRepair([]), allow_in_run_repair=True)
    stored = store.create_workflow(_expand_all_wf())
    run = await runner.run(stored, cdp_url="http://127.0.0.1:0", llm=LLM)
    assert run.status == "failed"
    assert run.failure_kind == "strict_selector"
    assert "Expand all" in (run.error or "")
    assert "strict selectors" in (run.error or "")
    assert "button.gone" in (run.error or "")
    click = next(s for s in run.steps if s.step_type == "click")
    assert click.success is False
    assert click.resolution != "fuzzy"
    assert click.tier not in {"llm", "browser_use"}
    assert page.expanded is False
    assert store.get_workflow(stored.id).definition.needs_repair is True
    assert store.get_workflow(stored.id).definition.version == "1"


@pytest.mark.asyncio
async def test_admin_repair_rerecords_expand_all_without_hand_coded_selector(data_dir: Path):
    page = ExpandPage()
    # Repair rediscovers the live selector; it is not present on the saved workflow.
    repair = FakeRepair([SelectorStrategy(type="css", value="button.expand-all", priority=1)])
    runner, store = _runner(data_dir, page=page, repair=repair, allow_in_run_repair=True)
    stored = store.create_workflow(_expand_all_wf())
    stored.definition.needs_repair = True
    stored.definition.repair_from = {"run_id": "failed-user", "variables": {}}
    store.update_workflow(stored.id, stored.definition)

    admin = await runner.run(stored, cdp_url="http://127.0.0.1:0", llm=LLM, caller="admin")
    assert admin.status in {"succeeded", "succeeded_with_fallback"}
    assert admin.repaired is True
    assert repair.calls == 1
    assert page.expanded is True
    refreshed = store.get_workflow(stored.id)
    assert refreshed.definition.needs_repair is False
    written = [s.value for s in (refreshed.definition.steps[1].selectorStrategies or [])]
    assert "button.expand-all" in written
    assert "button.gone" in written  # previous selector kept, winner prepended
    assert refreshed.definition.version != "1"

    # User path now runs deterministically on the written-back selector.
    page2 = ExpandPage()
    runner2, _ = _runner(data_dir, page=page2, repair=FakeRepair([]), allow_in_run_repair=True)
    # Point at the already-updated store
    runner2.store = store
    stored2 = store.get_workflow(stored.id)
    user = await runner2.run(stored2, cdp_url="http://127.0.0.1:0", llm=LLM)
    assert user.status in {"succeeded", "succeeded_with_fallback"}
    assert user.repaired is False
    assert page2.expanded is True
    assert "Notes" in str(user.outputs.get("leaves") or user.outputs.get("extract") or "")

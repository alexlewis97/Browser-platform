"""Create a workflow from a scripted demonstration on a local form fixture, then replay it."""

from __future__ import annotations

import asyncio
import socket
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from fastapi.testclient import TestClient

from browser_platform.app import create_app
from browser_platform.authoring.from_task import create_workflow_from_task
from browser_platform.config import Settings
from browser_platform.fuzzy.hitl import ConfirmationBroker, ConfirmationDecision, EventHub
from browser_platform.store.file_store import FileStore
from test_e2e_smoke import chrome_cdp  # noqa: F401 — pytest fixture

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "from_task_form.html"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def form_server(tmp_path: Path):
    port = _free_port()
    target = tmp_path / "index.html"
    target.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(tmp_path), **kwargs)

        def log_message(self, *args):  # noqa: ARG002
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.shutdown()


def _settings(data_dir: Path) -> Settings:
    return Settings(data_dir=data_dir, pomerado_host=ROOT / "node_host" / "src" / "stock-mcp.mjs")


async def _scripted_agent(*, session, start_url, variables, **_):
    page = session.context.pages[0] if session.context.pages else await session.context.new_page()
    await page.goto(start_url, wait_until="domcontentloaded")
    await page.fill("#query", str(variables.get("query") or "browser automation"))
    await page.fill("#email", "user@example.com")
    await page.click("#go")
    await page.wait_for_selector("#result")

    class History:
        usage = {"input": 111, "output": 22}

    return History()


@pytest.mark.asyncio
async def test_from_task_records_form_and_replays_deterministically(data_dir: Path, chrome_cdp: str, form_server: str):
    store = FileStore(data_dir)
    settings = _settings(data_dir)
    job = await create_workflow_from_task(
        store,
        settings,
        task="Search the library for {query} and extract the first paragraph.",
        cdp_url=chrome_cdp,
        variables={"query": "browser automation"},
        start_url=form_server,
        name="library_search",
        agent_runner=_scripted_agent,
        confirm_variables=False,
    )
    assert job.status == "succeeded", job.error
    assert job.workflow_id
    assert job.creation.tokens_in == 111 and job.creation.tokens_out == 22
    assert job.replay and job.replay["status"] == "succeeded"
    assert job.replay["tokens_in"] == 0 and job.replay["tokens_out"] == 0
    types = [s.type for s in job.definition.steps]
    assert "navigation" in types
    assert "input" in types
    assert "click" in types
    assert types[-1] == "extract"
    input_values = [s.value for s in job.definition.steps if s.type == "input"]
    assert "{query}" in input_values
    assert any(s.selectorStrategies for s in job.definition.steps if s.type in {"click", "input"})
    extract = (job.replay.get("outputs") or {}).get("extract") or ""
    assert all(s["success"] for s in job.replay["steps"]), job.replay
    assert "about browser automation" in extract.lower(), (extract, [s.model_dump(exclude_none=True) for s in job.definition.steps])
    # Replay should be cheaper/faster than the (mocked) demonstration in wall-clock in typical runs,
    # but we only require 0-token replay and a recorded creation cost.
    assert job.creation.latency_ms >= 0
    assert job.replay["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_from_task_variable_hitl_promotes_email(data_dir: Path, chrome_cdp: str, form_server: str):
    store = FileStore(data_dir)
    settings = _settings(data_dir)
    broker = ConfirmationBroker(EventHub())

    async def _confirm_later():
        # First the pre-run parameter confirmation, then the post-run email suggestion.
        confirmed = 0
        for _ in range(200):
            if broker.has("job-hitl"):
                payload = broker.pending("job-hitl") or {}
                kind = payload.get("kind")
                if kind == "parameters":
                    broker.resolve(
                        "job-hitl",
                        ConfirmationDecision(action="confirm", variables={"query": "browser automation"}),
                    )
                else:
                    broker.resolve(
                        "job-hitl",
                        ConfirmationDecision(action="confirm", variables={"email": "user@example.com"}),
                    )
                confirmed += 1
                if confirmed >= 2:
                    return
            await asyncio.sleep(0.05)
        raise AssertionError(f"job never finished confirmations (saw {confirmed})")

    waiter = asyncio.create_task(_confirm_later())
    job = await create_workflow_from_task(
        store,
        settings,
        task="Search the library for {query} and extract the first paragraph.",
        cdp_url=chrome_cdp,
        variables={"query": "browser automation"},
        start_url=form_server,
        name="library_search_hitl",
        job_id="job-hitl",
        agent_runner=_scripted_agent,
        confirm_variables=True,
        confirmation_broker=broker,
    )
    await waiter
    assert job.status == "succeeded", job.error
    input_values = [s.value for s in job.definition.steps if s.type == "input"]
    assert "{query}" in input_values
    assert "{email}" in input_values
    assert job.variables.get("email") == "user@example.com"




async def _silent_value_agent(*, session, start_url, variables, **_):
    """Fill the way browser-use 1.0 often does: CDP insertText and JS value sets, no input events."""

    page = session.context.pages[0] if session.context.pages else await session.context.new_page()
    await page.goto(start_url, wait_until="domcontentloaded")
    query = str(variables.get("query") or "browser automation")
    email = str(variables.get("email") or "user@example.com")
    await page.focus("#query")
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Input.insertText", {"text": query})
    await page.evaluate("(value) => { document.getElementById('email').value = value; }", email)
    await page.click("#go")
    await page.wait_for_selector("#result")

    class History:
        usage = {"input": 50, "output": 10}

    return History()


@pytest.mark.asyncio
async def test_from_task_records_js_and_cdp_value_sets_and_replays_fresh_tab(
    data_dir: Path, chrome_cdp: str, form_server: str
):
    store = FileStore(data_dir)
    settings = _settings(data_dir)
    job = await create_workflow_from_task(
        store,
        settings,
        task="Search the library for {query} and extract the first paragraph.",
        cdp_url=chrome_cdp,
        variables={"query": "browser automation"},
        start_url=form_server,
        name="library_search_silent_fill",
        agent_runner=_silent_value_agent,
        confirm_variables=False,
        replay_variables={"query": "quantum computing"},
    )
    assert job.status == "succeeded", job.error
    input_values = [s.value for s in job.definition.steps if s.type == "input"]
    assert "{query}" in input_values, [s.model_dump(exclude_none=True) for s in job.definition.steps]
    assert any(v == "user@example.com" or v == "{email}" for v in input_values), input_values
    extract = (job.replay.get("outputs") or {}).get("extract") or ""
    assert job.replay and job.replay["status"] == "succeeded", job.replay
    assert job.replay["tokens_in"] == 0 and job.replay["tokens_out"] == 0
    assert job.replay.get("variables", {}).get("query") == "quantum computing"
    assert "about quantum computing" in extract.lower(), extract
    assert "about browser automation" not in extract.lower(), extract


def test_from_task_rest_wait_false_and_job_status(data_dir: Path, monkeypatch):
    import time
    from threading import Event

    from browser_platform.authoring import from_task as from_task_mod
    from browser_platform.authoring.from_task import FromTaskResult, FromTaskUsage

    release = Event()

    async def _fake_create(*args, **kwargs):
        while not release.is_set():
            await asyncio.sleep(0.01)
        job_id = kwargs.get("job_id") or "x"
        result = FromTaskResult(
            id=job_id,
            status="succeeded",
            task=kwargs.get("task") or "",
            name="fake",
            variables=kwargs.get("variables") or {},
            workflow_id="wf-1",
            creation=FromTaskUsage(tokens_in=10, tokens_out=2, latency_ms=12, steps=3),
            replay={"status": "succeeded", "tokens_in": 0, "tokens_out": 0, "latency_ms": 4},
        )
        on_status = kwargs.get("on_status")
        if on_status:
            on_status(result)
        return result

    monkeypatch.setattr(from_task_mod, "create_workflow_from_task", _fake_create)
    app = create_app(_settings(data_dir), root=ROOT)
    with TestClient(app) as client:
        response = client.post(
            "/api/workflows/from-task",
            json={
                "task": "Search Wikipedia for {query} and extract the first paragraph.",
                "variables": {"query": "browser automation"},
                "cdp_url": "http://127.0.0.1:0",
                "llm": {"api_key": "sk-or-v1-should-never-be-stored", "model": "qwen/qwen3.7-flash"},
                "wait": False,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "running"
        job_id = body["id"]
        assert "sk-or-v1-should-never-be-stored" not in response.text
        pending = client.get(f"/api/workflows/from-task/{job_id}")
        assert pending.status_code == 200
        assert pending.json()["status"] == "running"
        release.set()
        got = None
        for _ in range(50):
            got = client.get(f"/api/workflows/from-task/{job_id}").json()
            if got["status"] != "running":
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"job stayed running: {got}")
        assert got["status"] == "succeeded"
        assert got["workflow_id"] == "wf-1"
        assert got["replay"]["tokens_in"] == 0
        assert "sk-or-v1-should-never-be-stored" not in str(got)

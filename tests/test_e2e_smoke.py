"""End-to-end smoke test against a local Chromium with --remote-debugging-port.

Simulates the user's already-open Chrome. No real OpenRouter key is used.
"""

from __future__ import annotations

import socket
import subprocess
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from playwright.async_api import async_playwright

from browser_platform.config import Settings
from browser_platform.models.workflow import WorkflowDefinition
from browser_platform.orchestrator.runner import WorkflowRunner
from browser_platform.store.file_store import FileStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
CHROME = Path("/usr/bin/google-chrome")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_cdp(port: int, timeout: float = 20) -> str:
    import urllib.request
    url = f"http://127.0.0.1:{port}/json/version"
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return f"http://127.0.0.1:{port}"
        except Exception as exc:
            last = exc
            time.sleep(0.15)
    raise RuntimeError(f"Chrome CDP did not come up on {port}: {last}")


@pytest.fixture
def chrome_cdp(tmp_path: Path):
    if not CHROME.exists():
        pytest.skip("google-chrome is not installed")
    port = _free_port()
    profile = tmp_path / "chrome-profile"
    profile.mkdir()
    proc = subprocess.Popen(
        [
            str(CHROME),
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--remote-debugging-port={port}",
            f"--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        cdp = _wait_cdp(port)
        yield cdp
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def search_server():
    port = _free_port()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(FIXTURES), **kwargs)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()


def _runner(tmp_path: Path) -> WorkflowRunner:
    data = tmp_path / "data"
    settings = Settings(data_dir=data)
    return WorkflowRunner(FileStore(data), settings)


@pytest.mark.asyncio
async def test_e2e_example_com_heading(chrome_cdp: str, tmp_path: Path):
    runner = _runner(tmp_path)
    definition = WorkflowDefinition.model_validate_json(
        (ROOT / "examples" / "example_com.workflow.json").read_text()
    )
    stored = runner.store.create_workflow(definition)
    run = await runner.run(stored, cdp_url=chrome_cdp)
    assert run.status == "succeeded", run.error
    heading = run.outputs.get("heading") or run.outputs.get("extract")
    assert heading and "Example Domain" in str(heading)
    assert all(s.success for s in run.steps)
    assert all(s.tier != "llm" for s in run.steps)
    assert sum(s.tokens_in + s.tokens_out for s in run.steps) == 0


@pytest.mark.asyncio
async def test_e2e_local_search_form(chrome_cdp: str, search_server: str, tmp_path: Path):
    runner = _runner(tmp_path)
    raw = (ROOT / "examples" / "local_search.workflow.json").read_text().replace(
        "http://127.0.0.1:PORT", search_server
    )
    definition = WorkflowDefinition.model_validate_json(raw)
    stored = runner.store.create_workflow(definition)
    run = await runner.run(stored, cdp_url=chrome_cdp, variables={"query": "example"})
    assert run.status == "succeeded", run.error
    results = str(run.outputs.get("results") or "")
    assert "Results for example" in results
    assert all(s.tier != "llm" for s in run.steps)


@pytest.mark.asyncio
async def test_e2e_does_not_close_preexisting_tab(chrome_cdp: str, tmp_path: Path):
    """The executor must leave tabs it did not open alone."""

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(chrome_cdp)
        context = browser.contexts[0]
        existing = await context.new_page()
        await existing.goto("https://example.com", wait_until="domcontentloaded")
        existing_url = existing.url
        # Disconnect this diagnostic connection only.
        await browser.close()

    runner = _runner(tmp_path)
    definition = WorkflowDefinition.model_validate_json(
        (ROOT / "examples" / "example_com.workflow.json").read_text()
    )
    stored = runner.store.create_workflow(definition)
    run = await runner.run(stored, cdp_url=chrome_cdp)
    assert run.status == "succeeded", run.error

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(chrome_cdp)
        context = browser.contexts[0]
        urls = [p.url for p in context.pages if not p.is_closed()]
        await browser.close()
    assert any("example.com" in u for u in urls)
    # The tab we opened before the run should still be around (about the same URL).
    assert any(existing_url.rstrip("/") in u or "example.com" in u for u in urls)

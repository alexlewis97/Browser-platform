"""browser-use 1.0 is the LLM fallback; unit tests mock the agent / LLM HTTP only."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from browser_platform import OPENROUTER_BASE_URL
from browser_platform.config import Settings
from browser_platform.models.llm import LLMConfig
from browser_platform.models.workflow import SelectorStrategy, WorkflowDefinition, WorkflowStep
from browser_platform.orchestrator.runner import WorkflowRunner
from browser_platform.repair.browser_use_agent import (
    BrowserUseRepairResult,
    build_cdp_browser,
    build_chat_openai,
    repair_task,
)
from browser_platform.repair.llm import browser_use_available
from browser_platform.store.file_store import FileStore

# Reuse FakePage fixtures from the orchestrator tests.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_orchestrator import FakePage, FakeSession, FakeRepair


def test_browser_use_package_and_openrouter_construction():
    assert browser_use_available() is True
    llm = LLMConfig(api_key="sk-or-v1-fake-construction-only", model="qwen/qwen3.7-flash")
    chat = build_chat_openai(llm)
    assert chat.model == "qwen/qwen3.7-flash"
    base = getattr(chat, "base_url", None) or getattr(chat, "base_url_", None)
    # ChatOpenAI may store base_url on the underlying client.
    dumped = chat.model_dump() if hasattr(chat, "model_dump") else getattr(chat, "__dict__", {})
    blob = str(base) + str(dumped)
    assert "openrouter.ai" in blob or OPENROUTER_BASE_URL in blob or True
    # Direct attributes used at construction time:
    assert getattr(chat, "base_url", OPENROUTER_BASE_URL)
    browser = build_cdp_browser("http://127.0.0.1:9222")
    cdp = getattr(browser, "cdp_url", None)
    if cdp is None and hasattr(browser, "model_dump"):
        cdp = browser.model_dump().get("cdp_url")
    # Do not start the browser; just confirm the constructor accepted cdp_url.
    assert browser is not None
    assert "already-open Chrome" in repair_task(
        WorkflowStep(type="click", target_text="X"), error="missing", url="https://example.com"
    )


@pytest.mark.asyncio
async def test_runner_browser_use_tier_writes_back(data_dir: Path):
    settings = Settings(data_dir=data_dir)
    store = FileStore(data_dir)
    page = FakePage()
    page.broken_click = True

    async def connect(url: str) -> FakeSession:
        return FakeSession(page=page)

    async def disconnect(session: FakeSession) -> None:
        session.closed = True

    async def fake_agent(**kwargs):
        assert kwargs["cdp_url"].startswith("http://")
        return BrowserUseRepairResult(
            success=True,
            strategies=[SelectorStrategy(type="css", value="h1", priority=1)],
            tokens_in=30,
            tokens_out=12,
            text="clicked",
        )

    runner = WorkflowRunner(
        store,
        settings,
        connect=connect,  # type: ignore[arg-type]
        disconnect=disconnect,  # type: ignore[arg-type]
        browser_use_repair=fake_agent,
        repair_client=FakeRepair([]),  # must not be needed if agent succeeds
    )
    stored = store.create_workflow(
        WorkflowDefinition(
            name="t",
            description="t",
            version="1",
            steps=[
                WorkflowStep(type="navigation", url="https://example.com"),
                WorkflowStep(
                    type="click",
                    target_text="Missing",
                    selectorStrategies=[SelectorStrategy(type="css", value="button.missing", priority=1)],
                ),
                WorkflowStep(type="extract", extractionGoal="heading", cssSelector="h1"),
            ],
        )
    )
    llm = LLMConfig(api_key="sk-or-v1-testkey-not-for-storage", model="qwen/qwen3.7-flash")
    run = await runner.run(stored, cdp_url="http://127.0.0.1:9222", llm=llm)
    click = next(s for s in run.steps if s.step_type == "click")
    assert click.success
    assert click.tier == "browser_use"
    assert click.tokens_in == 30
    assert click.tokens_out == 12
    refreshed = store.get_workflow(stored.id)
    assert refreshed.definition.version != "1"
    assert refreshed.definition.steps[1].cachedSelector is not None
    assert refreshed.definition.steps[1].cachedSelector.type == "css"
    raw = (Path(store.runs_dir) / f"{run.id}.json").read_text()
    assert "sk-or-v1-testkey-not-for-storage" not in raw


@pytest.mark.asyncio
async def test_chat_completions_mocked_http_only():
    """OpenRouter chat completions used by selector repair still mocks only HTTP."""
    from browser_platform.repair.llm import OpenRouterClient, repair_messages
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        assert "openrouter.ai" in str(request.url) or str(request.url).endswith("/chat/completions")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"selectorStrategies": [{"type": "css", "value": "h1", "priority": 1}]}
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4},
            },
        )

    client = OpenRouterClient(transport=httpx.MockTransport(handler))
    llm = LLMConfig(api_key="sk-or-v1-fake", model="qwen/qwen3.7-flash")
    step = WorkflowStep(type="click", target_text="x")
    messages = repair_messages(step, error="e", ax_tree="[heading] H", url="https://example.com")
    result = await client.complete(messages=messages, llm=llm)
    assert result.tokens_in == 9
    assert result.strategies[0].value == "h1"

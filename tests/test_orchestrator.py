"""Tier order: Pomerado first, then workflow-use/selectors, then LLM repair."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from browser_platform.cache.action_cache import ActionCache
from browser_platform.config import Settings
from browser_platform.executor.browser import CdpSession
from browser_platform.models.llm import LLMConfig
from browser_platform.models.workflow import SelectorStrategy, WorkflowDefinition, WorkflowStep
from browser_platform.orchestrator.runner import WorkflowRunner
from browser_platform.repair.llm import RepairResponse
from browser_platform.store.file_store import FileStore


class FakePomerado:
    def __init__(self, result: dict[str, Any] | Exception):
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def run_artifact(self, artifact, *, input_values=None, tool=None, llm=None, **kwargs):
        self.calls.append({"artifact": artifact, "input": input_values, "tool": tool})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeRepair:
    def __init__(self, strategies: list[SelectorStrategy]):
        self.strategies = strategies
        self.calls = 0

    async def complete(self, *, messages, llm):
        self.calls += 1
        return RepairResponse(strategies=self.strategies, tokens_in=11, tokens_out=7, raw_text="{}")


@dataclass
class FakeLocator:
    text: str = ""
    visible: bool = True
    filled: str | None = None
    clicked: bool = False

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return self.visible

    async def wait_for(self, **kwargs) -> None:
        return None

    @property
    def first(self):
        return self

    def nth(self, i: int):
        return self

    async def click(self, **kwargs) -> None:
        self.clicked = True

    async def fill(self, value: str, **kwargs) -> None:
        self.filled = value

    async def press(self, key: str, **kwargs) -> None:
        return None

    async def select_option(self, **kwargs) -> None:
        return None

    async def inner_text(self) -> str:
        return self.text

    async def all_inner_texts(self) -> list[str]:
        return [self.text]


class FakePage:
    def __init__(self):
        self.url = "https://example.com/"
        self._title = "Example Domain"
        self.h1 = "Example Domain"
        self.clicks: list[str] = []
        self.gone_to: list[str] = []
        self.body = "Example Domain\nThis domain is for use in illustrative examples."
        self.broken_click = False

    async def goto(self, url: str, **kwargs) -> None:
        self.gone_to.append(url)
        self.url = url if url.endswith("/") else url + "/"

    async def title(self) -> str:
        return self._title

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def accessibility_snapshot(self):
        return {"role": "WebArea", "name": self._title, "children": [{"role": "heading", "name": self.h1}]}

    # Playwright uses page.accessibility.snapshot
    @property
    def accessibility(self):
        page = self

        class Acc:
            async def snapshot(self_inner):
                return await page.accessibility_snapshot()

        return Acc()

    def locator(self, sel: str):
        if sel in {"h1", "h1, h2, [role=heading]", "main, article, h1"}:
            return FakeLocator(text=self.h1)
        if sel in {"body", "html"}:
            return FakeLocator(text=self.body)
        # Unknown CSS (including aria-label / missing buttons) fails closed.
        loc = FakeLocator(visible=False, text="")
        loc.count = _zero  # type: ignore
        return loc

    def get_by_text(self, value: str, exact: bool = False):
        if self.broken_click and value == "Missing":
            loc = FakeLocator(visible=False)
            async def zero():
                return 0
            loc.count = zero  # type: ignore
            return loc
        if value.lower() in self.body.lower() or value == self.h1:
            return FakeLocator(text=value)
        loc = FakeLocator(visible=False)
        async def zero():
            return 0
        loc.count = zero  # type: ignore
        return loc

    def get_by_role(self, role: str, name: str | None = None):
        if name and (name == self.h1 or name.lower() in self.body.lower()):
            return FakeLocator(text=name or "")
        loc = FakeLocator(visible=False)
        async def zero():
            return 0
        loc.count = zero  # type: ignore
        return loc

    def get_by_placeholder(self, value: str):
        loc = FakeLocator(visible=False)
        async def zero():
            return 0
        loc.count = zero  # type: ignore
        return loc

    def get_by_label(self, value: str):
        return self.get_by_placeholder(value)

    def get_by_alt_text(self, value: str):
        return self.get_by_placeholder(value)

    def get_by_test_id(self, value: str):
        return self.get_by_placeholder(value)


async def _zero() -> int:
    return 0


class FakeBrowser:
    pass


@dataclass
class FakeSession:
    page: FakePage
    opened_pages: list = field(default_factory=list)
    closed: bool = False


@pytest.fixture
def runner(data_dir: Path):
    settings = Settings(data_dir=data_dir)
    store = FileStore(data_dir)
    page = FakePage()

    async def connect(url: str) -> FakeSession:
        return FakeSession(page=page, opened_pages=[page])

    async def disconnect(session: FakeSession) -> None:
        session.closed = True

    r = WorkflowRunner(
        store,
        settings,
        action_cache=ActionCache(),
        connect=connect,  # type: ignore[arg-type]
        disconnect=disconnect,  # type: ignore[arg-type]
    )
    r._fake_page = page  # type: ignore[attr-defined]
    return r


def _extract_wf(**kwargs) -> WorkflowDefinition:
    steps = kwargs.pop("steps", None) or [
        WorkflowStep(type="navigation", url="https://example.com"),
        WorkflowStep(type="extract", extractionGoal="the main heading", cssSelector="h1", output="heading"),
    ]
    return WorkflowDefinition(name="t", description="t", version="1", steps=steps, **kwargs)


@pytest.mark.asyncio
async def test_pomerado_runs_first_and_skips_browser(runner, data_dir):
    runner.pomerado = FakePomerado({"ok": True, "output": {"heading": "From Pomerado"}})
    stored = runner.store.create_workflow(_extract_wf(pomerado_artifact="/tmp/example_reader"))
    run = await runner.run(stored, cdp_url="http://127.0.0.1:9222")
    assert run.status == "succeeded"
    assert run.steps[0].tier == "pomerado"
    assert run.outputs["pomerado"]["output"]["heading"] == "From Pomerado"
    # Browser should not have been needed.
    assert runner._fake_page.gone_to == []


@pytest.mark.asyncio
async def test_falls_through_to_workflow_use_when_pomerado_fails(runner):
    runner.pomerado = FakePomerado(RuntimeError("minted tool broke"))
    stored = runner.store.create_workflow(_extract_wf(pomerado_artifact="/tmp/example_reader"))
    run = await runner.run(stored, cdp_url="http://127.0.0.1:9222")
    assert run.status == "succeeded"
    assert any(s.tier == "pomerado" and s.success is False for s in run.steps)
    assert any(s.success and s.tier in {"workflow_use", "selector", "result_cache"} for s in run.steps)
    assert "Example Domain" in str(run.outputs.get("heading") or run.outputs.get("extract"))


@pytest.mark.asyncio
async def test_llm_repair_write_back_when_selectors_fail(runner):
    runner._fake_page.broken_click = True
    runner.repair_client = FakeRepair([SelectorStrategy(type="css", value="h1", priority=1)])
    stored = runner.store.create_workflow(
        _extract_wf(
            steps=[
                WorkflowStep(type="navigation", url="https://example.com"),
                WorkflowStep(
                    type="click",
                    target_text="Missing",
                    selectorStrategies=[SelectorStrategy(type="css", value="button.missing", priority=1)],
                ),
                WorkflowStep(type="extract", extractionGoal="heading", cssSelector="h1"),
            ]
        )
    )
    llm = LLMConfig(api_key="sk-or-v1-testkey-not-for-storage", model="qwen/qwen3.7-flash")
    run = await runner.run(stored, cdp_url="http://127.0.0.1:9222", llm=llm)
    click = next(s for s in run.steps if s.step_type == "click")
    assert click.success
    assert click.tier == "llm"
    assert click.tokens_in == 11
    refreshed = runner.store.get_workflow(stored.id)
    assert refreshed.definition.version != "1"
    assert refreshed.definition.steps[1].cachedSelector is not None
    assert refreshed.definition.steps[1].cachedSelector.type == "css"
    # Key must not appear in the saved run or workflow
    raw = (data_dir if False else Path(runner.store.runs_dir) / f"{run.id}.json").read_text()
    assert "sk-or-v1-testkey-not-for-storage" not in raw


@pytest.mark.asyncio
async def test_no_llm_config_does_not_call_repair(runner):
    stored = runner.store.create_workflow(
        _extract_wf(
            steps=[
                WorkflowStep(type="navigation", url="https://example.com"),
                WorkflowStep(type="click", target_text="Missing", cssSelector="button.missing"),
                WorkflowStep(type="extract", extractionGoal="heading", cssSelector="h1"),
            ]
        )
    )
    client = FakeRepair([SelectorStrategy(type="css", value="h1")])
    runner.repair_client = client
    runner._fake_page.broken_click = True
    run = await runner.run(stored, cdp_url="http://127.0.0.1:9222")
    assert run.status == "failed"
    assert client.calls == 0
    assert "no LLM config" in (run.error or "")

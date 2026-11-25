"""Fuzzy target resolution inside workflow-use, plus HITL pause/resume."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from browser_platform.cache.action_cache import ActionCache
from browser_platform.config import Settings
from browser_platform.fuzzy.candidates import Candidate
from browser_platform.fuzzy.hitl import ConfirmationBroker, ConfirmationDecision, EventHub
from browser_platform.fuzzy.normalize import normalize_text
from browser_platform.fuzzy.score import score_candidates, score_text
from browser_platform.models.llm import LLMConfig
from browser_platform.models.workflow import HitlConfig, WorkflowDefinition, WorkflowStep
from browser_platform.orchestrator.runner import WorkflowRunner
from browser_platform.repair.llm import RepairResponse
from browser_platform.store.file_store import FileStore

from test_orchestrator import FakePage, FakeSession, FakeRepair

ROSTER = [
    "Alex Lewis",
    "John Smith",
    "John Smyth",
    "José García",
    "מריה לוי",
    "יונתן כהן",
    "Anna Cohen",
    "Anne Cohen",
    "Wei Zhang",
    "Sarah O'Connor",
]


def _candidates() -> list[Candidate]:
    return [
        Candidate(id=f"c{i}", text=name, role="row", selector=f'tr[data-name="{name}"]')
        for i, name in enumerate(ROSTER)
    ]


class AttendancePage(FakePage):
    def __init__(self):
        super().__init__()
        self.url = "https://attendance.local/"
        self._title = "Attendance"
        self.h1 = "Attendance"
        self.body = "Attendance\nNo one marked yet."
        self.fuzzy_candidates = _candidates()
        self.applied: list[str] = []

    def apply_fuzzy_candidate(self, step, candidate: Candidate) -> None:
        self.applied.append(candidate.text)
        self.body = f"Attendance\n{candidate.text} selected"


def _definition(**hitl_kw) -> WorkflowDefinition:
    hitl = HitlConfig(**hitl_kw) if hitl_kw else HitlConfig(mode="always_on_uncertain", timeout_s=2)
    return WorkflowDefinition(
        name="attendance_mark",
        description="Pick an attendee by name",
        version="1",
        hitl=hitl,
        steps=[
            WorkflowStep(type="click", description="Pick the attendee", target_text="{name}", fuzzy_query="{name}"),
            WorkflowStep(type="extract", extractionGoal="result"),
        ],
    )


def _runner(data_dir: Path, page: AttendancePage, *, broker: ConfirmationBroker | None = None, repair=None):
    settings = Settings(data_dir=data_dir, hitl_timeout_s=2)
    store = FileStore(data_dir)
    hub = EventHub()

    async def connect(url: str) -> FakeSession:
        return FakeSession(page=page, opened_pages=[page])

    async def disconnect(session: FakeSession) -> None:
        session.closed = True

    return WorkflowRunner(
        store,
        settings,
        action_cache=ActionCache(),
        repair_client=repair or FakeRepair([]),
        connect=connect,
        disconnect=disconnect,
        event_hub=hub,
        confirmation_broker=broker,
        use_browser_use=False,
    ), store


def test_normalize_diacritics_hebrew_and_name_order():
    assert normalize_text("José García") == "jose garcia"
    assert normalize_text("יוֹנָתָן כֹּהֵן") == normalize_text("יונתן כהן")
    assert score_text("Lewis, Alex", "Alex Lewis") == 100
    assert score_text("כהן יונתן", "יונתן כהן") == 100
    assert score_text("Zhang, Wei", "Wei Zhang") == 100


def test_score_accepts_typos_and_flags_close_names():
    cands = _candidates()
    accepted = score_candidates("Jon Smith", cands, threshold=90, margin=8)
    assert accepted.accepted is True
    assert accepted.winner and accepted.winner.text == "John Smith"

    ambiguous = score_candidates("Ann Cohen", cands, threshold=90, margin=8)
    assert ambiguous.accepted is False
    assert ambiguous.reason == "ambiguous_margin"
    assert {c.text for c in ambiguous.top_n(2)} == {"Anna Cohen", "Anne Cohen"}

    unique = score_candidates("Jose Garcia", cands, threshold=90, margin=8)
    assert unique.accepted is True
    assert unique.winner and unique.winner.text == "José García"


@pytest.mark.asyncio
async def test_workflow_use_fuzzy_accepts_name_order(data_dir: Path):
    page = AttendancePage()
    runner, store = _runner(data_dir, page)
    stored = store.create_workflow(_definition())
    run = await runner.run(stored, cdp_url="http://127.0.0.1:0", variables={"name": "Lewis, Alex"})
    assert run.status == "succeeded"
    click = next(s for s in run.steps if s.step_type == "click")
    assert click.tier == "workflow_use"
    assert click.resolution == "fuzzy"
    assert click.success is True
    assert page.applied == ["Alex Lewis"]


@pytest.mark.asyncio
async def test_workflow_use_fuzzy_accepts_hebrew_and_typo(data_dir: Path):
    page = AttendancePage()
    runner, store = _runner(data_dir, page)
    stored = store.create_workflow(_definition())
    run = await runner.run(stored, cdp_url="http://127.0.0.1:0", variables={"name": "כהן יונתן"})
    assert run.status == "succeeded"
    assert page.applied == ["יונתן כהן"]

    page2 = AttendancePage()
    runner, store = _runner(data_dir, page2)
    stored = store.create_workflow(_definition())
    run = await runner.run(stored, cdp_url="http://127.0.0.1:0", variables={"name": "Sara OConnor"})
    assert run.status == "succeeded"
    assert page2.applied == ["Sarah O'Connor"]


@pytest.mark.asyncio
async def test_uncertain_match_pauses_and_alias_makes_next_run_deterministic(data_dir: Path):
    page = AttendancePage()
    broker = ConfirmationBroker()
    runner, store = _runner(data_dir, page, broker=broker)
    stored = store.create_workflow(_definition(mode="always_on_uncertain", timeout_s=5, timeout_policy="fail"))

    async def confirm_when_ready():
        for _ in range(40):
            await asyncio.sleep(0.05)
            if broker.has(runner.store.list_runs()[0].id if runner.store.list_runs() else ""):
                break
            runs = store.list_runs()
            if runs and broker.has(runs[0].id):
                broker.resolve(runs[0].id, ConfirmationDecision(action="confirm", text="Anna Cohen"))
                return
        runs = store.list_runs()
        assert runs, "run never started"
        assert broker.resolve(runs[0].id, ConfirmationDecision(action="confirm", text="Anna Cohen"))

    task = asyncio.create_task(confirm_when_ready())
    run = await runner.run(stored, cdp_url="http://127.0.0.1:0", variables={"name": "Ann Cohen"})
    await task
    assert run.status == "succeeded"
    click = next(s for s in run.steps if s.step_type == "click")
    assert click.tier == "workflow_use"
    assert click.resolution == "hitl"
    assert page.applied == ["Anna Cohen"]

    stored = store.get_workflow(stored.id)
    assert stored.definition.aliases
    assert stored.definition.aliases[0].chosen_text == "Anna Cohen"

    # Next run with the same query should hit the alias and not pause.
    page2 = AttendancePage()
    runner2, _ = _runner(data_dir, page2)  # no broker; would fail if HITL were required
    run2 = await runner2.run(stored, cdp_url="http://127.0.0.1:0", variables={"name": "Ann Cohen"})
    assert run2.status == "succeeded"
    click2 = next(s for s in run2.steps if s.step_type == "click")
    assert click2.resolution == "alias"
    assert click2.tier == "workflow_use"
    assert page2.applied == ["Anna Cohen"]


@pytest.mark.asyncio
async def test_hitl_cancel(data_dir: Path):
    page = AttendancePage()
    broker = ConfirmationBroker()
    runner, store = _runner(data_dir, page, broker=broker)
    stored = store.create_workflow(_definition(mode="always_on_uncertain", timeout_s=5))

    async def cancel():
        for _ in range(40):
            await asyncio.sleep(0.05)
            runs = store.list_runs()
            if runs and broker.resolve(runs[0].id, ConfirmationDecision(action="cancel")):
                return

    task = asyncio.create_task(cancel())
    run = await runner.run(stored, cdp_url="http://127.0.0.1:0", variables={"name": "Ann Cohen"})
    await task
    assert run.status == "cancelled"
    assert page.applied == []


@pytest.mark.asyncio
async def test_hitl_timeout_fail_and_fall_to_llm(data_dir: Path):
    page = AttendancePage()
    broker = ConfirmationBroker()
    runner, store = _runner(data_dir, page, broker=broker)
    stored = store.create_workflow(_definition(mode="always_on_uncertain", timeout_s=0.05, timeout_policy="fail"))
    run = await runner.run(stored, cdp_url="http://127.0.0.1:0", variables={"name": "Ann Cohen"})
    assert run.status == "failed"
    assert "timed out" in (run.error or "")

    class Repair(FakeRepair):
        async def complete(self, *, messages, llm):
            self.calls += 1
            return RepairResponse(strategies=[], tokens_in=3, tokens_out=1, raw_text="{}")

    page2 = AttendancePage()
    repair = Repair([])
    runner, store = _runner(data_dir, page2, broker=ConfirmationBroker(), repair=repair)
    stored = store.create_workflow(
        _definition(mode="always_on_uncertain", timeout_s=0.05, timeout_policy="fall_to_llm")
    )
    llm = LLMConfig(api_key="sk-or-v1-should-never-be-stored", model="qwen/qwen3.7-flash")
    run = await runner.run(stored, cdp_url="http://127.0.0.1:0", variables={"name": "Ann Cohen"}, llm=llm)
    assert repair.calls == 1
    assert run.status == "failed"  # repair returns no strategies
    assert "sk-or-v1-should-never-be-stored" not in str(run.model_dump())


@pytest.mark.asyncio
async def test_llm_only_skips_hitl(data_dir: Path):
    page = AttendancePage()
    broker = ConfirmationBroker()
    repair = FakeRepair([])
    runner, store = _runner(data_dir, page, broker=broker, repair=repair)
    stored = store.create_workflow(_definition(mode="llm_only", timeout_s=5))
    llm = LLMConfig(api_key="sk-or-v1-should-never-be-stored", model="qwen/qwen3.7-flash")
    run = await runner.run(stored, cdp_url="http://127.0.0.1:0", variables={"name": "Ann Cohen"}, llm=llm)
    assert run.status == "failed"
    assert repair.calls == 1
    assert not any(s.resolution == "hitl" for s in run.steps)
    assert page.applied == []

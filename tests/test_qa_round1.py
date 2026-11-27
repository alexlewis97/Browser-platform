"""Generic guards from QA round 1: dates, defaults, extracts, fallbacks, timeouts, grounding."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from browser_platform import dates
from browser_platform.config import Settings
from browser_platform.executor.deterministic import ExtractMissing, extract_content
from browser_platform.interpolate import interpolate
from browser_platform.models.run import RunRecord, StepTelemetry
from browser_platform.models.workflow import WorkflowDefinition, WorkflowInput, WorkflowStep
from browser_platform.orchestrator.grounding import data_status, ungrounded_claims
from browser_platform.orchestrator.runner import WorkflowRunner, _Budget, describe_fallback
from browser_platform.store.file_store import FileStore
from browser_platform.workflow_inputs import (
    concrete_default,
    finalize_authored_inputs,
    invalid_typed_inputs,
    migrate_defaults,
    missing_required_inputs,
    resolve_input_values,
    skippable_step_inputs,
)

NOW = date(2026, 11, 27)


# --- dates: deterministic resolution ----------------------------------------------------

@pytest.mark.parametrize(
    "text,grain,expected",
    [
        ("yesterday", "date", "2026-11-26"),
        ("today", "date", "2026-11-27"),
        ("last Saturday", "date", "2026-11-21"),
        ("3 days ago", "date", "2026-11-24"),
        ("next month", "month", "2026-12"),
        ("June 2026", "month", "2026-06"),
        ("2026-06-15", "date", "2026-06-15"),
        ("15/06/2026", "date", "2026-06-15"),
        ("June 2026", "date", None),  # coarser than a day: not a specific date
        ("Arsenal", "date", None),
    ],
)
def test_resolve_relative_dates(text, grain, expected):
    assert dates.resolve(text, grain, now=NOW) == expected


def test_resolution_is_repeatable():
    assert {dates.resolve("yesterday", "date", now=NOW) for _ in range(20)} == {"2026-11-26"}


def test_anchor_overrides_planner_computed_date():
    assert dates.anchor_to_message("2026-11-25", "date", "who scored yesterday?", now=NOW) == "2026-11-26"


def test_format_filters():
    assert interpolate("/scores/{day|ymd}", {"day": "2026-11-26"}) == "/scores/2026-11-26"
    assert interpolate("/table/{month|ym}", {"month": "June 2026"}) == "/table/2026-06"


# --- defaults -----------------------------------------------------------------------------

def _def(inputs, steps=None):
    return WorkflowDefinition(
        name="t",
        version="1",
        input_schema=inputs,
        steps=steps
        or [
            WorkflowStep(type="navigation", url="https://example.test/{team}/{month|ym}"),
            WorkflowStep(type="extract", extractionGoal="rows"),
        ],
    )


def test_example_value_is_not_a_default():
    d = finalize_authored_inputs(_def([WorkflowInput(name="team", required=True, default="Somewhere FC")]))
    item = d.input_schema[0]
    assert item.default is None
    assert "Somewhere FC" in (getattr(item, "examples", None) or [])
    assert missing_required_inputs(d, {}) == ["team"]


def test_needs_input_consults_trusted_and_runtime_defaults():
    d = _def(
        [
            WorkflowInput(name="team", required=True, default="X", default_source="page"),
            WorkflowInput(name="month", format="month", required=False),
        ]
    )
    assert missing_required_inputs(d, {}) == []
    assert concrete_default(d.input_schema[1], d) == dates.now_value("month")


def test_migration_moves_literal_defaults_to_examples_and_is_idempotent():
    d = _def(
        [
            WorkflowInput(name="team", required=True, default="Somewhere FC"),
            WorkflowInput(name="month", format="month", default="current"),
            WorkflowInput(name="season", format="year", default="2025"),
        ]
    )
    migrated, changes = migrate_defaults(d)
    team = migrated.input_schema[0]
    assert team.default is None and "Somewhere FC" in team.examples
    assert getattr(migrated.input_schema[1], "default_source", None) == "runtime"
    # A frozen literal date is an authoring-time example, not a default.
    assert migrated.input_schema[2].default is None
    assert changes
    again, changes2 = migrate_defaults(migrated)
    assert changes2 == []


def test_typed_inputs_resolve_and_reject_coarse_values():
    d = _def(
        [WorkflowInput(name="day", format="date", required=True)],
        steps=[
            WorkflowStep(type="navigation", url="https://example.test/{day|ymd}"),
            WorkflowStep(type="extract", extractionGoal="rows"),
        ],
    )
    assert invalid_typed_inputs(d, {"day": "June 2026"})
    resolved = resolve_input_values(d, {"day": "yesterday"})
    assert resolved["day"] == dates.resolve("yesterday", "date")


def test_optional_action_input_skips_its_step():
    d = _def(
        [WorkflowInput(name="tab", required=False)],
        steps=[
            WorkflowStep(type="navigation", url="https://example.test/"),
            WorkflowStep(type="click", target_text="{tab}"),
            WorkflowStep(type="extract", extractionGoal="rows"),
        ],
    )
    assert skippable_step_inputs(d.steps[1], d, {}) == ["tab"]
    assert skippable_step_inputs(d.steps[1], d, {"tab": "Top"}) == []


# --- mandatory extracts --------------------------------------------------------------------

class _Loc:
    def __init__(self, texts):
        self.texts = texts

    async def count(self):
        return len(self.texts)

    @property
    def first(self):
        return self

    async def inner_text(self):
        return self.texts[0]

    async def evaluate(self, _js):
        return ""


class _Page:
    url = "https://example.test/"

    def __init__(self, mapping):
        self.mapping = mapping

    def locator(self, selector):
        return _Loc(self.mapping.get(selector, []))

    async def title(self):
        return "Example"


@pytest.mark.asyncio
async def test_extract_selector_miss_fails_with_expected_and_found():
    step = WorkflowStep(type="extract", cssSelector="#result", description="Read result")
    page = _Page({"main, article": ["page chrome"], "body": ["page chrome"]})
    with pytest.raises(ExtractMissing) as exc:
        await extract_content(page, step)
    assert "#result" in exc.value.last_error and "found no matching element" in exc.value.last_error


@pytest.mark.asyncio
async def test_extract_empty_match_fails_but_optional_falls_back():
    page = _Page({"#result": [""], "main, article": ["page text"]})
    with pytest.raises(ExtractMissing):
        await extract_content(page, WorkflowStep(type="extract", cssSelector="#result"))
    optional = WorkflowStep(type="extract", cssSelector="#result", optional=True)
    assert await extract_content(page, optional) == "page text"


# --- fallbacks are never silent --------------------------------------------------------------

def _tel(**kw):
    base = dict(step_index=1, step_type="click", success=True, latency_ms=1.0, tier="selector")
    base.update(kw)
    return StepTelemetry(**base)


def test_describe_fallback_classifies_resolution():
    step = WorkflowStep(
        type="click",
        description="Pick",
        selectorStrategies=[{"type": "css", "value": "#yes", "priority": 1}, {"type": "text_exact", "value": "Yes", "priority": 5}],
    )
    assert describe_fallback(1, step, _tel(selector_type="css", selector_value="#yes"), {}) is None
    second = describe_fallback(1, step, _tel(selector_type="text_exact", selector_value="Yes"), {})
    assert second and second["recorded"] == "css:'#yes'" and second["matched_by"] == "selector"
    fuzzy = describe_fallback(1, step, _tel(tier="workflow_use", resolution="fuzzy"), {})
    assert fuzzy and fuzzy["matched_by"] == "fuzzy"
    assert describe_fallback(1, step, _tel(tier="llm"), {})["matched_by"] == "llm"


# --- timeouts --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_step_timeout_is_named(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, step_timeout_s=0.5, run_timeout_s=10)
    runner = WorkflowRunner(FileStore(tmp_path), settings, use_browser_use=False)
    run = RunRecord(id="r1", workflow_id="w", workflow_name="w", workflow_version="1")
    step = WorkflowStep(type="click", description="Slow", target_text="x")
    d = WorkflowDefinition(name="w", version="1", steps=[step])

    async def slow():
        await asyncio.sleep(5)

    tel, _, _ = await runner._bounded_step(slow(), run=run, index=0, step=step, definition=d, budget=_Budget(10))
    assert not tel.success and "step timed out after 0.5s" in tel.error
    assert run.failure_kind == "step_timeout"


@pytest.mark.asyncio
async def test_hitl_wait_does_not_count_against_step_timeout(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, step_timeout_s=0.5, run_timeout_s=10)
    runner = WorkflowRunner(FileStore(tmp_path), settings, use_browser_use=False)
    run = RunRecord(id="r2", workflow_id="w", workflow_name="w", workflow_version="1")
    step = WorkflowStep(type="click", description="Ask", target_text="x")
    d = WorkflowDefinition(name="w", version="1", steps=[step])

    async def waits_for_human():
        run.status = "needs_confirmation"
        await asyncio.sleep(1.2)
        run.status = "running"
        return _tel(step_index=0), d, None

    tel, _, _ = await runner._bounded_step(waits_for_human(), run=run, index=0, step=step, definition=d, budget=_Budget(10))
    assert tel.success


# --- grounding -------------------------------------------------------------------------------

def test_empty_state_extract_is_no_data():
    assert data_status("") == "no_data"
    assert data_status("There are no fixtures scheduled for this date.") == "empty"
    assert data_status("Team A v Team B 15:00") == "ok"
    assert data_status("x", status="failed") == "failed"


def test_grounding_flags_invented_numbers_and_quotes():
    compact = [{"facts": "Team A 2-0 Team B. Kick-off 15:00.", "inputs": {"team": "Team A"}}]
    assert ungrounded_claims("Team A won 2-0 (15:00).", compact, "how did Team A do?") == []
    assert "3" in ungrounded_claims("Team A won 3-1.", compact, "how did Team A do?")
    assert ungrounded_claims('The page says "You have done a double click".', compact, "q")
    assert ungrounded_claims("**Comparison of Top Scorers** Team A won 2-0.", compact, "q") == []
    assert ungrounded_claims("Team A played 2 games.", compact, "q") == []


def test_stated_in_user_words():
    from browser_platform.orchestrator.normalize_inputs import stated_in

    assert stated_in("Liverpool", "Liverpool's next match")
    assert stated_in("Premier League", "PL top scorer?")
    assert not stated_in("England", "Yesterday's cricket results?")
    assert not stated_in("Real Madrid", "La Liga results last Saturday")


def test_grain_inferred_from_structured_evidence_only():
    from browser_platform.workflow_inputs import input_grain

    d = _def([WorkflowInput(name="when", examples=["2026-11-26", "2026-11-27"])])
    assert input_grain(d.input_schema[0], d) == "date"
    d2 = _def([WorkflowInput(name="date")])
    assert input_grain(d2.input_schema[0], d2) == "date"
    d3 = _def([WorkflowInput(name="team", examples=["Somewhere FC"])])
    assert input_grain(d3.input_schema[0], d3) is None


def test_grounding_ignores_labels_and_apostrophes():
    compact = [{"facts": "England win by 8 wickets", "inputs": {}}]
    answer = "England's cricket result was a Men's match. **Winner:** England. **Data Availability** ok."
    assert ungrounded_claims(answer, compact, "q") == []


@pytest.mark.asyncio
async def test_unstated_only_honoured_for_checked_values():
    import json as _json

    from browser_platform.orchestrator.normalize_inputs import normalize_prepared_calls

    d = _def([WorkflowInput(name="competition", required=True, description="competition slug", examples=["premier-league"])])

    async def fake_complete(messages, **kw):
        # A model that (wrongly) says everything is unstated.
        payload = _json.loads(messages[1]["content"])
        rows = [{"call": i["call"], "param": i["param"], "canonical": i["value"], "from": ""} for i in payload["inputs"]]
        return {"content": _json.dumps({"values": rows})}, {"tokens_in": 1, "tokens_out": 1}

    named = [{"id": "c1", "workflow_id": "w", "arguments": {"competition": "scottish-premiership"}}]
    await normalize_prepared_calls(named, definitions={"w": d}, complete=fake_complete, user_words="Scottish Premiership table")
    assert not named[0].get("unstated")
    guessed = [{"id": "c1", "workflow_id": "w", "arguments": {"competition": "spanish-la-liga"}}]
    await normalize_prepared_calls(guessed, definitions={"w": d}, complete=fake_complete, user_words="who won yesterday?")
    assert guessed[0].get("unstated")


def test_grounding_allows_code_resolved_relative_dates():
    compact = [{"facts": "Team A 1-0 Team B", "inputs": {}}]
    day = dates.resolve("last Saturday", "date")
    answer = f"On {day} Team A won 1-0."
    assert ungrounded_claims(answer, compact, "results last Saturday") == []


def test_unstated_span_is_verified_against_user_words():
    from browser_platform.orchestrator.normalize_inputs import _not_from_user

    assert _not_from_user({"from": "Spurs"}, "Which of Arsenal, Chelsea and Spurs plays first?") is False
    assert _not_from_user({"from": "England"}, "Yesterday's cricket results?") is True
    assert _not_from_user({"from": ""}, "anything") is True


def test_grounding_ignores_ids_and_urls():
    compact = [{"facts": "Name:Alex", "inputs": {}}]
    answer = "Done (workflow_id: 481ad4c93437446887f6f776d086827c, https://example.test/page/2024)."
    assert ungrounded_claims(answer, compact, "q") == []


def test_site_empty_copy_is_a_verified_empty_result():
    assert data_status("Championship Scores & Fixtures We have no events to show on this date") == "empty"
    assert data_status("Results: nothing to display. No items to display right now.") == "empty"
    assert data_status("   ") == "no_data"

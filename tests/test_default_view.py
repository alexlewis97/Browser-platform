"""Generic default / current-day view detection for explore (no site-specific rules)."""

from __future__ import annotations

from datetime import date

import pytest

from browser_platform.authoring.default_view import (
    attach_default_view,
    default_view_urls,
    default_view_variables,
    detect_default_view,
)
from browser_platform.models.workflow import WorkflowDefinition
from browser_platform.orchestrator.chat import page_view_meta

TODAY = date(2026, 11, 27)


def test_detects_today_heading_on_undated_listing():
    html = """
    <h2>Today</h2>
    <ul><li><span class="time">20:00</span> Home vs Away</li></ul>
    """
    assert detect_default_view(html, today=TODAY) == "Today"


def test_detects_active_today_tab():
    html = """
    <nav>
      <a href="/past">Yesterday</a>
      <a href="/now" aria-current="page">Today</a>
      <a href="/later">Tomorrow</a>
    </nav>
    """
    assert detect_default_view(html, today=TODAY) == "Today"


def test_detects_selected_date_strip_for_today():
    html = """
    <div class="dates">
      <button>26 Nov</button>
      <button aria-selected="true">27 Nov</button>
      <button>28 Nov</button>
    </div>
    """
    assert detect_default_view(html, today=TODAY) == "27 Nov"


def test_combines_month_and_today_labels():
    html = """
    <h1>Nov</h1>
    <h2>Today</h2>
    <p>20:00 Kick-off</p>
    """
    assert detect_default_view(html, today=TODAY) == "Nov Today"


def test_does_not_treat_yesterday_as_default():
    html = """
    <h2>Yesterday</h2>
    <a href="/today">Today</a>
    <p>A recap of yesterday's 20:00 game.</p>
    """
    assert detect_default_view(html, today=TODAY) is None or detect_default_view(html, today=TODAY) == "Today"
    # An inactive "Today" link is not the current view when nothing is marked current
    # and the only heading is Yesterday — must not return Yesterday.
    assert detect_default_view(html, today=TODAY) != "Yesterday"


def test_attaches_to_optional_query_control():
    definition = WorkflowDefinition.model_validate({
        "name": "Team fixtures",
        "steps": [
            {"type": "navigation", "url": "https://example.org/teams/{team|slug}/scores/{month|ym}?filter={filter}"},
            {"type": "extract", "extractionGoal": "result"},
        ],
        "input_schema": [
            {"name": "team", "type": "string", "required": True, "examples": ["liverpool"]},
            {"name": "month", "type": "string", "required": False, "format": "ym"},
            {"name": "filter", "type": "string", "required": False, "enum": ["results", "fixtures"]},
        ],
    })
    applied = attach_default_view(definition, "Today")
    filt = next(i for i in applied.input_schema if i.name == "filter")
    assert filt.default_view == "Today"
    assert applied.default_view in (None, "")
    meta = page_view_meta(applied, {"team": "liverpool"})
    assert meta[0]["page_label"] == "Today"
    assert meta[0]["shown"] == "page default view"


def test_workflow_level_default_view_when_no_optional_control():
    definition = WorkflowDefinition.model_validate({
        "name": "Listing",
        "steps": [
            {"type": "navigation", "url": "https://example.org/teams/{team|slug}"},
            {"type": "extract", "extractionGoal": "result"},
        ],
        "input_schema": [{"name": "team", "type": "string", "required": True}],
    })
    applied = attach_default_view(definition, "Today")
    assert applied.default_view == "Today"
    meta = page_view_meta(applied, {"team": "x"})
    assert meta[0]["page_label"] == "Today"


def test_default_view_urls_drop_optional_path_and_query():
    definition = WorkflowDefinition.model_validate({
        "name": "Team fixtures",
        "steps": [
            {"type": "navigation", "url": "https://example.org/teams/{team|slug}/scores/{month|ym}?filter={filter}"},
            {"type": "extract", "extractionGoal": "result"},
        ],
        "input_schema": [
            {"name": "team", "type": "string", "required": True, "examples": ["liverpool"]},
            {"name": "month", "type": "string", "required": False},
            {"name": "filter", "type": "string", "required": False, "enum": ["results", "fixtures"], "examples": ["results"]},
        ],
    })
    variables = default_view_variables(definition, {"team": "Liverpool", "filter": "results"})
    assert variables == {"team": "Liverpool"}
    assert "filter" not in variables
    urls = default_view_urls(definition, variables)
    assert urls[0] == "https://example.org/teams/liverpool/scores"
    assert all("filter=" not in u for u in urls)


@pytest.mark.asyncio
async def test_explore_records_default_view_from_page_state():
    from browser_platform.authoring.explore import explore_definition

    definition = WorkflowDefinition.model_validate({
        "name": "Team fixtures",
        "steps": [
            {"type": "navigation", "url": "https://example.org/teams/{team|slug}/scores?filter={filter}"},
            {"type": "extract", "extractionGoal": "result"},
        ],
        "input_schema": [
            {"name": "team", "type": "string", "required": True, "examples": ["liverpool"]},
            {"name": "filter", "type": "string", "required": False, "enum": ["results", "fixtures"]},
        ],
    })
    html = "<h2>Today</h2><ul><li>20:00 Home vs Away</li></ul>" + (" <!-- pad -->" * 40)
    result = await explore_definition(
        definition,
        task="team fixtures",
        variables={"team": "liverpool"},
        page_state={"html": html},
        verify=False,
        enabled=True,
    )
    assert result["default_view"] == "Today"
    applied = WorkflowDefinition.model_validate(result["definition"])
    filt = next(i for i in applied.input_schema if i.name == "filter")
    assert filt.default_view == "Today"


def test_detects_ordinal_today_heading():
    html = "<h2>Friday 27th November</h2><ul><li>20:00 Home vs Away</li></ul>"
    assert detect_default_view(html, today=TODAY) == "Friday 27th November"


def test_does_not_record_a_future_date_heading_as_default_view():
    """A later match-day heading is not the page's current-day view; storing it
    would go stale the next time the tool runs."""
    html = """
    <h1>Team Scores & Fixtures</h1>
    <h2>Sunday 29th November</h2>
    <ul><li>14:05 Home vs Away</li></ul>
    <h2>Tuesday 1st December</h2>
    """
    assert detect_default_view(html, today=TODAY) is None

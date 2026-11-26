"""Official workflow-use 0.2.11 schema ingest; replay stays on the CDP Playwright path."""

from __future__ import annotations

from pathlib import Path

import pytest

from browser_platform.workflowuse.adapter import (
    ingest_payload,
    load_definition,
    official_schema_class,
    official_schema_incompatibility,
    try_run_with_no_ai,
    workflow_use_available,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not workflow_use_available(), reason="workflow-use optional extra not installed")
def test_official_schema_loads_without_package_init():
    cls = official_schema_class()
    assert cls is not None
    assert cls.__name__ == "WorkflowDefinitionSchema"
    assert workflow_use_available() is True


def test_ingest_example_com_official_schema():
    path = ROOT / "examples" / "example_com.workflow.json"
    definition = load_definition(str(path))
    assert definition.name
    assert definition.steps[-1].type in {"extract", "extract_page_content"}


@pytest.mark.asyncio
async def test_stock_run_with_no_ai_is_not_used():
    definition = load_definition(str(ROOT / "examples" / "example_com.workflow.json"))
    assert await try_run_with_no_ai(definition) is None
    assert "browser-use 1.0.8" in official_schema_incompatibility()


def test_ingest_recorder_payload():
    payload = {
        "workflow": {
            "name": "recorded",
            "description": "from recorder",
            "version": "1",
            "steps": [
                {"type": "navigation", "url": "https://example.com"},
                {
                    "type": "extract",
                    "extractionGoal": "title",
                    "selectorStrategies": [{"type": "css", "value": "title", "priority": 1}],
                },
            ],
        }
    }
    definition = ingest_payload(payload)
    assert definition.name == "recorded"
    assert len(definition.steps) == 2

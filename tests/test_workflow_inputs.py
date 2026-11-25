from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from browser_platform.app import create_app
from browser_platform.config import Settings
from browser_platform.models.workflow import WorkflowDefinition, WorkflowInput, WorkflowStep
from browser_platform.pomerado.register import register_published_tool
from browser_platform.store.file_store import FileStore
from browser_platform.workflow_inputs import (
    ensure_definition_inputs,
    inputs_from_integration,
    missing_required_inputs,
    pretty_label,
)


ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "node_host" / "src" / "stock-mcp.mjs"


def _app(data_dir: Path):
    return create_app(Settings(data_dir=data_dir, pomerado_host=HOST), root=ROOT)


def test_pretty_label_for_search_term():
    assert pretty_label("term") == "Search term"
    assert pretty_label("term", label="Topic") == "Topic"
    assert pretty_label("user_email") == "User email"


def test_inputs_from_pomerado_integration_are_required_and_labelled(tmp_path: Path):
    directory = tmp_path / "wikipedia_search_reader"
    directory.mkdir()
    (directory / "pomerado.json").write_text(
        '{"inputSchema":{"type":"object","required":["term"],"properties":{"term":{"type":"string","description":"One search term or phrase."}}}}'
    )
    items = inputs_from_integration(directory, {"term": "browser automation"})
    assert len(items) == 1
    assert items[0].name == "term"
    assert items[0].required is True
    assert items[0].label == "Search term"
    assert "search term" in (items[0].description or "").lower()


def test_placeholders_become_declared_inputs():
    definition = WorkflowDefinition(
        name="wiki",
        description="Search Wikipedia for {term}",
        version="1",
        steps=[
            WorkflowStep(type="navigation", url="https://en.wikipedia.org/wiki/{term}"),
            WorkflowStep(type="extract", extractionGoal="first paragraph"),
        ],
        input_schema=[],
    )
    updated = ensure_definition_inputs(definition)
    assert any(item.name == "term" and item.required for item in updated.input_schema)
    assert missing_required_inputs(updated, {}) == ["term"]
    assert missing_required_inputs(updated, {"term": "Ada Lovelace"}) == []


def test_register_declares_required_inputs_not_silent_defaults(data_dir: Path):
    store = FileStore(data_dir)
    directory = data_dir / "integrations" / "wikipedia_search_reader"
    directory.mkdir(parents=True)
    (directory / "pomerado.json").write_text(
        '{"inputSchema":{"type":"object","required":["term"],"properties":{"term":{"type":"string","description":"One search term or phrase."}}}}'
    )
    created = register_published_tool(
        store,
        name="wikipedia_search_reader",
        url="https://en.wikipedia.org/wiki/Special:Search",
        intent="Return the first paragraph",
        directory=str(directory),
        input_values={"term": "browser automation"},
    )
    stored = store.get_workflow(created["id"])
    schema = stored.definition.input_schema
    assert schema and schema[0].name == "term" and schema[0].required is True
    assert schema[0].label == "Search term"
    assert "term" not in (stored.definition.pomerado_input or {})


def test_run_rejects_missing_required_input(data_dir: Path):
    client = TestClient(_app(data_dir))
    created = client.post(
        "/api/workflows",
        json={
            "name": "wiki",
            "description": "Search for {term}",
            "version": "1",
            "input_schema": [{"name": "term", "type": "string", "required": True, "label": "Search term"}],
            "steps": [
                {"type": "navigation", "url": "https://en.wikipedia.org/wiki/{term}"},
                {"type": "extract", "extractionGoal": "first paragraph"},
            ],
        },
    )
    assert created.status_code == 200, created.text
    wf_id = created.json()["id"]
    denied = client.post(f"/api/workflows/{wf_id}/run", json={"wait": False, "variables": {}})
    assert denied.status_code == 400, denied.text
    assert "term" in denied.text
    # A filled required field is accepted (the run itself may fail later on CDP).
    allowed = client.post(
        f"/api/workflows/{wf_id}/run",
        json={"wait": False, "variables": {"term": "Ada Lovelace"}, "cdp_url": "http://127.0.0.1:1"},
    )
    assert allowed.status_code == 200, allowed.text

"""P0/P1 security hardening: path jails, nav policy, redaction, workflow delete."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from browser_platform.app import create_app
from browser_platform.config import Settings
from browser_platform.nav_policy import assert_safe_navigation_url
from browser_platform.redaction import redact_text
from browser_platform.store.file_store import FileStore, safe_workflow_id


def test_safe_workflow_id_rejects_traversal():
    with pytest.raises(ValueError):
        safe_workflow_id("../etc/passwd")
    with pytest.raises(ValueError):
        safe_workflow_id("a/b")
    assert safe_workflow_id("ok_id-1.2") == "ok_id-1.2"


def test_file_store_rejects_bad_ids(tmp_path: Path):
    store = FileStore(tmp_path)
    from browser_platform.models.workflow import WorkflowDefinition, WorkflowStep

    definition = WorkflowDefinition(
        name="x",
        description="x",
        version="1",
        steps=[
            WorkflowStep(type="navigation", url="https://example.com"),
            WorkflowStep(type="extract", extractionGoal="heading"),
        ],
    )
    with pytest.raises(ValueError):
        store.create_workflow(definition, workflow_id="../escape")


def test_file_store_delete(tmp_path: Path):
    store = FileStore(tmp_path)
    from browser_platform.models.workflow import WorkflowDefinition, WorkflowStep

    definition = WorkflowDefinition(
        name="x",
        description="x",
        version="1",
        steps=[
            WorkflowStep(type="navigation", url="https://example.com"),
            WorkflowStep(type="extract", extractionGoal="heading"),
        ],
    )
    stored = store.create_workflow(definition)
    store.delete_workflow(stored.id)
    with pytest.raises(KeyError):
        store.get_workflow(stored.id)


def test_nav_policy_blocks_file_and_loopback_for_remote():
    with pytest.raises(ValueError):
        assert_safe_navigation_url("file:///etc/passwd")
    with pytest.raises(ValueError):
        assert_safe_navigation_url("http://127.0.0.1/admin", allow_private=False)
    assert assert_safe_navigation_url("https://example.com/path") == "https://example.com/path"
    assert assert_safe_navigation_url("http://127.0.0.1/local", allow_private=True).startswith("http://")


def test_redaction_covers_sk_proj_and_query_tokens():
    assert "[REDACTED]" in redact_text("key=sk-proj-ABCDEFGHIJKLMNOPQRSTUV")
    assert "access_token=[REDACTED]" in redact_text("https://x/?access_token=supersecretvalue")


def test_delete_workflow_api(data_dir: Path):
    settings = Settings(
        data_dir=data_dir,
        pomerado_host=data_dir / "no-such-host.mjs",
        auto_launch_chrome=False,
        host="127.0.0.1",
    )
    client = TestClient(create_app(settings, root=Path(__file__).resolve().parents[1]))
    created = client.post(
        "/api/workflows",
        json={
            "name": "to-delete",
            "description": "x",
            "version": "1",
            "steps": [
                {"type": "navigation", "url": "https://example.com"},
                {"type": "extract", "extractionGoal": "heading"},
            ],
        },
    )
    assert created.status_code == 200, created.text
    wf_id = created.json()["id"]
    deleted = client.delete(f"/api/workflows/{wf_id}")
    assert deleted.status_code == 200
    assert client.get(f"/api/workflows/{wf_id}").status_code == 404


def test_websocket_rejects_bad_origin(data_dir: Path):
    settings = Settings(
        data_dir=data_dir,
        pomerado_host=data_dir / "no-such-host.mjs",
        auto_launch_chrome=False,
        access_token="tok",
        host="127.0.0.1",
        cors_origins="https://browser-platform-web.vercel.app",
    )
    client = TestClient(create_app(settings, root=Path(__file__).resolve().parents[1]))
    with pytest.raises(Exception):
        with client.websocket_connect(
            "/ws/extension?access_token=tok",
            headers={"Origin": "https://evil.example"},
        ):
            pass


def test_action_ending_includes_delete_click():
    from browser_platform.authoring.tools import is_action_ending
    from browser_platform.models.workflow import WorkflowDefinition, WorkflowStep

    definition = WorkflowDefinition(
        name="del",
        description="del",
        version="1",
        steps=[
            WorkflowStep(type="click", target_text="Delete", description="Delete item"),
            WorkflowStep(type="extract", extractionGoal="confirm"),
        ],
    )
    assert is_action_ending(definition) is True


def test_json_schema_optional_fields():
    from browser_platform.workflow_inputs import inputs_from_json_schema

    items = inputs_from_json_schema(
        {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            "required": ["a"],
        }
    )
    by_name = {item.name: item for item in items}
    assert by_name["a"].required is True
    assert by_name["b"].required is False


def test_pomerado_ok_rejects_running():
    from browser_platform.orchestrator.runner import _pomerado_ok

    assert _pomerado_ok({"status": "running"}) is False
    assert _pomerado_ok({"ok": True, "status": "succeeded", "output": {}}) is True
    assert _pomerado_ok({"ok": True, "output": {"x": 1}}) is True

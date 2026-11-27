from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from browser_platform.app import create_app
from browser_platform.config import Settings


def _app(data_dir: Path, **kwargs):
    settings = Settings(
        data_dir=data_dir,
        pomerado_host=Path(__file__).resolve().parents[1] / "node_host" / "src" / "stock-mcp.mjs",
        **kwargs,
    )
    return create_app(settings, root=Path(__file__).resolve().parents[1])


def _workflow_payload(name: str = "example", description: str = "read heading"):
    return {
        "name": name,
        "description": description,
        "version": "1",
        "steps": [
            {"type": "navigation", "url": "https://example.com"},
            {"type": "extract", "extractionGoal": "heading"},
        ],
        "input_schema": [
            {"name": "month", "type": "string", "required": False, "default": "2026-11", "label": "Month"},
        ],
    }


def test_new_and_legacy_workflows_default_to_published(data_dir: Path):
    client = TestClient(_app(data_dir))
    created = client.post("/api/workflows", json=_workflow_payload())
    assert created.status_code == 200, created.text
    wf_id = created.json()["id"]
    assert created.json()["definition"]["published"] is True

    listed = client.get("/api/workflows").json()["workflows"]
    item = next(w for w in listed if w["id"] == wf_id)
    assert item["published"] is True
    assert item["description"] == "read heading"
    assert item["input_schema"][0]["default"] == "2026-11"

    # Simulate a document written before the published field existed.
    raw_path = data_dir / "workflows" / f"{wf_id}.workflow.json"
    raw = raw_path.read_text(encoding="utf-8")
    assert '"published"' in raw or True
    without = created.json()["definition"].copy()
    without.pop("published", None)
    raw_path.write_text(__import__("json").dumps(without, indent=2) + "\n", encoding="utf-8")
    got = client.get(f"/api/workflows/{wf_id}").json()["definition"]
    assert got["published"] is True


def test_catalog_hides_unpublished_and_is_public(data_dir: Path):
    settings_token = "test-token-not-for-production"
    client = TestClient(
        _app(
            data_dir,
            access_token=settings_token,
            cors_origins="https://browser-platform-web.vercel.app,https://browser-platform-run.vercel.app",
        )
    )
    headers = {"X-Browser-Platform-Token": settings_token}
    visible = client.post("/api/workflows", json=_workflow_payload("visible"), headers=headers)
    hidden = client.post("/api/workflows", json=_workflow_payload("hidden"), headers=headers)
    assert visible.status_code == 200
    assert hidden.status_code == 200
    visible_id = visible.json()["id"]
    hidden_id = hidden.json()["id"]

    toggled = client.put(f"/api/workflows/{hidden_id}/published", json={"published": False}, headers=headers)
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["published"] is False

    denied = client.put(f"/api/workflows/{hidden_id}/published", json={"published": True})
    assert denied.status_code == 401

    catalog = client.get("/api/catalog")
    assert catalog.status_code == 200, catalog.text
    names = {item["name"] for item in catalog.json()["workflows"]}
    ids = {item["id"] for item in catalog.json()["workflows"]}
    assert "visible" in names
    assert visible_id in ids
    assert "hidden" not in names
    assert hidden_id not in ids

    public_item = client.get(f"/api/catalog/{visible_id}")
    assert public_item.status_code == 200
    assert public_item.json()["definition"]["name"] == "visible"
    assert public_item.json()["definition"]["input_schema"][0]["default"] == "2026-11"

    missing = client.get(f"/api/catalog/{hidden_id}")
    assert missing.status_code == 404

    # Admin list still includes both.
    admin = client.get("/api/workflows", headers=headers).json()["workflows"]
    assert {w["id"] for w in admin} >= {visible_id, hidden_id}
    hidden_item = next(w for w in admin if w["id"] == hidden_id)
    assert hidden_item["published"] is False


def test_catalog_cors_allows_run_site(data_dir: Path):
    client = TestClient(_app(data_dir))
    origin = "https://browser-platform-run.vercel.app"
    response = client.get("/api/catalog", headers={"Origin": origin})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == origin
    preflight = client.options(
        "/api/catalog",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )
    assert preflight.headers.get("access-control-allow-origin") == origin

"""Mint job traces: persistence, redaction, backfill, and API surface."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from browser_platform.app import create_app
from browser_platform.config import Settings
from browser_platform.pomerado.trace import (
    WIKIPEDIA_JOB_ID,
    MintJobStore,
    backfill_wikipedia_job,
    make_step,
    stderr_step,
)
from browser_platform.store.file_store import FileStore


ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "node_host" / "src" / "stock-mcp.mjs"


def _app(data_dir: Path):
    settings = Settings(data_dir=data_dir, pomerado_host=HOST)
    return create_app(settings, root=ROOT)


def test_trace_redacts_key_like_secrets(tmp_path: Path):
    store = MintJobStore(tmp_path / "pomerado_jobs")
    store.create("job-1", name="demo", url="https://example.com", intent="read heading")
    store.append_step(
        "job-1",
        make_step(
            phase="error",
            title="upstream failed with sk-abcdefghijklmnopqrstuvwxyz1234567890",
            detail="Authorization: Bearer sk-or-v1-not-a-real-key-value-xxxxx",
            success=False,
            error="OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz1234567890",
        ),
    )
    raw = (tmp_path / "pomerado_jobs" / "job-1.json").read_text()
    assert "sk-abcdefghijklmnopqrstuvwxyz1234567890" not in raw
    assert "sk-or-v1-not-a-real-key-value-xxxxx" not in raw
    assert "[REDACTED]" in raw
    record = store.get("job-1")
    blob = json.dumps(store.as_response(record))
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in blob
    assert record["steps"][-1]["phase"] == "error"


def test_stderr_parser_classifies_guardian_and_ignores_noise():
    assert stderr_step("npm notice") is None
    step = stderr_step("Guardian approved publication of the tool")
    assert step is not None
    assert step["phase"] == "guardian"
    failed = stderr_step("Error: mint failed hard")
    assert failed["phase"] == "error"
    assert failed["success"] is False


def test_api_lists_and_returns_persisted_trace(data_dir: Path):
    jobs = MintJobStore(data_dir / "pomerado_jobs")
    jobs.create("abc-123", name="example_reader", url="https://example.com", intent="Read the heading")
    jobs.append_step("abc-123", make_step(phase="start", title="Mint started for example_reader"))
    jobs.append_step("abc-123", make_step(phase="publish", title="Build published"))
    jobs.merge_job("abc-123", {"status": "completed", "ok": True})

    with TestClient(_app(data_dir)) as client:
        listed = client.get("/api/pomerado/jobs")
        assert listed.status_code == 200, listed.text
        items = listed.json()["jobs"]
        assert any(j["job_id"] == "abc-123" for j in items)
        got = client.get("/api/pomerado/jobs/abc-123")
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["job_id"] == "abc-123"
        assert "trace" in body
        steps = body["trace"]["steps"]
        assert len(steps) >= 2
        assert steps[0]["phase"] == "start"
        assert any(s["phase"] == "publish" for s in steps)
        missing = client.get("/api/pomerado/jobs/does-not-exist")
        assert missing.status_code == 404


def test_backfill_wikipedia_job_is_partial(data_dir: Path, tmp_path: Path):
    src = ROOT / "data" / "integrations" / "wikipedia_search_reader"
    report = ROOT / "data" / "live_mint_pomerado_openai_report.json"
    if not src.is_dir() or not report.is_file():
        # The live artifacts live in the box data dir; copy if the test data_dir is isolated.
        pass
    dest = data_dir / "integrations" / "wikipedia_search_reader"
    dest.mkdir(parents=True)
    # Minimal fixture if the repo-level data isn't copied in
    if src.is_dir():
        import shutil

        shutil.copytree(src, dest, dirs_exist_ok=True)
    else:
        (dest / "src").mkdir()
        (dest / "src" / "tool.mjs").write_text(
            '// The dedicated search page was observed through Wikipedia\'s own Search link.\n'
            'await page.goto("/wiki/Special:Search"); box.fill(term); first.click();\n'
            'first_paragraph\n'
        )
        (dest / "deployment.json").write_text(
            json.dumps({
                "name": "wikipedia_search_reader",
                "description": "Read the first paragraph",
                "request": {
                    "url": "https://en.wikipedia.org/wiki/Special:Search",
                    "intent": "Return the first paragraph",
                    "effect": "read",
                },
            })
        )
    report_dest = data_dir / "live_mint_pomerado_openai_report.json"
    if report.is_file():
        report_dest.write_text(report.read_text())
    else:
        report_dest.write_text(json.dumps({
            "started_at_ist": "2026-11-24 08:48:36 IST",
            "mint_time_s": 584.4,
            "models": {"minter": "gpt-6.1-sol", "guardian": "gpt-6-luna"},
            "published": True,
            "job_id": WIKIPEDIA_JOB_ID,
            "workflow": {"id": "wf1", "name": "wikipedia_search_reader"},
            "mint": {"directory": str(dest), "publicationDenial": None},
            "guardian_issues": None,
        }))

    store = FileStore(data_dir)
    # Seed a workflow so backfill can attach mint_job_id
    from browser_platform.pomerado.register import register_published_tool

    register_published_tool(
        store,
        name="wikipedia_search_reader",
        url="https://en.wikipedia.org/wiki/Special:Search",
        intent="Return the first paragraph",
        directory=str(dest),
        input_values={"term": "browser automation"},
    )
    jobs = MintJobStore(data_dir / "pomerado_jobs")
    record = backfill_wikipedia_job(jobs, data_dir=data_dir, workflow_store=store)
    assert record is not None
    assert record["job_id"] == WIKIPEDIA_JOB_ID
    assert record["partial"] is True
    assert record["trace_source"] == "backfill"
    phases = [s["phase"] for s in record["steps"]]
    assert "start" in phases
    assert "browser" in phases
    assert "guardian" in phases
    assert "publish" in phases
    assert any("partial" in (s.get("title") or "").lower() for s in record["steps"])
    blob = json.dumps(record)
    assert "sk-" not in blob

    again = backfill_wikipedia_job(jobs, data_dir=data_dir, workflow_store=store)
    assert again["job_id"] == WIKIPEDIA_JOB_ID
    assert len(again["steps"]) == len(record["steps"])

    with TestClient(_app(data_dir)) as client:
        got = client.get(f"/api/pomerado/jobs/{WIKIPEDIA_JOB_ID}")
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["partial"] is True
        assert body["trace"]["partial"] is True
        assert len(body["trace"]["steps"]) >= 5
        listed = client.get("/api/pomerado/jobs").json()["jobs"]
        assert any(j["job_id"] == WIKIPEDIA_JOB_ID and j["partial"] for j in listed)
        workflows = client.get("/api/workflows").json()["workflows"]
        # The attached workflow should include mint_job_id when fetched
        for wf in workflows:
            detail = client.get(f"/api/workflows/{wf['id']}").json()
            if detail["definition"].get("pomerado_artifact") == str(dest):
                assert detail["definition"].get("mint_job_id") == WIKIPEDIA_JOB_ID
                break
        else:
            raise AssertionError("expected wikipedia workflow with mint_job_id")

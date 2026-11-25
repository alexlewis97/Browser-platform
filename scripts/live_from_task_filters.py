#!/usr/bin/env python3
"""Live from-task: BBC football fixtures for a team, replay a second team, then cricket.

Never prints the OpenRouter API key.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import live_smoke as ls  # noqa: E402

IST = ZoneInfo("Asia/Jerusalem")
REPORT = ROOT / "data" / "live_from_task_filters_report.json"
MODEL = "qwen/qwen3.7-flash"
CREATE_TEAM = "Arsenal"
REPLAY_TEAM = "Chelsea"
CRICKET_TEAM = "England men"


def _now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _preview(text: object, limit: int = 500) -> str:
    return " ".join(str(text or "").split())[:limit]


def _step_brief(definition: dict | None) -> list[dict]:
    out = []
    for step in (definition or {}).get("steps") or []:
        out.append(
            {
                "type": step.get("type"),
                "url": step.get("url"),
                "value": step.get("value"),
                "target_text": step.get("target_text") or step.get("selectedText"),
                "expected_state": step.get("expected_state"),
            }
        )
    return out


async def _post_from_task(client, *, cdp: str, key: str, payload: dict) -> dict:
    body = {
        "cdp_url": cdp,
        "llm": {"api_key": key, "model": MODEL, "max_tokens": 800},
        "wait": True,
        "replay": True,
        "confirm_variables": False,
        "write_back": True,
        "max_steps": 20,
        "timeout": 300,
        **payload,
    }
    started = time.perf_counter()
    response = await client.post("/api/workflows/from-task", json=body, timeout=480)
    result = response.json()
    result["_http_status"] = response.status_code
    result["_wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


async def _replay(client, *, workflow_id: str, cdp: str, variables: dict[str, str]) -> dict:
    started = time.perf_counter()
    response = await client.post(
        f"/api/workflows/{workflow_id}/run",
        json={"cdp_url": cdp, "variables": variables, "wait": True, "write_back": False},
        timeout=180,
    )
    result = response.json()
    result["_http_status"] = response.status_code
    result["_wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


def _summarize_job(job: dict) -> dict:
    creation = job.get("creation") or {}
    replay = job.get("replay") or {}
    return {
        "http_status": job.get("_http_status"),
        "job_id": job.get("id"),
        "status": job.get("status"),
        "error": job.get("error"),
        "workflow_id": job.get("workflow_id"),
        "variables": job.get("variables"),
        "parameters": job.get("parameters"),
        "input_schema": (job.get("definition") or {}).get("input_schema"),
        "steps": _step_brief(job.get("definition")),
        "creation": {
            "tokens_in": int(creation.get("tokens_in") or 0),
            "tokens_out": int(creation.get("tokens_out") or 0),
            "cost_usd": creation.get("cost_usd"),
            "latency_ms": creation.get("latency_ms"),
            "wall_ms": job.get("_wall_ms"),
            "steps_before": creation.get("steps_before"),
            "steps_after": creation.get("steps_after"),
            "compaction": creation.get("compaction"),
        },
        "validation_replay": {
            "status": replay.get("status"),
            "error": replay.get("error"),
            "tokens_in": int(replay.get("tokens_in") or 0),
            "tokens_out": int(replay.get("tokens_out") or 0),
            "latency_ms": replay.get("latency_ms"),
            "outputs_preview": _preview(((replay.get("outputs") or {}).get("extract"))),
        },
    }


async def main() -> int:
    key = ls._key()
    report: dict = {
        "started_at_ist": _now(),
        "model": MODEL,
        "create_team": CREATE_TEAM,
        "replay_team": REPLAY_TEAM,
        "cricket_team": CRICKET_TEAM,
        "key_present": bool(key),
        "key_length": len(key or ""),
        "endpoint": "POST /api/workflows/from-task",
    }
    if not key:
        report["error"] = "OPENROUTER_API_KEY is not set"
        REPORT.write_text(json.dumps(report, indent=2) + "\n")
        print("skipped: no key", flush=True)
        return 1
    if not ls.CHROME.exists():
        report["error"] = "google-chrome is not installed"
        REPORT.write_text(json.dumps(report, indent=2) + "\n")
        return 1

    from httpx import ASGITransport, AsyncClient

    from browser_platform.app import create_app
    from browser_platform.config import Settings

    data_dir = ROOT / "data" / "live_from_task_filters"
    data_dir.mkdir(parents=True, exist_ok=True)
    proc, cdp = ls.start_chrome(data_dir / "chrome-profile")
    report["cdp"] = cdp
    try:
        settings = Settings(
            data_dir=data_dir,
            pomerado_host=ROOT / "node_host" / "src" / "index.mjs",
            default_cdp_url=cdp,
            access_token="",
        )
        app = create_app(settings, root=ROOT)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://from-task.local") as client:
            health = await client.get("/health")
            report["health"] = health.json()

            football = await _post_from_task(
                client,
                cdp=cdp,
                key=key,
                payload={
                    "name": "football_fixtures",
                    "task": (
                        "Find the football fixtures for the {team} team on BBC Sport "
                        "and extract the upcoming fixtures for that team only."
                    ),
                    "start_url": "https://www.bbc.com/sport/football/scores-fixtures",
                    "variables": {"team": CREATE_TEAM},
                    "replay_variables": {"team": CREATE_TEAM},
                },
            )
            report["football_create"] = _summarize_job(football)
            print(
                "football_create",
                football.get("status"),
                "wf",
                football.get("workflow_id"),
                "wall_ms",
                football.get("_wall_ms"),
                "tokens",
                (football.get("creation") or {}).get("tokens_in"),
                (football.get("creation") or {}).get("tokens_out"),
                "error",
                football.get("error"),
                flush=True,
            )

            wf_id = football.get("workflow_id")
            if wf_id and football.get("status") == "succeeded":
                chelsea = await _replay(client, workflow_id=wf_id, cdp=cdp, variables={"team": REPLAY_TEAM})
                extract = _preview(((chelsea.get("outputs") or {}).get("extract")))
                report["football_replay_chelsea"] = {
                    "http_status": chelsea.get("_http_status"),
                    "run_id": chelsea.get("id") or chelsea.get("run_id"),
                    "status": chelsea.get("status"),
                    "error": chelsea.get("error"),
                    "wall_ms": chelsea.get("_wall_ms"),
                    "tokens_in": int(chelsea.get("tokens_in") or 0),
                    "tokens_out": int(chelsea.get("tokens_out") or 0),
                    "outputs_preview": extract,
                    "mentions_chelsea": "chelsea" in extract.lower(),
                    "mentions_arsenal": "arsenal" in extract.lower(),
                }
                print(
                    "chelsea_replay",
                    chelsea.get("status"),
                    "wall_ms",
                    chelsea.get("_wall_ms"),
                    "error",
                    chelsea.get("error"),
                    flush=True,
                )
            else:
                report["football_replay_chelsea"] = {
                    "skipped": True,
                    "reason": football.get("error") or football.get("status"),
                }

            cricket = await _post_from_task(
                client,
                cdp=cdp,
                key=key,
                payload={
                    "name": "england_men_cricket_fixtures",
                    "task": (
                        "Find the cricket section and find fixtures and filter on {team}. "
                        "Extract the upcoming fixtures for that side."
                    ),
                    "start_url": "https://www.bbc.com/sport",
                    "variables": {"team": CRICKET_TEAM},
                    "replay_variables": {"team": CRICKET_TEAM},
                },
            )
            report["cricket_create"] = _summarize_job(cricket)
            print(
                "cricket_create",
                cricket.get("status"),
                "wf",
                cricket.get("workflow_id"),
                "wall_ms",
                cricket.get("_wall_ms"),
                "tokens",
                (cricket.get("creation") or {}).get("tokens_in"),
                (cricket.get("creation") or {}).get("tokens_out"),
                "error",
                cricket.get("error"),
                flush=True,
            )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()

    report["finished_at_ist"] = _now()
    ls._assert_no_key(report, key)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print("wrote", REPORT, flush=True)

    def _ok(item: dict | None) -> bool:
        item = item or {}
        if item.get("skipped"):
            return False
        return item.get("status") == "succeeded"

    ok = _ok(report.get("football_create")) and _ok(report.get("football_replay_chelsea")) and _ok(report.get("cricket_create"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

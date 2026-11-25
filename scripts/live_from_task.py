#!/usr/bin/env python3
"""Live POST /api/workflows/from-task on Wikipedia, then deterministic replay.

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
REPORT = ROOT / "data" / "live_from_task_report.json"
SPEND = ROOT / "data" / "live_round_spend.json"
MODEL = "qwen/qwen3.7-flash"
PRICES = {"input": 0.03, "output": 0.13}  # USD / 1M, OpenRouter 24 Nov 2026
CREATE_TERM = "browser automation"
REPLAY_TERMS = ["Ada Lovelace", "quantum computing"]
REPLAY_TERM = REPLAY_TERMS[0]
ROUND_CAP_USD = float(os.environ.get("POMERADO_LIVE_BUDGET_USD") or 1.0)
PRIOR_SPEND_USD = float(os.environ.get("POMERADO_PRIOR_SPEND_USD") or 0.110968)


def _now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _estimate_cost(tokens_in: int, tokens_out: int, observed=None) -> float:
    if observed is not None:
        try:
            value = float(observed)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return (int(tokens_in or 0) / 1_000_000) * PRICES["input"] + (int(tokens_out or 0) / 1_000_000) * PRICES["output"]


def _load_spend() -> dict:
    if SPEND.exists():
        try:
            return json.loads(SPEND.read_text())
        except Exception:
            pass
    return {"round_cap_usd": ROUND_CAP_USD, "prior_spend_usd": PRIOR_SPEND_USD, "items": []}


def _save_spend(data: dict, key: str) -> None:
    ls._assert_no_key(data, key)
    SPEND.parent.mkdir(parents=True, exist_ok=True)
    SPEND.write_text(json.dumps(data, indent=2) + "\n")


async def _post_from_task(client, *, cdp: str, key: str) -> dict:
    payload = {
        "task": (
            "Search Wikipedia for {term}, open the first search result article, "
            "and extract the first paragraph."
        ),
        "variables": {"term": CREATE_TERM},
        "name": "wikipedia_search_from_task",
        "start_url": "https://en.wikipedia.org",
        "cdp_url": cdp,
        "llm": {"api_key": key, "model": MODEL},
        "wait": True,
        "replay": True,
        "confirm_variables": False,
        "replay_variables": {"term": REPLAY_TERM},
        "write_back": True,
        "max_steps": 25,
        "timeout": 300,
    }
    started = time.perf_counter()
    response = await client.post("/api/workflows/from-task", json=payload, timeout=420)
    wall = round((time.perf_counter() - started) * 1000, 1)
    body = response.json()
    body["_http_status"] = response.status_code
    body["_wall_ms"] = wall
    return body


async def _replay(client, *, workflow_id: str, cdp: str, term: str) -> dict:
    started = time.perf_counter()
    response = await client.post(
        f"/api/workflows/{workflow_id}/run",
        json={
            "cdp_url": cdp,
            "variables": {"term": term},
            "wait": True,
            "write_back": False,
        },
        timeout=180,
    )
    wall = round((time.perf_counter() - started) * 1000, 1)
    body = response.json()
    body["_http_status"] = response.status_code
    body["_wall_ms"] = wall
    return body


async def main() -> int:
    key = ls._key()
    report: dict = {
        "started_at_ist": _now(),
        "model": MODEL,
        "create_term": CREATE_TERM,
        "replay_terms": list(REPLAY_TERMS),
        "key_present": bool(key),
        "key_length": len(key),
        "endpoint": "POST /api/workflows/from-task",
    }
    if not key:
        report["error"] = "OPENROUTER_API_KEY is not set"
        REPORT.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"event": "skipped", "reason": "no key"}, default=str), flush=True)
        return 0
    if not ls.CHROME.exists():
        report["error"] = "google-chrome is not installed"
        REPORT.write_text(json.dumps(report, indent=2) + "\n")
        return 1

    from httpx import ASGITransport, AsyncClient

    from browser_platform.app import create_app
    from browser_platform.config import Settings

    data_dir = ROOT / "data" / "live_from_task"
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
            job = await _post_from_task(client, cdp=cdp, key=key)
            creation = job.get("creation") or {}
            replay = job.get("replay") or {}
            tokens_in = int(creation.get("tokens_in") or 0)
            tokens_out = int(creation.get("tokens_out") or 0)
            cost = _estimate_cost(tokens_in, tokens_out, creation.get("cost_usd"))
            report["from_task"] = {
                "http_status": job.get("_http_status"),
                "job_id": job.get("id"),
                "status": job.get("status"),
                "error": job.get("error"),
                "workflow_id": job.get("workflow_id"),
                "variables": job.get("variables"),
                "suggestions": job.get("suggestions"),
                "creation": {
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "cost_usd": round(cost, 8),
                    "latency_ms": creation.get("latency_ms"),
                    "wall_ms": job.get("_wall_ms"),
                    "steps": creation.get("steps"),
                    "steps_before": creation.get("steps_before"),
                    "steps_after": creation.get("steps_after"),
                    "compaction": creation.get("compaction"),
                },
                "validation_replay": {
                    "status": replay.get("status"),
                    "error": replay.get("error"),
                    "tokens_in": replay.get("tokens_in"),
                    "tokens_out": replay.get("tokens_out"),
                    "latency_ms": replay.get("latency_ms"),
                    "outputs_preview": str((replay.get("outputs") or {}).get("extract") or "")[:400],
                },
                "definition_steps": [
                    {"type": s.get("type"), "value": s.get("value"), "url": s.get("url"), "has_strategies": bool(s.get("selectorStrategies"))}
                    for s in ((job.get("definition") or {}).get("steps") or [])
                ],
            }
            workflow_id = job.get("workflow_id")
            # Built-in validation replay already used REPLAY_TERMS[0] via replay_variables.
            # Run an additional fresh-tab 0-token replay for each requested term.
            report["replays"] = []
            if workflow_id:
                for term in REPLAY_TERMS:
                    if term == REPLAY_TERM and (replay.get("status") == "succeeded") and int(replay.get("tokens_in") or 0) == 0 and int(replay.get("tokens_out") or 0) == 0:
                        report["replays"].append({
                            "term": term,
                            "source": "validation_replay",
                            "http_status": job.get("_http_status"),
                            "run_id": replay.get("run_id"),
                            "status": replay.get("status"),
                            "error": replay.get("error"),
                            "latency_ms": replay.get("latency_ms"),
                            "tokens_in": int(replay.get("tokens_in") or 0),
                            "tokens_out": int(replay.get("tokens_out") or 0),
                            "outputs_preview": str((replay.get("outputs") or {}).get("extract") or "")[:400],
                            "steps": replay.get("steps") or [],
                        })
                        continue
                    run = await _replay(client, workflow_id=workflow_id, cdp=cdp, term=term)
                    outputs = run.get("outputs") or {}
                    tokens_in_r = sum(int((s or {}).get("tokens_in") or 0) for s in (run.get("steps") or []))
                    tokens_out_r = sum(int((s or {}).get("tokens_out") or 0) for s in (run.get("steps") or []))
                    if run.get("tokens_in") is not None:
                        tokens_in_r = int(run.get("tokens_in") or tokens_in_r)
                    if run.get("tokens_out") is not None:
                        tokens_out_r = int(run.get("tokens_out") or tokens_out_r)
                    report["replays"].append({
                        "term": term,
                        "source": "fresh_tab_run",
                        "http_status": run.get("_http_status"),
                        "run_id": run.get("id"),
                        "status": run.get("status"),
                        "error": run.get("error"),
                        "latency_ms": run.get("latency_ms") or run.get("_wall_ms"),
                        "wall_ms": run.get("_wall_ms"),
                        "tokens_in": tokens_in_r,
                        "tokens_out": tokens_out_r,
                        "outputs_preview": str(outputs.get("extract") or outputs)[:400],
                        "steps": [
                            {
                                "type": s.get("step_type") or s.get("type"),
                                "tier": s.get("tier"),
                                "success": s.get("success"),
                                "latency_ms": s.get("latency_ms"),
                            }
                            for s in (run.get("steps") or [])
                        ],
                    })

        spend = _load_spend()
        spend["round_cap_usd"] = ROUND_CAP_USD
        spend["prior_spend_usd"] = PRIOR_SPEND_USD
        spend["items"] = [item for item in spend.get("items") or [] if item.get("name") != "from_task_wikipedia"]
        spend["items"].append({"name": "from_task_wikipedia", "cost_usd": round(cost, 8), "at": _now()})
        spend["from_task_spend_usd"] = round(cost, 8)
        spend["total_spend_usd"] = round(PRIOR_SPEND_USD + sum(float(i.get("cost_usd") or 0) for i in spend["items"]), 6)
        spend["remaining_usd"] = round(ROUND_CAP_USD - spend["total_spend_usd"], 6)
        report["spend"] = {
            "round_cap_usd": ROUND_CAP_USD,
            "prior_mint_spend_usd": PRIOR_SPEND_USD,
            "from_task_cost_usd": round(cost, 8),
            "total_spend_usd": spend["total_spend_usd"],
            "remaining_usd": spend["remaining_usd"],
        }
        report["finished_at_ist"] = _now()
        ls._assert_no_key(report, key)
        REPORT.write_text(json.dumps(report, indent=2) + "\n")
        _save_spend(spend, key)
        print(
            json.dumps(
                {
                    "event": "from_task_finished",
                    "status": job.get("status"),
                    "creation": report["from_task"]["creation"],
                    "validation_replay": {
                        k: report["from_task"]["validation_replay"].get(k)
                        for k in ("status", "tokens_in", "tokens_out", "latency_ms")
                    },
                    "replays": [
                        {k: r.get(k) for k in ("term", "status", "tokens_in", "tokens_out", "latency_ms")}
                        for r in (report.get("replays") or [])
                    ],
                    "spend": report["spend"],
                    "error": job.get("error"),
                    "report": str(REPORT),
                },
                default=str,
            ),
            flush=True,
        )
        replays_ok = bool(report.get("replays")) and all(
            r.get("status") == "succeeded" and int(r.get("tokens_in") or 0) == 0 and int(r.get("tokens_out") or 0) == 0
            for r in report.get("replays") or []
        )
        return 0 if job.get("status") == "succeeded" and replays_ok else 2
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

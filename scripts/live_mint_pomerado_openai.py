#!/usr/bin/env python3
"""Live official Pomerado mint (OpenAI, stock MCP) via the running API, then tier replay.

Never prints OPENAI_API_KEY or the access token.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

REPORT = ROOT / "data" / "live_mint_pomerado_openai_report.json"
IST = ZoneInfo("Asia/Jerusalem")
BASE = os.environ.get("BROWSER_PLATFORM_URL") or "http://127.0.0.1:8745"
WAIT_S = float(os.environ.get("POMERADO_LIVE_WAIT_S") or 1200)
POLL = float(os.environ.get("POMERADO_LIVE_POLL_S") or 5)


def _now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _assert_no_secrets(obj, *secrets: str) -> None:
    blob = json.dumps(obj, default=str)
    for secret in secrets:
        if secret:
            assert secret not in blob, "secret leaked into report or log payload"


def _settings():
    from browser_platform.config import load_settings

    return load_settings(ROOT)


def _headers(settings) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = settings.access_token or os.environ.get("BROWSER_PLATFORM_ACCESS_TOKEN") or ""
    if token:
        headers["X-Browser-Platform-Token"] = token
    return headers


def _request(method: str, path: str, settings, body=None, timeout: float = 60):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=_headers(settings))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"detail": raw[:500]}
        return exc.code, parsed


def _brief(job: dict) -> dict:
    output = job.get("output") or {}
    usage = job.get("usage") or {}
    return {
        "status": job.get("status"),
        "ok": job.get("ok"),
        "error": job.get("error"),
        "directory": job.get("directory") or (output.get("integration") or {}).get("directory"),
        "build": output.get("build"),
        "summary": output.get("summary"),
        "stopReason": output.get("stopReason") or output.get("stop_reason"),
        "hostFailure": output.get("hostFailure"),
        "publicationDenial": output.get("publicationDenial"),
        "workflow": job.get("workflow"),
        "usage": usage,
        "guardian": output.get("publicationDenial") or output.get("capability_review"),
    }


def main() -> int:
    from browser_platform.pomerado.keys import looks_like_live_key

    settings = _settings()
    key = os.environ.get("OPENAI_API_KEY") or ""
    token = settings.access_token or os.environ.get("BROWSER_PLATFORM_ACCESS_TOKEN") or ""
    report = {
        "started_at_ist": _now(),
        "provider": "openai",
        "stock_pomerado": True,
        "models": {"minter": "gpt-6.1-sol", "guardian": "gpt-6-luna"},
        "key_present": looks_like_live_key(key),
        "key_length": len(key),
        "budget_usd": 1.0,
        "spend_usd": 0,
        "base_url": BASE,
        "published": False,
    }
    _assert_no_secrets(report, key, token)

    status, health = _request("GET", "/health", settings)
    report["health"] = {"http": status, "body": health}
    status, caps = _request("GET", "/api/pomerado/capabilities", settings)
    report["capabilities"] = caps if status == 200 else {"http": status, "body": caps}

    if not looks_like_live_key(key):
        report["error"] = "OPENAI_API_KEY is not set on this process"
        report["finished_at_ist"] = _now()
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=2, default=str) + "\n")
        print(json.dumps({"report": str(REPORT), "error": report["error"], "key_present": False}))
        return 1

    intent = (
        "Read-only: on English Wikipedia, search for the term 'browser automation', "
        "open the first search result, and return the first paragraph of the article. "
        "Do not edit, log in, or change any settings."
    )
    payload = {
        "name": "wikipedia_search_reader",
        "url": "https://en.wikipedia.org/wiki/Special:Search",
        "intent": intent,
        "effect": "read",
        "input": {"term": "browser automation"},
        "wait_s": 0,
    }
    t0 = time.perf_counter()
    status, body = _request("POST", "/api/pomerado/mint", settings, payload, timeout=180)
    job = body.get("result") if isinstance(body, dict) else None
    if status != 200 or not isinstance(job, dict) or not job.get("job_id"):
        report["mint"] = {"http": status, "body": body, "error": "mint did not return a job_id"}
        report["finished_at_ist"] = _now()
        _assert_no_secrets(report, key, token)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=2, default=str) + "\n")
        print(json.dumps({"report": str(REPORT), "mint": report["mint"]}, default=str))
        return 1

    job_id = job["job_id"]
    report["job_id"] = job_id
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        st, polled = _request("GET", f"/api/pomerado/jobs/{job_id}", settings, timeout=45)
        if st == 200 and isinstance(polled, dict):
            job = polled
            status_name = job.get("status")
            if status_name and status_name not in {"running", "queued", "needs_input", "input_required"}:
                break
        time.sleep(POLL)
    mint_s = time.perf_counter() - t0
    report["mint_time_s"] = round(mint_s, 2)
    report["mint"] = _brief(job)
    usage = job.get("usage") or {}
    report["spend_usd"] = usage.get("cost_usd") or 0
    report["usage"] = usage
    published = job.get("ok") is True or (job.get("output") or {}).get("build") == "published"
    report["published"] = bool(published)
    report["guardian"] = (job.get("output") or {}).get("publicationDenial") or (job.get("output") or {}).get("capability_review")

    workflow = (job.get("workflow") or {})
    if not workflow.get("id") and published:
        st, listing = _request("GET", "/api/workflows", settings)
        report["workflows_list_http"] = st
        if st == 200:
            directory = report["mint"].get("directory")
            for item in listing.get("workflows") or []:
                # fetch if needed later
                workflow.setdefault("candidates", []).append({"id": item.get("id"), "name": item.get("name")})
            # pick by name
            for item in listing.get("workflows") or []:
                if item.get("name") == "wikipedia_search_reader":
                    workflow = {"id": item["id"], "name": item["name"], "created": None}
                    break
    report["workflow"] = workflow

    if workflow.get("id"):
        t1 = time.perf_counter()
        st, run_body = _request(
            "POST",
            f"/api/workflows/{workflow['id']}/run",
            settings,
            {"cdp_url": "http://127.0.0.1:9", "wait": True, "variables": {"term": "browser automation"}},
            timeout=240,
        )
        replay_s = time.perf_counter() - t1
        report["replay_http"] = st
        report["replay_latency_ms"] = round(replay_s * 1000, 1)
        run = run_body if st == 200 else {"http": st, "body": run_body}
        if st == 200 and "id" in run_body:
            st2, run = _request("GET", f"/api/runs/{run_body['id']}", settings)
            report["replay_http"] = st2
        steps = run.get("steps") or []
        tokens_in = sum(int(s.get("tokens_in") or 0) for s in steps)
        tokens_out = sum(int(s.get("tokens_out") or 0) for s in steps)
        tiers = [s.get("tier") for s in steps]
        report["replay"] = {
            "status": run.get("status"),
            "error": run.get("error"),
            "tiers": tiers,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "steps": [
                {
                    "type": s.get("step_type") or s.get("type"),
                    "tier": s.get("tier"),
                    "success": s.get("success"),
                    "latency_ms": s.get("latency_ms"),
                    "tokens_in": s.get("tokens_in") or 0,
                    "tokens_out": s.get("tokens_out") or 0,
                }
                for s in steps
            ],
            "outputs_preview": run.get("outputs"),
        }
        report["replay_tier"] = "pomerado" if "pomerado" in tiers and run.get("status") == "succeeded" else (tiers[0] if tiers else None)
        report["replay_zero_tokens"] = tokens_in == 0 and tokens_out == 0

    report["finished_at_ist"] = _now()
    # Spend file if the proxy wrote one
    spend_path = ROOT / "data" / "pomerado_openai_spend.json"
    if spend_path.is_file():
        try:
            spend = json.loads(spend_path.read_text())
            report["spend_usd"] = spend.get("cost_usd") or report["spend_usd"]
            report["usage"] = spend
        except Exception:
            pass
    _assert_no_secrets(report, key, token)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(
        json.dumps(
            {
                "report": str(REPORT),
                "published": report.get("published"),
                "model": report.get("models"),
                "cost_usd": report.get("spend_usd"),
                "mint_time_s": report.get("mint_time_s"),
                "replay_latency_ms": report.get("replay_latency_ms"),
                "replay_tier": report.get("replay_tier"),
                "replay_zero_tokens": report.get("replay_zero_tokens"),
                "workflow": report.get("workflow"),
                "guardian": report.get("guardian"),
            },
            default=str,
        )
    )
    return 0 if report.get("published") else 2


if __name__ == "__main__":
    raise SystemExit(main())

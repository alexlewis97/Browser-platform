"""Live e2e for chat tools, batch synthesis, and input/output validation.

Public sites only. Never prints access tokens or API keys.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

BACKEND = os.environ.get("BROWSER_PLATFORM_BACKEND", "https://browser.129-159-156-75.sslip.io")
SITE = os.environ.get("BROWSER_PLATFORM_SITE", "https://browser-platform-web.vercel.app")
REPORT = Path("/workspace/browser-platform/data/live_chat_e2e_report.json")
IST = timezone(timedelta(hours=2))


def token() -> str:
    value = os.environ.get("BROWSER_PLATFORM_USER_TOKEN") or os.environ.get("BROWSER_PLATFORM_ACCESS_TOKEN") or ""
    if not value:
        env = Path("/workspace/browser-platform/.env")
        if env.exists():
            for line in env.read_text().splitlines():
                key, _, raw = line.partition("=")
                if key in {"BROWSER_PLATFORM_USER_TOKEN", "BROWSER_PLATFORM_ACCESS_TOKEN"}:
                    value = raw.strip().strip('"')
                    break
    if not value:
        raise SystemExit("access token is not set")
    return value


def now_label() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def headers() -> dict[str, str]:
    return {"X-Browser-Platform-Token": token(), "Content-Type": "application/json"}


def usage_of(payload: dict[str, Any] | None) -> dict[str, Any]:
    u = (payload or {}).get("usage") or {}
    return {
        "tokens_in": int(u.get("tokens_in") or 0),
        "tokens_out": int(u.get("tokens_out") or 0),
        "cost_usd": u.get("cost_usd"),
        "used": bool(u.get("used")),
    }


def main() -> None:
    report: dict[str, Any] = {
        "started": now_label(),
        "backend": BACKEND,
        "site": SITE,
        "cases": {},
    }
    timeout = httpx.Timeout(300.0, connect=30.0)
    with httpx.Client(timeout=timeout, headers=headers(), follow_redirects=True) as client:
        health = client.get(f"{BACKEND}/health")
        report["health"] = {"status": health.status_code, "body": health.json() if health.headers.get("content-type", "").startswith("application/json") else health.status_code}
        if health.status_code != 200:
            raise SystemExit(f"backend health failed: {health.status_code}")

        tools = client.get(f"{BACKEND}/api/tools")
        tools.raise_for_status()
        catalog = tools.json().get("tools") or []
        report["tools"] = {
            "count": len(catalog),
            "names": [((t.get("function") or {}).get("name"), t.get("workflow_name")) for t in catalog],
        }

        # Deliberately bad creation-time inputs
        t0 = time.perf_counter()
        bad = client.post(
            f"{BACKEND}/api/workflows/validate-inputs",
            json={
                "task": "Find fixtures for a football team",
                "use_llm": True,
                "definition": {
                    "name": "bad_fixtures",
                    "version": "1",
                    "input_schema": [{"name": "color", "type": "string", "required": True, "default": "blue"}],
                    "steps": [
                        {"type": "navigation", "url": "https://www.bbc.com/sport/football/teams/Arsenal/scores-fixtures"},
                        {"type": "extract", "extractionGoal": "result", "output": "extract"},
                    ],
                },
            },
        )
        bad.raise_for_status()
        body = bad.json()
        report["cases"]["validate_inputs_bad"] = {
            "ok": bool(body.get("blocks_publish")) and int(body.get("error_count") or 0) >= 1,
            "blocks_publish": body.get("blocks_publish"),
            "error_count": body.get("error_count"),
            "codes": [f.get("code") for f in body.get("findings") or []],
            "usage": usage_of(body),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }

        # Output does not match (Arsenal text, Chelsea input)
        t0 = time.perf_counter()
        mismatch = client.post(
            f"{BACKEND}/api/runs/validate-output",
            json={
                "request": "upcoming fixtures for Chelsea",
                "inputs": {"team": "Chelsea"},
                "sample": "Arsenal fixtures November 2026. Next: Arsenal vs Liverpool, Emirates Stadium.",
                "url": "https://www.bbc.com/sport/football/teams/arsenal/scores-fixtures/2026-11?filter=fixtures",
                "title": "Arsenal scores & fixtures",
                "use_llm": True,
            },
        )
        mismatch.raise_for_status()
        mbody = mismatch.json()
        report["cases"]["validate_output_mismatch"] = {
            "ok": mbody.get("verdict") == "wrong",
            "verdict": mbody.get("verdict"),
            "reason": mbody.get("reason"),
            "source": mbody.get("source"),
            "usage": usage_of(mbody),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }

        def chat(message: str, name: str) -> dict[str, Any]:
            t0 = time.perf_counter()
            res = client.post(
                f"{BACKEND}/api/chat",
                json={"message": message, "target": "cloud", "stream": False},
            )
            elapsed = round((time.perf_counter() - t0) * 1000, 1)
            res.raise_for_status()
            job = res.json()
            return {
                "ok": job.get("status") == "succeeded" and bool(job.get("answer")),
                "status": job.get("status"),
                "answer": job.get("answer"),
                "question": job.get("question"),
                "confirm_reason": job.get("confirm_reason"),
                "calls": [
                    {"tool": c.get("tool") or c.get("workflow_name"), "inputs": c.get("arguments")}
                    for c in job.get("calls") or []
                ],
                "items": [
                    {
                        "workflow": i.get("workflow_name"),
                        "inputs": i.get("inputs"),
                        "status": i.get("status"),
                        "validation": (i.get("validation") or {}).get("verdict"),
                        "reason": (i.get("validation") or {}).get("reason"),
                        "error": i.get("error"),
                    }
                    for i in job.get("items") or []
                ],
                "flagged": job.get("flagged") or [],
                "usage": usage_of(job),
                "elapsed_ms": job.get("elapsed_ms") or elapsed,
                "error": job.get("error"),
            }

        report["cases"]["single"] = chat(
            "What are Arsenal's upcoming football fixtures? Use the football fixtures workflow with team Arsenal.",
            "single",
        )
        report["cases"]["fanout_compare"] = chat(
            "Which of Arsenal, Chelsea and Spurs plays first? Give a direct answer and a compact combined table.",
            "fanout_compare",
        )


        single = report["cases"]["single"]
        single_ok = (
            single.get("status") == "succeeded"
            and bool(single.get("answer"))
            and any((i.get("inputs") or {}).get("team") in {"Arsenal", "arsenal"} and i.get("status") == "succeeded" for i in single.get("items") or [])
        )
        single["ok"] = bool(single_ok)
        fan = report["cases"]["fanout_compare"]
        teams = {(i.get("inputs") or {}).get("team") for i in fan.get("items") or []}
        fan["ok"] = bool(
            fan.get("status") == "succeeded"
            and fan.get("answer")
            and {"Arsenal", "Chelsea", "Spurs"} <= teams
        )

        # Website Chat tab is present after deploy; checked separately if SITE is reachable.
        try:
            site = client.get(SITE)
            html = site.text
            report["site"] = {
                "status": site.status_code,
                "has_chat_bundle": "Chat" in html or "chat" in html,
            }
        except Exception as exc:
            report["site"] = {"error": str(exc)}

    report["finished"] = now_label()
    # Totals
    totals = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "elapsed_ms": 0.0}
    for case in report["cases"].values():
        u = case.get("usage") or {}
        totals["tokens_in"] += int(u.get("tokens_in") or 0)
        totals["tokens_out"] += int(u.get("tokens_out") or 0)
        if u.get("cost_usd"):
            try:
                totals["cost_usd"] += float(u["cost_usd"])
            except (TypeError, ValueError):
                pass
        totals["elapsed_ms"] += float(case.get("elapsed_ms") or 0)
    totals["cost_usd"] = round(totals["cost_usd"], 8)
    report["totals"] = totals
    report["passed"] = all(case.get("ok") for case in report["cases"].values())
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    # Print a secret-free summary
    print(f"started {report['started']}")
    print(f"tools {report['tools']['count']}")
    for name, case in report["cases"].items():
        u = case.get("usage") or {}
        print(
            f"{name}: ok={case.get('ok')} status={case.get('status') or case.get('verdict')} "
            f"tokens={u.get('tokens_in')}/{u.get('tokens_out')} cost={u.get('cost_usd')} "
            f"elapsed_ms={case.get('elapsed_ms')}"
        )
        if case.get("answer"):
            print("--- answer ---")
            print(case["answer"][:1200])
            print("--------------")
        if case.get("error"):
            print("error:", case["error"])
        if name == "validate_inputs_bad":
            print("codes:", case.get("codes"), "blocks_publish:", case.get("blocks_publish"))
        if name == "validate_output_mismatch":
            print("verdict:", case.get("verdict"), "reason:", case.get("reason"))
        if case.get("calls"):
            print("calls:", case["calls"])
        if case.get("flagged"):
            print("flagged:", case["flagged"])
    print("totals", totals)
    print("passed", report["passed"])
    print("finished", report["finished"])
    print("report", str(REPORT))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

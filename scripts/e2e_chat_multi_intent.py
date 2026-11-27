"""Multi-intent chat e2e against the BBC Sport skills (fixtures, results, table, top
scorers, cricket, rugby, F1). Checks that every intent in the request became a call to the
right workflow with the right arguments, and that the job finished cleanly.

Creates no workflows. Never prints tokens or keys.

  BROWSER_PLATFORM_BACKEND=http://127.0.0.1:8899 python scripts/e2e_chat_multi_intent.py --out report.json
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import httpx

BACKEND = os.environ.get("BROWSER_PLATFORM_BACKEND", "https://browser.129-159-156-75.sslip.io")

# Workflow-id prefixes of the BBC library (27 Nov 2026).
FIXTURES, RESULTS, TABLE, SCORERS, CRICKET, RUGBY, F1 = "9f27bcc8", "fec9514f", "d79e4ca1", "424b5b4f", "fd896b70", "ca3a55eb", "62818d14"

CASES = [
    ("results_and_fixtures", "Arsenal's results and next fixtures", [(RESULTS, {"team": "arsenal"}), (FIXTURES, {"team": "arsenal"})], {}),
    ("compare_scorers", "compare top scorers in the Premier League and La Liga", [(SCORERS, {"competition": "premier"}), (SCORERS, {"competition": "liga"})], {}),
    ("cricket_and_rugby", "England cricket and Six Nations scores", [(CRICKET, {"team": "england"}), (RUGBY, {"competition": "six"})], {}),
    (
        "two_teams_results_and_table",
        "How did Arsenal and Chelsea get on in their recent games, and where are they in the Premier League table?",
        [(RESULTS, {"team": "arsenal"}), (RESULTS, {"team": "chelsea"}), (TABLE, {"competition": "premier"})],
        {},
    ),
    ("f1_and_fixtures", "Who won the 2025 Formula 1 races, and what are Liverpool's next fixtures?", [(F1, {"year": "2025"}), (FIXTURES, {"team": "liverpool"})], {}),
    ("scoped_month", "Arsenal results in January 2026 and England cricket", [(RESULTS, {"team": "arsenal", "month": "2026-01"}), (CRICKET, {"team": "england"})], {CRICKET: {"month": "2026-01"}}),
]


def token() -> str:
    value = os.environ.get("BROWSER_PLATFORM_USER_TOKEN") or os.environ.get("BROWSER_PLATFORM_ACCESS_TOKEN") or ""
    if not value:
        raise SystemExit("access token is not set")
    return value


def matches(item_args: dict, want: dict) -> bool:
    for key, needle in want.items():
        if needle.lower() not in str(item_args.get(key) or "").lower():
            return False
    return True


def judge(job: dict, expected: list, forbidden: dict) -> dict:
    calls = [{"wf": (c.get("workflow_id") or "")[:8], "tool": c.get("tool"), "args": c.get("arguments") or {}} for c in job.get("calls") or []]
    items = job.get("items") or []
    missing = []
    pool = list(calls)
    for wf, want in expected:
        hit = next((c for c in pool if c["wf"] == wf and matches(c["args"], want)), None)
        if hit is None:
            missing.append({"workflow": wf, "args": want})
        else:
            pool.remove(hit)
    leaked = [c for c in calls if c["wf"] in forbidden and matches(c["args"], forbidden[c["wf"]])]
    failed = [i.get("workflow_name") for i in items if i.get("status") != "succeeded"]
    ok = not missing and not leaked and job.get("status") == "succeeded" and not failed
    return {"ok": ok, "missing_calls": missing, "leaked_args": leaked, "failed_items": failed, "calls": calls}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/live_chat_multi_intent_report.json")
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    report = {"backend": BACKEND, "label": args.label, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "cases": {}}
    with httpx.Client(timeout=httpx.Timeout(400, connect=30), headers={"X-Browser-Platform-Token": token()}) as client:
        for name, message, expected, forbidden in CASES:
            t0 = time.perf_counter()
            try:
                res = client.post(f"{BACKEND}/api/chat", json={"message": message, "target": "cloud", "stream": False, "confirm": True})
                res.raise_for_status()
                job = res.json()
            except Exception as exc:
                job = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            verdict = judge(job, expected, forbidden)
            usage = job.get("usage") or {}
            report["cases"][name] = {
                "message": message,
                **verdict,
                "status": job.get("status"),
                "uncovered": job.get("uncovered"),
                "skills_considered": job.get("skills_considered"),
                "tokens_in": usage.get("tokens_in"),
                "tokens_out": usage.get("tokens_out"),
                "token_breakdown": {k: (v or {}).get("tokens_in") for k, v in (job.get("token_breakdown") or {}).items()},
                "answer": (job.get("answer") or "")[:600],
                "elapsed_ms": round((time.perf_counter() - t0) * 1000),
            }
            print(f"{name}: ok={verdict['ok']} status={job.get('status')} missing={verdict['missing_calls']} leaked={len(verdict['leaked_args'])} tokens_in={usage.get('tokens_in')}", flush=True)
    cases = report["cases"].values()
    report["passed"] = sum(1 for c in cases if c["ok"])
    report["total"] = len(report["cases"])
    report["tokens_in"] = sum(int(c.get("tokens_in") or 0) for c in cases)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print("passed", report["passed"], "/", report["total"], "tokens_in", report["tokens_in"])


if __name__ == "__main__":
    main()

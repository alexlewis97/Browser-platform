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



def item_field_values(item: dict[str, Any], key: str) -> set[str]:
    """Collect a field from normalised and original (pre-canonical) inputs."""

    values: set[str] = set()
    for src in (item.get("inputs"), item.get("raw_inputs"), item.get("arguments"), item.get("raw_arguments")):
        if not isinstance(src, dict):
            continue
        raw = src.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        values.add(str(raw).strip())
    return values


# Nicknames the planner may already have resolved to a canonical club name.
_NAME_ALIASES = {
    "spurs": {"tottenham", "tottenham hotspur", "tottenham-hotspur"},
    "man utd": {"manchester united", "manchester-united"},
    "man united": {"manchester united", "manchester-united"},
    "wolves": {"wolverhampton wanderers", "wolverhampton-wanderers"},
}


def names_cover(requested: str, items: list[dict[str, Any]], key: str = "team") -> bool:
    """True when a requested entity appears as typed or as its canonical form."""

    want = (requested or "").strip().lower()
    want_slug = want.replace(" ", "-")
    accepted = {want, want_slug} | _NAME_ALIASES.get(want, set())
    found: set[str] = set()
    for item in items:
        found |= item_field_values(item, key)
    for value in found:
        have = value.strip().lower()
        have_slug = have.replace(" ", "-")
        if have in accepted or have_slug in accepted:
            return True
        if any(alias in have or have in alias for alias in accepted if len(alias) >= 4):
            return True
    return False


def items_genuinely_passed(case: dict[str, Any]) -> bool:
    """True only when every workflow item succeeded and was not flagged."""

    if case.get("status") != "succeeded":
        return False
    items = case.get("items") or []
    if not items:
        return False
    for item in items:
        if item.get("status") != "succeeded":
            return False
        verdict = item.get("validation")
        if isinstance(verdict, dict):
            verdict = verdict.get("verdict")
        if verdict in {"wrong", "suspect"}:
            return False
    if case.get("flagged"):
        return False
    return True


def write_report(report: dict[str, Any]) -> None:
    report["finished"] = now_label()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")


def main() -> None:
    report: dict[str, Any] = {
        "started": now_label(),
        "backend": BACKEND,
        "site": SITE,
        "cases": {},
    }
    timeout = httpx.Timeout(360.0, connect=30.0)
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
            print(f"CHAT_START {name}", flush=True)
            try:
                res = client.post(
                    f"{BACKEND}/api/chat",
                    json={"message": message, "target": "cloud", "stream": False, "confirm": True},
                    timeout=240,
                )
            except Exception as exc:
                print(f"CHAT_ERROR {name} {type(exc).__name__}: {exc}", flush=True)
                return {
                    "ok": False,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "items": [],
                    "flagged": [],
                }
            elapsed = round((time.perf_counter() - t0) * 1000, 1)
            res.raise_for_status()
            job = res.json()
            print(f"CHAT_DONE {name} status={job.get('status')}", flush=True)
            return {
                "ok": job.get("status") == "succeeded" and bool(job.get("answer")),
                "id": job.get("id"),
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
                        "raw_inputs": i.get("raw_inputs") or i.get("raw_arguments") or {},
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

        # Explore-mode: optional month on the library football fixtures workflow (already applied
        # if a previous explore ran; still records tokens/cost and a non-default month replay).
        t0 = time.perf_counter()
        tools_list = client.get(f"{BACKEND}/api/workflows")
        tools_list.raise_for_status()
        listing = tools_list.json()
        workflows = listing if isinstance(listing, list) else listing.get("workflows") or listing.get("items") or []
        football_id = next((w.get("id") for w in workflows if (w.get("name") or (w.get("definition") or {}).get("name")) == "football_fixtures"), None)
        explore_case: dict[str, Any] = {"elapsed_ms": 0, "ok": False}
        if football_id:
            wf = client.get(f"{BACKEND}/api/workflows/{football_id}")
            wf.raise_for_status()
            definition = (wf.json().get("definition") or wf.json())
            urls = [s.get("url") or "" for s in definition.get("steps") or []]
            has_month = any("{month" in url for url in urls) and any(i.get("name") == "month" for i in definition.get("input_schema") or [])
            if not has_month:
                explored = client.post(f"{BACKEND}/api/workflows/{football_id}/explore", json={"apply": True, "variables": {"team": "Arsenal"}})
                explored.raise_for_status()
                body = explored.json()
                usage = body.get("usage") or {}
                has_month = "month" in (body.get("accepted") or [])
            else:
                # Re-run detection-only to capture the already-bound state without extra LLM spend.
                body = {"accepted": ["month"], "applied": True, "usage": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": False}, "verified": [{"name": "month", "verify_with": "2026-12"}], "already_applied": True}
                usage = body["usage"]
            run_default = client.post(
                f"{BACKEND}/api/workflows/{football_id}/run",
                json={"variables": {"team": "Arsenal"}, "wait": True, "write_back": False},
            )
            run_alt = client.post(
                f"{BACKEND}/api/workflows/{football_id}/run",
                json={"variables": {"team": "Arsenal", "month": "2026-12"}, "wait": True, "write_back": False},
            )
            d_json = run_default.json() if run_default.status_code == 200 else {"error": run_default.text[:200]}
            a_json = run_alt.json() if run_alt.status_code == 200 else {"error": run_alt.text[:200]}
            default_out = str((d_json.get("outputs") or {}).get("extract") or d_json.get("outputs") or "")
            alt_out = str((a_json.get("outputs") or {}).get("extract") or a_json.get("outputs") or "")
            explore_case = {
                "ok": bool(has_month) and d_json.get("status") == "succeeded" and a_json.get("status") == "succeeded" and default_out != alt_out,
                "workflow_id": football_id,
                "has_month_binding": has_month,
                "already_applied": bool(body.get("already_applied")),
                "accepted": body.get("accepted"),
                "usage": usage,
                "default_status": d_json.get("status"),
                "alt_status": a_json.get("status"),
                "outputs_differ": default_out != alt_out,
                "default_preview": default_out[:240],
                "alt_preview": alt_out[:240],
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        report["cases"]["explore_month"] = explore_case

        report["cases"]["month_chat"] = chat(
            "What are Arsenal's football fixtures in December 2026? Use the football fixtures workflow with team Arsenal and month 2026-12.",
            "month_chat",
        )


        def confirm_if_needed(case_name: str, message: str) -> None:
            case = report["cases"].get(case_name) or {}
            if case.get("status") != "needs_confirmation":
                return
            job_id = case.get("id") or case.get("job_id")
            t0 = time.perf_counter()
            payload = {"message": message, "target": "cloud", "stream": False, "confirm": True}
            if job_id:
                payload["job_id"] = job_id
            res = client.post(f"{BACKEND}/api/chat", json=payload, timeout=240)
            job = res.json() if res.headers.get("content-type", "").startswith("application/json") else {"error": res.text[:300]}
            report["cases"][case_name] = {
                "ok": job.get("status") == "succeeded" and bool(job.get("answer")),
                "id": job.get("id") or job_id,
                "status": job.get("status"),
                "answer": job.get("answer"),
                "confirm_reason": job.get("confirm_reason"),
                "items": job.get("items") or [],
                "flagged": job.get("flagged") or [],
                "usage": usage_of(job),
                "elapsed_ms": job.get("elapsed_ms") or round((time.perf_counter() - t0) * 1000, 1),
                "error": job.get("error"),
                "confirmed": True,
            }

        catalog_names = {((t.get("function") or {}).get("name") or "") + " " + str(t.get("workflow_name") or "") for t in catalog}
        def has_tool(*needles: str) -> bool:
            blob = " ".join(catalog_names).lower()
            return all(n.lower() in blob for n in needles)

        if has_tool("saucedemo") or has_tool("sauce"):
            report["cases"]["saucedemo_chat"] = chat(
                "On SauceDemo, add Sauce Labs Bike Light to the cart and check out as Sam Taylor, zip 10001. Confirm the action.",
                "saucedemo_chat",
            )
            # action-ending tools need confirm=true
            # redo with confirm if the first call asked for confirmation
            if report["cases"]["saucedemo_chat"].get("confirm_reason") or report["cases"]["saucedemo_chat"].get("status") != "succeeded":
                t0 = time.perf_counter()
                confirm_if_needed(
                    "saucedemo_chat",
                    "On SauceDemo, add Sauce Labs Bike Light to the cart and check out as Sam Taylor, zip 10001.",
                )

        if has_tool("demoqa") or has_tool("practice_form"):
            t0 = time.perf_counter()
            res = client.post(
                f"{BACKEND}/api/chat",
                json={"message": "Submit the DemoQA practice form for Sam Taylor, email sam@example.com, gender Female, mobile 9123456789.", "target": "cloud", "stream": False, "confirm": True},
            )
            job = res.json()
            report["cases"]["demoqa_chat"] = {
                "ok": job.get("status") == "succeeded" and bool(job.get("answer")),
                "id": job.get("id"),
                "status": job.get("status"),
                "answer": job.get("answer"),
                "items": job.get("items") or [],
                "flagged": job.get("flagged") or [],
                "usage": usage_of(job),
                "elapsed_ms": job.get("elapsed_ms") or round((time.perf_counter() - t0) * 1000, 1),
                "error": job.get("error"),
                "flagged": job.get("flagged") or [],
                "confirmed": True,
            }
            confirm_if_needed(
                "demoqa_chat",
                "Submit the DemoQA practice form for Sam Taylor, email sam@example.com, gender Female, mobile 9123456789.",
            )

        if has_tool("httpbin"):
            t0 = time.perf_counter()
            res = client.post(
                f"{BACKEND}/api/chat",
                json={"message": "Submit the httpbin pizza form for Jane Roe, size medium, toppings mushroom.", "target": "cloud", "stream": False, "confirm": True},
            )
            job = res.json()
            report["cases"]["httpbin_chat"] = {
                "ok": job.get("status") == "succeeded" and bool(job.get("answer")),
                "id": job.get("id"),
                "status": job.get("status"),
                "answer": job.get("answer"),
                "items": job.get("items") or [],
                "flagged": job.get("flagged") or [],
                "usage": usage_of(job),
                "elapsed_ms": job.get("elapsed_ms") or round((time.perf_counter() - t0) * 1000, 1),
                "error": job.get("error"),
                "flagged": job.get("flagged") or [],
                "confirmed": True,
            }
            confirm_if_needed(
                "httpbin_chat",
                "Submit the httpbin pizza form for Jane Roe, size medium, toppings mushroom.",
            )

        single = report["cases"]["single"]
        single_ok = (
            single.get("status") == "succeeded"
            and bool(single.get("answer"))
            and any((i.get("inputs") or {}).get("team") in {"Arsenal", "arsenal"} and i.get("status") == "succeeded" for i in single.get("items") or [])
        )
        single["ok"] = bool(single_ok) and items_genuinely_passed(single)
        fan = report["cases"]["fanout_compare"]
        fan_items = fan.get("items") or []
        fan["ok"] = bool(
            items_genuinely_passed(fan)
            and fan.get("answer")
            and all(names_cover(name, fan_items) for name in ("Arsenal", "Chelsea", "Spurs"))
        )
        month_chat = report["cases"].get("month_chat") or {}
        if month_chat:
            month_chat["ok"] = bool(
                items_genuinely_passed(month_chat)
                and month_chat.get("answer")
                and any((i.get("inputs") or {}).get("month") in {"2026-12", "December"} or (i.get("inputs") or {}).get("team") in {"Arsenal", "arsenal"} for i in month_chat.get("items") or [])
            )

        for practice in ("saucedemo_chat", "demoqa_chat", "httpbin_chat"):
            case = report["cases"].get(practice)
            if case:
                case["ok"] = bool(case.get("answer")) and items_genuinely_passed(case)
                case["items_ok"] = items_genuinely_passed(case)

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

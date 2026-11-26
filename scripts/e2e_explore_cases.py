"""Live explore-mode cases on several public sites. Never prints tokens."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

BACKEND = os.environ.get("BROWSER_PLATFORM_BACKEND", "https://browser.129-159-156-75.sslip.io")
REPORT = Path("/workspace/browser-platform/data/live_explore_cases_report.json")
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


def preview(outputs: Any) -> str:
    if isinstance(outputs, dict):
        return str(outputs.get("extract") or outputs)[:400]
    return str(outputs or "")[:400]


def create_nav_workflow(client: httpx.Client, name: str, url: str, description: str = "") -> str:
    body = {
        "definition": {
            "name": name,
            "description": description or name,
            "version": "1",
            "input_schema": [],
            "steps": [
                {"type": "navigation", "url": url},
                {"type": "extract", "extractionGoal": "a short sample of visible catalogue results, not only the site name", "output": "extract"},
            ],
        }
    }
    res = client.post(f"{BACKEND}/api/workflows", json=body, timeout=30)
    res.raise_for_status()
    data = res.json()
    return data.get("id") or (data.get("workflow") or {}).get("id") or data.get("workflow_id")




def create_workflow(client: httpx.Client, name: str, steps: list[dict[str, Any]], description: str = "") -> str:
    body = {
        "definition": {
            "name": name,
            "description": description or name,
            "version": "1",
            "input_schema": [],
            "steps": steps,
        }
    }
    res = client.post(f"{BACKEND}/api/workflows", json=body, timeout=30)
    res.raise_for_status()
    data = res.json()
    return data.get("id") or (data.get("workflow") or {}).get("id") or data.get("workflow_id")


def css(selector: str) -> dict[str, Any]:
    return {"type": "css", "value": selector, "name": selector}


def input_step(css_sel: str, value: str, target: str) -> dict[str, Any]:
    return {
        "type": "input",
        "cssSelector": css_sel,
        "value": value,
        "target_text": target,
        "selectorStrategies": [css(css_sel)],
    }


def click_step(css_sel: str, target: str) -> dict[str, Any]:
    return {
        "type": "click",
        "target_text": target,
        "cssSelector": css_sel,
        "selectorStrategies": [css(css_sel)],
    }


def browser_verify_ok(body: dict[str, Any]) -> bool:
    """Pass if a select/tab/radio proposal was accepted or rejected after a real browser replay."""
    if not isinstance(body, dict):
        return False
    verified = body.get("verified") or []
    dropped = body.get("dropped") or []
    proposals = body.get("proposals") or verified or dropped
    if not proposals and not verified and not dropped:
        return False
    interesting = []
    for item in list(verified) + list(dropped) + list(proposals):
        if not isinstance(item, dict):
            continue
        kind = ((item.get("binding") or {}).get("kind") or "")
        name = str(item.get("name") or "")
        if item.get("verify_mode") == "browser" or kind in {"select", "select_change", "tab", "radio", "click"} or name in {"sort", "product_sort", "product_sort_container", "choice", "like", "competition", "tab"}:
            interesting.append(item)
    if not interesting:
        interesting = list(verified) + list(dropped)
    if not interesting:
        return False
    # A real browser decision, not the old HTTP short-circuit.
    for item in interesting:
        reason = str(item.get("dropped_reason") or "")
        if item.get("verified") or item.get("name") in (body.get("accepted") or []):
            return True
        if reason and reason != "needs_browser_verify":
            return True
    return False

def run_wf(client: httpx.Client, workflow_id: str, variables: dict[str, str], timeout_s: int = 180) -> dict[str, Any]:
    res = client.post(
        f"{BACKEND}/api/workflows/{workflow_id}/run",
        json={"variables": variables, "wait": False, "write_back": False},
        timeout=60,
    )
    data = res.json() if res.headers.get("content-type", "").startswith("application/json") else {"error": res.text[:300]}
    run_id = data.get("id")
    deadline = time.time() + timeout_s
    while time.time() < deadline and data.get("status") not in {"succeeded", "failed", "cancelled"} and run_id:
        time.sleep(2)
        got = client.get(f"{BACKEND}/api/runs/{run_id}", timeout=30)
        data = got.json() if got.headers.get("content-type", "").startswith("application/json") else data
    outputs = data.get("outputs") if isinstance(data.get("outputs"), dict) else {}
    return {
        "http_status": res.status_code,
        "status": data.get("status"),
        "error": data.get("error"),
        "outputs_preview": preview(data.get("outputs")),
        "url": outputs.get("url") or data.get("url"),
    }


def main() -> int:
    report: dict[str, Any] = {"started": now_label(), "backend": BACKEND, "cases": {}}
    with httpx.Client(timeout=httpx.Timeout(300.0, connect=30.0), headers=headers(), follow_redirects=True) as client:
        health = client.get(f"{BACKEND}/health")
        report["health"] = health.status_code
        if health.status_code != 200:
            raise SystemExit(f"health {health.status_code}")

        # 1. BBC month on the library football fixtures workflow (already bound or apply now).
        listing = client.get(f"{BACKEND}/api/workflows").json()
        workflows = listing if isinstance(listing, list) else listing.get("workflows") or listing.get("items") or []
        football_id = next((w.get("id") for w in workflows if (w.get("name") or "") == "football_fixtures"), None)
        if football_id:
            t0 = time.perf_counter()
            explored = client.post(f"{BACKEND}/api/workflows/{football_id}/explore", json={"apply": True, "variables": {"team": "Arsenal"}})
            body = explored.json()
            default = run_wf(client, football_id, {"team": "Arsenal"})
            alt = run_wf(client, football_id, {"team": "Arsenal", "month": "2026-12"})
            wf = client.get(f"{BACKEND}/api/workflows/{football_id}").json()
            defn = wf.get("definition") or wf
            urls = [s.get("url") for s in defn.get("steps") or [] if s.get("url")]
            report["cases"]["bbc_month"] = {
                "ok": "month" in (body.get("accepted") or ["month"])
                and any("{month" in (u or "") for u in urls)
                and default.get("status") == "succeeded"
                and alt.get("status") == "succeeded"
                and default.get("outputs_preview") != alt.get("outputs_preview"),
                "accepted": body.get("accepted"),
                "usage": body.get("usage"),
                "default": default,
                "alt": alt,
                "urls": urls,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
            print("CASE bbc_month", report["cases"]["bbc_month"]["ok"], flush=True)

        # 2. books.toscrape pagination
        t0 = time.perf_counter()
        books_id = create_nav_workflow(
            client,
            "books_toscrape_page",
            "https://books.toscrape.com/catalogue/page-1.html",
            "Browse a page of books and extract titles",
        )
        explored = client.post(f"{BACKEND}/api/workflows/{books_id}/explore", json={"apply": True, "variables": {}})
        body = explored.json()
        default = run_wf(client, books_id, {})
        alt = run_wf(client, books_id, {"page": "2"})
        wf = client.get(f"{BACKEND}/api/workflows/{books_id}").json()
        defn = wf.get("definition") or wf
        report["cases"]["books_toscrape_page"] = {
            "ok": "page" in (body.get("accepted") or [])
            and default.get("status") == "succeeded"
            and alt.get("status") == "succeeded"
            and default.get("outputs_preview") != alt.get("outputs_preview"),
            "workflow_id": books_id,
            "accepted": body.get("accepted"),
            "proposals": [p.get("name") for p in (body.get("proposals") or body.get("verified") or [])],
            "usage": body.get("usage"),
            "default": default,
            "alt": alt,
            "schema": defn.get("input_schema"),
            "urls": [s.get("url") for s in defn.get("steps") or [] if s.get("url")],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
        print("CASE books_toscrape_page", report["cases"]["books_toscrape_page"]["ok"], flush=True)

        # 3. books.toscrape category (travel vs mystery)
        t0 = time.perf_counter()
        cat_id = create_nav_workflow(
            client,
            "books_toscrape_category",
            "https://books.toscrape.com/catalogue/category/books/travel_2/index.html",
            "Browse a book category",
        )
        explored = client.post(
            f"{BACKEND}/api/workflows/{cat_id}/explore",
            json={"apply": True, "variables": {"category": "travel_2"}},
        )
        body = explored.json()
        default = run_wf(client, cat_id, {"category": "travel_2"})
        alt = run_wf(client, cat_id, {"category": "mystery_3"})
        wf = client.get(f"{BACKEND}/api/workflows/{cat_id}").json()
        defn = wf.get("definition") or wf
        report["cases"]["books_toscrape_category"] = {
            "ok": "category" in (body.get("accepted") or [])
            and default.get("status") == "succeeded"
            and alt.get("status") == "succeeded"
            and default.get("outputs_preview") != alt.get("outputs_preview"),
            "workflow_id": cat_id,
            "accepted": body.get("accepted"),
            "usage": body.get("usage"),
            "default": default,
            "alt": alt,
            "schema": defn.get("input_schema"),
            "urls": [s.get("url") for s in defn.get("steps") or [] if s.get("url")],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
        print("CASE books_toscrape_category", report["cases"]["books_toscrape_category"]["ok"], flush=True)

        # 4. DemoQA radio / select-menu: create nav workflow, explore should see radios or old select in HTML
        t0 = time.perf_counter()
        demo_id = create_nav_workflow(
            client,
            "demoqa_radio_explore",
            "https://demoqa.com/radio-button",
            "Read the DemoQA radio button page",
        )
        explored = client.post(f"{BACKEND}/api/workflows/{demo_id}/explore", json={"apply": True, "variables": {}}, timeout=240)
        body = explored.json()
        accepted = body.get("accepted") or []
        # If a radio/choice was applied, replay default and alt; otherwise record the proposals.
        schema = ((client.get(f"{BACKEND}/api/workflows/{demo_id}").json().get("definition") or {}).get("input_schema") or [])
        alt_var = {}
        default_var = {}
        verified = body.get("verified") or []
        dropped = body.get("dropped") or []
        choice = next((p for p in verified if p.get("name") in accepted), None)
        if choice:
            default_var[choice["name"]] = choice.get("default") or ""
            alt_var[choice["name"]] = choice.get("verify_with") or ""
        default = run_wf(client, demo_id, default_var) if default_var else run_wf(client, demo_id, {})
        alt = run_wf(client, demo_id, alt_var) if alt_var else {"status": "skipped"}
        report["cases"]["demoqa_radio"] = {
            "ok": browser_verify_ok(body),
            "workflow_id": demo_id,
            "accepted": accepted,
            "verified": [p.get("name") for p in verified],
            "dropped": [{"name": p.get("name"), "reason": p.get("dropped_reason")} for p in dropped],
            "usage": body.get("usage"),
            "schema": schema,
            "default": default,
            "alt": alt,
            "error": body.get("error") or body.get("detail"),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
        print("CASE demoqa_radio", report["cases"]["demoqa_radio"]["ok"], "accepted", accepted, flush=True)

        # 5. SauceDemo inventory sort (login required; browser verify of <select>)
        t0 = time.perf_counter()
        sauce_id = create_workflow(
            client,
            "saucedemo_sort_explore",
            [
                {"type": "navigation", "url": "https://www.saucedemo.com/"},
                input_step("#user-name", "standard_user", "Username"),
                input_step("#password", "secret_sauce", "Password"),
                click_step("#login-button", "Login"),
                {"type": "extract", "extractionGoal": "inventory product names and the current sort option", "output": "extract"},
            ],
            "Log in to SauceDemo and view the inventory sort control",
        )
        explored = client.post(f"{BACKEND}/api/workflows/{sauce_id}/explore", json={"apply": True, "variables": {}}, timeout=240)
        body = explored.json() if explored.headers.get("content-type", "").startswith("application/json") else {"error": explored.text[:300]}
        accepted = body.get("accepted") or []
        verified = body.get("verified") or []
        dropped = body.get("dropped") or []
        choice = next((p for p in verified if p.get("name") in accepted), None)
        default_var = {choice["name"]: choice.get("default") or ""} if choice else {}
        alt_var = {choice["name"]: choice.get("verify_with") or ""} if choice else {}
        default = run_wf(client, sauce_id, default_var) if default_var else run_wf(client, sauce_id, {})
        alt = run_wf(client, sauce_id, alt_var) if alt_var else {"status": "skipped"}
        report["cases"]["saucedemo_sort"] = {
            "ok": browser_verify_ok(body),
            "workflow_id": sauce_id,
            "accepted": accepted,
            "verified": [p.get("name") for p in verified],
            "dropped": [{"name": p.get("name"), "reason": p.get("dropped_reason")} for p in dropped],
            "usage": body.get("usage"),
            "default": default,
            "alt": alt,
            "error": body.get("error") or body.get("detail"),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
        print("CASE saucedemo_sort", report["cases"]["saucedemo_sort"]["ok"], "accepted", accepted, flush=True)

        # 6. BBC football competition tabs
        t0 = time.perf_counter()
        bbc_id = create_nav_workflow(
            client,
            "bbc_competition_explore",
            "https://www.bbc.com/sport/football/scores-fixtures",
            "Read BBC football scores and competition tabs",
        )
        explored = client.post(f"{BACKEND}/api/workflows/{bbc_id}/explore", json={"apply": True, "variables": {}}, timeout=240)
        body = explored.json() if explored.headers.get("content-type", "").startswith("application/json") else {"error": explored.text[:300]}
        accepted = body.get("accepted") or []
        verified = body.get("verified") or []
        dropped = body.get("dropped") or []
        choice = next((p for p in verified if p.get("name") in accepted), None)
        default_var = {choice["name"]: choice.get("default") or ""} if choice else {}
        alt_var = {choice["name"]: choice.get("verify_with") or ""} if choice else {}
        default = run_wf(client, bbc_id, default_var) if default_var else run_wf(client, bbc_id, {})
        alt = run_wf(client, bbc_id, alt_var) if alt_var else {"status": "skipped"}
        report["cases"]["bbc_competition"] = {
            "ok": browser_verify_ok(body),
            "workflow_id": bbc_id,
            "accepted": accepted,
            "verified": [p.get("name") for p in verified],
            "dropped": [{"name": p.get("name"), "reason": p.get("dropped_reason")} for p in dropped],
            "usage": body.get("usage"),
            "default": default,
            "alt": alt,
            "error": body.get("error") or body.get("detail"),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
        print("CASE bbc_competition", report["cases"]["bbc_competition"]["ok"], "accepted", accepted, flush=True)

    report["finished"] = now_label()
    report["ok"] = all(case.get("ok") for case in report["cases"].values()) if report["cases"] else False
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print("ALL_DONE", report["ok"], report["finished"], "cases", list(report["cases"]), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

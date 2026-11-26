"""Live from-task create + two-input replay for Alex's football task and practice sites.

Never prints or writes API keys or access tokens.
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
REPORT = Path("/workspace/browser-platform/data/live_from_task_practice_report.json")
IST = timezone(timedelta(hours=2))
MODEL = "qwen/qwen3.7-flash"
POLL_S = 420

CASES = [
    {
        "id": "football_repro",
        "name": "football_fixtures_from_task",
        "task": "Search the website for a specific football team fixtures or fixture",
        "start_url": "https://www.bbc.com/sport/football",
        "variables": {"team": "Arsenal"},
        "replay2": {"team": "Chelsea"},
        "expect_inputs": ["team"],
    },
    {
        "id": "saucedemo",
        "name": "saucedemo_checkout",
        "task": (
            "On SauceDemo, log in with the published test credentials username standard_user "
            "and password secret_sauce. Add the product {product} to the cart, open the cart, "
            "checkout, fill first name {first_name}, last name {last_name} and zip {zip}, "
            "continue and finish. Extract the checkout confirmation heading and message."
        ),
        "start_url": "https://www.saucedemo.com",
        "variables": {"product": "Sauce Labs Backpack", "first_name": "Alex", "last_name": "Lewis", "zip": "90210"},
        "replay2": {"product": "Sauce Labs Bike Light", "first_name": "Sam", "last_name": "Taylor", "zip": "10001"},
        "expect_inputs": ["product", "first_name", "zip"],
    },
    {
        "id": "demoqa",
        "name": "demoqa_practice_form",
        "task": (
            "On the DemoQA automation-practice-form, fill first name {first_name}, last name {last_name}, "
            "email {email}, select gender {gender}, enter mobile {mobile}, submit the form, "
            "and extract the confirmation modal title and the submitted values."
        ),
        "start_url": "https://demoqa.com/automation-practice-form",
        "variables": {
            "first_name": "Alex",
            "last_name": "Lewis",
            "email": "alex@example.com",
            "gender": "Male",
            "mobile": "9876543210",
        },
        "replay2": {
            "first_name": "Sam",
            "last_name": "Taylor",
            "email": "sam@example.com",
            "gender": "Female",
            "mobile": "9123456789",
        },
        "expect_inputs": ["first_name", "email", "gender", "mobile"],
    },
    {
        "id": "httpbin",
        "name": "httpbin_form_post",
        "task": (
            "On the httpbin pizza form, fill customer name {customer}, choose size {size}, "
            "select topping {toppings}, submit the form, and extract the echoed JSON."
        ),
        "start_url": "https://httpbin.org/forms/post",
        "variables": {"customer": "John Doe", "size": "large", "toppings": "bacon"},
        "replay2": {"customer": "Jane Roe", "size": "medium", "toppings": "mushroom"},
        "expect_inputs": ["customer", "size", "toppings"],
    },
]


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


def llm_key() -> str:
    value = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not value:
        env = Path("/workspace/browser-platform/.env")
        if env.exists():
            for line in env.read_text().splitlines():
                key, _, raw = line.partition("=")
                if key.strip() == "OPENROUTER_API_KEY":
                    value = raw.strip().strip('"')
                    break
    return value


def now_label() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def headers() -> dict[str, str]:
    return {"X-Browser-Platform-Token": token(), "Content-Type": "application/json"}


def slim_job(job: dict[str, Any]) -> dict[str, Any]:
    params = job.get("parameters") or []
    schema = job.get("input_schema") or (job.get("definition") or {}).get("input_schema") or []
    steps = (job.get("definition") or {}).get("steps") or job.get("steps") or []
    explore = job.get("explore") or {}
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "status": job.get("status"),
        "error": job.get("error"),
        "workflow_id": job.get("workflow_id"),
        "parameters": [{"name": p.get("name"), "example": p.get("example"), "required": p.get("required")} for p in params],
        "input_schema": [{"name": i.get("name"), "required": i.get("required"), "default": i.get("default"), "format": i.get("format")} for i in schema],
        "variables": job.get("variables"),
        "urls": [s.get("url") for s in steps if isinstance(s, dict) and s.get("url")],
        "creation": job.get("creation"),
        "replay": {
            "status": (job.get("replay") or {}).get("status"),
            "error": (job.get("replay") or {}).get("error"),
            "variables": (job.get("replay") or {}).get("variables"),
            "latency_ms": (job.get("replay") or {}).get("latency_ms"),
            "tokens_in": (job.get("replay") or {}).get("tokens_in"),
            "tokens_out": (job.get("replay") or {}).get("tokens_out"),
            "outputs_preview": str(((job.get("replay") or {}).get("outputs") or {}).get("extract") or (job.get("replay") or {}).get("outputs") or "")[:400],
        },
        "explore": {
            "accepted": explore.get("accepted"),
            "applied": explore.get("applied"),
            "usage": explore.get("usage"),
            "verified": [i.get("name") for i in explore.get("verified") or []],
            "dropped": [{"name": i.get("name"), "reason": i.get("dropped_reason")} for i in explore.get("dropped") or []],
        },
        "input_validation": {
            "error_count": (job.get("input_validation") or {}).get("error_count"),
            "blocks_publish": (job.get("input_validation") or {}).get("blocks_publish"),
            "codes": [f.get("code") for f in (job.get("input_validation") or {}).get("findings") or []],
        },
    }


def main() -> int:
    key = llm_key()
    report: dict[str, Any] = {"started": now_label(), "backend": BACKEND, "cases": {}, "key_present": bool(key), "key_length": len(key)}
    if REPORT.exists():
        try:
            previous = json.loads(REPORT.read_text())
            if isinstance(previous.get("cases"), dict):
                report["cases"] = previous["cases"]
                report["previous_started"] = previous.get("started")
        except Exception:
            pass
    # The Oracle backend holds the LLM key. A missing local key is not a reason to skip the live cases.
    timeout = httpx.Timeout(60.0, connect=30.0)
    with httpx.Client(timeout=timeout, headers=headers(), follow_redirects=True) as client:
        health = client.get(f"{BACKEND}/health")
        report["health"] = health.status_code
        if health.status_code != 200:
            raise SystemExit(f"health {health.status_code}")
        only = {item.strip() for item in os.environ.get("FROM_TASK_CASES", "").split(",") if item.strip()}
        cases = [case for case in CASES if not only or case["id"] in only]
        report["selected"] = [case["id"] for case in cases]
        for case in cases:
            print(f"CASE_START {case['id']} {now_label()}", flush=True)
            try:
                result = run_case(client, case, key)
            except Exception as exc:
                result = {"create_ok": False, "extraction_ok": False, "error": f"{type(exc).__name__}: {exc}", "replay2": {"status": "error", "error": str(exc)}}
            report["cases"][case["id"]] = result
            REPORT.parent.mkdir(parents=True, exist_ok=True)
            REPORT.write_text(json.dumps(report, indent=2) + "\n")
            print(
                f"CASE_DONE {case['id']} status={result.get('job', {}).get('status')} "
                f"wf={result.get('job', {}).get('workflow_id')} replay2={result.get('replay2', {}).get('status')}",
                flush=True,
            )
    report["finished"] = now_label()
    report["ok"] = all(
        item.get("extraction_ok") and item.get("create_ok") and item.get("replay2", {}).get("status") == "succeeded"
        for item in report["cases"].values()
    )
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print("ALL_DONE", report["ok"], report["finished"], flush=True)
    return 0 if report["ok"] else 1


def run_case(client: httpx.Client, case: dict[str, Any], key: str) -> dict[str, Any]:
    started = time.perf_counter()
    payload = {
        "task": case["task"],
        "name": case["name"],
        "start_url": case["start_url"],
        "variables": case["variables"],
        "confirm_variables": True,
        "explore": True,
        "wait": False,
        "replay": True,
        "write_back": True,
        "max_steps": 40,
        "timeout": 300,
    }
    # Use the Oracle server's LLM key. Never send a client api_key (an empty one would disable it).
    if key:
        payload["llm"] = {"api_key": key, "model": MODEL}
    # When the local key is absent, omit llm so the server uses its environment key.
    res = client.post(f"{BACKEND}/api/workflows/from-task", json=payload, timeout=60)
    body = res.json() if res.headers.get("content-type", "").startswith("application/json") else {"error": res.text[:300]}
    if res.status_code != 200:
        return {"http_status": res.status_code, "error": body, "create_ok": False, "extraction_ok": False}
    job_id = body.get("id")
    job = poll_and_confirm(client, job_id, case)
    slim = slim_job(job)
    names = {item.get("name") for item in (slim.get("input_schema") or slim.get("parameters") or [])}
    extraction_ok = set(case["expect_inputs"]) <= names
    create_ok = job.get("status") == "succeeded" and bool(job.get("workflow_id"))
    result: dict[str, Any] = {
        "http_status": 200,
        "job": slim,
        "extraction_ok": extraction_ok,
        "create_ok": create_ok,
        "expected_inputs": case["expect_inputs"],
        "extracted_inputs": sorted(names),
        "wall_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    workflow_id = job.get("workflow_id")
    if workflow_id:
        try:
            result["replay2"] = poll_run(client, workflow_id, case["replay2"])
        except Exception as exc:
            result["replay2"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}", "variables": case["replay2"]}
    else:
        result["replay2"] = {"status": "skipped", "error": "no workflow id"}
    return result



def poll_run(client: httpx.Client, workflow_id: str, variables: dict[str, Any], timeout_s: int = 240) -> dict[str, Any]:
    replay = client.post(
        f"{BACKEND}/api/workflows/{workflow_id}/run",
        json={"variables": variables, "wait": False, "write_back": False},
        timeout=60,
    )
    run = replay.json() if replay.headers.get("content-type", "").startswith("application/json") else {"error": replay.text[:300]}
    if replay.status_code != 200:
        return {"http_status": replay.status_code, "status": run.get("status") or "failed", "error": run.get("error") or run, "variables": variables}
    run_id = run.get("id")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if run.get("status") in {"succeeded", "failed", "cancelled"}:
            break
        if not run_id:
            break
        time.sleep(2)
        got = client.get(f"{BACKEND}/api/runs/{run_id}", timeout=30)
        run = got.json() if got.headers.get("content-type", "").startswith("application/json") else run
    return {
        "http_status": replay.status_code,
        "status": run.get("status"),
        "error": run.get("error"),
        "variables": variables,
        "tokens_in": run.get("tokens_in"),
        "tokens_out": run.get("tokens_out"),
        "latency_ms": run.get("latency_ms"),
        "outputs_preview": str((run.get("outputs") or {}).get("extract") or run.get("outputs") or "")[:400],
    }


def poll_and_confirm(client: httpx.Client, job_id: str, case: dict[str, Any]) -> dict[str, Any]:
    deadline = time.time() + POLL_S
    confirmed = set()
    job: dict[str, Any] = {}
    while time.time() < deadline:
        res = client.get(f"{BACKEND}/api/workflows/from-task/{job_id}", timeout=30)
        job = res.json()
        status = job.get("status")
        if status == "needs_confirmation":
            kind = (job.get("confirmation") or {}).get("kind") or ""
            key = f"{job_id}:{kind}:{status}"
            if key not in confirmed:
                variables = dict(case.get("variables") or {})
                parameters = job.get("parameters") or (job.get("confirmation") or {}).get("parameters") or []
                # Keep extracted names, fill missing examples from the case.
                payload: dict[str, Any] = {"action": "confirm", "variables": variables}
                if kind == "parameters" and parameters:
                    filled = []
                    for item in parameters:
                        name = item.get("name")
                        example = item.get("example") or variables.get(name) or ""
                        if name in variables:
                            example = variables[name]
                        filled.append({**item, "example": example, "name": name})
                    payload["parameters"] = filled
                conf = client.post(f"{BACKEND}/api/workflows/from-task/{job_id}/confirmation", json=payload, timeout=30)
                confirmed.add(key)
                print(f"CONFIRMED {case['id']} kind={kind} http={conf.status_code}", flush=True)
        if status in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(2)
    job["error"] = job.get("error") or "poll timeout"
    job["status"] = job.get("status") or "timeout"
    return job


if __name__ == "__main__":
    raise SystemExit(main())

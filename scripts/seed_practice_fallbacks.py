"""Upsert deterministic practice-site workflows and replay two input sets.

Used when from-task extraction succeeds but the browser-use demonstration
does not yield a replayable recording. Never prints tokens or keys.
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
REPORT = Path("/workspace/browser-platform/data/live_practice_fallback_report.json")
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


def css(selector: str, name: str | None = None) -> dict[str, Any]:
    return {"type": "css", "value": selector, "name": name or selector}


def text_strategy(value: str) -> dict[str, Any]:
    return {"type": "text_exact", "value": value, "name": "text"}


def input_step(css_sel: str, value: str, target: str) -> dict[str, Any]:
    return {
        "type": "input",
        "cssSelector": css_sel,
        "value": value,
        "target_text": target,
        "selectorStrategies": [css(css_sel), text_strategy(target)],
    }


def click_step(css_sel: str | None, target: str, strategies: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    rec: dict[str, Any] = {"type": "click", "target_text": target}
    if css_sel:
        rec["cssSelector"] = css_sel
    rec["selectorStrategies"] = strategies or ([css(css_sel)] if css_sel else [text_strategy(target)])
    return rec


WORKFLOWS = [
    {
        "name": "saucedemo_checkout",
        "description": "Log in to SauceDemo, add a product, and complete checkout.",
        "fallback_reason": "from-task extracted inputs correctly but the demonstration stopped on checkout.html before Finish, and earlier recordings rewrote saucedemo.com from the product name.",
        "schema": [
            {"name": "product", "type": "string", "required": True, "default": "Sauce Labs Backpack", "label": "Product"},
            {"name": "first_name", "type": "string", "required": True, "default": "Alex", "label": "First name"},
            {"name": "last_name", "type": "string", "required": True, "default": "Lewis", "label": "Last name"},
            {"name": "zip", "type": "string", "required": True, "default": "90210", "label": "Zip"},
        ],
        "steps": [
            {"type": "navigation", "url": "https://www.saucedemo.com/"},
            input_step("#user-name", "standard_user", "Username"),
            input_step("#password", "secret_sauce", "Password"),
            click_step("#login-button", "Login", [css("#login-button"), css("input[type=submit]")]),
            click_step("#add-to-cart-{product|slug}", "Add to cart", [css("#add-to-cart-{product|slug}"), text_strategy("{product}")]),
            click_step("#shopping_cart_container a", "Cart", [css("#shopping_cart_container a"), css(".shopping_cart_link")]),
            click_step("#checkout", "Checkout"),
            input_step("#first-name", "{first_name}", "First Name"),
            input_step("#last-name", "{last_name}", "Last Name"),
            input_step("#postal-code", "{zip}", "Zip/Postal Code"),
            click_step("#continue", "Continue"),
            click_step("#finish", "Finish"),
            {
                "type": "extract",
                "extractionGoal": "the checkout confirmation heading and message",
                "output": "extract",
                "expected_state": {
                    "text_contains": "Thank you for your order",
                },
            },
        ],
        "replay1": {"product": "Sauce Labs Backpack", "first_name": "Alex", "last_name": "Lewis", "zip": "90210"},
        "replay2": {"product": "Sauce Labs Bike Light", "first_name": "Sam", "last_name": "Taylor", "zip": "10001"},
    },
    {
        "name": "demoqa_practice_form",
        "description": "Fill the DemoQA practice form and read the confirmation modal.",
        "fallback_reason": "from-task extracted inputs correctly but browser-use crashed after the demo with: id=\"firstName\"] is not an IP address.",
        "schema": [
            {"name": "first_name", "type": "string", "required": True, "default": "Alex", "label": "First name"},
            {"name": "last_name", "type": "string", "required": True, "default": "Lewis", "label": "Last name"},
            {"name": "email", "type": "string", "required": True, "default": "alex@example.com", "label": "Email"},
            {"name": "gender", "type": "string", "required": True, "default": "Male", "label": "Gender"},
            {"name": "mobile", "type": "string", "required": True, "default": "9876543210", "label": "Mobile"},
        ],
        "steps": [
            {"type": "navigation", "url": "https://demoqa.com/automation-practice-form"},
            input_step("#firstName", "{first_name}", "First Name"),
            input_step("#lastName", "{last_name}", "Last Name"),
            input_step("#userEmail", "{email}", "name@example.com"),
            click_step("label[for='gender-radio-1']", "{gender}", [text_strategy("{gender}"), css("label[for='gender-radio-1']"), css("label[for='gender-radio-2']")]),
            input_step("#userNumber", "{mobile}", "Mobile Number"),
            click_step("#submit", "Submit"),
            {
                "type": "extract",
                "extractionGoal": "the confirmation modal title and the submitted values",
                "output": "extract",
                "expected_state": {"echo_values": ["{first_name}", "{last_name}", "{email}", "{gender}", "{mobile}"]},
            },
        ],
        "replay1": {"first_name": "Alex", "last_name": "Lewis", "email": "alex@example.com", "gender": "Male", "mobile": "9876543210"},
        "replay2": {"first_name": "Sam", "last_name": "Taylor", "email": "sam@example.com", "gender": "Female", "mobile": "9123456789"},
    },
    {
        "name": "httpbin_form_post",
        "description": "Submit the httpbin pizza form and extract the echoed JSON.",
        "fallback_reason": "from-task extracted inputs correctly but the recorder lost fills on form navigation, leaving a GET of /post with no body.",
        "schema": [
            {"name": "customer", "type": "string", "required": True, "default": "John Doe", "label": "Customer"},
            {"name": "size", "type": "string", "required": True, "default": "large", "label": "Size"},
            {"name": "toppings", "type": "string", "required": True, "default": "bacon", "label": "Toppings"},
        ],
        "steps": [
            {"type": "navigation", "url": "https://httpbin.org/forms/post"},
            input_step("input[name=custname]", "{customer}", "Customer name"),
            click_step("input[name=size][value='{size}']", "{size}", [css("input[name=size][value='{size}']"), text_strategy("{size}")]),
            click_step("input[name=topping][value='{toppings}']", "{toppings}", [css("input[name=topping][value='{toppings}']"), text_strategy("{toppings}")]),
            click_step("p > input[type=submit]", "Submit order", [css("p > input[type=submit]"), css("input[type=submit]")]),
            {
                "type": "extract",
                "extractionGoal": "the echoed JSON",
                "output": "extract",
                "expected_state": {"echo_values": ["{customer}", "{size}", "{toppings}"]},
            },
        ],
        "replay1": {"customer": "John Doe", "size": "large", "toppings": "bacon"},
        "replay2": {"customer": "Jane Roe", "size": "medium", "toppings": "mushroom"},
    },
]


def find_id(client: httpx.Client, name: str) -> str | None:
    listing = client.get(f"{BACKEND}/api/workflows").json()
    items = listing if isinstance(listing, list) else listing.get("workflows") or listing.get("items") or []
    matches = [w.get("id") for w in items if (w.get("name") or "") == name]
    return matches[-1] if matches else None


def upsert(client: httpx.Client, spec: dict[str, Any]) -> str:
    body = {
        "definition": {
            "name": spec["name"],
            "description": spec["description"],
            "version": "1",
            "input_schema": spec["schema"],
            "steps": spec["steps"],
            "workflow_analysis": "Deterministic practice-site fallback (from-task create did not yield a replayable recording).",
        }
    }
    existing = find_id(client, spec["name"])
    if existing:
        res = client.put(f"{BACKEND}/api/workflows/{existing}", json=body, timeout=30)
        res.raise_for_status()
        return existing
    res = client.post(f"{BACKEND}/api/workflows", json=body, timeout=30)
    res.raise_for_status()
    data = res.json()
    return data.get("id") or (data.get("workflow") or {}).get("id") or data.get("workflow_id")


def poll_run(client: httpx.Client, workflow_id: str, variables: dict[str, Any], timeout_s: int = 180) -> dict[str, Any]:
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
        "variables": variables,
        "latency_ms": data.get("latency_ms"),
        "outputs_preview": str(outputs.get("extract") or outputs or "")[:400],
    }


def main() -> int:
    report: dict[str, Any] = {"started": now_label(), "backend": BACKEND, "kind": "deterministic_fallback", "cases": {}}
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=30.0), headers=headers(), follow_redirects=True) as client:
        health = client.get(f"{BACKEND}/health")
        report["health"] = health.status_code
        if health.status_code != 200:
            raise SystemExit(f"health {health.status_code}")
        for spec in WORKFLOWS:
            print(f"CASE_START {spec['name']} {now_label()}", flush=True)
            try:
                wid = upsert(client, spec)
                r1 = poll_run(client, wid, spec["replay1"])
                r2 = poll_run(client, wid, spec["replay2"])
                ok = r1.get("status") == "succeeded" and r2.get("status") == "succeeded"
                report["cases"][spec["name"]] = {
                    "ok": ok,
                    "workflow_id": wid,
                    "fallback": True,
                    "fallback_reason": spec["fallback_reason"],
                    "replay1": r1,
                    "replay2": r2,
                    "inputs": [item["name"] for item in spec["schema"]],
                }
            except Exception as exc:
                report["cases"][spec["name"]] = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "fallback": True}
            REPORT.parent.mkdir(parents=True, exist_ok=True)
            REPORT.write_text(json.dumps(report, indent=2) + "\n")
            item = report["cases"][spec["name"]]
            print(f"CASE_DONE {spec['name']} ok={item.get('ok')} wf={item.get('workflow_id')} r1={((item.get('replay1') or {}).get('status'))} r2={((item.get('replay2') or {}).get('status'))}", flush=True)
    report["finished"] = now_label()
    report["ok"] = all(case.get("ok") for case in report["cases"].values()) if report["cases"] else False
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print("ALL_DONE", report["ok"], report["finished"], flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

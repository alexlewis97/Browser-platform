"""Headless iPhone check: ambiguous attendance name pauses, confirm, alias re-run.

Uses BROWSER_PLATFORM_USER_TOKEN from the environment and never prints it.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path("/workspace/browser-platform")
LOG = Path("/tmp/browser-platform-logs")
SITE = "https://browser-platform-web.vercel.app"
TUNNEL = (LOG / "tunnel_url.txt").read_text().strip()
WF_ID = (LOG / "attendance_workflow_id.txt").read_text().strip()
FIXTURE = "http://127.0.0.1:8765/attendance.html"


def token() -> str:
    value = os.environ.get("BROWSER_PLATFORM_USER_TOKEN", "").strip()
    if not value:
        raise SystemExit("BROWSER_PLATFORM_USER_TOKEN is not set")
    return value


def api(method: str, path: str, body=None):
    headers = {"X-Browser-Platform-Token": token(), "content-type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:8745" + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


async def configure_settings(page, shots) -> None:
    await page.goto(SITE, wait_until="networkidle")
    if await page.locator("a", has_text="Open settings").count():
        await page.locator("a", has_text="Open settings").click()
    else:
        await page.goto(f"{SITE}/#/settings", wait_until="networkidle")
    await page.locator('input[name="backendUrl"]').fill(TUNNEL)
    await page.locator('input[name="accessToken"]').fill(token())
    await page.locator("button", has_text="Test connection").click()
    for _ in range(40):
        status = (await page.locator("#settings-status").inner_text()).strip()
        pill = (await page.locator(".pill").inner_text()).strip()
        if "Connected" in status or ("backend" in pill.lower() and "needed" not in pill.lower()):
            if "fail" not in status.lower() and "error" not in status.lower():
                break
        await page.wait_for_timeout(250)
    await page.screenshot(path=str(shots / "hitl-1-settings.png"))
    status = (await page.locator("#settings-status").inner_text()).strip()
    if "Connected" not in status and "backend" not in (await page.locator(".pill").inner_text()).lower():
        raise SystemExit(f"SETTINGS_FAIL {status[:200]}")
    print("SETTINGS_OK")


async def fill_and_run(page, *, name: str) -> None:
    await page.goto(f"{SITE}/#/workflows/{WF_ID}", wait_until="networkidle")
    await page.locator("#run-form").wait_for()
    await page.locator('#run-form input[name="name"]').fill(name)
    if await page.locator('#run-form input[name="status"]').count():
        await page.locator('#run-form input[name="status"]').fill("Present")
    if await page.locator('#run-form input[name="url"]').count():
        await page.locator('#run-form input[name="url"]').fill(FIXTURE)
    await page.locator('#run-form select[name="hitl_mode"]').select_option("always_on_uncertain")
    await page.locator("#run-form button[type=submit]").click()
    await page.wait_for_url("**/#/runs/**", timeout=20_000)


async def wait_status(page, wanted: set[str], timeout_s: float = 60) -> str:
    steps = int(timeout_s / 0.4)
    last = ""
    for _ in range(steps):
        pills = " ".join(await page.locator(".pill").all_inner_texts())
        last = pills
        for status in wanted:
            if status in pills.lower():
                return status
        await page.wait_for_timeout(400)
    raise SystemExit(f"STATUS_TIMEOUT last={last!r}")


async def main() -> int:
    shots = LOG
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome",
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2,
            is_mobile=True,
            has_touch=True,
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            ),
        )
        page = await context.new_page()
        page.set_default_timeout(30_000)
        await configure_settings(page, shots)

        await fill_and_run(page, name="Ann Cohen")
        paused = await wait_status(page, {"needs_confirmation", "failed", "succeeded"}, timeout_s=45)
        await page.screenshot(path=str(shots / "hitl-2-paused.png"))
        if paused != "needs_confirmation":
            body = await page.inner_text("body")
            print("PAUSE_FAIL", paused, body[:800])
            await browser.close()
            return 1
        body = await page.inner_text("body")
        if "Anna Cohen" not in body or "Anne Cohen" not in body:
            print("CANDIDATES_FAIL", body[:800])
            await browser.close()
            return 1
        print("PAUSE_OK ambiguous Ann Cohen -> Anna / Anne visible")

        await page.locator("button.candidate", has_text="Anna Cohen").click()
        await page.locator("button", has_text="Confirm").click()
        first = await wait_status(page, {"succeeded", "failed", "cancelled"}, timeout_s=45)
        await page.screenshot(path=str(shots / "hitl-3-confirmed.png"))
        body = await page.inner_text("body")
        print("FIRST_RUN", first)
        if first != "succeeded":
            print("FIRST_FAIL", body[:800])
            await browser.close()
            return 1
        if "Anna Cohen" not in body or "hitl" not in body.lower():
            # resolution may be shown as 'hitl' on the click step
            print("FIRST_BODY_CHECK", "has_anna", "Anna Cohen" in body, "has_hitl", "hitl" in body.lower())
        run_url = page.url
        first_run_id = run_url.rsplit("/", 1)[-1].split("?")[0]
        print("FIRST_RUN_ID", first_run_id)

        # Inspect stored run + aliases without printing secrets
        record = api("GET", f"/api/runs/{first_run_id}")
        click = next((s for s in record.get("steps") or [] if s.get("step_type") == "click"), None)
        print("FIRST_CLICK_TIER", (click or {}).get("tier"), "RESOLUTION", (click or {}).get("resolution"))
        print("FIRST_OUTPUTS", json.dumps(record.get("outputs") or {}, ensure_ascii=False)[:200])
        wf = api("GET", f"/api/workflows/{WF_ID}")
        aliases = (wf.get("definition") or {}).get("aliases") or []
        print("ALIAS_COUNT", len(aliases))
        if aliases:
            alias = aliases[0]
            print(
                "ALIAS",
                "query=",
                alias.get("query"),
                "chosen=",
                alias.get("chosen_text"),
                "step=",
                alias.get("step_index"),
            )

        # Re-run: alias should make it deterministic (no confirmation)
        await fill_and_run(page, name="Ann Cohen")
        second = await wait_status(page, {"needs_confirmation", "succeeded", "failed", "cancelled"}, timeout_s=45)
        await page.screenshot(path=str(shots / "hitl-4-rerun.png"))
        body = await page.inner_text("body")
        print("SECOND_STATUS", second)
        if second == "needs_confirmation":
            print("RERUN_STILL_PAUSED", body[:800])
            await browser.close()
            return 1
        if second != "succeeded":
            print("RERUN_FAIL", body[:800])
            await browser.close()
            return 1
        second_run_id = page.url.rsplit("/", 1)[-1].split("?")[0]
        record2 = api("GET", f"/api/runs/{second_run_id}")
        click2 = next((s for s in record2.get("steps") or [] if s.get("step_type") == "click"), None)
        print("SECOND_CLICK_TIER", (click2 or {}).get("tier"), "RESOLUTION", (click2 or {}).get("resolution"))
        print("SECOND_OUTPUTS", json.dumps(record2.get("outputs") or {}, ensure_ascii=False)[:200])
        if (click2 or {}).get("resolution") != "alias":
            print("ALIAS_RESOLUTION_FAIL")
            await browser.close()
            return 1
        print("RERUN_OK deterministic alias")
        await browser.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

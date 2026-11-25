"""Headless iPhone check of Create from task on the deployed site.

Uses BROWSER_PLATFORM_USER_TOKEN and OPENROUTER_API_KEY from the
environment when present, and never prints them.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

LOG = Path("/tmp/browser-platform-logs")
SITE = "https://browser-platform-web.vercel.app"
TUNNEL = (LOG / "tunnel_url.txt").read_text().strip()
FIXTURE = "http://127.0.0.1:8765/from_task_form.html"
TASK = (
    "On this library search page, type browser automation in the Query field, "
    "type user@example.com in the Email field, click Search, and extract the first paragraph."
)


def access_token() -> str:
    value = os.environ.get("BROWSER_PLATFORM_USER_TOKEN", "").strip()
    if not value:
        raise SystemExit("BROWSER_PLATFORM_USER_TOKEN is not set")
    return value


def openrouter_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


async def configure_settings(page) -> None:
    await page.goto(SITE, wait_until="networkidle")
    if await page.locator("a", has_text="Open settings").count():
        await page.locator("a", has_text="Open settings").click()
    else:
        await page.goto(f"{SITE}/#/settings", wait_until="networkidle")
    await page.locator('input[name="backendUrl"]').fill(TUNNEL)
    await page.locator('input[name="accessToken"]').fill(access_token())
    key = openrouter_key()
    if key:
        await page.locator('input[name="openrouterKey"]').fill(key)
    await page.locator("button", has_text="Test connection").click()
    for _ in range(40):
        status = (await page.locator("#settings-status").inner_text()).strip()
        if "Connected" in status:
            break
        await page.wait_for_timeout(250)
    status = (await page.locator("#settings-status").inner_text()).strip()
    if "Connected" not in status:
        raise SystemExit(f"SETTINGS_FAIL {status[:200]}")
    print("SETTINGS_OK has_openrouter_key=" + ("yes" if key else "no"))


async def job_status(page) -> str:
    pills = " ".join(await page.locator(".pill").all_inner_texts())
    for state in ("needs_confirmation", "succeeded", "failed", "cancelled", "running"):
        if state in pills.lower():
            return state
    return pills.lower()


async def maybe_confirm_variables(page) -> bool:
    panel = page.locator("#from-task-confirm")
    if await panel.count() == 0:
        return False
    if "variables" not in ((await panel.get_attribute("data-kind")) or ""):
        return False
    body = await panel.inner_text()
    print("CONFIRM_UI_VISIBLE", "email" in body.lower(), "rows", await page.locator(".var-row").count())
    await page.locator("button", has_text="Confirm variables").click()
    return True


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
        await configure_settings(page)

        await page.goto(f"{SITE}/#/from-task", wait_until="networkidle")
        await page.locator("#from-task-form").wait_for()
        await page.screenshot(path=str(shots / "fromtask-1-form.png"))
        form_text = await page.inner_text("body")
        checks = {
            "task_field": await page.locator('textarea[name="task"]').count() > 0,
            "start_url_field": await page.locator('input[name="start_url"]').count() > 0,
            "variables_field": await page.locator('textarea[name="variables"]').count() > 0,
            "confirm_checkbox": await page.locator('input[name="confirm_variables"]').count() > 0,
        }
        print("FORM_OK", checks, "heading", "Create from task" in form_text)

        await page.locator('textarea[name="task"]').fill(TASK)
        await page.locator('input[name="start_url"]').fill(FIXTURE)
        await page.locator('input[name="name"]').fill("library_search_from_task")
        # Leave variables empty so confirm_variables can surface detected literals.
        if not await page.locator('input[name="confirm_variables"]').is_checked():
            await page.locator('input[name="confirm_variables"]').check()

        if not openrouter_key():
            print("SKIP_LIVE_JOB no OpenRouter key in the environment; UI form verified.")
            await browser.close()
            return 0

        await page.locator("#from-task-form button[type=submit]").click()
        try:
            await page.wait_for_url("**/#/from-task/**", timeout=20_000)
        except Exception:
            await page.screenshot(path=str(shots / "fromtask-2-start-fail.png"))
            print("START_FAIL", (await page.inner_text("body"))[:600])
            await browser.close()
            return 1
        print("JOB_STARTED", page.url.rsplit("/", 1)[-1][:16])
        await page.screenshot(path=str(shots / "fromtask-2-running.png"))

        confirmed = False
        final = "running"
        # Live LLM on a small fixture; give it two minutes.
        for i in range(130):
            state = await job_status(page)
            if state == "needs_confirmation" and not confirmed:
                await page.screenshot(path=str(shots / "fromtask-3-confirm.png"))
                confirmed = await maybe_confirm_variables(page)
                print("CONFIRMED" if confirmed else "CONFIRM_SKIPPED", "t", i)
            if state in {"succeeded", "failed", "cancelled"}:
                final = state
                break
            await page.wait_for_timeout(1000)
        else:
            final = await job_status(page)
            print("JOB_TIMEOUT", final)

        await page.screenshot(path=str(shots / "fromtask-4-final.png"))
        body = await page.inner_text("body")
        has_result = await page.locator("#from-task-result").count() > 0
        has_run = await page.locator("#from-task-result a", has_text="Run").count() > 0
        has_creation = "Creation" in body
        has_replay = "Replay" in body
        print("FINAL_STATUS", final)
        print("RESULT_CARD", has_result, "RUN_BUTTON", has_run, "CREATION_STATS", has_creation, "REPLAY_STATS", has_replay)
        if final != "succeeded":
            print("FINAL_SNIPPET", body[:900])
            await browser.close()
            # UI + job start still count as a partial pass if we got past start.
            return 1 if final in {"failed", "cancelled"} or final == "running" else 1
        await browser.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

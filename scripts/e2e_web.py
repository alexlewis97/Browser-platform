"""Headless check of the deployed mobile site: settings, list workflows, run example.com.

Reads the access token from /workspace/browser-platform/.env and never prints it.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path("/workspace/browser-platform")
LOG = Path("/tmp/browser-platform-logs")
SITE = (LOG / "vercel_url.txt").read_text().strip()
TUNNEL = (LOG / "tunnel_url.txt").read_text().strip()


def load_token() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("BROWSER_PLATFORM_ACCESS_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("access token missing from .env")


async def main() -> int:
    token = load_token()
    shots = LOG
    shots.mkdir(parents=True, exist_ok=True)
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
        console: list[str] = []
        page.on("console", lambda msg: console.append(f"{msg.type}: {msg.text}"[:300]))

        await page.goto(SITE, wait_until="networkidle")
        await page.screenshot(path=str(shots / "e2e-1-landing.png"))
        # Open settings (landing may already send us there, or show a setup card)
        if await page.locator("a", has_text="Open settings").count():
            await page.locator("a", has_text="Open settings").click()
        else:
            await page.goto(f"{SITE}/#/settings", wait_until="networkidle")

        await page.locator('input[name="backendUrl"]').fill(TUNNEL)
        await page.locator('input[name="accessToken"]').fill(token)
        await page.locator("button", has_text="Test connection").click()
        ok = False
        status_text = ""
        for _ in range(40):
            status_text = (await page.locator("#settings-status").inner_text()).strip()
            pill = (await page.locator(".pill").inner_text()).strip()
            if "Connected" in status_text or "backend" in pill.lower() and "setup" not in pill.lower() and "checking" not in pill.lower():
                if "Connected" in status_text or "visible" in status_text.lower() or pill.lower().strip().endswith("backend") or "backend" == pill.lower().replace("•","").strip():
                    ok = "Error" not in status_text and "failed" not in status_text.lower() and "setup needed" not in pill.lower()
                    if ok and ("Connected" in status_text or "backend" in pill.lower()):
                        # Prefer waiting a moment for either banner or connected pill
                        if "Connected" in status_text or ( "backend" in pill.lower() and "needed" not in pill.lower()):
                            break
            await page.wait_for_timeout(250)
        pill = (await page.locator(".pill").inner_text()).strip()
        status_text = (await page.locator("#settings-status").inner_text()).strip()
        await page.screenshot(path=str(shots / "e2e-2-settings.png"))
        connected = "Connected" in status_text or ("backend" in pill.lower() and "needed" not in pill.lower() and "checking" not in pill.lower())
        if not connected:
            print("SETTINGS_FAIL", "pill=", pill, "status=", status_text[:300])
            # print console (redact token / key-like strings)
            for line in console[-20]:
                safe = line
                if token and token in safe:
                    safe = safe.replace(token, "[token]")
                print("CONSOLE", safe)
            await browser.close()
            return 1
        print("SETTINGS_OK", (status_text or pill).replace("\n", " ")[:200])

        await page.goto(f"{SITE}/#/workflows", wait_until="networkidle")
        await page.wait_for_timeout(800)
        body = await page.inner_text("body")
        await page.screenshot(path=str(shots / "e2e-3-workflows.png"))
        if "example_com_heading" not in body:
            print("LIST_FAIL no example workflow visible")
            print(body[:500])
            await browser.close()
            return 1
        print("LIST_OK example_com_heading visible")

        await page.locator("a.card-link", has_text="example_com_heading").click()
        await page.locator("button", has_text="Run now").wait_for()
        await page.locator("button", has_text="Run now").click()
        # Live run view
        await page.wait_for_url("**/#/runs/**", timeout=15_000)
        result = None
        for _ in range(60):
            pill = await page.locator(".pill").all_inner_texts()
            joined = " ".join(pill)
            if "succeeded" in joined.lower():
                result = "succeeded"
                break
            if "failed" in joined.lower() or "cancelled" in joined.lower():
                result = joined
                break
            await page.wait_for_timeout(500)
        await page.screenshot(path=str(shots / "e2e-4-run.png"))
        body = await page.inner_text("body")
        # Summarize without dumping huge text
        has_nav = "navigation" in body.lower()
        has_extract = "extract" in body.lower()
        has_latency = "ms" in body or " s" in body
        print("RUN_STATUS", result)
        print("RUN_HAS_NAV", has_nav, "HAS_EXTRACT", has_extract, "HAS_LATENCY", has_latency)
        if result != "succeeded":
            print("RUN_BODY_SNIPPET", body[:800])
            await browser.close()
            return 1
        await browser.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

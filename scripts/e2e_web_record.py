"""Live-site Playwright e2e for website Record mode (desktop + mobile).

Never prints the access token or LLM keys.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

SITE = os.environ.get("BROWSER_PLATFORM_SITE", "https://browser-platform-web.vercel.app")
BACKEND = os.environ.get("BROWSER_PLATFORM_BACKEND", "https://browser.129-159-156-75.sslip.io")
REPORT = Path("/workspace/browser-platform/data/live_web_record_e2e_report.json")
IST = timezone(timedelta(hours=2))


def token() -> str:
    value = os.environ.get("BROWSER_PLATFORM_USER_TOKEN") or os.environ.get("BROWSER_PLATFORM_ACCESS_TOKEN") or ""
    if not value:
        env = Path("/workspace/browser-platform/.env")
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("BROWSER_PLATFORM_USER_TOKEN=") or line.startswith("BROWSER_PLATFORM_ACCESS_TOKEN="):
                    value = line.split("=", 1)[1].strip().strip('"')
                    break
    if not value:
        raise SystemExit("access token is not set")
    return value


def now_label() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


async def ensure_settings(page: Page, access: str) -> None:
    await page.goto(f"{SITE}/#/settings", wait_until="domcontentloaded")
    await page.wait_for_selector('input[name="backendUrl"]', timeout=20000)
    await page.locator('input[name="backendUrl"]').fill(BACKEND)
    await page.locator('input[name="accessToken"]').fill(access)
    if await page.locator("button", has_text="Save").count():
        await page.locator("button", has_text="Save").click()
        await page.wait_for_timeout(400)
    if await page.locator("button", has_text="Test connection").count():
        await page.locator("button", has_text="Test connection").click()
        for _ in range(40):
            status = await page.locator("#settings-status").inner_text()
            if "Connected" in status or "backend" in (await page.locator(".pill").inner_text()).lower():
                if "Error" not in status and "failed" not in status.lower():
                    break
            await page.wait_for_timeout(250)


async def locate_click(page: Page, query: dict) -> bool:
    return bool(await page.evaluate("async (q) => window.__bpRecord ? window.__bpRecord.locateAndClick(q) : false", query))


async def wait_for_stream(page: Page, timeout_ms: int = 60000) -> None:
    await page.wait_for_function(
        "() => Boolean(document.getElementById('record-screen') && document.getElementById('record-screen').src && document.getElementById('record-screen').src.length > 80)",
        timeout=timeout_ms,
    )


async def open_record(page: Page, name: str, goal: str) -> None:
    await page.goto(f"{SITE}/#/record", wait_until="domcontentloaded")
    await page.wait_for_selector("#record-start, #record-setup", timeout=20000)
    await page.locator("#record-goal").fill(goal)
    await page.locator("#record-name").fill(name)


async def run_remote_flow(page: Page, *, name: str, report: dict) -> dict:
    result: dict = {"name": name, "started_at": now_label()}
    await open_record(page, name, "Find the fixtures for a football team")
    # default source is remote
    t0 = time.time()
    await page.locator("#record-start").click()
    try:
        await page.wait_for_selector("#record-stop", timeout=60000)
    except Exception as exc:
        body = await page.locator("body").inner_text()
        raise AssertionError(f"Start did not enter a live session: {exc}\n{body[:2000]}") from exc
    err = await page.locator(".banner.err").inner_text() if await page.locator(".banner.err").count() else ""
    if err:
        raise AssertionError(f"Start failed: {err}")
    await wait_for_stream(page, 90000)
    result["start_s"] = round(time.time() - t0, 2)

    await page.locator("#record-url").fill("https://www.bbc.com/sport/football")
    await page.locator("#record-url-form button[type=submit]").click()
    # wait for BBC to show up in the url/meta
    for _ in range(40):
        meta = await page.locator(".record-meta").inner_text()
        if "bbc.com" in meta.lower() or "bbc.com" in (await page.locator("#record-url").input_value()):
            break
        await page.wait_for_timeout(500)
    await page.wait_for_timeout(1500)

    # cookie banner (best-effort)
    for query in (
        {"role": "button", "text": "Accept additional cookies"},
        {"role": "button", "text": "Yes, I agree"},
        {"role": "button", "text": "Reject additional cookies"},
        {"text": "Accept additional cookies"},
        {"text": "Agree"},
    ):
        if await locate_click(page, query):
            await page.wait_for_timeout(800)
            break

    # deliberate redundant clicks
    await locate_click(page, {"role": "link", "text": "Skip to content"})
    await page.wait_for_timeout(400)
    await locate_click(page, {"role": "link", "text": "Skip to main content"})
    await page.wait_for_timeout(400)
    await locate_click(page, {"text": "Football"})
    await page.wait_for_timeout(400)
    await locate_click(page, {"role": "link", "text": "Scores & Fixtures"})
    await page.wait_for_timeout(600)
    await locate_click(page, {"role": "link", "text": "Scores & Fixtures"})
    await page.wait_for_timeout(600)

    # land on a team fixtures page (URL bar is a first-class recorded navigation)
    await page.locator("#record-url").fill("https://www.bbc.com/sport/football/teams/arsenal/scores-fixtures")
    await page.locator("#record-url-form button[type=submit]").click()
    for _ in range(40):
        url = await page.locator("#record-url").input_value()
        if "/teams/arsenal" in url:
            break
        await page.wait_for_timeout(500)
    await page.wait_for_timeout(1000)
    result["recorded_url"] = await page.locator("#record-url").input_value()

    t1 = time.time()
    await page.locator("#record-stop").click()
    await page.wait_for_selector("#record-review", timeout=120000)
    result["clean_s"] = round(time.time() - t1, 2)

    review = await page.locator("#record-review").inner_text()
    result["review_excerpt"] = review[:2000]
    dropped = await page.locator(".rec-step.dropped").count()
    kept = await page.locator(".rec-step.kept").count()
    result["dropped"] = dropped
    result["kept"] = kept
    result["usage"] = ""
    for line in review.splitlines():
        if "token" in line.lower() or "deterministic" in line.lower() or "recorded steps" in line.lower():
            result["usage"] = (result.get("usage") or "") + line + " | "
    if dropped <= 0:
        raise AssertionError(f"expected pruned/greyed steps, got dropped={dropped} kept={kept}\n{review}")
    example = await page.locator('input[data-field="example"]').first.input_value()
    pname = await page.locator('input[data-field="name"]').first.input_value()
    result["param_name"] = pname
    result["param_example"] = example
    if "team" not in pname.lower():
        raise AssertionError(f"expected a team parameter, got {pname}")
    if "arsenal" not in example.lower() and "{team" not in review:
        raise AssertionError(f"expected Arsenal example or {{team}} in review, example={example}")

    t2 = time.time()
    await page.locator("#record-save").scroll_into_view_if_needed()
    await page.locator("#record-save").click(force=True)
    await page.wait_for_function("() => Boolean(window.__bpRecord && window.__bpRecord.recordState.workflowId)", timeout=60000)
    result["save_s"] = round(time.time() - t2, 2)
    workflow_id = await page.evaluate("() => window.__bpRecord.recordState.workflowId")
    result["workflow_id"] = workflow_id

    # Replay with Chelsea on the website run form
    await page.goto(f"{SITE}/#/workflows/{workflow_id}", wait_until="domcontentloaded")
    await page.wait_for_selector("#run-form", timeout=20000)
    team = page.locator('#run-form input[name="team"]')
    if await team.count() == 0:
        team = page.locator("#run-form input").first
    await team.fill("Chelsea")
    t3 = time.time()
    await page.locator("#run-form button[type=submit]").click()
    # run page
    for _ in range(80):
        if "#/runs/" in page.url or await page.locator(".idx.ok, .idx.bad").count():
            break
        await page.wait_for_timeout(250)
    # wait for terminal status
    status = ""
    for _ in range(80):
        pills = [txt.strip() for txt in await page.locator(".pill").all_inner_texts()]
        status = " | ".join(pills)
        joined = " ".join(pills).lower()
        if any(word in joined for word in ("succeeded", "failed", "cancelled")):
            break
        await page.wait_for_timeout(400)
    result["replay_s"] = round(time.time() - t3, 2)
    result["replay_status"] = status
    body = await page.locator("body").inner_text()
    result["replay_excerpt"] = body[:1500]
    if "chelsea" not in body.lower() and "succeeded" not in status.lower():
        raise AssertionError(f"replay did not clearly succeed for Chelsea: status={status}")
    if "failed" in status.lower() and "succeeded" not in status.lower():
        raise AssertionError(f"replay failed: {status}\n{body[:1500]}")

    # Publish from the Record page
    await page.goto(f"{SITE}/#/record", wait_until="domcontentloaded")
    await page.wait_for_selector("#record-publish", timeout=20000)
    t4 = time.time()
    await page.locator("#record-publish").scroll_into_view_if_needed()
    await page.locator("#record-publish").click(force=True)
    await page.wait_for_function(
        "() => Boolean(window.__bpRecord && window.__bpRecord.recordState.listing && window.__bpRecord.recordState.listing.id)",
        timeout=60000,
    )
    listing = await page.evaluate("() => window.__bpRecord.recordState.listing")
    result["publish_s"] = round(time.time() - t4, 2)
    result["listing"] = {"id": listing.get("id"), "name": listing.get("name"), "version": listing.get("version")}

    await page.goto(f"{SITE}/#/marketplace", wait_until="domcontentloaded")
    await page.wait_for_timeout(800)
    market_text = await page.locator("body").inner_text()
    listed = name in market_text or (listing.get("id") or "") in market_text or (listing.get("name") or "") in market_text
    result["listed_in_market"] = listed
    if not listed:
        raise AssertionError("published listing was not visible in the marketplace")

    # delete the test listing
    listing_id = listing.get("id")
    card = page.locator("article, .card", has_text=listing.get("name") or name).first
    if await card.locator("button", has_text="Delete").count():
        page.once("dialog", lambda dialog: dialog.accept())
        await card.locator("button", has_text="Delete").click()
        await page.wait_for_timeout(1000)
    else:
        # API fallback so we still clean up
        access = token()
        await page.evaluate(
            """async ({id, token}) => {
              const origin = location.origin;
              await fetch(`${origin}/api/market/${id}`, { method: 'DELETE', headers: { 'X-Browser-Platform-Token': token } });
            }""",
            {"id": listing_id, "token": access},
        )
    await page.goto(f"{SITE}/#/marketplace", wait_until="domcontentloaded")
    await page.wait_for_timeout(600)
    leftover = await page.locator("body").inner_text()
    result["deleted"] = listing_id not in leftover and (listing.get("name") or name) not in leftover
    result["ok"] = True
    result["finished_at"] = now_label()
    return result


async def make_context(browser: Browser, *, mobile: bool) -> BrowserContext:
    if mobile:
        return await browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2,
            is_mobile=True,
            has_touch=True,
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            ),
        )
    return await browser.new_context(viewport={"width": 1280, "height": 900})


async def main() -> int:
    access = token()
    stamp = datetime.now(IST).strftime("%H%M%S")
    report: dict = {"started_at": now_label(), "site": SITE, "backend": BACKEND, "runs": {}}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        try:
            for kind, mobile in (("desktop", False), ("mobile", True)):
                context = await make_context(browser, mobile=mobile)
                page = await context.new_page()
                page.set_default_timeout(30000)
                try:
                    await ensure_settings(page, access)
                    name = f"e2e_record_{kind}_{stamp}"
                    report["runs"][kind] = await run_remote_flow(page, name=name, report=report)
                except Exception as exc:
                    report["runs"][kind] = {**(report.get("runs", {}).get(kind) or {}), "ok": False, "error": str(exc)[:1500], "finished_at": now_label()}
                    report["ok"] = False
                    raise
                finally:
                    await context.close()
            report["ok"] = True
        finally:
            await browser.close()
            report["finished_at"] = now_label()
            REPORT.parent.mkdir(parents=True, exist_ok=True)
            REPORT.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in report if k != "runs"} | {"runs": {k: {kk: vv for kk, vv in v.items() if kk not in {"review_excerpt", "replay_excerpt"}} for k, v in report.get("runs", {}).items()}}, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

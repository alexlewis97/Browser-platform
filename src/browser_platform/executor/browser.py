"""Attach to the user's existing Chrome over CDP without resetting their session.

Contract:
  - connect_over_cdp to the given endpoint
  - reuse the existing default browser context (cookies, logins, extensions stay)
  - open a new tab for the run; never close tabs we did not open
  - never clear cookies, storage, or cache
  - disconnect only — do not terminate the user's Chrome
"""

from __future__ import annotations

from dataclasses import dataclass, field

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright


@dataclass
class CdpSession:
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    opened_pages: list[Page] = field(default_factory=list)
    cdp_url: str = ""


async def connect(cdp_url: str) -> CdpSession:
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(cdp_url)
    if browser.contexts:
        context = browser.contexts[0]
    else:
        # Should not happen on a real user Chrome; last resort only.
        context = await browser.new_context()
    page = await context.new_page()
    return CdpSession(
        playwright=pw,
        browser=browser,
        context=context,
        page=page,
        opened_pages=[page],
        cdp_url=cdp_url,
    )


async def disconnect(session: CdpSession) -> None:
    """Close only tabs we opened, then drop the CDP connection."""

    for page in list(session.opened_pages):
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass
    session.opened_pages.clear()
    try:
        # On a CDP connection this disconnects; it does not quit the user's Chrome.
        await session.browser.close()
    except Exception:
        pass
    try:
        await session.playwright.stop()
    except Exception:
        pass

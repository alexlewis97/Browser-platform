/**
 * Attach to an existing Chromium over CDP without closing other tabs or
 * resetting cookies / storage / site data.
 *
 * Stock createPomerado / makeSession *rejects* `browser.cdp` because its mint
 * and run paths inject resetPageCode, which closes every other tab in the
 * driven context and can reset site data on live example / first-write steps.
 * We therefore never pass `cdp` into createPomerado. Mint and artifact runs
 * use a Pomerado-owned launched Chromium (`browser: { headless: true }`).
 *
 * --cdp is still accepted for a health check: connect, list contexts/pages,
 * disconnect. We never close foreign pages or call clearCookies.
 */

export function cdpAttachOptions(_cdpUrl) {
  // Intentionally empty for createPomerado — see module doc.
  return {};
}

export function cdpPolicy(cdpUrl) {
  return {
    requested: Boolean(cdpUrl),
    url: cdpUrl || null,
    passed_to_createPomerado: false,
    closeOtherTabs: false,
    resetSiteData: false,
    reason:
      "Stock Pomerado mint/run close other tabs (resetPageCode) and may reset site data; mint and serve use an owned Chromium. User Chrome stays untouched.",
  };
}

/**
 * If Playwright is available, connect_over_cdp and reuse the default context.
 * Health check only: never close other tabs or clear site data.
 */
export async function connectExistingChrome(cdpUrl) {
  const { chromium } = await import("playwright");
  const browser = await chromium.connectOverCDP(cdpUrl);
  const context = browser.contexts()[0] ?? null;
  return {
    browser,
    context,
    pages: context ? context.pages().map((p) => p.url()) : [],
    contexts: browser.contexts().length,
    async disconnect() {
      // Disconnect only; Playwright close on a CDP connection is a disconnect.
      await browser.close();
    },
  };
}

export async function cdpHealth(cdpUrl) {
  if (!cdpUrl) return { ok: false, error: "no cdp url" };
  const attached = await connectExistingChrome(cdpUrl);
  try {
    return {
      ok: true,
      contexts: attached.contexts,
      pages: attached.pages,
      closeOtherTabs: false,
      resetSiteData: false,
    };
  } finally {
    await attached.disconnect();
  }
}


/**
 * Launch a Playwright browser server on an owned Chrome/Chromium.
 * createPomerado accepts `endpoint` (not `cdp`) so mint/run never close the
 * user's tabs. Prefers system Chrome when Playwright's headless shell is missing.
 */
export async function launchOwnedPlaywrightServer() {
  const { chromium } = await import("playwright");
  const candidates = [
    process.env.POMERADO_CHROME,
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);
  let lastError;
  for (const executablePath of [...new Set(candidates)]) {
    try {
      const server = await chromium.launchServer({
        headless: true,
        executablePath,
        args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--no-first-run"],
      });
      return { server, endpoint: server.wsEndpoint(), executablePath };
    } catch (err) {
      lastError = err;
    }
  }
  try {
    const server = await chromium.launchServer({
      headless: true,
      args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
    });
    return { server, endpoint: server.wsEndpoint(), executablePath: "playwright-default" };
  } catch (err) {
    const detail = lastError ? `${lastError}; then ${err}` : String(err);
    throw new Error(`Failed to launch an owned Chromium for Pomerado: ${detail}`);
  }
}

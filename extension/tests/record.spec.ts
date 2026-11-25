import { test, expect, chromium, type BrowserContext, type Page } from "@playwright/test";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const dist = path.join(root, "dist");
const reportPath = path.join(root, "..", "data", "live_record_e2e_report.json");
const BACKEND = "https://browser.129-159-156-75.sslip.io";

async function extensionId(context: BrowserContext): Promise<string> {
  let [sw] = context.serviceWorkers();
  if (!sw) sw = await context.waitForEvent("serviceworker");
  return new URL(sw.url()).host;
}

async function openPanel(context: BrowserContext, extId: string): Promise<Page> {
  const existing = context.pages().find((p) => p.url().includes("sidepanel.html"));
  if (existing) {
    await existing.bringToFront();
    return existing;
  }
  const page = await context.newPage();
  await page.goto(`chrome-extension://${extId}/sidepanel.html`);
  await expect(page.getByRole("heading", { name: "Installed" })).toBeVisible();
  return page;
}

async function acceptCookies(page: Page): Promise<void> {
  const candidates = [
    page.getByRole("button", { name: /accept (all|additional )?cookies/i }),
    page.getByRole("button", { name: /yes,? i agree/i }),
    page.getByRole("button", { name: /^agree$/i }),
    page.locator("#bbccookies-accept-button, #bbccookies-continue-button, #waitForMe"),
    page.getByRole("button", { name: /reject (all|additional )?cookies/i }),
  ];
  const deadline = Date.now() + 8000;
  while (Date.now() < deadline) {
    for (const loc of candidates) {
      const btn = loc.first();
      if (await btn.isVisible().catch(() => false)) {
        await btn.click({ timeout: 2000 }).catch(() => undefined);
        await page.waitForTimeout(500);
        return;
      }
    }
    await page.waitForTimeout(250);
  }
}

async function clickIfVisible(page: Page, locator: ReturnType<Page["locator"]>, times = 1): Promise<boolean> {
  const el = locator.first();
  if (!(await el.isVisible().catch(() => false))) return false;
  for (let i = 0; i < times; i += 1) {
    await el.click({ timeout: 5000 }).catch(() => undefined);
    await page.waitForTimeout(400);
  }
  return true;
}

async function openTeamFixtures(page: Page, slug: string, name: string): Promise<void> {
  const teamHref = page.locator(`a[href*="/sport/football/teams/${slug}"]`).first();
  if (await teamHref.isVisible().catch(() => false)) {
    await teamHref.click();
  } else {
    const teams = page.getByRole("link", { name: /^teams$/i }).first();
    if (await teams.isVisible().catch(() => false)) {
      await teams.click();
      await page.waitForTimeout(800);
      const again = page.locator(`a[href*="/sport/football/teams/${slug}"]`).first();
      if (await again.isVisible({ timeout: 10000 }).catch(() => false)) {
        await again.click();
      } else {
        await page.goto(`https://www.bbc.com/sport/football/teams/${slug}`, { waitUntil: "domcontentloaded" });
      }
    } else {
      await page.goto(`https://www.bbc.com/sport/football/teams/${slug}`, { waitUntil: "domcontentloaded" });
    }
  }
  await page.waitForURL(new RegExp(`/teams/${slug}`), { timeout: 25000 }).catch(() => undefined);
  const fixtures = page.locator(`a[href*="/sport/football/teams/${slug}/scores-fixtures"]`).first();
  const fixturesText = page.getByRole("link", { name: /scores\s*(&|and)\s*fixtures/i }).first();
  if (await fixtures.isVisible().catch(() => false)) {
    await fixtures.click();
  } else if (await fixturesText.isVisible().catch(() => false)) {
    await fixturesText.click();
  } else if (!/scores-fixtures/.test(page.url())) {
    await page.goto(`https://www.bbc.com/sport/football/teams/${slug}/scores-fixtures`, { waitUntil: "domcontentloaded" });
  }
  await page.waitForURL(/scores-fixtures/, { timeout: 25000 });
  void name;
}

test.describe.serial("record a BBC Sport workflow", () => {
  test.setTimeout(240_000);
  let context: BrowserContext;
  let extId: string;
  const report: Record<string, unknown> = { started_at: new Date().toISOString() };

  test.beforeAll(async () => {
    const token = process.env.BROWSER_PLATFORM_USER_TOKEN || "";
    if (!token) throw new Error("BROWSER_PLATFORM_USER_TOKEN is not set");
    const userDataDir = mkdtempSync(path.join(tmpdir(), "bp-rec-"));
    context = await chromium.launchPersistentContext(userDataDir, {
      headless: false,
      args: [`--disable-extensions-except=${dist}`, `--load-extension=${dist}`, "--no-first-run", "--no-default-browser-check"],
    });
    extId = await extensionId(context);
  });

  test.afterAll(async () => {
    try {
      mkdirSync(path.dirname(reportPath), { recursive: true });
      writeFileSync(reportPath, JSON.stringify(report, null, 2));
    } catch {
      /* ignore */
    }
    await context?.close();
  });

  test("records, prunes redundant clicks, parameterizes the team, replays with another team", async () => {
    const token = process.env.BROWSER_PLATFORM_USER_TOKEN || "";
    const panel = await openPanel(context, extId);
    await panel.getByRole("button", { name: "Settings" }).click();
    await panel.locator('input[name="backendUrl"]').fill(BACKEND);
    await panel.locator('input[name="marketToken"]').fill(token);
    await panel.getByRole("button", { name: "Save" }).click();
    await expect(panel.getByText("Saved on this computer only.")).toBeVisible();

    const bbc = await context.newPage();
    await bbc.goto("https://www.bbc.com/sport/football", { waitUntil: "domcontentloaded", timeout: 60000 });
    await acceptCookies(bbc);
    await bbc.waitForTimeout(800);

    await panel.bringToFront();
    await panel.getByRole("button", { name: "Record" }).click();
    await panel.locator("#record-goal").fill("Find the fixtures for a football team");
    await panel.locator("#record-name").fill("football_fixtures_recorded");
    await panel.getByRole("button", { name: "Start recording" }).click();
    await expect(panel.getByRole("button", { name: /Stop recording/i })).toBeVisible({ timeout: 20000 });

    await bbc.bringToFront();
    await bbc.waitForTimeout(600);
    await clickIfVisible(bbc, bbc.getByRole("link", { name: /skip to (content|main content)/i }));
    await clickIfVisible(bbc, bbc.locator("h1"), 1);
    const scores = bbc.getByRole("link", { name: /scores\s*(&|and)\s*fixtures/i });
    await clickIfVisible(bbc, scores, 2);
    await bbc.waitForTimeout(500);
    await openTeamFixtures(bbc, "arsenal", "Arsenal");
    await bbc.waitForTimeout(800);
    const recordedUrl = bbc.url();
    report.recorded_url = recordedUrl;

    await panel.bringToFront();
    const stopStarted = Date.now();
    await panel.getByRole("button", { name: /Stop recording/i }).click();
    await expect(panel.getByRole("heading", { name: "Review steps" })).toBeVisible({ timeout: 120000 });
    report.clean_ms = Date.now() - stopStarted;

    const usage = ((await panel.locator(".card .muted").allTextContents()) || []).join(" | ");
    report.usage_text = usage;
    const statsLine = usage.match(/(\d+)\s+recorded steps\s+→\s+(\d+)\s+kept/i);
    if (statsLine) {
      report.steps_recorded = Number(statsLine[1]);
      report.steps_kept = Number(statsLine[2]);
    }
    const dropped = await panel.locator(".rec-step.dropped").count();
    const kept = await panel.locator(".rec-step.kept").count();
    report.dropped_shown = dropped;
    report.kept_shown = kept;
    expect(dropped, "redundant clicks should be listed as greyed-out / dropped").toBeGreaterThan(0);
    expect(kept).toBeGreaterThan(0);
    if (statsLine) expect(Number(statsLine[2])).toBeLessThan(Number(statsLine[1]));

    const panelText = await panel.locator(".page").innerText();
    report.review_excerpt = panelText.slice(0, 1500);
    expect(panelText, "kept steps should bind the team in the URL").toMatch(/\{team/i);
    const paramName = panel.locator('input[data-field="name"]').first();
    const paramExample = panel.locator('input[data-field="example"]').first();
    await expect(paramName).toBeVisible();
    await expect(paramName).toHaveValue(/team/i);
    const example = (await paramExample.inputValue().catch(() => "")) || "";
    report.param_example = example;
    expect(example, "team example should be the recorded club").toMatch(/arsenal/i);

    const saveStarted = Date.now();
    await panel.getByRole("button", { name: "Save workflow" }).click();
    await expect(panel.getByText(/Saved to your library/i)).toBeVisible({ timeout: 60000 });
    report.save_ms = Date.now() - saveStarted;
    await expect(panel.getByRole("button", { name: "Run in current tab" })).toBeVisible({ timeout: 20000 });

    const teamInput = panel.locator('input[name="team"], input[name="Team"]').first();
    if (await teamInput.count()) {
      await teamInput.fill("Chelsea");
    } else {
      const labelled = panel.locator("input").filter({ has: panel.locator("xpath=..") });
      const inputs = panel.locator("#run-form input");
      const n = await inputs.count();
      let filled = false;
      for (let i = 0; i < n; i += 1) {
        const name = (await inputs.nth(i).getAttribute("name")) || "";
        if (/team|value|term/i.test(name)) {
          await inputs.nth(i).fill("Chelsea");
          filled = true;
        }
      }
      expect(filled, "run form should have a team (or similar) input").toBeTruthy();
    }

    await bbc.bringToFront();
    await panel.bringToFront();
    const replayStarted = Date.now();
    await panel.getByRole("button", { name: "Run in current tab" }).click();
    await expect(panel.locator(".step.ok, .step.fail, .step.hitl").first()).toBeVisible({ timeout: 60000 });
    report.replay_ms = Date.now() - replayStarted;
    const fail = await panel.locator(".step.fail").count();
    const hitl = await panel.locator(".step.hitl").count();
    report.replay_fail_steps = fail;
    report.replay_hitl_steps = hitl;
    const replayTrace = await panel.locator(".trace").innerText().catch(() => "");
    report.replay_trace = replayTrace.slice(0, 2000);

    const deadline = Date.now() + 45000;
    let replayUrl = "";
    let replayTitle = "";
    while (Date.now() < deadline) {
      replayUrl = bbc.url();
      replayTitle = await bbc.title().catch(() => "");
      if (/\/teams\/chelsea/i.test(replayUrl) || /chelsea/i.test(replayTitle)) break;
      await bbc.waitForTimeout(400);
    }
    report.replay_url = replayUrl;
    report.replay_title = replayTitle;
    expect(
      /\/teams\/chelsea/i.test(replayUrl) || /chelsea/i.test(replayTitle),
      `replay should land on Chelsea fixtures, got ${replayUrl} / ${replayTitle}`,
    ).toBeTruthy();
    expect(fail, `replay should not fail steps: ${replayTrace}`).toBe(0);
    report.ok = true;
    report.finished_at = new Date().toISOString();
  });
});

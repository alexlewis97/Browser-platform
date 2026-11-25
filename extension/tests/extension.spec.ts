import { test, expect, chromium, type BrowserContext, type Page } from "@playwright/test";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const dist = path.join(root, "dist");
const fixtures = path.join(root, "fixtures");

function startFixtureServer(): Promise<{ url: string; close: () => Promise<void> }> {
  const attendance = readFileSync(path.join(fixtures, "attendance.html"));
  const server = createServer((req: IncomingMessage, res: ServerResponse) => {
    const url = req.url || "/";
    if (url.startsWith("/attendance")) {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(attendance);
      return;
    }
    res.writeHead(404);
    res.end("not found");
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address();
      const port = typeof addr === "object" && addr ? addr.port : 0;
      resolve({
        url: `http://127.0.0.1:${port}`,
        close: () => new Promise((r, j) => server.close((err) => (err ? j(err) : r()))),
      });
    });
  });
}

async function openExtension(context: BrowserContext, extId: string): Promise<Page> {
  const page = await context.newPage();
  await page.goto(`chrome-extension://${extId}/sidepanel.html`);
  await expect(page.getByRole("heading", { name: "Installed" })).toBeVisible();
  return page;
}


async function attendanceResult(context: BrowserContext): Promise<Page> {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    for (const p of context.pages()) {
      if (!p.url().includes("/attendance")) continue;
      const text = (await p.locator("#result").textContent().catch(() => "")) || "";
      if (/John Smith: Present|Anna Cohen: Late/.test(text)) return p;
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  const pages = context.pages().filter((p) => p.url().includes("/attendance"));
  if (pages[0]) return pages[0];
  throw new Error("No attendance page found");
}

async function extensionId(context: BrowserContext): Promise<string> {
  let [sw] = context.serviceWorkers();
  if (!sw) sw = await context.waitForEvent("serviceworker");
  const host = new URL(sw.url()).host;
  return host;
}

test.describe.serial("extension replay", () => {
  let context: BrowserContext;
  let extId: string;
  let fixture: { url: string; close: () => Promise<void> };

  test.beforeAll(async () => {
    fixture = await startFixtureServer();
    const userDataDir = mkdtempSync(path.join(tmpdir(), "bp-ext-"));
    context = await chromium.launchPersistentContext(userDataDir, {
      headless: false,
      args: [
        `--disable-extensions-except=${dist}`,
        `--load-extension=${dist}`,
        "--no-first-run",
        "--no-default-browser-check",
      ],
    });
    extId = await extensionId(context);
  });

  test.afterAll(async () => {
    await context?.close();
    await fixture?.close();
  });

  test("attendance fixture: required inputs, fuzzy typo, extract", async () => {
    const panel = await openExtension(context, extId);
    const jsonPath = path.join(fixtures, "attendance.workflow.json");
    await panel.locator("#import-file").setInputFiles(jsonPath);
    await expect(panel.getByText("Imported. You can run it from this list.")).toBeVisible();
    await panel.getByRole("article").getByRole("button", { name: "Run" }).click();
    await expect(panel.getByRole("heading", { name: "attendance_mark" })).toBeVisible();
    await expect(panel.getByText("Name", { exact: false }).first()).toBeVisible();

    // Block until required fields are filled (URL is derived from {url}).
    await panel.getByRole("button", { name: "Run in current tab" }).click();
    await expect(panel.getByText(/Fill required fields/i)).toBeVisible();

    const page = await context.newPage();
    await page.goto(`${fixture.url}/attendance`);
    await page.bringToFront();

    await panel.bringToFront();
    await panel.locator('input[name="name"]').fill("Jon Smith");
    await panel.locator('input[name="status"]').fill("Present");
    await panel.locator('input[name="url"]').fill(`${fixture.url}/attendance`);
    await panel.getByRole("button", { name: "Run in current tab" }).click();
    await expect(panel.locator(".step.ok").first()).toBeVisible({ timeout: 20000 });
    await expect(panel.getByText(/John Smith: Present|John Smith selected/)).toBeVisible({ timeout: 20000 });
    const automated = await attendanceResult(context);
    await expect(automated.locator("#result")).toHaveText(/John Smith: Present/);
  });

  test("HITL on ambiguous name, then alias is reused", async () => {
    const panel = context.pages().find((p) => p.url().includes("sidepanel.html")) || (await openExtension(context, extId));
    await panel.bringToFront();
    if (!(await panel.getByRole("heading", { name: "attendance_mark" }).count())) {
      await panel.getByRole("button", { name: "Installed" }).click();
      await panel.getByRole("article").getByRole("button", { name: "Run" }).click();
    }
    const page = context.pages().find((p) => p.url().includes("/attendance")) || (await context.newPage());
    if (!page.url().includes("/attendance")) await page.goto(`${fixture.url}/attendance`);
    const roster = context.pages().find((p) => p.url().includes("/attendance")) || page;
    await roster.evaluate(() => {
      document.querySelectorAll("tr.selected").forEach((r) => r.classList.remove("selected"));
      const result = document.getElementById("result");
      if (result) result.textContent = "No one marked yet.";
      document.querySelectorAll("select").forEach((s) => ((s as HTMLSelectElement).value = ""));
    });
    await panel.locator('input[name="name"]').fill("Ann Cohen");
    await panel.locator('input[name="status"]').fill("Late");
    await panel.locator('input[name="url"]').fill(`${fixture.url}/attendance`);
    await panel.getByRole("button", { name: "Run in current tab" }).click();
    await expect(panel.getByRole("heading", { name: "Choose a match" })).toBeVisible({ timeout: 20000 });
    await panel.getByRole("button", { name: /Anna Cohen/ }).click();
    await panel.getByRole("button", { name: "Use this match" }).click();
    const automated = await attendanceResult(context);
    await expect(automated.locator("#result")).toHaveText(/Anna Cohen: Late/, { timeout: 20000 });

    await automated.evaluate(() => {
      document.querySelectorAll("tr.selected").forEach((r) => r.classList.remove("selected"));
      const result = document.getElementById("result");
      if (result) result.textContent = "No one marked yet.";
      document.querySelectorAll("select").forEach((s) => ((s as HTMLSelectElement).value = ""));
    });
    await panel.getByRole("button", { name: "Run in current tab" }).click();
    const automated2 = await attendanceResult(context);
    await expect(automated2.locator("#result")).toHaveText(/Anna Cohen: Late/, { timeout: 20000 });
    await expect(panel.getByRole("heading", { name: "Choose a match" })).toHaveCount(0);
  });

  test("live Wikipedia search extracts a paragraph", async () => {
    const panel = context.pages().find((p) => p.url().includes("sidepanel.html")) || (await openExtension(context, extId));
    await panel.getByRole("button", { name: "Installed" }).click();
    await panel.locator("#import-file").setInputFiles(path.join(fixtures, "wikipedia_search.workflow.json"));
    await expect(panel.getByText("Imported. You can run it from this list.")).toBeVisible();
    await panel.getByRole("article").filter({ hasText: "wikipedia_search" }).getByRole("button", { name: "Run" }).click();
    const wiki = await context.newPage();
    await wiki.goto("about:blank");
    await panel.bringToFront();
    await panel.locator('input[name="term"]').fill("browser automation");
    await panel.getByRole("button", { name: "Run in current tab" }).click();
    try {
      await expect(panel.locator(".step.ok")).toHaveCount(5, { timeout: 60000 });
    } catch (err) {
      console.log(await panel.locator(".page").innerText());
      throw err;
    }
    const extract = panel.locator(".step.ok").last();
    await expect(extract).toContainText(/automat|browser|software|computer|script/i);
  });
  test("publish JSON from Market tab, list, install (live marketplace)", async () => {
    const token = process.env.BROWSER_PLATFORM_USER_TOKEN || "";
    test.skip(!token, "BROWSER_PLATFORM_USER_TOKEN not set");
    const panel = context.pages().find((p) => p.url().includes("sidepanel.html")) || (await openExtension(context, extId));
    await panel.bringToFront();
    await panel.getByRole("button", { name: "Settings" }).click();
    await panel.locator('input[name="marketToken"]').fill(token);
    await panel.getByRole("button", { name: "Save" }).click();
    await expect(panel.getByText("Saved on this computer only.")).toBeVisible();

    await panel.getByRole("button", { name: "Installed" }).click();
    const name = `e2e_publish_${Date.now()}`;
    const def = JSON.parse(readFileSync(path.join(fixtures, "attendance.workflow.json"), "utf8"));
    def.name = name;
    const tmp = path.join(mkdtempSync(path.join(tmpdir(), "bp-pub-")), "wf.workflow.json");
    writeFileSync(tmp, JSON.stringify(def));
    await panel.locator("#import-file").setInputFiles(tmp);
    await expect(panel.getByText("Imported. You can run it from this list.")).toBeVisible();

    await panel.getByRole("button", { name: "Market" }).click();
    await expect(panel.locator("#ext-market-source")).toBeVisible({ timeout: 20000 });
    await panel.locator("#ext-market-source").selectOption({ label: name });
    await expect(panel.locator('#ext-publish-form input[name="name"]')).toHaveValue(name);
    await panel.locator("#ext-publish-form").getByRole("button", { name: "Publish" }).click();
    await expect(panel.getByText(new RegExp(`Published ${name} v1`))).toBeVisible({ timeout: 30000 });
    await expect(panel.locator("#ext-publish-form").getByRole("button", { name: "Update" })).toBeVisible();
    await panel.locator("#ext-publish-form").getByRole("button", { name: "Update" }).click();
    await expect(panel.getByText(new RegExp(`Published ${name} v2`))).toBeVisible({ timeout: 30000 });

    const card = panel.getByRole("article").filter({ hasText: name });
    await expect(card).toBeVisible();
    await panel.getByRole("button", { name: "Installed" }).click();
    panel.once("dialog", (d) => void d.accept());
    await panel.getByRole("article").filter({ hasText: name }).getByRole("button", { name: "Remove" }).click();
    await expect(panel.getByRole("article").filter({ hasText: name })).toHaveCount(0);
    await panel.getByRole("button", { name: "Market" }).click();
    await panel.getByRole("article").filter({ hasText: name }).getByRole("button", { name: "Install" }).click();
    await expect(panel.getByText("Installed. Open it from the Installed tab to run.")).toBeVisible({ timeout: 30000 });
    await panel.getByRole("button", { name: "Installed" }).click();
    await expect(panel.getByRole("article").filter({ hasText: name })).toContainText("v2");

    const listingId = name.replaceAll("_", "-");
    const res = await fetch(`https://browser-platform-web.vercel.app/api/market/${encodeURIComponent(listingId)}`, {
      method: "DELETE",
      headers: { "X-Browser-Platform-Token": token },
    });
    expect(res.ok, `cleanup delete ${res.status}`).toBeTruthy();
  });
});

/**
 * Headless check: the run site talks to the extension over the postMessage
 * bridge (bp-run-request), pre-fills explore defaults, and shows results.
 * A stub extension is injected; no real Chrome extension is required.
 */
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFileSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const site = path.join(root, "site");

const workflow = {
  id: "demo-month",
  name: "BBC month",
  description: "Read fixtures for a month",
  version: "1",
  published: true,
  input_schema: [
    { name: "month", type: "string", required: false, default: "2026-11", label: "חודש", description: "Optional explore default" },
  ],
  definition: {
    name: "BBC month",
    description: "Read fixtures for a month",
    version: "1",
    published: true,
    input_schema: [
      { name: "month", type: "string", required: false, default: "2026-11", label: "חודש", description: "Optional explore default" },
    ],
    steps: [
      { type: "navigation", url: "https://example.com/{{month}}" },
      { type: "extract", extractionGoal: "heading", output: "heading" },
    ],
  },
};

function contentType(file) {
  if (file.endsWith(".html")) return "text/html; charset=utf-8";
  if (file.endsWith(".js")) return "text/javascript; charset=utf-8";
  if (file.endsWith(".css")) return "text/css; charset=utf-8";
  if (file.endsWith(".json")) return "application/json; charset=utf-8";
  if (file.endsWith(".zip")) return "application/zip";
  return "application/octet-stream";
}

function startServers() {
  const api = createServer((req, res) => {
    const url = req.url || "/";
    res.setHeader("access-control-allow-origin", "*");
    if (req.method === "OPTIONS") {
      res.writeHead(204);
      res.end();
      return;
    }
    if (url === "/api/catalog") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ workflows: [{ ...workflow, definition: undefined }] }));
      return;
    }
    if (url.startsWith("/api/catalog/")) {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(workflow));
      return;
    }
    res.writeHead(404);
    res.end("not found");
  });

  const web = createServer((req, res) => {
    const url = new URL(req.url || "/", "http://127.0.0.1");
    if (url.pathname === "/backend.json") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ backendUrl: apiUrl }));
      return;
    }
    let file = url.pathname === "/" ? "/index.html" : url.pathname;
    const target = path.join(site, file);
    if (!target.startsWith(site) || !existsSync(target)) {
      const index = path.join(site, "index.html");
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      res.end(readFileSync(index));
      return;
    }
    res.writeHead(200, { "content-type": contentType(target) });
    res.end(readFileSync(target));
  });

  let apiUrl = "";
  return new Promise((resolve) => {
    api.listen(0, "127.0.0.1", () => {
      const port = api.address().port;
      apiUrl = `http://127.0.0.1:${port}`;
      web.listen(0, "127.0.0.1", () => {
        const webPort = web.address().port;
        resolve({
          apiUrl,
          webUrl: `http://127.0.0.1:${webPort}`,
          close: () => Promise.all([
            new Promise((r) => api.close(() => r())),
            new Promise((r) => web.close(() => r())),
          ]),
        });
      });
    });
  });
}

const stubExtension = () => {
  window.__runRequests = [];
  window.postMessage({ source: "bp-ext", direction: "hello", ok: true }, "*");
  window.addEventListener("message", (event) => {
    const data = event.data;
    if (!data || data.source !== "bp-web" || data.direction !== "request") return;
    if (data.kind === "bp-web-ping") {
      window.postMessage({ source: "bp-ext", direction: "response", id: data.id, result: { ok: true, extension: true } }, "*");
      return;
    }
    if (data.kind === "bp-run-request") {
      window.__runRequests.push(data.payload);
      window.postMessage({
        source: "bp-ext",
        direction: "progress",
        kind: "bp-run",
        run: { status: "running", traces: [{ index: 0, type: "navigation", status: "running", description: "open" }] },
      }, "*");
      setTimeout(() => {
        window.postMessage({
          source: "bp-ext",
          direction: "response",
          id: data.id,
          result: {
            ok: true,
            confirmed: true,
            run: {
              status: "ok",
              traces: [
                { index: 0, type: "navigation", status: "ok", description: "open" },
                { index: 1, type: "extract", status: "ok", description: "heading", output: "November fixtures" },
              ],
              outputs: { heading: "November fixtures" },
            },
          },
        }, "*");
      }, 40);
    }
  });
};

async function main() {
  if (!existsSync(path.join(site, "index.html"))) {
    throw new Error("web-run/site is missing — run npm run build first");
  }
  const { webUrl, close } = await startServers();
  const playwrightPath = path.join(root, "../extension/node_modules/playwright/index.mjs");
  const { chromium } = existsSync(playwrightPath) ? await import(playwrightPath) : await import("playwright");
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_PATH || '/usr/bin/google-chrome-stable' });
  const page = await browser.newPage();
  await page.addInitScript(stubExtension);
  await page.goto(webUrl, { waitUntil: "networkidle" });
  await page.waitForSelector("text=BBC month");
  const missing = await page.locator("text=התוסף לא מותקן").count();
  assert.equal(missing, 0, "stub extension should be detected");
  await page.click("text=BBC month");
  const month = page.locator('input[name="month"]');
  await month.waitFor();
  assert.equal(await month.inputValue(), "2026-11");
  await page.click('button[type="submit"]');
  await page.waitForURL(/#\/runs\//, { timeout: 10000 });
  await page.waitForSelector("text=November fixtures");
  const requests = await page.evaluate(() => window.__runRequests);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].id, "demo-month");
  assert.equal(requests[0].values.month, "2026-11");
  assert.equal(requests[0].definition.name, "BBC month");
  const adminUi = await page.locator("text=ממשימה").count();
  assert.equal(adminUi, 0);
  await browser.close();
  await close();
  console.log("run-site bridge e2e passed");
}

main().catch(async (err) => {
  console.error(err);
  process.exit(1);
});

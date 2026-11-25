#!/usr/bin/env node
/**
 * Long-lived Playwright Chromium server for official Pomerado `--endpoint`.
 * Stock Pomerado refuses CDP attach (it would close other tabs / reset site data).
 * This process stays up so mint and run sessions reconnect to the same browser.
 * The websocket endpoint is written to the path in POMERADO_ENDPOINT_FILE.
 */

import { writeFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { chromium } from "playwright";

const endpointFile = (process.env.POMERADO_ENDPOINT_FILE || "").trim();
const headed = process.env.POMERADO_HEADED === "1";
// Playwright forbids --user-data-dir on launchServer / launch(). Official login
// persistence is the minted artifact's sign-in replay. This process itself stays
// running so mint and run reconnect to the same browser server.
const args = ["--disable-dev-shm-usage", "--no-sandbox"];

const executable = (process.env.POMERADO_BROWSER_EXECUTABLE || "").trim();
const server = await chromium.launchServer({
  headless: !headed,
  args,
  ...(executable ? { executablePath: executable } : {}),
});
const endpoint = server.wsEndpoint();
if (endpointFile) {
  mkdirSync(dirname(endpointFile), { recursive: true });
  writeFileSync(endpointFile, endpoint + "\n");
}
process.stdout.write(endpoint + "\n");

const shutdown = async () => {
  try {
    await server.close();
  } finally {
    process.exit(0);
  }
};
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);

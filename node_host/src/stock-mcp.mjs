#!/usr/bin/env node
/**
 * Official Pomerado MCP host (stock minter, default OpenAI models, hosted tools).
 *
 * Starts `pomerado-mcp mint` via the package's startMcpCli. When
 * POMERADO_BROWSER_ENDPOINT is set, it is passed as official `--endpoint`
 * (a Playwright launchServer websocket). Stock Pomerado refuses CDP attach.
 *
 * OPENAI_API_KEY is read from the environment by stock Pomerado. Never log it.
 */

import { startMcpCli } from "pomerado";

const endpoint = (process.env.POMERADO_BROWSER_ENDPOINT || "").trim();
let args = process.argv.slice(2);
if (args.length === 0 || (args[0] !== "mint" && args[0] !== "serve" && !args[0].startsWith("--"))) {
  args = ["mint", ...args];
} else if (args[0].startsWith("--")) {
  args = ["mint", ...args];
}
if (endpoint && !args.includes("--endpoint")) {
  args.push("--endpoint", endpoint);
}
startMcpCli(args, {});

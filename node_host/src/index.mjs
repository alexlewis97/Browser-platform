#!/usr/bin/env node
/**
 * Custom Pomerado host.
 *
 *  - Mint / review LLM calls go to https://openrouter.ai/api/v1 (never api.openai.com).
 *  - Mint and artifact runs use a Pomerado-owned Chromium. --cdp is health-checked
 *    only: we never pass it to createPomerado (stock runs close other tabs).
 *  - When a live OpenRouter key is present, minting is delegated to createPomerado.
 */

import { mkdirSync, writeFileSync, readFileSync, existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { startMcpServer } from "./mcp-stdio.mjs";
import {
  mintWithPomerado,
  runWithPomerado,
  pomeradoModuleInfo,
  looksLikeLiveKey,
  sanitizeForClient,
  OPENROUTER_BASE_URL,
} from "./pomerado-bridge.mjs";
import { cdpHealth, cdpPolicy } from "./cdp-attach.mjs";

const args = parseArgs(process.argv.slice(2));
const root = resolve(args.root || join(process.cwd(), "data", "integrations"));
mkdirSync(root, { recursive: true });
const model = args.model || process.env.POMERADO_MODEL || "qwen/qwen3.7-flash";
const cdpUrl = args.cdp || process.env.BROWSER_PLATFORM_CDP_URL || "";
const jobs = new Map();

const tools = [
  {
    name: "capabilities",
    description: "Report whether the real pomerado package is wired and how browsers are used.",
    inputSchema: { type: "object", properties: {} },
    handler: async () => {
      const info = await pomeradoModuleInfo();
      return {
        pomerado: info,
        openrouter: { base_url: OPENROUTER_BASE_URL, model },
        cdp: cdpPolicy(cdpUrl),
        host: "browser-platform-pomerado-host",
      };
    },
  },
  {
    name: "cdp_health",
    description: "Connect to --cdp, list pages, disconnect. Never closes tabs or clears site data.",
    inputSchema: { type: "object", properties: { cdp: { type: "string" } } },
    handler: async ({ cdp }) => {
      try {
        return await cdpHealth(cdp || cdpUrl);
      } catch (err) {
        return { ok: false, error: String(err?.message || err) };
      }
    },
  },
  {
    name: "mint",
    description: "Start minting a deterministic site tool from a natural-language task (OpenRouter).",
    inputSchema: {
      type: "object",
      properties: {
        name: { type: "string" },
        url: { type: "string" },
        intent: { type: "string" },
        effect: { type: "string" },
        input: { type: "object" },
        api_key: { type: "string" },
        model: { type: "string" },
      },
      required: ["name", "url", "intent"],
    },
    handler: async (input) => {
      const jobId = randomUUID();
      const apiKey = input.api_key || process.env.OPENROUTER_API_KEY || process.env.OPENAI_API_KEY || "";
      const jobModel = input.model || model;
      // Never store the key on the job object (it is returned to the client).
      const request = {
        name: input.name,
        url: input.url,
        intent: input.intent,
        effect: input.effect || "read",
        input: input.input || {},
        model: jobModel,
      };
      const job = {
        job_id: jobId,
        status: "running",
        request,
        openrouter: true,
        base_url: OPENROUTER_BASE_URL,
        cdp: cdpPolicy(cdpUrl),
        closeOtherTabs: false,
        resetSiteData: false,
        pomerado: true,
      };
      jobs.set(jobId, job);

      const info = await pomeradoModuleInfo();
      job.pomerado_package = info;

      if (!info.available || !info.createPomerado) {
        const dir = writeStub(request, jobModel, info.reason || "pomerado not installed");
        job.status = "completed";
        job.output = {
          build: "stub",
          integration: { directory: dir, configPath: join(dir, "pomerado.json") },
          reason: info.reason || "pomerado not installed; stub artifact written",
        };
        return publicJob(job);
      }

      if (!looksLikeLiveKey(apiKey)) {
        job.status = "incomplete";
        job.output = {
          build: "incomplete",
          reason: "no live OpenRouter key; createPomerado is available but mint was not started",
          pomerado_package: info,
          openrouter: true,
          base_url: OPENROUTER_BASE_URL,
        };
        return publicJob(job);
      }

      const dir = join(root, sanitize(request.name || "tool"));
      mkdirSync(dir, { recursive: true });
      job.directory = dir;
      job._task = mintWithPomerado({
        apiKey,
        model: jobModel,
        request,
        job,
        directory: dir,
      })
        .then((result) => {
          if (job.status === "cancelled") return;
          job.status = result.status || (result.ok ? "completed" : "incomplete");
          job.output = result.output;
          job.usage = result.usage;
          job.latency_ms = result.latency_ms;
          job.directory = result.directory;
          job.ok = result.ok;
        })
        .catch((err) => {
          if (job.status === "cancelled") return;
          job.status = "failed";
          job.error = String(err?.message || err);
        });

      return publicJob(job);
    },
  },
  {
    name: "get_job",
    description: "Poll a mint or run job.",
    inputSchema: { type: "object", properties: { job_id: { type: "string" } }, required: ["job_id"] },
    handler: async ({ job_id }) => {
      const job = jobs.get(job_id);
      if (!job) return { job_id, status: "failed", error: "unknown job" };
      return publicJob(job);
    },
  },
  {
    name: "provide_input",
    description: "Answer a question a mint or run asked (login, 2FA, confirm).",
    inputSchema: {
      type: "object",
      properties: { job_id: { type: "string" }, answers: { type: "object" } },
      required: ["job_id"],
    },
    handler: async ({ job_id, answers }) => {
      const job = jobs.get(job_id);
      if (!job) return { job_id, status: "failed", error: "unknown job" };
      if (typeof job._provide === "function") {
        job._provide(answers || {});
      }
      job.answered = true;
      return publicJob(job);
    },
  },
  {
    name: "cancel_job",
    description: "Cancel a running job. Does not close the user's other Chrome tabs.",
    inputSchema: { type: "object", properties: { job_id: { type: "string" } }, required: ["job_id"] },
    handler: async ({ job_id }) => {
      const job = jobs.get(job_id);
      if (!job) return { job_id, status: "failed", error: "unknown job" };
      job.status = "cancelled";
      if (typeof job._cancelInput === "function") {
        try {
          job._cancelInput(new Error("cancelled"));
        } catch {
          // ignore
        }
      }
      return publicJob(job);
    },
  },
  {
    name: "run_artifact",
    description: "Run a previously minted integration with no model calls.",
    inputSchema: {
      type: "object",
      properties: {
        artifact: { type: "string" },
        tool: { type: "string" },
        input: { type: "object" },
        url: { type: "string" },
        effect: { type: "string" },
        intent: { type: "string" },
      },
      required: ["artifact"],
    },
    handler: async ({ artifact, tool, input, url, effect, intent }) => {
      const dir = resolve(artifact);
      const manifestPath = join(dir, "pomerado.json");
      if (!existsSync(manifestPath)) {
        return { ok: false, error: `no pomerado.json at ${dir}`, status: "failed" };
      }
      const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
      if (manifest.minted_via === "openrouter-host-stub" || manifest.build === "stub") {
        return {
          ok: false,
          status: "incomplete",
          error: "stub artifact; re-mint with a live OpenRouter key to run a real tool",
          manifest: sanitizeForClient(manifest),
          input: input || {},
          tool: tool || manifest.name,
        };
      }
      const info = await pomeradoModuleInfo();
      if (!info.available || !info.readArtifact) {
        return {
          ok: false,
          status: "failed",
          error: info.reason || "pomerado package unavailable",
        };
      }
      const jobId = randomUUID();
      const job = {
        job_id: jobId,
        status: "running",
        request: { artifact: dir, tool, input, url, effect, intent },
        pomerado: true,
      };
      jobs.set(jobId, job);
      try {
        const result = await runWithPomerado({
          artifactDir: dir,
          url: url || manifest.url || input?.url,
          intent: intent || manifest.intent || `run ${tool || manifest.name || "tool"}`,
          input: input || {},
          effect: effect || manifest.effect || "read",
          job,
        });
        Object.assign(job, result);
        return { ...publicJob(job), ...result, job_id: jobId, manifest: sanitizeForClient(manifest) };
      } catch (err) {
        job.status = "failed";
        job.error = String(err?.message || err);
        job.ok = false;
        return publicJob(job);
      }
    },
  },
];

startMcpServer({ tools });

function publicJob(job) {
  return sanitizeForClient({
    job_id: job.job_id,
    status: job.status,
    request: job.request,
    openrouter: job.openrouter,
    base_url: job.base_url,
    cdp: job.cdp,
    closeOtherTabs: job.closeOtherTabs,
    resetSiteData: job.resetSiteData,
    pomerado: job.pomerado,
    pomerado_package: job.pomerado_package,
    output: job.output,
    usage: job.usage,
    latency_ms: job.latency_ms,
    directory: job.directory,
    ok: job.ok,
    error: job.error,
    input_request: job.input_request,
    answered: job.answered,
    next: { tool: "get_job", arguments: { job_id: job.job_id } },
  });
}

function writeStub(request, jobModel, reason) {
  const dir = join(root, sanitize(request.name || "tool"));
  mkdirSync(dir, { recursive: true });
  const manifest = {
    name: request.name,
    url: request.url,
    intent: request.intent,
    effect: request.effect || "read",
    model: jobModel,
    minted_via: "openrouter-host-stub",
    build: "stub",
    note: reason,
  };
  writeFileSync(join(dir, "pomerado.json"), JSON.stringify(manifest, null, 2));
  return dir;
}

function sanitize(name) {
  return String(name).toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^[^a-z]/, "t_") || "tool";
}

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const key = a.slice(2);
      const val = argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[++i] : true;
      out[key] = val;
    }
  }
  return out;
}

/**
 * Wire createPomerado when the `pomerado` package is installed.
 *
 * Minting and review models go through OpenRouter (chat completions).
 * Mint and artifact runs use a Pomerado-owned launched Chromium — never the
 * user's default CDP context — because stock mint/run close other tabs.
 */

import { createOpenRouterProvider, looksLikeLiveKey, OPENROUTER_BASE_URL } from "./openrouter-provider.mjs";
import { cdpPolicy, launchOwnedPlaywrightServer } from "./cdp-attach.mjs";

export { looksLikeLiveKey, OPENROUTER_BASE_URL };

export async function pomeradoModuleInfo() {
  try {
    const pomerado = await import("pomerado");
    const pkg = await import("pomerado/package.json", { with: { type: "json" } }).catch(() => null);
    return {
      available: true,
      createPomerado: typeof pomerado.createPomerado === "function",
      readArtifact: typeof pomerado.readArtifact === "function",
      writeArtifact: typeof pomerado.writeArtifact === "function",
      version: pkg?.default?.version || pkg?.version || "installed",
    };
  } catch (err) {
    return { available: false, reason: String(err?.message || err) };
  }
}

export function sanitizeForClient(value) {
  return stripSecrets(value, 0);
}

function stripSecrets(value, depth) {
  if (depth > 12 || value == null) return value;
  if (typeof value === "string") return value;
  if (typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map((item) => stripSecrets(item, depth + 1));
  const out = {};
  for (const [key, val] of Object.entries(value)) {
    if (/^(api_?key|openai_api_key|openrouter_api_key|authorization|secret|token)$/i.test(key)) {
      continue;
    }
    if (key.startsWith("_")) continue;
    try {
      out[key] = stripSecrets(val, depth + 1);
    } catch {
      out[key] = "[unserializable]";
    }
  }
  return out;
}

export function serializeOutcome(outcome) {
  if (outcome == null) return outcome;
  if (typeof outcome !== "object") return outcome;
  const artifact = outcome.artifact
    ? {
        has_artifact: true,
        entrypoint: outcome.artifact.entrypoint,
        inputSchema: outcome.artifact.inputSchema,
        outputSchema: outcome.artifact.outputSchema,
        files: Array.isArray(outcome.artifact.files)
          ? outcome.artifact.files.map((f) => f.path || f)
          : undefined,
      }
    : undefined;
  const known = {
    build: outcome.build,
    effect: outcome.effect,
    publicationRef: outcome.publicationRef,
    summary: outcome.summary,
    assumptions: outcome.assumptions,
    stopReason: outcome.stopReason,
    stopExplanation: outcome.stopExplanation,
    recoveryReason: outcome.recoveryReason,
    hostFailure: outcome.hostFailure,
    blocked: outcome.blocked,
    noResponse: outcome.noResponse,
    possibleCommit: outcome.possibleCommit,
    publicationDenial: outcome.publicationDenial,
    capability_review: outcome.capability_review,
    rejectedCredential: outcome.rejectedCredential,
    artifact,
  };
  const extras = {};
  for (const key of Object.keys(outcome)) {
    if (!(key in known) && key !== "artifact") extras[key] = outcome[key];
  }
  if (Object.keys(extras).length) known.extra_keys = extras;
  known.outcome_keys = Object.keys(outcome);
  return sanitizeForClient(known);
}

/**
 * Park input questions on the job until provide_input answers them.
 */
export async function createJobAsker(job) {
  const { makeInputAsker } = await import("pomerado");
  const { Effect } = await import("effect");
  return makeInputAsker((request) =>
    Effect.tryPromise({
      try: () =>
        new Promise((resolve, reject) => {
          job.status = "needs_input";
          job.input_request = sanitizeForClient(request);
          const timer = setTimeout(() => {
            job._provide = null;
            reject(new Error("input request timed out"));
          }, 10 * 60 * 1000);
          job._provide = (answers) => {
            clearTimeout(timer);
            job._provide = null;
            job.status = "running";
            job.input_request = undefined;
            resolve(answers || {});
          };
          job._cancelInput = (err) => {
            clearTimeout(timer);
            job._provide = null;
            reject(err instanceof Error ? err : new Error(String(err)));
          };
        }),
      catch: (e) => (e instanceof Error ? e : new Error(String(e))),
    })
  );
}

export async function mintWithPomerado({ apiKey, model, request, job, directory }) {
  const { createPomerado, writeArtifact } = await import("pomerado");
  const { Effect } = await import("effect");
  const provider = await createOpenRouterProvider({ apiKey, model });
  const ask = await createJobAsker(job);
  const started = Date.now();
  const owned = await launchOwnedPlaywrightServer();
  const program = Effect.scoped(
    Effect.gen(function* () {
      const session = yield* createPomerado({
        ask,
        minterProvider: provider,
        guardianProvider: provider,
        outcomeReviewerProvider: provider,
        // Owned Chromium via Playwright endpoint — never the user's default CDP context.
        browser: { endpoint: owned.endpoint },
        timeoutMs: Number(process.env.POMERADO_TIMEOUT_MS || 15 * 60 * 1000),
      });
      const outcome = yield* session.mint({
        intent: request.intent,
        url: request.url,
        input: request.input || {},
        effect: request.effect === "write" ? "write" : request.effect === "ask" ? "ask" : "read",
      });
      if (outcome?.artifact) {
        yield* writeArtifact(directory, outcome.artifact);
      }
      return outcome;
    })
  );
  try {
    const outcome = await Effect.runPromise(program);
    const usage = provider.usage?.snapshot?.() || { tokens_in: 0, tokens_out: 0, requests: 0 };
    return {
      ok: outcome?.build === "published",
      status: outcome?.build === "published" ? "completed" : "incomplete",
      output: serializeOutcome(outcome),
      usage,
      latency_ms: Date.now() - started,
      directory,
      pomerado: true,
      openrouter: true,
      base_url: OPENROUTER_BASE_URL,
      browser: owned.executablePath,
    };
  } finally {
    try {
      await owned.server.close();
    } catch {
      // ignore
    }
    try {
      await provider.close?.();
    } catch {
      // ignore
    }
  }
}

export async function runWithPomerado({ artifactDir, url, intent, input, effect, job }) {
  const { createPomerado, readArtifact } = await import("pomerado");
  const { Effect } = await import("effect");
  const ask = await createJobAsker(job);
  const started = Date.now();
  const owned = await launchOwnedPlaywrightServer();
  const program = Effect.scoped(
    Effect.gen(function* () {
      const artifact = yield* readArtifact(artifactDir);
      const session = yield* createPomerado({
        ask,
        browser: { endpoint: owned.endpoint },
      });
      return yield* session.run(artifact, {
        intent: intent || "run minted tool",
        url,
        input: input || {},
        effect: effect === "write" ? "write" : "read",
      });
    })
  );
  try {
    const output = await Effect.runPromise(program);
    return {
      ok: true,
      status: "completed",
      output: sanitizeForClient(output),
      latency_ms: Date.now() - started,
      pomerado: true,
    };
  } finally {
    try {
      await owned.server.close();
    } catch {
      // ignore
    }
  }
}

export { cdpPolicy };

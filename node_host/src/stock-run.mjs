#!/usr/bin/env node
/**
 * Official Pomerado run (no model request) against the dedicated Chromium.
 * Usage: node stock-run.mjs --artifact DIR --url URL [--input JSON] [--intent TEXT]
 * OPENAI_API_KEY is not required. Never log secrets.
 */

import { parseArgs } from "node:util";
import { resolve } from "node:path";
import { createPomerado, readArtifact, makeInputAsker } from "pomerado";
import { Effect } from "effect";

const { values } = parseArgs({
  options: {
    artifact: { type: "string" },
    url: { type: "string" },
    input: { type: "string" },
    intent: { type: "string" },
    effect: { type: "string" },
    "timeout-seconds": { type: "string" },
  },
  strict: false,
});

if (!values.artifact) {
  process.stderr.write("stock-run: --artifact is required\n");
  process.exit(2);
}

const endpoint = (process.env.POMERADO_BROWSER_ENDPOINT || "").trim();
let input = {};
try {
  input = JSON.parse(values.input || "{}");
} catch (err) {
  process.stderr.write("stock-run: --input must be valid JSON\n");
  process.exit(2);
}

const timeoutMs = Number(values["timeout-seconds"] || "180") * 1000;
const started = Date.now();

const program = Effect.scoped(
  Effect.gen(function* () {
    const artifact = yield* readArtifact(resolve(values.artifact));
    const pomerado = yield* createPomerado({
      ask: makeInputAsker(() => Effect.succeed({})),
      timeoutMs: Number.isFinite(timeoutMs) && timeoutMs > 0 ? timeoutMs : 180_000,
      browser: endpoint ? { endpoint } : {},
    });
    return yield* pomerado.run(artifact, {
      url: values.url,
      intent: values.intent || "run minted tool",
      input,
    });
  }),
);

try {
  const output = await Effect.runPromise(program);
  process.stdout.write(
    JSON.stringify({
      ok: true,
      status: "completed",
      output,
      latency_ms: Date.now() - started,
      pomerado: true,
      tokens_in: 0,
      tokens_out: 0,
    }) + "\n",
  );
} catch (err) {
  const message = err instanceof Error ? err.message : String(err);
  process.stdout.write(
    JSON.stringify({
      ok: false,
      status: "failed",
      error: message,
      latency_ms: Date.now() - started,
      pomerado: true,
      tokens_in: 0,
      tokens_out: 0,
    }) + "\n",
  );
  process.exitCode = 1;
}

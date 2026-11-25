import { writeFileSync } from 'node:fs';
/**
 * Point Pomerado's Agents SDK ModelProviders at OpenRouter.
 *
 * Hosted tools (apply_patch, shell, …) are rewritten to ordinary function tools
 * and mapped back so Pomerado's local editor still applies diffs. If the
 * Responses API rejects a request, the same turn is retried as Chat Completions.
 * Usage/cost is tallied without logging the API key.
 */

import {
  isResponsesIncompatible,
  rewriteRequest,
  rewriteResponse,
  stripResponsesOnly,
} from "./openrouter-tools.mjs";
import { fulfillHostToolCalls } from "./workspace-tools.mjs";

export const OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1";

/** USD per 1M tokens (OpenRouter list, checked 24 Nov 2026). Used when usage has no cost field. */
export const MODEL_PRICES_USD_PER_MILLION = {
  "qwen/qwen3.7-flash": { input: 0.03, output: 0.13 },
  "qwen/qwen3-coder-30b-a3b-instruct": { input: 0.07, output: 0.28 },
  "deepseek/deepseek-v4.1-flash": { input: 0.0498, output: 0.1992 },
};

export const MINT_MODEL_PRIMARY = "qwen/qwen3.7-flash";
export const MINT_MODEL_CODER_FALLBACK = "qwen/qwen3-coder-30b-a3b-instruct";

export function openRouterConfig({ apiKey, model, baseUrl = OPENROUTER_BASE_URL } = {}) {
  return {
    apiKey,
    model,
    baseURL: baseUrl,
  };
}

export function looksLikeLiveKey(apiKey) {
  if (typeof apiKey !== "string" || apiKey.length < 24) return false;
  const lower = apiKey.toLowerCase();
  if (/fake|not-used|notused|testkey|should-never|example|placeholder|xxx/.test(lower)) {
    return false;
  }
  return true;
}

export function estimateCostUsd(model, tokensIn, tokensOut, observed) {
  if (typeof observed === "number" && Number.isFinite(observed) && observed > 0) return observed;
  const prices = MODEL_PRICES_USD_PER_MILLION[model];
  if (!prices) return null;
  return (tokensIn / 1_000_000) * prices.input + (tokensOut / 1_000_000) * prices.output;
}


function summarizeRequest(request = {}) {
  const tools = Array.isArray(request.tools)
    ? request.tools.map((tool) => ({
        type: tool?.type,
        name: tool?.name,
        strict: tool?.strict,
        paramKeys: tool?.parameters ? Object.keys(tool.parameters.properties || {}) : undefined,
        additionalProperties: tool?.parameters?.additionalProperties,
      }))
    : [];
  const input = request.input;
  return {
    model: request.model,
    toolCount: tools.length,
    tools,
    toolChoice: request.modelSettings?.toolChoice ?? request.toolChoice,
    reasoning: request.modelSettings?.reasoning,
    maxTokens: request.modelSettings?.maxTokens,
    providerDataKeys: request.providerData ? Object.keys(request.providerData) : [],
    inputTypes: Array.isArray(input) ? input.map((item) => item?.type).slice(0, 30) : typeof input,
    inputLength: Array.isArray(input) ? input.length : undefined,
  };
}

function dumpProviderFailure(error, request, transport, modelName) {
  try {
    const payload = {
      transport,
      model: modelName,
      error: serializeProviderError(error),
      request: summarizeRequest(request),
    };
    writeFileSync(
      process.env.OPENROUTER_ERROR_DUMP || "/workspace/browser-platform/data/openrouter_last_error.json",
      redactSecrets(payload) + "\n",
    );
  } catch {
    // never throw from diagnostics
  }
}

export function redactSecrets(value) {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.replace(/sk-[a-zA-Z0-9-_]{8,}/g, "[redacted]");
}

export function serializeProviderError(error) {
  const err = error && typeof error === "object" ? error : { message: String(error) };
  const body =
    err.error ||
    err.body ||
    err.response?.data ||
    err.cause?.error ||
    err.cause?.message ||
    undefined;
  const payload = {
    message: String(err.message || error || ""),
    status: err.status ?? err.statusCode ?? err.code ?? null,
    type: err.type || err.name || null,
    body: body ?? null,
  };
  try {
    return JSON.parse(redactSecrets(payload));
  } catch {
    return { message: redactSecrets(payload.message) };
  }
}

export function createUsageLedger() {
  const usage = {
    tokens_in: 0,
    tokens_out: 0,
    requests: 0,
    models: [],
    cost_usd: 0,
    transports: [],
    last_errors: [],
  };
  return {
    add(response, modelName, transport) {
      usage.requests += 1;
      if (modelName && !usage.models.includes(modelName)) usage.models.push(modelName);
      if (transport && !usage.transports.includes(transport)) usage.transports.push(transport);
      const u = response?.usage || {};
      const inn = Number(u.inputTokens ?? u.prompt_tokens ?? u.input_tokens ?? 0) || 0;
      const out = Number(u.outputTokens ?? u.completion_tokens ?? u.output_tokens ?? 0) || 0;
      usage.tokens_in += inn;
      usage.tokens_out += out;
      const observed =
        Number(u.cost ?? u.cost_usd ?? response?.providerData?.cost ?? response?.providerData?.usage?.cost) || 0;
      const increment = estimateCostUsd(modelName, inn, out, observed);
      if (typeof increment === "number") usage.cost_usd += increment;
    },
    fail(error, modelName, transport) {
      const entry = { ...(serializeProviderError(error) || {}), model: modelName || null, transport: transport || null };
      usage.last_errors = [...(usage.last_errors || []), entry].slice(-8);
    },
    snapshot() {
      return {
        ...usage,
        models: [...usage.models],
        transports: [...usage.transports],
        last_errors: [...(usage.last_errors || [])],
        cost_usd: Number(usage.cost_usd.toFixed(8)),
      };
    },
  };
}

function isAsyncIterable(value) {
  return value && typeof value[Symbol.asyncIterator] === "function";
}

/**
 * Force every getModel() call onto the configured OpenRouter model, rewrite
 * hosted tools, and fall back from Responses to Chat Completions when needed.
 */
export function wrapProvider({ responsesProvider, chatProvider, provider } = {}, { model, usage } = {}) {
  const ledger = usage || createUsageLedger();
  const responses = responsesProvider || provider;
  const chat = chatProvider || provider;
  return {
    kind: "openrouter-wrapped",
    async getModel(name) {
      const requested = model || name;
      const responsesModel = responses ? await responses.getModel(requested) : null;
      const chatModel = chat ? await chat.getModel(requested) : null;

      const runOnce = async (request) => {
        const { request: rewritten, dropped } = rewriteRequest(request);
        let transport = "responses";
        let response;
        const tryResponses = Boolean(responsesModel);
        if (tryResponses) {
          try {
            response = await responsesModel.getResponse(rewritten);
          } catch (err) {
            ledger.fail?.(err, requested, "responses"); dumpProviderFailure(err, rewritten, "responses", requested);
            if (!chatModel || !isResponsesIncompatible(err)) throw err;
            transport = "chat";
            try {
              response = await chatModel.getResponse(stripResponsesOnly(rewritten));
            } catch (chatErr) {
              ledger.fail?.(chatErr, requested, "chat"); dumpProviderFailure(chatErr, stripResponsesOnly(rewritten), "chat", requested);
              const detail = redactSecrets(
                `${chatErr?.message || chatErr} | responses: ${err?.message || err}`
              );
              const wrapped = new Error(detail);
              wrapped.cause = chatErr;
              wrapped.responsesError = serializeProviderError(err);
              wrapped.chatError = serializeProviderError(chatErr);
              throw wrapped;
            }
          }
        } else if (chatModel) {
          transport = "chat";
          try {
            response = await chatModel.getResponse(stripResponsesOnly(rewritten));
          } catch (err) {
            ledger.fail?.(err, requested, "chat"); dumpProviderFailure(err, stripResponsesOnly(rewritten), "chat", requested);
            throw err;
          }
        } else {
          throw new Error("No OpenRouter model backend is available");
        }
        const out = await fulfillHostToolCalls(rewriteResponse(response));
        if (dropped.length) {
          out.providerData = { ...(out.providerData || {}), droppedHostedTools: dropped, transport };
        } else {
          out.providerData = { ...(out.providerData || {}), transport };
        }
        ledger.add(out, requested, transport);
        return out;
      };

      return {
        async getResponse(request) {
          return runOnce(request);
        },
        async *getStreamedResponse(request) {
          // Mint uses getResponse. Streaming goes through the same rewrite path.
          const result = await runOnce(request);
          yield { type: "response_done", response: result };
        },
      };
    },
    async close() {
      if (typeof responses?.close === "function") await responses.close();
      if (chat && chat !== responses && typeof chat.close === "function") await chat.close();
    },
    usage: ledger,
  };
}

export async function createOpenRouterProvider({ apiKey, model, baseUrl = OPENROUTER_BASE_URL, usage } = {}) {
  const config = openRouterConfig({ apiKey, model, baseUrl });
  const ledger = usage || createUsageLedger();
  try {
    const { OpenAIProvider } = await import("@openai/agents-openai");
    const shared = { apiKey: config.apiKey, baseURL: config.baseURL, strictFeatureValidation: false };
    const responsesProvider = new OpenAIProvider({ ...shared, useResponses: true });
    const chatProvider = new OpenAIProvider({ ...shared, useResponses: false });
    return wrapProvider({ responsesProvider, chatProvider }, { model: config.model, usage: ledger });
  } catch (err) {
    return {
      kind: "openrouter-unavailable",
      reason: String(err?.message || err),
      baseURL: config.baseURL,
      defaultModel: config.model,
      usage: ledger,
      async getModel(name) {
        throw new Error(
          `OpenAIProvider could not be constructed for OpenRouter (${String(err?.message || err)}). Requested model: ${name || config.model}`
        );
      },
    };
  }
}

import { test } from "node:test";
import assert from "node:assert/strict";
import { wrapProvider } from "./openrouter-provider.mjs";

test("wrapProvider rewrites apply_patch and falls back to chat completions", async () => {
  const calls = [];
  const responsesProvider = {
    async getModel() {
      return {
        async getResponse(request) {
          calls.push(["responses", request.tools.map((t) => t.type), request.tools[0]?.name]);
          throw new Error("400 Invalid Responses API request");
        },
      };
    },
  };
  const chatProvider = {
    async getModel() {
      return {
        async getResponse(request) {
          calls.push(["chat", request.tools.map((t) => t.type), request.previousResponseId]);
          return {
            usage: { inputTokens: 10, outputTokens: 4, cost: 0.00001 },
            output: [
              {
                type: "function_call",
                callId: "c9",
                name: "apply_patch",
                arguments: JSON.stringify({ type: "create_file", path: "t.ts", diff: "+t" }),
              },
            ],
          };
        },
      };
    },
  };
  const wrapped = wrapProvider({ responsesProvider, chatProvider }, { model: "qwen/qwen3.7-flash" });
  const model = await wrapped.getModel("ignored-openai-name");
  const response = await model.getResponse({
    previousResponseId: "should-be-stripped-on-fallback",
    tools: [{ type: "apply_patch", name: "apply_patch" }],
    input: "edit the file",
  });
  assert.equal(calls[0][0], "responses");
  assert.deepEqual(calls[0][1], ["function", "function"]);
  assert.equal(calls[0][2], "apply_patch");
  assert.equal(calls[1][0], "chat");
  assert.equal(calls[1][2], undefined);
  assert.equal(response.output[0].type, "apply_patch_call");
  assert.equal(response.output[0].operation.path, "t.ts");
  const snap = wrapped.usage.snapshot();
  assert.equal(snap.requests, 1);
  assert.equal(snap.tokens_in, 10);
  assert.equal(snap.tokens_out, 4);
  assert.ok(snap.cost_usd > 0);
  assert.deepEqual(snap.transports, ["chat"]);
});

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  isResponsesIncompatible,
  normalizeShellCommands,
  parseApplyPatchArguments,
  parsePatchEnvelope,
  rewriteOutputItem,
  rewriteRequest,
  rewriteResponse,
  rewriteTools,
  stripResponsesOnly,
  toCreateDiff,
  sanitizeOperation,
  normalizeReasoning,
  normalizeToolChoice,
  sanitizeProviderData,
} from "./openrouter-tools.mjs";

test("rewriteTools turns apply_patch into a function tool", () => {
  const { tools, dropped } = rewriteTools([
    { type: "apply_patch", name: "apply_patch" },
    { type: "function", name: "execute", description: "run", parameters: { type: "object" }, strict: true },
    { type: "hosted_tool", name: "web_search", providerData: { type: "web_search" } },
  ]);
  assert.equal(tools[0].type, "function");
  assert.equal(tools[0].name, "apply_patch");
  assert.equal(tools[0].parameters.properties.type.enum[0], "create_file");
  assert.equal(tools[1].name, "execute");
  assert.equal(dropped.length, 1);
  assert.equal(dropped[0].name, "web_search");
});

test("rewriteRequest maps apply_patch history to function_call items", () => {
  const { request } = rewriteRequest({
    tools: [{ type: "apply_patch", name: "apply_patch" }],
    input: [
      {
        type: "apply_patch_call",
        callId: "c1",
        status: "completed",
        operation: { type: "create_file", path: "a.ts", diff: "+hi" },
      },
      { type: "apply_patch_call_output", callId: "c1", status: "completed", output: "ok" },
    ],
  });
  assert.equal(request.tools[0].type, "function");
  assert.equal(request.input[0].type, "function_call");
  assert.equal(request.input[0].name, "apply_patch");
  assert.equal(JSON.parse(request.input[0].arguments).path, "a.ts");
  assert.equal(request.input[1].type, "function_call_result");
  assert.equal(request.input[1].output, "ok");
});

test("parseApplyPatchArguments accepts JSON and envelopes", () => {
  assert.deepEqual(
    parseApplyPatchArguments(JSON.stringify({ type: "update_file", path: "x.ts", diff: "@@\n-a\n+b" })),
    [{ type: "update_file", path: "x.ts", diff: "@@\n-a\n+b" }]
  );
  const ops = parsePatchEnvelope(
    "*** Begin Patch\n*** Add File: src/a.ts\n+export const a = 1;\n*** End Patch\n*** Begin Patch\n*** Delete File: src/b.ts\n*** End Patch\n"
  );
  assert.deepEqual(ops, [
    { type: "create_file", path: "src/a.ts", diff: "+export const a = 1;" },
    { type: "delete_file", path: "src/b.ts" },
  ]);
  const many = parseApplyPatchArguments({
    operations: [
      { type: "create_file", path: "a.ts", diff: "+a" },
      { type: "delete_file", path: "b.ts" },
    ],
  });
  assert.equal(many.length, 2);
});

test("rewriteOutputItem turns apply_patch function calls into apply_patch_call", () => {
  const items = rewriteOutputItem({
    type: "function_call",
    callId: "c2",
    name: "apply_patch",
    arguments: JSON.stringify({ type: "create_file", path: "tool.ts", diff: "+x" }),
  });
  assert.equal(items.length, 1);
  assert.equal(items[0].type, "apply_patch_call");
  assert.equal(items[0].operation.path, "tool.ts");
  assert.equal(items[0].callId, "c2");
});

test("rewriteResponse is applied to a full model output list", () => {
  const response = rewriteResponse({
    usage: { inputTokens: 3, outputTokens: 2 },
    output: [
      { type: "message", role: "assistant", content: [{ type: "output_text", text: "editing" }] },
      {
        type: "function_call",
        callId: "c3",
        name: "apply_patch",
        arguments: JSON.stringify({
          operations: [
            { type: "create_file", path: "a.ts", diff: "+a" },
            { type: "create_file", path: "b.ts", diff: "+b" },
          ],
        }),
      },
    ],
  });
  const calls = response.output.filter((item) => item.type === "apply_patch_call");
  assert.equal(calls.length, 2);
  assert.equal(calls[0].callId, "c3__0");
  assert.equal(calls[1].operation.path, "b.ts");
});

test("stripResponsesOnly drops previousResponseId and isResponsesIncompatible matches known errors", () => {
  const stripped = stripResponsesOnly({ previousResponseId: "resp_1", conversationId: "conv", input: "hi" });
  assert.equal(stripped.previousResponseId, undefined);
  assert.equal(stripped.conversationId, undefined);
  assert.equal(isResponsesIncompatible(new Error("400 Invalid Responses API request")), true);
  assert.equal(isResponsesIncompatible(new Error("Hosted tools are not supported with the ChatCompletions API. Got tool type: apply_patch")), true);
  assert.equal(isResponsesIncompatible(new Error("network down")), false);
});

test("toCreateDiff prefixes raw content so the editor can write src/tool.mjs", () => {
  const raw = "export default async function tool() {\n  return { title: document.title };\n}";
  const diff = toCreateDiff(raw);
  assert.equal(diff, "+export default async function tool() {\n+  return { title: document.title };\n+}");
  const already = toCreateDiff(diff);
  assert.equal(already, diff);
  const enveloped = toCreateDiff("*** Begin Patch\n*** Add File: src/tool.mjs\n+export default 1\n*** End Patch");
  assert.equal(enveloped, "+export default 1");
});

test("parseApplyPatchArguments sanitizes raw create_file content", () => {
  const ops = parseApplyPatchArguments(JSON.stringify({ type: "create_file", path: "src/tool.mjs", content: "export default 1" }));
  assert.equal(ops.length, 1);
  assert.equal(ops[0].type, "create_file");
  assert.equal(ops[0].path, "src/tool.mjs");
  assert.equal(ops[0].diff, "+export default 1");
});

test("rewriteOutputItem maps apply_patch function calls to apply_patch_call with V4A diffs", () => {
  const out = rewriteOutputItem({
    type: "function_call",
    name: "apply_patch",
    callId: "call_1",
    arguments: JSON.stringify({ type: "create_file", path: "src/tool.mjs", diff: "export default 1" }),
  });
  assert.equal(out[0].type, "apply_patch_call");
  assert.equal(out[0].operation.diff, "+export default 1");
});

test("rewriteOutputItem maps shell function calls to shell_call with commands[]", () => {
  const out = rewriteOutputItem({
    type: "function_call",
    name: "shell",
    callId: "call_2",
    arguments: JSON.stringify({ command: "ls src" }),
  });
  assert.equal(out[0].type, "shell_call");
  assert.deepEqual(out[0].action.commands, ["ls src"]);
  assert.deepEqual(normalizeShellCommands({ command: ["bash", "-lc", "ls"] }), ["bash", "-lc", "ls"]);
});

test("normalizeReasoning drops Responses-only fields that trigger invalid_prompt", () => {
  assert.deepEqual(normalizeReasoning({ effort: "medium", context: "all_turns", summary: "auto" }), { effort: "medium" });
  assert.equal(normalizeReasoning({ context: "all_turns" }), undefined);
  assert.equal(normalizeToolChoice("apply_patch"), "auto");
  assert.equal(normalizeToolChoice("required"), "required");
  const pd = sanitizeProviderData({ tool_choice: { type: "apply_patch" }, reasoning: { effort: "low", context: "all_turns" } });
  assert.equal(pd.tool_choice, "auto");
  assert.deepEqual(pd.reasoning, { effort: "low" });
});

test("rewriteRequest sanitizes reasoning and toolChoice before the API call", () => {
  const { request } = rewriteRequest({
    tools: [{ type: "apply_patch" }],
    modelSettings: { toolChoice: "apply_patch", reasoning: { effort: "medium", context: "all_turns", summary: "auto" } },
    providerData: { tool_choice: "apply_patch" },
  });
  assert.equal(request.modelSettings.toolChoice, "auto");
  assert.deepEqual(request.modelSettings.reasoning, { effort: "medium" });
  assert.equal(request.providerData.tool_choice, "auto");
});

import { fulfillHostToolCalls, ensureDefineOperation, isListingCommand, listWorkspaceTool } from "./workspace-tools.mjs";

test("rewriteTools injects the read-only list_workspace tool", () => {
  const { tools } = rewriteTools([{ type: "apply_patch", name: "apply_patch" }]);
  assert.equal(tools.some((tool) => tool.name === "list_workspace"), true);
  assert.equal(listWorkspaceTool().type, "function");
});

test("rewriteRequest appends a defineOperation host hint once", () => {
  const { request } = rewriteRequest({
    tools: [{ type: "apply_patch" }],
    input: [{ type: "message", role: "user", content: [{ type: "input_text", text: "mint this" }] }],
  });
  const dumped = JSON.stringify(request.input);
  assert.match(dumped, /defineOperation/);
  assert.match(dumped, /list_workspace/);
  const { request: again } = rewriteRequest(request);
  assert.equal(JSON.stringify(again.input).split("[[pomerado-host-addendum]]").length - 1, 1);
});

test("sanitizeOperation wraps a bare src/tool.mjs export in defineOperation", () => {
  const op = sanitizeOperation({
    type: "create_file",
    path: "src/tool.mjs",
    diff: "export default async function tool() {\n  return { title: document.title };\n}",
  });
  const content = op.diff.split("\n").map((line) => (line.startsWith("+") ? line.slice(1) : line)).join("\n");
  assert.match(content, /defineOperation/);
  assert.match(content, /kernel\.browsers\.playwright\.execute/);
  assert.match(content, /document\.title/);
});

test("ensureDefineOperation is idempotent when defineOperation is already used", () => {
  const src = 'import { defineOperation } from "../../runtime/index.js";\nexport default defineOperation({ name: "t", input: {}, output: {} }, async () => ({}));\n';
  assert.equal(ensureDefineOperation(src, "src/tool.mjs"), src);
});

test("rewriteOutputItem recovers a raw JS apply_patch payload instead of an unregistered function_call", () => {
  const out = rewriteOutputItem({
    type: "function_call",
    name: "apply_patch",
    callId: "c9",
    arguments: "export default async function tool() {\n  return { title: document.title };\n}",
  });
  assert.equal(out[0].type, "apply_patch_call");
  assert.equal(out[0].operation.path, "src/tool.mjs");
  assert.match(contentFromCreateDiffOrRaw(out[0].operation.diff), /defineOperation/);
});

test("fulfillHostToolCalls never leaves apply_patch as a function_call", async () => {
  const fulfilled = await fulfillHostToolCalls({
    output: [{ type: "function_call", name: "apply_patch", callId: "c10", arguments: "please edit the file" }],
  });
  assert.equal(fulfilled.output[0].type, "message");
  assert.doesNotMatch(JSON.stringify(fulfilled.output), /"type":"function_call"/);
});

test("fulfillHostToolCalls intercepts shell ls with a read-only listing", async () => {
  assert.equal(isListingCommand("ls -la src"), true);
  assert.equal(isListingCommand("rm -rf src"), false);
  const fulfilled = await fulfillHostToolCalls({
    output: [{ type: "shell_call", callId: "s1", action: { commands: ["ls -la"] } }],
  });
  assert.equal(fulfilled.output[0].type, "message");
  assert.match(fulfilled.output[0].content[0].text, /list_workspace|workspace listing/i);
});

function contentFromCreateDiffOrRaw(diff) {
  return String(diff)
    .split("\n")
    .map((line) => (line.startsWith("+") ? line.slice(1) : line))
    .join("\n");
}

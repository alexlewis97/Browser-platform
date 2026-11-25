/**
 * Translate OpenAI hosted tools (apply_patch, and other non-function tools) into
 * ordinary function tools that OpenRouter can serve, then map the model's
 * function calls back to the Agents SDK / Pomerado native item types so the
 * local editor still applies diffs to the artifact workspace.
 *
 * Kept in our host wrapper — no fork of pomerado.
 */

import {
  LIST_WORKSPACE_NAME,
  ensureDefineOperation,
  contentFromCreateDiff,
  isToolSourcePath,
  listWorkspaceTool,
  withMintHint,
} from "./workspace-tools.mjs";

export const APPLY_PATCH_NAME = "apply_patch";

export const APPLY_PATCH_DESCRIPTION = [
  "Apply a file edit to the local artifact workspace (not the website).",
  "This is the correct way to create or edit tool source such as src/tool.mjs.",
  "Workspace writes are always allowed on a read-authority mint; do not use shell to write files.",
  "Each call is one or more operations (create_file, update_file, or delete_file).",
  "Use the apply_patch envelope, or JSON {type, path, diff} / {operations:[...]}.",
  "Create file (every content line MUST start with '+'). src/tool.mjs MUST default-export defineOperation:",
  "  *** Begin Patch",
  "  *** Add File: src/tool.mjs",
  '  +import { Schema } from "effect";',
  '  +import { defineOperation } from "../../runtime/index.js";',
  "+export default defineOperation({",
  '+  name: "tool",',
  "+  input: Schema.Struct({}),",
  "+  output: Schema.Unknown,",
  "+}, async ({ kernel, sessionId, errors }) => {",
  "+  const answer = await kernel.browsers.playwright.execute(sessionId, {",
  "+    timeout_sec: 30,",
  "+    code: `const title = await page.title(); return { title, url: page.url() };`,",
  "+  });",
  "+  if (!answer.success) throw new errors.OperationFailure(String(answer.error), { stderr: answer.stderr });",
  "+  return answer.result;",
  "+});",
  "  *** End Patch",
  "Update file (V4A hunks with @@ and +/- lines):",
  "  *** Begin Patch",
  "  *** Update File: src/tool.mjs",
  "  @@",
  "   export default async function tool() {",
  "  -  return { title: document.title };",
  "  +  return { title: document.title, url: location.href };",
  "   }",
  "  *** End Patch",
  "Raw file content in `diff` or `content` is also accepted for create_file; the host will prefix lines with '+'.",
  "If one operation in a batch does not apply, nothing from that batch should be assumed applied.",
].join(" ");

export const APPLY_PATCH_PARAMETERS = {
  type: "object",
  additionalProperties: true,
  properties: {
    type: {
      type: "string",
      enum: ["create_file", "update_file", "delete_file"],
      description: "The operation to perform. Required when sending a single operation.",
    },
    path: { type: "string", description: "Workspace-relative file path." },
    diff: {
      type: "string",
      description: "Patch body for create_file or update_file. May include the *** Begin Patch envelope.",
    },
    moveTo: { type: "string", description: "Optional new path when type is update_file." },
    operation: {
      type: "object",
      description: "A single operation object {type, path, diff?, moveTo?}.",
    },
    operations: {
      type: "array",
      description: "Multiple operations to apply in order.",
      items: {
        type: "object",
        properties: {
          type: { type: "string", enum: ["create_file", "update_file", "delete_file"] },
          path: { type: "string" },
          diff: { type: "string" },
          moveTo: { type: "string" },
        },
      },
    },
    patch: { type: "string", description: "A full *** Begin Patch ... *** End Patch envelope (one or more files)." },
  },
};

export const SHELL_PARAMETERS = {
  type: "object",
  additionalProperties: true,
  properties: {
    command: { type: "string", description: "Command to run (shell string)." },
    commands: { type: "array", items: { type: "string" }, description: "Optional argv form of the command." },
    working_directory: { type: "string" },
    timeout_ms: { type: "number" },
  },
};

const HOSTED_DROP_NOTE = "dropped_hosted_tool";

export function functionTool({ name, description, parameters, strict = false }) {
  return {
    type: "function",
    name,
    description,
    parameters,
    strict,
  };
}

export function rewriteTools(tools = []) {
  const dropped = [];
  const rewritten = [];
  for (const tool of tools) {
    if (!tool || typeof tool !== "object") continue;
    if (tool.type === "function") {
      rewritten.push(tool);
      continue;
    }
    if (tool.type === "apply_patch") {
      rewritten.push(
        functionTool({
          name: tool.name || APPLY_PATCH_NAME,
          description: APPLY_PATCH_DESCRIPTION,
          parameters: APPLY_PATCH_PARAMETERS,
          strict: false,
        })
      );
      continue;
    }
    if (tool.type === "shell") {
      rewritten.push(
        functionTool({
          name: tool.name || "shell",
          description: "Run a command in the local tool environment and return its output.",
          parameters: SHELL_PARAMETERS,
          strict: false,
        })
      );
      continue;
    }
    dropped.push({ type: tool.type, name: tool.name, providerType: tool.providerData?.type });
  }
  if (!rewritten.some((tool) => tool && tool.name === LIST_WORKSPACE_NAME)) {
    rewritten.push(listWorkspaceTool());
  }
  return { tools: rewritten, dropped };
}

export function rewriteRequest(request = {}) {
  const { tools, dropped } = rewriteTools(request.tools || []);
  const next = {
    ...request,
    tools,
    input: withMintHint(rewriteInput(request.input)),
  };
  if (next.modelSettings && typeof next.modelSettings === "object") {
    const settings = { ...next.modelSettings };
    settings.toolChoice = normalizeToolChoice(settings.toolChoice);
    settings.reasoning = normalizeReasoning(settings.reasoning);
    next.modelSettings = settings;
  }
  if (next.providerData && typeof next.providerData === "object") {
    next.providerData = sanitizeProviderData(next.providerData);
  }
  return { request: next, dropped };
}

/** OpenRouter rejects Responses reasoning.context / summary on several models (invalid_prompt). */
export function normalizeReasoning(reasoning) {
  if (!reasoning || typeof reasoning !== "object") return reasoning;
  const effort = reasoning.effort;
  return effort ? { effort } : undefined;
}

export function sanitizeProviderData(providerData = {}) {
  const next = { ...providerData };
  if (next.tool_choice != null) {
    const choice = next.tool_choice;
    if (choice === "auto" || choice === "required" || choice === "none") {
      // keep
    } else if (choice && typeof choice === "object" && (choice.type === "function" || choice.function)) {
      const name = choice.function?.name || choice.name;
      next.tool_choice = name
        ? { type: "function", function: { name } }
        : "auto";
    } else {
      next.tool_choice = "auto";
    }
  }
  if (next.reasoning && typeof next.reasoning === "object") {
    next.reasoning = normalizeReasoning(next.reasoning);
    if (!next.reasoning) delete next.reasoning;
  }
  return next;
}

export function normalizeToolChoice(toolChoice) {
  // Chat Completions (Alibaba / several OpenRouter providers) require tool_choice to be
  // "auto" | "required" | "none" or {type:"function", function:{name}}. The Agents SDK
  // emits {function:{name}} without type when toolChoice is a tool name, which 400s.
  // Hosted-tool names are rewritten; "auto" is a valid, cheap equivalent.
  if (toolChoice == null) return toolChoice;
  if (toolChoice === "auto" || toolChoice === "required" || toolChoice === "none") return toolChoice;
  return "auto";
}

export function stripResponsesOnly(request = {}) {
  const next = { ...request };
  delete next.previousResponseId;
  delete next.conversationId;
  if (next.modelSettings && typeof next.modelSettings === "object") {
    const settings = { ...next.modelSettings };
    settings.toolChoice = normalizeToolChoice(settings.toolChoice);
    settings.reasoning = normalizeReasoning(settings.reasoning);
    next.modelSettings = settings;
  }
  if (next.providerData && typeof next.providerData === "object") {
    next.providerData = sanitizeProviderData(next.providerData);
  }
  return next;
}

export function rewriteInput(input) {
  if (!Array.isArray(input)) return input;
  return input.flatMap((item) => rewriteInputItem(item));
}

function rewriteInputItem(item) {
  if (!item || typeof item !== "object") return [item];
  if (item.type === "apply_patch_call") {
    return [
      {
        type: "function_call",
        callId: item.callId,
        id: item.id,
        name: APPLY_PATCH_NAME,
        status: item.status,
        arguments: JSON.stringify(item.operation ?? {}),
        ...(item.providerData ? { providerData: item.providerData } : {}),
      },
    ];
  }
  if (item.type === "apply_patch_call_output") {
    return [
      {
        type: "function_call_result",
        callId: item.callId,
        id: item.id,
        name: APPLY_PATCH_NAME,
        status: item.status === "failed" ? "incomplete" : item.status || "completed",
        output: item.output ?? "",
        ...(item.providerData ? { providerData: item.providerData } : {}),
      },
    ];
  }
  if (item.type === "shell_call") {
    return [
      {
        type: "function_call",
        callId: item.callId,
        id: item.id,
        name: "shell",
        status: item.status,
        arguments: JSON.stringify(item.action ?? item.command ?? item),
      },
    ];
  }
  if (item.type === "shell_call_output") {
    return [
      {
        type: "function_call_result",
        callId: item.callId,
        id: item.id,
        name: "shell",
        status: item.status || "completed",
        output: typeof item.output === "string" ? item.output : JSON.stringify(item.output ?? ""),
      },
    ];
  }
  return [item];
}

export function rewriteResponse(response) {
  if (!response || !Array.isArray(response.output)) return response;
  return {
    ...response,
    output: response.output.flatMap((item) => rewriteOutputItem(item)),
  };
}

export function rewriteOutputItem(item) {
  if (!item || typeof item !== "object") return [item];
  if (item.type === "function_call" && isApplyPatchName(item.name)) {
    const operations = parseApplyPatchArguments(item.arguments).map(sanitizeOperation).filter(Boolean);
    if (!operations.length) {
      // Do not leave a function_call named apply_patch: the agent only has the hosted
      // tool, and the SDK would throw "Tool apply_patch not found in agent Pomerado minter."
      const recovered = recoverApplyPatch(item.arguments);
      if (recovered.length) {
        return recovered.map((operation, index) => ({
          type: "apply_patch_call",
          callId: recovered.length === 1 ? item.callId : `${item.callId}__${index}`,
          id: item.id,
          status: item.status || "completed",
          operation: sanitizeOperation(operation),
          ...(item.providerData ? { providerData: item.providerData } : {}),
        }));
      }
      return [item];
    }
    return operations.map((operation, index) => ({
      type: "apply_patch_call",
      callId: operations.length === 1 ? item.callId : `${item.callId}__${index}`,
      id: item.id,
      status: item.status || "completed",
      operation,
      ...(item.providerData ? { providerData: item.providerData } : {}),
    }));
  }
  if (item.type === "function_call" && isShellName(item.name)) {
    let action = {};
    try {
      action = typeof item.arguments === "string" ? JSON.parse(item.arguments || "{}") : item.arguments || {};
    } catch {
      action = { command: String(item.arguments || "") };
    }
    const commands = normalizeShellCommands(action);
    if (!commands.length) return [item];
    return [
      {
        type: "shell_call",
        callId: item.callId,
        id: item.id,
        status: item.status === "failed" ? "incomplete" : item.status || "completed",
        action: {
          commands,
          ...(action.timeout_ms != null || action.timeoutMs != null
            ? { timeoutMs: Number(action.timeout_ms ?? action.timeoutMs) }
            : {}),
        },
      },
    ];
  }
  return [item];
}

export function isApplyPatchName(name) {
  const n = String(name || "").toLowerCase();
  return n === APPLY_PATCH_NAME || n.endsWith("." + APPLY_PATCH_NAME) || n.endsWith("/" + APPLY_PATCH_NAME);
}

export function isShellName(name) {
  return String(name || "").toLowerCase() === "shell";
}

export function recoverApplyPatch(raw) {
  const text = typeof raw === "string" ? raw : raw != null ? JSON.stringify(raw) : "";
  if (!text.trim()) return [];
  const pathMatch = /(?:src\/)?tool(?:-http)?\.(?:mjs|js|ts)/.exec(text);
  const path = pathMatch ? (text.includes("src/") ? pathMatch[0].includes("src/") ? pathMatch[0] : `src/${pathMatch[0]}` : `src/${pathMatch[0]}`) : "src/tool.mjs";
  const looksJs = /export\s+default|function\s+|document\.|page\.|defineOperation|Schema\.Struct/.test(text);
  if (looksJs) {
    let body = text.trim();
    // Strip accidental JSON quotes / fences.
    body = body.replace(/^```(?:javascript|js|ts)?\n?/, "").replace(/\n?```$/, "");
    return [{ type: "create_file", path, diff: body }];
  }
  return [];
}

export function parseApplyPatchArguments(raw) {
  return _parseApplyPatchArguments(raw).map(sanitizeOperation).filter(Boolean);
}

function _parseApplyPatchArguments(raw) {
  if (raw == null || raw === "") return [];
  let value = raw;
  if (typeof raw === "string") {
    const trimmed = raw.trim();
    if (!trimmed) return [];
    if (trimmed.startsWith("*** Begin Patch") || trimmed.startsWith("*** Add File:") || trimmed.startsWith("*** Update File:")) {
      return parsePatchEnvelope(trimmed);
    }
    try {
      value = JSON.parse(trimmed);
    } catch {
      return parsePatchEnvelope(trimmed);
    }
  }
  if (typeof value === "string") return parseApplyPatchArguments(value);
  if (Array.isArray(value)) {
    return value.flatMap((entry) => normalizeOperation(entry)).filter(Boolean);
  }
  if (typeof value !== "object") return [];
  if (Array.isArray(value.operations)) {
    return value.operations.flatMap((entry) => normalizeOperation(entry)).filter(Boolean);
  }
  if (value.operation && typeof value.operation === "object") {
    const op = normalizeOperation(value.operation);
    return op ? [op] : [];
  }
  if (typeof value.patch === "string") {
    return parsePatchEnvelope(value.patch);
  }
  const op = normalizeOperation(value);
  return op ? [op] : [];
}

export function normalizeOperation(value) {
  if (!value || typeof value !== "object") return null;
  const type = String(value.type || value.op || "").toLowerCase();
  const path = value.path || value.file || value.filename;
  if (!path) return null;
  if (type === "delete_file" || type === "delete" || type === "unlink") {
    return { type: "delete_file", path: String(path) };
  }
  if (type === "create_file" || type === "create" || type === "add_file" || type === "add") {
    return { type: "create_file", path: String(path), diff: String(value.diff ?? value.content ?? "") };
  }
  if (type === "update_file" || type === "update" || type === "edit" || type === "patch") {
    const operation = { type: "update_file", path: String(path), diff: String(value.diff ?? value.content ?? "") };
    const moveTo = value.moveTo || value.move_to;
    if (moveTo) operation.moveTo = String(moveTo);
    return operation;
  }
  return null;
}

export function normalizeShellCommands(action = {}) {
  if (Array.isArray(action.commands)) return action.commands.map((c) => String(c));
  if (Array.isArray(action.command)) return action.command.map((c) => String(c));
  if (typeof action.command === "string" && action.command.trim()) return [action.command];
  if (typeof action.cmd === "string" && action.cmd.trim()) return [action.cmd];
  if (Array.isArray(action.cmd)) return action.cmd.map((c) => String(c));
  return [];
}

/** Pomerado's editor applies a V4A create diff: every content line must start with '+'. */
export function toCreateDiff(diff) {
  const src = String(diff ?? "");
  if (!src.trim()) return "";
  if (src.includes("*** Add File:") || src.includes("*** Begin Patch") || src.includes("*** Update File:")) {
    const parsed = parsePatchEnvelope(src);
    const create = parsed.find((op) => op.type === "create_file") || parsed[0];
    if (create?.diff != null) return toCreateDiff(create.diff);
  }
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  const body = lines.filter(
    (line) =>
      line !== "*** Begin Patch" &&
      line !== "*** End Patch" &&
      line !== "*** End of File" &&
      !/^\*\*\* (Add|Update|Delete) File:/.test(line) &&
      line !== "@@" &&
      !line.startsWith("@@ ")
  );
  if (!body.length) return "";
  if (body.every((line) => line.startsWith("+"))) return body.join("\n");
  // Mixed hunk leftovers from a mistaken create-with-@@ description: keep added lines only
  // when every non-context line is already a V4A add and there are no deletions.
  const hunkish = body.filter((line) => line.startsWith("+") || line.startsWith("-") || line.startsWith(" "));
  if (hunkish.length === body.length && body.some((line) => line.startsWith("+")) && !body.some((line) => line.startsWith("-"))) {
    return body.filter((line) => line.startsWith("+")).join("\n");
  }
  return body.map((line) => (line.startsWith("+") ? line : `+${line}`)).join("\n");
}

export function toUpdateDiff(diff) {
  const src = String(diff ?? "");
  if (!src.trim()) return src;
  if (src.includes("*** Begin Patch") || src.includes("*** Update File:") || src.includes("*** Add File:")) {
    const parsed = parsePatchEnvelope(src);
    const update = parsed.find((op) => op.type === "update_file") || parsed.find((op) => op.type === "create_file") || parsed[0];
    if (update?.diff != null) return update.diff;
  }
  return src;
}

export function sanitizeOperation(operation) {
  if (!operation) return operation;
  if (operation.type === "create_file") {
    let diff = toCreateDiff(operation.diff);
    if (isToolSourcePath(operation.path)) {
      const content = contentFromCreateDiff(diff);
      const wrapped = ensureDefineOperation(content, operation.path);
      if (wrapped !== content) diff = toCreateDiff(wrapped);
    }
    return { ...operation, diff };
  }
  if (operation.type === "update_file") {
    return { ...operation, diff: toUpdateDiff(operation.diff) };
  }
  return operation;
}

export function parsePatchEnvelope(text) {
  const src = String(text || "").replace(/\r\n/g, "\n");
  if (!src.trim()) return [];
  const operations = [];
  let current = null;
  const flush = () => {
    if (!current) return;
    if (current.type === "delete_file") {
      operations.push({ type: "delete_file", path: current.path });
    } else {
      operations.push({
        type: current.type,
        path: current.path,
        diff: current.lines.join("\n"),
      });
    }
    current = null;
  };
  for (const line of src.split("\n")) {
    const add = /^\*\*\* Add File:\s*(.+)\s*$/.exec(line);
    const update = /^\*\*\* Update File:\s*(.+)\s*$/.exec(line);
    const del = /^\*\*\* Delete File:\s*(.+)\s*$/.exec(line);
    if (add) {
      flush();
      current = { type: "create_file", path: add[1].trim(), lines: [] };
      continue;
    }
    if (update) {
      flush();
      current = { type: "update_file", path: update[1].trim(), lines: [] };
      continue;
    }
    if (del) {
      flush();
      current = { type: "delete_file", path: del[1].trim(), lines: [] };
      continue;
    }
    if (line === "*** Begin Patch" || line === "*** End Patch") {
      if (line === "*** End Patch") flush();
      continue;
    }
    if (current && current.type !== "delete_file") current.lines.push(line);
  }
  flush();
  return operations;
}

export function isResponsesIncompatible(error) {
  const message = String(error?.message || error || "");
  return /hosted tools are not supported|invalid responses api request|apply_patch|programmatic tool calling|only supported with the responses api|400\b/i.test(
    message
  );
}

export { HOSTED_DROP_NOTE };

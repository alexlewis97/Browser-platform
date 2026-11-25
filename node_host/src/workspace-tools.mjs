/**
 * Host-side helpers for the OpenRouter minter wrapper:
 *  - a read-only list_workspace tool (so the model does not use shell/execute `ls`)
 *  - post-process src/tool.mjs so a bare default export becomes defineOperation
 *  - never leave apply_patch as an unregistered function_call
 */
import { readdir, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, relative, extname } from "node:path";

export const LIST_WORKSPACE_NAME = "list_workspace";

export const LIST_WORKSPACE_DESCRIPTION = [
  "Read-only listing of the mint workspace (files and directories).",
  "Use this instead of shell, exec_command, or execute(command) to inspect src/, explore/, test/, or scratch/.",
  "Listing commands via shell or execute are denied on a read-authority mint.",
  "Optional path is workspace-relative (default: .).",
].join(" ");

export const LIST_WORKSPACE_PARAMETERS = {
  type: "object",
  additionalProperties: true,
  properties: {
    path: { type: "string", description: "Workspace-relative directory to list (default: .)." },
  },
};

export const MINT_HINT_MARKER = "[[pomerado-host-addendum]]";

export const MINT_HINT = [
  MINT_HINT_MARKER,
  "Write src/tool.mjs as a defineOperation default export, not a bare function.",
  "Required shape:",
  '  import { Schema } from "effect";',
  '  import { defineOperation } from "../../runtime/index.js";',
  "  export default defineOperation({ name, input: Schema.Struct({...}), output: Schema.Struct({...}) },",
  "    async ({ kernel, sessionId, input, errors }) => {",
  "      const answer = await kernel.browsers.playwright.execute(sessionId, {",
  "        timeout_sec: 30,",
  "        code: `const title = await page.title(); return { title, url: page.url() };`,",
  "      });",
  "      if (!answer.success) throw new errors.OperationFailure(String(answer.error), { stderr: answer.stderr });",
  "      return answer.result;",
  "    });",
  "Do not use shell, exec_command, or execute(purpose=command) to list or write workspace files; those are denied on a read mint.",
  "List files with list_workspace or read them with read_source. Edit files only with apply_patch (or execute's patch field).",
].join(" ");

const LISTING_COMMAND = /^\s*(ls|dir|tree|find|ll)\b/i;

export function listWorkspaceTool() {
  return {
    type: "function",
    name: LIST_WORKSPACE_NAME,
    description: LIST_WORKSPACE_DESCRIPTION,
    parameters: LIST_WORKSPACE_PARAMETERS,
    strict: false,
  };
}

export function isListWorkspaceName(name) {
  const n = String(name || "").toLowerCase().split(".").pop();
  return n === LIST_WORKSPACE_NAME || n === "list_files" || n === "list_dir" || n === "listdir";
}

export function isListingCommand(command) {
  const text = Array.isArray(command) ? command.join(" ") : String(command || "");
  if (!LISTING_COMMAND.test(text)) return false;
  if (/\b(-delete|--delete|rm\s|unlink|chmod|chown|mv\s|cp\s|mkdir|touch|wget|curl)\b/i.test(text)) return false;
  return true;
}

export function isListingExecuteCall(args) {
  const value = typeof args === "string" ? safeJson(args) : args || {};
  const text = typeof args === "string" ? args : JSON.stringify(value);
  return isListingCommand(text) || /\bls\s+-l[a-zA-Z]*\b/.test(text);
}

function safeJson(text) {
  try {
    return JSON.parse(text || "{}");
  } catch {
    return { raw: text };
  }
}

export function toolNameOf(item) {
  if (!item || typeof item !== "object") return "";
  return item.name || item.function?.name || item.toolName || item.providerData?.name || "";
}

export function isToolSourcePath(path) {
  const p = String(path || "").replace(/^\.?\//, "");
  return p === "src/tool.mjs" || p === "src/tool.js" || p === "src/tool.ts" || p === "src/tool-http.mjs";
}

export function contentFromCreateDiff(diff) {
  const lines = String(diff ?? "").replace(/\r\n/g, "\n").split("\n");
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  return lines.map((line) => (line.startsWith("+") ? line.slice(1) : line)).join("\n");
}

/**
 * If the model wrote a bare default export (or a simple function), wrap it in defineOperation
 * with Playwright page code inside kernel.browsers.playwright.execute.
 */
export function ensureDefineOperation(source, path = "src/tool.mjs") {
  const src = String(source ?? "").replace(/^\uFEFF/, "");
  if (!src.trim()) return src;
  if (/\bdefineOperation\s*\(/.test(src) && /export\s+default\s+/.test(src)) return src;
  if (!isToolSourcePath(path) && !/src\/tool/.test(String(path))) return src;

  const name = inferOperationName(src, path);
  const pageCode = extractPageCode(src);
  if (!pageCode) return src;

  return [
    'import { Schema } from "effect";',
    'import { defineOperation } from "../../runtime/index.js";',
    "",
    "export default defineOperation({",
    `  name: ${JSON.stringify(name)},`,
    "  input: Schema.Struct({}),",
    "  output: Schema.Unknown,",
    "}, async ({ kernel, sessionId, errors }) => {",
    "  const answer = await kernel.browsers.playwright.execute(sessionId, {",
    "    timeout_sec: 30,",
    "    code: " + JSON.stringify(pageCode) + ",",
    "  });",
    "  if (!answer.success) throw new errors.OperationFailure(String(answer.error), { stderr: answer.stderr });",
    "  return answer.result;",
    "});",
    "",
  ].join("\n");
}

function inferOperationName(source, path) {
  const fromFn = /export\s+default\s+async\s+function\s+([A-Za-z_][\w]*)/.exec(source)
    || /export\s+default\s+function\s+([A-Za-z_][\w]*)/.exec(source)
    || /function\s+([A-Za-z_][\w]*)\s*\(/.exec(source);
  if (fromFn && fromFn[1] && fromFn[1] !== "tool") return fromFn[1];
  const base = String(path || "tool").split("/").pop().replace(/\.(mjs|js|ts)$/, "");
  return base && base !== "tool" ? base : "tool";
}

function extractPageCode(source) {
  const src = source.trim();
  // export default async function name(...) { body }
  const fn = /export\s+default\s+(async\s+)?function(?:\s+[A-Za-z_][\w]*)?\s*\([^)]*\)\s*\{([\s\S]*)\}\s*$/.exec(src);
  if (fn) return wrapDomBody(fn[2].trim());
  const arrow = /export\s+default\s+(async\s+)?\([^)]*\)\s*=>\s*\{([\s\S]*)\}\s*$/.exec(src);
  if (arrow) return wrapDomBody(arrow[2].trim());
  const bare = /export\s+default\s+(async\s+)?function(?:\s+[A-Za-z_][\w]*)?\s*\([^)]*\)\s*\{([\s\S]*)\}/.exec(src);
  if (bare) return wrapDomBody(bare[2].trim());
  if (/\b(document|window|location|page)\b/.test(src) && !/defineOperation/.test(src)) {
    const withoutImports = src.replace(/^(\s*import\s+[^;]+;\s*)+/g, "").trim();
    return wrapDomBody(withoutImports);
  }
  return null;
}

function wrapDomBody(body) {
  const trimmed = String(body || "").trim();
  if (!trimmed) return "return { ok: true };";
  if (/\bpage\./.test(trimmed) && !/\bdocument\./.test(trimmed) && !/\bwindow\./.test(trimmed)) {
    return trimmed;
  }
  // Browser DOM in a Node Playwright `code` string must run inside page.evaluate.
  if (/^\s*return\b/.test(trimmed)) {
    return `return await page.evaluate(async () => {\n${indent(trimmed, 2)}\n});`;
  }
  return `return await page.evaluate(async () => {\n${indent(trimmed, 2)}\n  ${/\breturn\b/.test(trimmed) ? "" : "return undefined;"}\n});`.replace(/\n\s*\n\}/, "\n}");
}

function indent(text, n) {
  const pad = " ".repeat(n);
  return text.split("\n").map((line) => (line ? pad + line : line)).join("\n");
}

export async function listMintWorkspace(pathHint = ".") {
  const roots = await findWorkspaceRoots();
  if (!roots.length) {
    return { ok: false, error: "no pomerado workspace found on this host", files: [] };
  }
  const trees = [];
  for (const root of roots) {
    const rel = String(pathHint || ".").replace(/^\/workspace\/?/, "").replace(/^\//, "") || ".";
    const start = rel === "." ? root : join(root, rel);
    trees.push({
      root,
      path: rel,
      files: await walkFiles(start, root, 5, 200),
    });
  }
  return { ok: true, workspaces: trees };
}

async function findWorkspaceRoots() {
  const tmp = tmpdir();
  let names = [];
  try {
    names = await readdir(tmp);
  } catch {
    return [];
  }
  const dirs = [];
  for (const name of names) {
    if (!name.startsWith("pomerado-workspace-")) continue;
    const full = join(tmp, name);
    try {
      const info = await stat(full);
      if (info.isDirectory()) dirs.push({ full, mtime: info.mtimeMs });
    } catch {
      // skip
    }
  }
  dirs.sort((a, b) => b.mtime - a.mtime);
  return dirs.slice(0, 3).map((d) => d.full);
}

async function walkFiles(start, root, depth, limit, acc = []) {
  if (acc.length >= limit || depth < 0) return acc;
  let entries = [];
  try {
    entries = await readdir(start, { withFileTypes: true });
  } catch {
    return acc;
  }
  entries.sort((a, b) => a.name.localeCompare(b.name));
  for (const entry of entries) {
    if (acc.length >= limit) break;
    if (entry.name === "node_modules" || entry.name === ".git") continue;
    const full = join(start, entry.name);
    const rel = relative(root, full) || ".";
    if (entry.isDirectory()) {
      acc.push({ path: rel + "/", type: "dir" });
      await walkFiles(full, root, depth - 1, limit, acc);
    } else {
      const ext = extname(entry.name);
      acc.push({ path: rel, type: "file" });
      void ext;
    }
  }
  return acc;
}

export function assistantMessage(text, callId) {
  return {
    type: "message",
    role: "assistant",
    status: "completed",
    ...(callId ? { id: callId } : {}),
    content: [{ type: "output_text", text: String(text) }],
  };
}

export function parseToolArguments(raw) {
  if (raw == null || raw === "") return {};
  if (typeof raw === "object") return raw;
  try {
    return JSON.parse(String(raw));
  } catch {
    return { raw: String(raw) };
  }
}

/**
 * Fulfill host-implemented function tools and rewrite listing shell/execute calls
 * so the Agents SDK never looks up an unregistered tool or sends `ls` to Guardian.
 */
export async function fulfillHostToolCalls(response) {
  if (!response || !Array.isArray(response.output)) return response;
  const output = [];
  for (const item of response.output) {
    const rewritten = await fulfillOne(item);
    output.push(...rewritten);
  }
  return { ...response, output };
}

async function fulfillOne(item) {
  if (!item || typeof item !== "object") return [item];
  const name = toolNameOf(item);
  if (item.type === "function_call" && isListWorkspaceName(name)) {
    const args = parseToolArguments(item.arguments);
    const listing = await listMintWorkspace(args.path || args.directory || ".");
    return [assistantMessage(`list_workspace result:\n${JSON.stringify(listing, null, 2)}`, item.callId)];
  }
  if (item.type === "function_call" && (name === "shell" || name === "exec_command" || /shell/i.test(name))) {
    const args = parseToolArguments(item.arguments);
    const commands = []
      .concat(args.commands || [])
      .concat(args.command ? [args.command] : [])
      .concat(args.cmd ? [args.cmd] : []);
    if (commands.some(isListingCommand) || isListingCommand(JSON.stringify(args))) {
      const listing = await listMintWorkspace(".");
      return [assistantMessage(
        `Read-only workspace listing (shell/exec_command listing is not permitted on a read mint; use list_workspace or read_source):\n${JSON.stringify(listing, null, 2)}`,
        item.callId
      )];
    }
  }
  if (item.type === "shell_call") {
    const commands = item.action?.commands || item.action?.command || [];
    if (isListingCommand(commands) || (Array.isArray(commands) && commands.some(isListingCommand))) {
      const listing = await listMintWorkspace(".");
      return [assistantMessage(
        `Read-only workspace listing (shell listing is not permitted on a read mint; use list_workspace or read_source):\n${JSON.stringify(listing, null, 2)}`,
        item.callId
      )];
    }
  }
  if (item.type === "function_call" && String(name).toLowerCase() === "execute" && isListingExecuteCall(item.arguments)) {
    const listing = await listMintWorkspace(".");
    return [assistantMessage(
      `Read-only workspace listing (execute purpose=command listing is not permitted on a read mint; use list_workspace or read_source, and apply_patch to edit src/tool.mjs):\n${JSON.stringify(listing, null, 2)}`,
      item.callId
    )];
  }
  if (item.type === "function_call" && String(name).toLowerCase().includes("apply_patch")) {
    // Last resort: never let an unregistered apply_patch function_call reach the SDK.
    return [assistantMessage(
      `apply_patch arguments could not be parsed as a file operation. Call apply_patch again with JSON {type:"create_file"|"update_file"|"delete_file", path, diff} or a *** Begin Patch envelope. Raw arguments were not applied.`,
      item.callId
    )];
  }
  return [item];
}

export function withMintHint(input) {
  if (!Array.isArray(input)) return input;
  const already = input.some((item) => {
    const text = typeof item === "string" ? item : JSON.stringify(item);
    return text.includes(MINT_HINT_MARKER);
  });
  if (already) return input;
  return [
    ...input,
    {
      type: "message",
      role: "developer",
      content: [{ type: "input_text", text: MINT_HINT }],
    },
  ];
}

import { backendFetch } from "./api";
import { he } from "./copy/he";
import { probeExtension } from "./record";
import { loadSettings } from "./storage";
import { escapeHtml, formatUsage } from "./validation";
import { cachedWorkspaces, currentWorkspace, loadWorkspaces, setCurrentWorkspace, workspaceSelect } from "./workspaces";

export type ChatCall = {
  id: string;
  tool?: string;
  workflow_id?: string;
  workflow_name?: string;
  arguments?: Record<string, unknown>;
  action_ending?: boolean;
  definition?: Record<string, unknown>;
};

export type ChatItem = {
  id: string;
  tool?: string;
  workflow_id?: string;
  workflow_name?: string;
  inputs?: Record<string, unknown>;
  status?: string;
  error?: string;
  validation?: { verdict?: string; reason?: string };
  facts?: string;
  outputs?: Record<string, unknown>;
  url?: string;
  action_ending?: boolean;
  needs_repair?: boolean;
  fallbacks?: { step_index: number; step: string; recorded?: string | null; matched_by?: string; matched?: string }[];
};

export type ChatJob = {
  id?: string;
  status?: string;
  message?: string;
  target?: string;
  calls?: ChatCall[];
  items?: ChatItem[];
  answer?: string | null;
  question?: string | null;
  confirm_reason?: string | null;
  flagged?: { id?: string; workflow?: string; inputs?: Record<string, unknown>; verdict?: string; reason?: string }[];
  usage?: { tokens_in?: number; tokens_out?: number; cost_usd?: number; used?: boolean };
  elapsed_ms?: number;
  error?: string;
};

type ChatMessage = { role: "user" | "assistant" | "system"; content: string; job?: ChatJob };

type Preset = { name: string; message: string; target?: string };

const PRESET_KEY = "bp-chat-presets";

const CHAT_STATUS: Record<string, string> = {
  planned: he.chat.planned,
  running: he.chat.running,
  succeeded: he.chat.succeeded,
  failed: he.chat.failed,
};

function chatStatusLabel(status: string): string {
  return CHAT_STATUS[status] ?? status;
}

export const chatState = {
  messages: [] as ChatMessage[],
  job: null as ChatJob | null,
  events: [] as Record<string, unknown>[],
  busy: false,
  error: "",
  target: "cloud" as "cloud" | "extension",
  extensionReady: false,
  draft: "",
};

export function loadPresets(): Preset[] {
  try {
    const raw = localStorage.getItem(PRESET_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function savePreset(name: string, message: string, target?: string): void {
  const next = loadPresets().filter((item) => item.name !== name);
  next.unshift({ name, message, target });
  localStorage.setItem(PRESET_KEY, JSON.stringify(next.slice(0, 20)));
}

export function renderChatPage(): string {
  const s = chatState;
  const history = s.messages
    .map((msg) => {
      if (msg.role === "user") {
        return `<div class="chat-bubble user">${escapeHtml(msg.content)}</div>`;
      }
      return `<div class="chat-bubble assistant">${msg.job ? renderJob(msg.job, msg.content) : renderMarkdown(msg.content)}</div>`;
    })
    .join("");
  const live = s.busy && s.job ? renderJob(s.job, s.job.answer || "") : "";
  const confirm = s.job?.status === "needs_confirmation"
    ? `<div class="banner info" id="chat-confirm">
        ${escapeHtml(s.job.confirm_reason || he.chat.confirmDefault)}
        <div class="btn-row" style="margin-top:8px">
          <button type="button" class="btn primary" data-action="chat-confirm">${he.chat.confirmRun}</button>
        </div>
      </div>`
    : "";
  const err = s.error ? `<div class="banner err">${escapeHtml(s.error)}</div>` : "";
  const presets = loadPresets();
  const presetList = presets.length
    ? `<div class="preset-row">${presets
        .map(
          (item) =>
            `<button type="button" class="btn" data-action="chat-load-preset" data-name="${escapeHtml(item.name)}">${escapeHtml(item.name)}</button>`,
        )
        .join("")}</div>`
    : "";
  return `
    ${err}
    <div class="card chat-thread" id="chat-thread">
      ${history || `<p class="muted">${he.chat.empty}</p>`}
      ${live}
      ${confirm}
      ${s.busy ? `<p class="muted"><span class="spinner"></span> ${he.chat.working}</p>` : ""}
    </div>
    <form id="chat-form" class="card chat-composer">
      ${cachedWorkspaces().length ? `<label class="field"><span>${he.workspaces.picker}</span>${workspaceSelect("chat-workspace", currentWorkspace())}</label>` : ""}
      <label class="field"><span>${he.chat.message}</span>
        <textarea id="chat-input" name="message" required placeholder="${he.chat.messagePh}" style="min-height:88px;font-family:var(--font);font-size:16px">${escapeHtml(s.draft)}</textarea>
      </label>
      <label class="check">
        <input type="checkbox" id="chat-target-extension" ${s.target === "extension" ? "checked" : ""} ${s.extensionReady ? "" : "disabled"} />
        <span>${he.chat.runInChrome} ${s.extensionReady ? "" : he.chat.extMissing}</span>
      </label>
      <div class="btn-row">
        <button class="btn primary wide" type="submit" ${s.busy ? "disabled" : ""}>${he.chat.send}</button>
        <button class="btn" type="button" data-action="chat-save-preset" ${s.busy ? "disabled" : ""}>${he.chat.savePreset}</button>
      </div>
      ${presetList}
    </form>
  `;
}

function renderJob(job: ChatJob, answer: string): string {
  const calls = (job.calls || []).map((call) => {
    const item = (job.items || []).find((entry) => entry.id === call.id);
    return renderCallCard(call, item);
  }).join("");
  const done = (job.items || []).filter((i) => i.status && i.status !== "running").length;
  const total = (job.calls || []).length;
  const progress = job.status === "running"
    ? `<p class="muted">${he.chat.batchProgress} ${done}/${total} ${he.chat.finished}</p>`
    : "";
  const flagged = (job.flagged || []).length
    ? `<div class="banner err">${he.chat.flagged} ${(job.flagged || [])
        .map((f) => `${escapeHtml(f.workflow || "")} ${escapeHtml(JSON.stringify(f.inputs || {}))} — ${escapeHtml(f.verdict || "")}: ${escapeHtml(f.reason || "")}`)
        .join(" · ")}</div>`
    : "";
  const usage = job.usage ? `<p class="muted">${he.chat.thisTurn} ${escapeHtml(formatUsage(job.usage))}${job.elapsed_ms != null ? ` · ${(job.elapsed_ms / 1000).toFixed(1)}s` : ""}</p>` : "";
  const text = answer || job.answer || job.question || job.error || "";
  return `<div class="chat-job" data-job-id="${escapeHtml(job.id || "")}">
    ${calls}
    ${progress}
    ${text ? `<div class="chat-answer">${renderMarkdown(text)}</div>` : ""}
    ${flagged}
    ${usage}
  </div>`;
}

function renderCallCard(call: ChatCall, item?: ChatItem): string {
  const status = item?.status || (item ? "running" : "planned");
  const pill = status === "succeeded" ? "ok" : status === "failed" ? "bad" : status === "running" ? "" : "warn";
  const verdict = item?.validation?.verdict;
  const verdictPill = verdict ? `<span class="pill ${verdict === "ok" ? "ok" : verdict === "wrong" ? "bad" : "warn"}">${escapeHtml(verdict)}</span>` : "";
  const inputs = Object.entries(call.arguments || item?.inputs || {})
    .map(([k, v]) => `${k}=${v}`)
    .join(", ");
  const raw = item
    ? `<details class="raw-result"><summary>${he.chat.raw}</summary><pre class="output">${escapeHtml(JSON.stringify({ inputs: item.inputs, outputs: item.outputs, validation: item.validation, error: item.error, url: item.url }, null, 2))}</pre></details>`
    : "";
  const reason = item?.validation?.reason ? `<div class="muted">${escapeHtml(item.validation.reason)}</div>` : "";
  const err = item?.error ? `<div class="muted">${escapeHtml(item.error)}</div>` : "";
  return `<div class="card tool-card">
    <div class="row">
      <strong>${escapeHtml(call.workflow_name || call.tool || he.workflow.title)}</strong>
      <span class="pill ${pill}">${escapeHtml(chatStatusLabel(status))}</span>
      ${verdictPill}
      ${item?.needs_repair ? `<span class="pill warn" title="${escapeHtml((item.fallbacks || []).map((f) => `#${f.step_index + 1} ${f.step}: ${f.recorded || ""} → ${f.matched_by || ""} ${f.matched || ""}`).join("\n"))}">${he.chat.needsRepair}</span>` : ""}
    </div>
    <div class="mono muted">${escapeHtml(inputs || he.chat.noInputs)}</div>
    ${reason}${err}
    ${item?.action_ending && item.status === "succeeded" ? `<div class="muted">${he.chat.actionDone}</div>` : ""}
    ${raw}
  </div>`;
}

function renderMarkdown(text: string): string {
  const escaped = escapeHtml(text);
  const withBreaks = escaped.replace(/\n/g, "<br>");
  return withBreaks.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}

export async function bindChatPage(): Promise<void> {
  if (!cachedWorkspaces().length) {
    const before = cachedWorkspaces().length;
    const list = await loadWorkspaces();
    if (list.length !== before && document.getElementById("chat-page")) {
      rerenderChat();
      return;
    }
  }
  chatState.extensionReady = await probeExtension().catch(() => false);
  const picker = document.getElementById("chat-workspace") as HTMLSelectElement | null;
  picker?.addEventListener("change", () => {
    // A workspace is its own assistant: start a fresh conversation in it.
    setCurrentWorkspace(picker.value);
    chatState.messages = [];
    chatState.job = null;
    rerenderChat();
  });
  const form = document.getElementById("chat-form") as HTMLFormElement | null;
  const input = document.getElementById("chat-input") as HTMLTextAreaElement | null;
  const ext = document.getElementById("chat-target-extension") as HTMLInputElement | null;
  if (input) input.value = chatState.draft;
  ext?.addEventListener("change", () => {
    chatState.target = ext.checked ? "extension" : "cloud";
  });
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    const message = String(input?.value || "").trim();
    if (!message || chatState.busy) return;
    void sendChat(message);
  });
  const thread = document.getElementById("chat-thread");
  if (thread) thread.scrollTop = thread.scrollHeight;
}

export async function sendChat(message: string, opts?: { confirm?: boolean; jobId?: string; results?: ChatItem[] }): Promise<void> {
  chatState.error = "";
  chatState.draft = opts?.confirm ? chatState.draft : "";
  if (!opts?.confirm && !opts?.results) {
    chatState.messages.push({ role: "user", content: message });
  }
  chatState.busy = true;
  chatState.job = { status: "planning", message, calls: [], items: [], target: chatState.target };
  rerenderChat();
  try {
    // Exclude the current user turn from history (it is sent as `message`).
    const history = chatState.messages
      .filter((m) => m.role === "user" || m.role === "assistant")
      .slice(0, -1)
      .slice(-8)
      .map((m) => ({ role: m.role, content: m.content }));
    const settings = loadSettings();
    const body: Record<string, unknown> = {
      message,
      history,
      target: chatState.target,
      stream: true,
      confirm: Boolean(opts?.confirm),
      job_id: opts?.jobId,
      results: opts?.results,
      workspace: currentWorkspace() || undefined,
    };
    if (settings.openrouterKey) {
      body.llm = { api_key: settings.openrouterKey, model: settings.model || undefined };
    }
    const response = await backendFetch("/api/chat", { method: "POST", body: JSON.stringify(body) });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `Chat failed (${response.status})`);
    }
    const ctype = response.headers.get("content-type") || "";
    let job: ChatJob | null = null;
    if (ctype.includes("text/event-stream") && response.body) {
      job = await readSse(response, (event) => {
        applyEvent(event);
        if (event.type === "done" && event.job) job = event.job as ChatJob;
      });
    } else {
      job = (await response.json()) as ChatJob;
      chatState.job = job;
    }
    if (!job) throw new Error("Chat returned no job.");
    if (
      job.status === "awaiting_results" &&
      chatState.target === "extension" &&
      Array.isArray(job.calls) &&
      job.calls.length > 0
    ) {
      const results = await runCallsInExtension(job.calls);
      await sendChat(message, { jobId: job.id, results });
      return;
    }
    const answer = String(job.answer || job.question || job.error || "");
    chatState.messages.push({ role: "assistant", content: answer, job });
    chatState.job = job.status === "needs_confirmation" || job.status === "needs_input" ? job : job;
  } catch (err) {
    chatState.error = err instanceof Error ? err.message : String(err);
  } finally {
    chatState.busy = false;
    rerenderChat();
  }
}

function applyEvent(event: Record<string, unknown>): void {
  const job = chatState.job || { calls: [], items: [] };
  if (event.job_id) job.id = String(event.job_id);
  if (event.type === "planned" && Array.isArray(event.calls)) job.calls = event.calls as ChatCall[];
  if (event.type === "needs_confirmation") {
    job.status = "needs_confirmation";
    job.confirm_reason = String(event.reason || "");
    if (Array.isArray(event.calls)) job.calls = event.calls as ChatCall[];
  }
  if (event.type === "needs_input") {
    job.status = "needs_input";
    job.question = String(event.question || "");
    job.answer = job.question;
  }
  if (event.type === "running") job.status = "running";
  if (event.type === "item_start" || event.type === "item_done") {
    const item = event.item as ChatItem;
    job.items = [...(job.items || []).filter((entry) => entry.id !== item.id), item];
  }
  if (event.type === "answer") {
    job.answer = String(event.text || "");
    if (event.usage) job.usage = event.usage as ChatJob["usage"];
  }
  if (event.type === "error") job.error = String(event.error || "Chat failed");
  if (event.type === "done" && event.job) {
    chatState.job = event.job as ChatJob;
    return;
  }
  chatState.job = { ...job };
  patchLiveJob();
}

function patchLiveJob(): void {
  const root = document.getElementById("chat-thread");
  if (!root || !chatState.job) return;
  // Keep the live region updated without wiping the composer.
  const live = root.querySelector(".chat-job") || null;
  const tmp = document.createElement("div");
  tmp.innerHTML = renderJob(chatState.job, chatState.job.answer || "");
  const next = tmp.firstElementChild;
  if (live && next) live.replaceWith(next);
  else if (next) root.appendChild(next);
}

async function readSse(response: Response, onEvent: (event: Record<string, unknown>) => void): Promise<ChatJob | null> {
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let job: ChatJob | null = null;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const chunks = buf.split("\n\n");
    buf = chunks.pop() || "";
    for (const chunk of chunks) {
      const line = chunk.split("\n").find((part) => part.startsWith("data: "));
      if (!line) continue;
      try {
        const event = JSON.parse(line.slice(6)) as Record<string, unknown>;
        onEvent(event);
        if (event.type === "done" && event.job) job = event.job as ChatJob;
      } catch {
        /* ignore a partial JSON frame */
      }
    }
  }
  return job || chatState.job;
}

async function runCallsInExtension(calls: ChatCall[]): Promise<ChatItem[]> {
  const items: ChatItem[] = [];
  for (const call of calls) {
    const { extensionRequest } = await import("./record");
    try {
      const result = await (extensionRequest as (kind: string, payload: Record<string, unknown>) => Promise<{ ok: boolean; error?: string; run?: Record<string, unknown> }>)(
        "bp-run-definition",
        {
          definition: call.definition,
          values: call.arguments || {},
          id: call.workflow_id,
          name: call.workflow_name,
        },
      );
      const run = result.run || {};
      const outputs = (run.outputs as Record<string, unknown>) || {};
      items.push({
        id: call.id,
        tool: call.tool,
        workflow_id: call.workflow_id,
        workflow_name: call.workflow_name,
        inputs: call.arguments,
        status: result.ok ? "succeeded" : "failed",
        error: result.error,
        outputs,
        facts: JSON.stringify(outputs).slice(0, 800),
        action_ending: call.action_ending,
      });
    } catch (err) {
      items.push({
        id: call.id,
        tool: call.tool,
        workflow_id: call.workflow_id,
        workflow_name: call.workflow_name,
        inputs: call.arguments,
        status: "failed",
        error: err instanceof Error ? err.message : String(err),
        action_ending: call.action_ending,
      });
    }
  }
  return items;
}

function rerenderChat(): void {
  const page = document.getElementById("chat-page");
  if (!page) return;
  const inner = renderChatPage();
  page.innerHTML = inner;
  void bindChatPage();
}

export function handleChatClick(action: string, target: HTMLElement): boolean {
  if (action === "chat-confirm") {
    const jobId = chatState.job?.id;
    if (jobId) void sendChat(chatState.job?.message || "", { confirm: true, jobId });
    return true;
  }
  if (action === "chat-save-preset") {
    const message = String((document.getElementById("chat-input") as HTMLTextAreaElement | null)?.value || chatState.messages.filter((m) => m.role === "user").at(-1)?.content || "").trim();
    if (!message) return true;
    const name = window.prompt(he.chat.presetName, message.slice(0, 32)) || "";
    if (name) {
      savePreset(name, message, chatState.target);
      rerenderChat();
    }
    return true;
  }
  if (action === "chat-load-preset") {
    const name = target.dataset.name;
    const preset = loadPresets().find((item) => item.name === name);
    if (preset) {
      chatState.draft = preset.message;
      if (preset.target === "extension" && chatState.extensionReady) chatState.target = "extension";
      rerenderChat();
    }
    return true;
  }
  return false;
}
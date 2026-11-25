import { api, type MintJob, type MintTraceStep, type RunRecord, type StepTelemetry, type WorkflowDefinition, type WorkflowInput, type WorkflowSummary } from "./api";
import { HITL_MODES, cancelRun, confirmRun, extractConfirmation, formatScore, getConfirmation, openRunSocket, resolveScreenshotSrc, type ConfirmationPrompt, type HitlMode } from "./hitl";
import { cancelFromTask, confirmFromTaskVariables, formatLatency, formatTokens, getFromTaskJob, isLiveJob, parseVariableLines, startFromTask, type FromTaskJob, type FromTaskSuggestion } from "./from-task";
import {
  bindRecordScreen,
  cancelRemote,
  publishRecording,
  recordState,
  refreshExtensionProbe,
  resumeLiveSession,
  saveRecording,
  startExtension,
  startRemote,
  stepSummary,
  stopExtension,
  stopRemote,
  submitTypeBox,
  submitUrlBar,
  teardownRecordSockets,
} from "./record";
import { refreshDiscovery } from "./discover";
import { ALT_MODEL, DEFAULT_MODEL, isLocalOrigin, isManagedBackendUrl, loadRunInputs, loadSettings, saveRunInputs, saveSettings, settingsComplete, type Settings } from "./storage";
import {
  deleteMarketListing,
  inferWorkflowType,
  listMarket,
  listingIdForName,
  matchListing,
  packageUrl,
  publishWorkflowJson,
  type MarketInput,
  type MarketListing,
} from "./market";

type Route =
  | { name: "workflows" }
  | { name: "new" }
  | { name: "workflow"; id: string }
  | { name: "runs" }
  | { name: "run"; id: string }
  | { name: "mint" }
  | { name: "mint-job"; id: string }
  | { name: "from-task" }
  | { name: "from-task-job"; id: string }
  | { name: "record" }
  | { name: "marketplace" }
  | { name: "settings" };

const ICONS = {
  workflows: `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><path d="M14 16.5h5M16.5 14v5"/></svg>`,
  runs: `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M5 6h14M5 12h14M5 18h9"/><circle cx="18" cy="18" r="2.2"/></svg>`,
  mint: `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3v18M8 7c0-2 8-2 8 1.5S8 11 8 14.5 16 19 16 17"/><path d="M7 21h10"/></svg>`,
  settings: `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="3"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4"/></svg>`,
  fromTask: `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M5 5h9l5 5v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z"/><path d="M14 5v5h5M8 13h8M8 17h5"/></svg>`,
  record: `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2.5" fill="currentColor"/></svg>`,
  market: `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 10h16v9H4z"/><path d="M4 10 6 5h12l2 5"/><path d="M9 14v5M15 14v5"/></svg>`,
};

let root: HTMLElement;
let pollTimer: number | undefined;
let runSocket: WebSocket | null = null;
let healthTimer: number | undefined;
let connected = false;
let lastConnectError: string | null = null;
let flashMessage: { kind: "ok" | "err" | "info"; text: string } | null = null;
let selectedCandidateId: string | null = null;
let selectedCandidateRunId: string | null = null;

function parseRoute(): Route {
  const raw = location.hash.replace(/^#/, "") || "/";
  const parts = raw.split("?").shift()!.split("/").filter(Boolean);
  if (parts[0] === "settings") return { name: "settings" };
  if (parts[0] === "marketplace" || parts[0] === "market") return { name: "marketplace" };
  if (parts[0] === "mint" && parts[1]) return { name: "mint-job", id: parts[1] };
  if (parts[0] === "mint") return { name: "mint" };
  if (parts[0] === "from-task" && parts[1]) return { name: "from-task-job", id: parts[1] };
  if (parts[0] === "from-task") return { name: "from-task" };
  if (parts[0] === "record") return { name: "record" };
  if (parts[0] === "runs" && parts[1]) return { name: "run", id: parts[1] };
  if (parts[0] === "runs") return { name: "runs" };
  if (parts[0] === "workflows" && parts[1] === "new") return { name: "new" };
  if (parts[0] === "workflows" && parts[1]) return { name: "workflow", id: parts[1] };
  return { name: "workflows" };
}

function go(path: string): void {
  location.hash = path;
}

function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmtMs(ms?: number): string {
  if (ms == null || Number.isNaN(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function fmtTokens(step: StepTelemetry): string {
  const inn = step.tokens_in ?? 0;
  const out = step.tokens_out ?? 0;
  if (!inn && !out) return "0 tok";
  return `${inn}↓ ${out}↑`;
}

function totalTokens(run: RunRecord): string {
  const inn = (run.steps ?? []).reduce((s, x) => s + (x.tokens_in ?? 0), 0);
  const out = (run.steps ?? []).reduce((s, x) => s + (x.tokens_out ?? 0), 0);
  if (!inn && !out) return "0 tokens";
  return `${inn + out} tokens`;
}

function statusClass(status: string): string {
  if (status === "succeeded" || status === "completed" || status === "published") return "ok";
  if (status === "failed" || status === "cancelled" || status === "canceled" || status === "error" || status === "incomplete") return "bad";
  if (status === "needs_confirmation" || status === "needs_input" || status === "input_required") return "warn";
  return "";
}

function isLiveStatus(status: string): boolean {
  return status === "running" || status === "needs_confirmation";
}

function reasonLabel(reason?: string): string {
  if (reason === "ambiguous_margin") return "Close scores — pick the right match";
  if (reason === "below_threshold") return "No confident match";
  return "Needs confirmation";
}

function renderConfirmation(run: RunRecord, prompt: ConfirmationPrompt): string {
  if (selectedCandidateRunId !== run.id) {
    selectedCandidateId = prompt.candidates[0]?.id ?? null;
    selectedCandidateRunId = run.id;
  }
  const src = resolveScreenshotSrc(prompt.screenshot);
  const shot = src ? `<img class="shot" alt="Page screenshot" src="${escapeHtml(src)}" />` : "";
  const query = prompt.query ? `“${prompt.query}”` : "this step";
  const stepBit = [prompt.stepType, prompt.stepIndex != null ? `step ${prompt.stepIndex + 1}` : "", prompt.stepDescription].filter(Boolean).join(" · ");
  const scores = [
    prompt.threshold != null ? `threshold ${formatScore(prompt.threshold)}` : "",
    prompt.margin != null ? `margin ${formatScore(prompt.margin)}` : "",
    prompt.winnerScore != null ? `top ${formatScore(prompt.winnerScore)}` : "",
    prompt.runnerUpScore != null ? `runner-up ${formatScore(prompt.runnerUpScore)}` : "",
  ].filter(Boolean).join(" · ");
  const timeout = prompt.timeoutAt
    ? `Timeout ${new Date(prompt.timeoutAt).toLocaleTimeString()} (${escapeHtml(prompt.timeoutPolicy || "fail")})`
    : prompt.timeoutS
      ? `Timeout ${prompt.timeoutS}s (${escapeHtml(prompt.timeoutPolicy || "fail")})`
      : "";
  const cards = prompt.candidates.length
    ? prompt.candidates
        .map((candidate) => {
          const selected = candidate.id === selectedCandidateId ? "selected" : "";
          const score = candidate.score != null ? `<span class="score">${escapeHtml(formatScore(candidate.score))}</span>` : "";
          const selector = candidate.selector ? `<div class="sel">${escapeHtml(candidate.selector)}</div>` : "";
          const role = candidate.role ? `<span class="muted"> · ${escapeHtml(candidate.role)}</span>` : "";
          const imgSrc = resolveScreenshotSrc(candidate.screenshot);
          const img = imgSrc ? `<img class="shot" alt="" src="${escapeHtml(imgSrc)}" />` : "";
          return `<button type="button" class="candidate ${selected}" data-action="hitl-select" data-candidate-id="${escapeHtml(candidate.id)}" data-candidate-text="${escapeHtml(candidate.label)}">
            <div class="row"><strong class="grow">${escapeHtml(candidate.label)}</strong>${score}</div>
            ${role}${selector}${img}
          </button>`;
        })
        .join("")
    : `<p class="muted">No candidates were included with this prompt yet.</p>`;
  return `<div class="card confirm-panel" id="hitl-panel" data-run-id="${escapeHtml(run.id)}" data-prompt-id="${escapeHtml(prompt.id)}">
    <p class="eyebrow">${escapeHtml(reasonLabel(prompt.reason))}${prompt.hitlMode ? " · " + escapeHtml(prompt.hitlMode) : ""}</p>
    <h2>Choose a match for ${escapeHtml(query)}</h2>
    <p class="sub">${escapeHtml(prompt.message || "Pick the element this step should use, then confirm.")}</p>
    ${stepBit ? `<div class="meta">${escapeHtml(stepBit)}</div>` : ""}
    ${scores ? `<div class="meta">${escapeHtml(scores)}</div>` : ""}
    ${timeout ? `<div class="meta">${timeout}</div>` : ""}
    ${shot}
    <div class="candidates">${cards}</div>
    <div class="btn-row">
      <button type="button" class="btn danger" data-action="hitl-cancel">Cancel</button>
      <button type="button" class="btn primary" data-action="hitl-confirm" ${selectedCandidateId ? "" : "disabled"}>Confirm</button>
    </div>
    <div id="hitl-status" style="margin-top:12px"></div>
  </div>`;
}

function shell(title: string, eyebrow: string, body: string, subtitle = ""): string {
  const settings = loadSettings();
  const ready = settingsComplete(settings);
  const pill = connected
    ? `<span class="pill ok"><span class="dot"></span>backend</span>`
    : ready
      ? `<span class="pill"><span class="dot"></span>checking</span>`
      : `<span class="pill bad"><span class="dot"></span>setup needed</span>`;
  return `
    <div class="shell">
      <header class="top">
        <div>
          <p class="eyebrow">${eyebrow}</p>
          <h1>${title}</h1>
          ${subtitle ? `<p class="sub">${subtitle}</p>` : ""}
        </div>
        ${pill}
      </header>
      ${body}
    </div>
    ${nav()}
  `;
}

function nav(): string {
  const route = parseRoute();
  const active = (name: string) => (route.name === name || (name === "workflows" && route.name === "workflow") || (name === "workflows" && route.name === "new") || (name === "runs" && route.name === "run") || (name === "mint" && (route.name === "mint" || route.name === "mint-job")) || (name === "from-task" && (route.name === "from-task" || route.name === "from-task-job")) || (name === "record" && route.name === "record") || (name === "marketplace" && route.name === "marketplace") ? "active" : "");
  return `
    <nav class="nav">
      <a href="#/workflows" class="${active("workflows")}">${ICONS.workflows}Workflows</a>
      <a href="#/from-task" class="${active("from-task")}">${ICONS.fromTask}From task</a>
      <a href="#/record" class="${active("record")}">${ICONS.record}Record</a>
      <a href="#/runs" class="${active("runs")}">${ICONS.runs}Runs</a>
      <a href="#/mint" class="${active("mint")}">${ICONS.mint}Mint</a>
      <a href="#/marketplace" class="${active("marketplace")}">${ICONS.market}Market</a>
      <a href="#/settings" class="${active("settings")}">${ICONS.settings}Settings</a>
    </nav>
  `;
}

function needSetup(): string | null {
  const s = loadSettings();
  if (!settingsComplete(s)) {
    return shell(
      "Connect the backend",
      "Setup",
      `<div class="card">
        <p class="sub" style="margin-top:0">This phone page is the remote control. The browser still runs on the box. Add the tunnel URL and the shared access token to get started. Your OpenRouter key stays in this browser only.</p>
        <div style="margin-top:16px"><a class="btn primary" href="#/settings">Open settings</a></div>
      </div>`,
    );
  }
  return null;
}

async function ping(): Promise<void> {
  const s = loadSettings();
  if (!s.backendUrl) {
    connected = false;
    lastConnectError = "No backend URL yet. Refresh to rediscover the tunnel, or add one in Settings.";
    return;
  }
  try {
    const result = await api.health(s);
    connected = Boolean(result.ok);
    lastConnectError = connected ? null : "The backend answered but did not report healthy.";
  } catch (err) {
    connected = false;
    lastConnectError = err instanceof Error ? err.message : String(err);
  }
}

function bannerError(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err);
  return `<div class="banner err">${escapeHtml(message)}</div>`;
}

function backendDownBanner(): string {
  if (connected || !settingsComplete(loadSettings())) return "";
  const detail = lastConnectError || "Can't reach the backend.";
  return `<div class="banner err">${escapeHtml(detail)}</div>`;
}

async function renderWorkflows(): Promise<string> {
  const setup = needSetup();
  if (setup) return setup;
  if (!connected) {
    return shell("Workflows", "Saved automations", `${backendDownBanner()}<div class="card"><p class="sub" style="margin:0">The site can't list workflows until it reconnects to the backend. Refresh this page to pick up a new tunnel URL, or check Settings.</p></div>`);
  }
  try {
    const { workflows } = await api.listWorkflows();
    const list = workflows.length
      ? workflows
          .map(
            (w: WorkflowSummary) => `
          <div class="card">
            <a class="card-link" href="#/workflows/${encodeURIComponent(w.id)}">
              <div class="row">
                <div class="grow">
                  <strong>${escapeHtml(w.name)}</strong>
                  <div class="meta">v${escapeHtml(w.version)} · ${escapeHtml(w.id.slice(0, 8))}</div>
                </div>
                <span class="muted">Open</span>
              </div>
            </a>
            <div class="btn-row" style="margin-top:10px">
              <button type="button" class="btn wide" data-action="publish-workflow" data-id="${escapeHtml(w.id)}">Publish to marketplace</button>
            </div>
          </div>`,
          )
          .join("")
      : `<div class="card empty">No workflows yet. Create one, or start with the example.com smoke test.</div>`;
    return shell(
      "Workflows",
      "Library",
      `${list}
       <div class="btn-row" style="margin-top:14px">
         <a class="btn" href="#/workflows/new">New workflow</a>
         <a class="btn primary" href="#/from-task">Create from task</a>
         <a class="btn" href="#/record">Record</a>
         <button class="btn wide" data-action="seed-example">Add example.com</button>
       </div>`,
      "Run a saved workflow against the box browser.",
    );
  } catch (err) {
    return shell("Workflows", "Library", `${bannerError(err)}<a class="btn" href="#/settings">Check settings</a>`);
  }
}

async function renderNew(): Promise<string> {
  const setup = needSetup();
  if (setup) return setup;
  return shell(
    "New workflow",
    "Create",
    `<form id="create-form" class="card">
      <label class="field"><span>Name</span><input name="name" type="text" required placeholder="example_com_heading" /></label>
      <label class="field"><span>Description</span><input name="description" type="text" placeholder="What this does" /></label>
      <label class="field"><span>Paste workflow JSON</span>
        <textarea name="json" placeholder='{"name":"...","steps":[{"type":"navigation","url":"https://example.com"},{"type":"extract","extractionGoal":"the page title","output":"heading"}]}'></textarea>
      </label>
      <p class="muted" style="margin-top:-4px">Must end with an extract step. You can also leave the JSON blank and use the simple fields below.</p>
      <label class="field"><span>Or just a URL to open</span><input name="url" type="url" placeholder="https://example.com" /></label>
      <label class="field"><span>Extract goal</span><input name="goal" type="text" placeholder="the main heading" /></label>
      <button class="btn primary" type="submit">Create</button>
    </form>`,
  );
}


const INPUT_LABELS: Record<string, string> = {
  term: "Search term",
  query: "Search query",
  q: "Search query",
  search: "Search term",
  name: "Name",
  status: "Status",
  url: "URL",
  email: "Email",
  title: "Title",
};
const RESERVED_RUN_FIELDS = new Set(["hitl_mode", "instructions"]);

function prettyInputLabel(name: string, label?: string): string {
  if (label && label.trim()) return label.trim();
  const key = (name || "").trim();
  if (INPUT_LABELS[key.toLowerCase()]) return INPUT_LABELS[key.toLowerCase()];
  const text = key.replace(/_/g, " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "Input";
}

function placeholdersIn(value: unknown, found: string[] = []): string[] {
  if (typeof value === "string") {
    const re = /\{\{?\s*([a-zA-Z_][\w]*)\s*\}?\}/g;
    let match: RegExpExecArray | null;
    while ((match = re.exec(value))) {
      if (!found.includes(match[1])) found.push(match[1]);
    }
  } else if (Array.isArray(value)) {
    value.forEach((item) => placeholdersIn(item, found));
  } else if (value && typeof value === "object") {
    Object.values(value as Record<string, unknown>).forEach((item) => placeholdersIn(item, found));
  }
  return found;
}

function deriveWorkflowInputs(def: WorkflowDefinition): WorkflowInput[] {
  const byName = new Map<string, WorkflowInput>();
  const add = (item: WorkflowInput) => {
    const name = String(item.name || "").trim();
    if (!name || name === "intent" || name === "effect") return;
    const pomeradoArtifact = def.pomerado_artifact;
    if (name === "url" && pomeradoArtifact && !(def.input_schema ?? []).some((item) => item.name === "url")) return;
    const existing = byName.get(name);
    if (!existing) {
      byName.set(name, { ...item, name, required: item.required !== false, label: prettyInputLabel(name, item.label) });
      return;
    }
    byName.set(name, {
      ...existing,
      ...item,
      name,
      required: existing.required !== false || item.required !== false,
      label: existing.label || item.label || prettyInputLabel(name),
      description: existing.description || item.description,
      default: existing.default ?? item.default,
    });
  };
  (def.input_schema ?? []).forEach(add);
  placeholdersIn(def.steps).forEach((name) => add({ name, type: "string", required: true }));
  placeholdersIn(def.description).forEach((name) => add({ name, type: "string", required: true }));
  const pomeradoInput = def.pomerado_input;
  if (pomeradoInput && typeof pomeradoInput === "object") {
    for (const name of Object.keys(pomeradoInput as Record<string, unknown>)) {
      if (name === "url" || name === "intent" || name === "effect") continue;
      add({ name, type: "string", required: true, default: (pomeradoInput as Record<string, unknown>)[name] as string });
    }
  }
  return [...byName.values()];
}

function renderRunInputs(id: string, inputs: WorkflowInput[]): string {
  if (!inputs.length) {
    return `<p class="muted" style="margin-top:0">This workflow has no variables.</p>`;
  }
  const lastUsed = loadRunInputs(id);
  return inputs
    .map((input) => {
      const label = prettyInputLabel(input.name, input.label);
      const saved = lastUsed[input.name];
      const value = saved != null ? saved : "";
      const example = input.default == null ? "" : String(input.default);
      const hint = input.description || (example ? `Example: ${example}` : "");
      return `<label class="field">
        <span>${escapeHtml(label)} <span class="muted">required</span></span>
        <input name="${escapeHtml(input.name)}" type="text" required value="${escapeHtml(value)}" placeholder="${escapeHtml(example)}" autocomplete="off" />
        ${hint ? `<span class="muted">${escapeHtml(hint)}</span>` : ""}
      </label>`;
    })
    .join("");
}

async function renderWorkflow(id: string): Promise<string> {
  const setup = needSetup();
  if (setup) return setup;
  try {
    const stored = await api.getWorkflow(id);
    const def = stored.definition;
    const inputs = deriveWorkflowInputs(def);
    const fields = renderRunInputs(stored.id, inputs);
    const steps = def.steps
      .map(
        (step, i) => `
        <div class="step">
          <div class="idx">${i + 1}</div>
          <div>
            <strong>${escapeHtml(step.type)}</strong>
            <div class="muted">${escapeHtml(step.description || step.url || step.extractionGoal || "")}</div>
          </div>
        </div>`,
      )
      .join("");
    return shell(
      escapeHtml(def.name),
      "Workflow",
      `<div class="card">
        <p class="sub" style="margin:0 0 12px">${escapeHtml(def.description || "No description")}</p>
        <div class="mono muted">id ${escapeHtml(stored.id)} · v${escapeHtml(def.version || "1")}</div>
        <div style="margin-top:14px">${steps}</div>
        ${typeof def.mint_job_id === "string" && def.mint_job_id ? `<p style="margin:16px 0 0"><a class="btn" href="#/mint/${encodeURIComponent(String(def.mint_job_id))}">Mint trace</a></p>` : ""}
        <div class="btn-row" style="margin-top:14px">
          <button type="button" class="btn wide" data-action="publish-workflow" data-id="${escapeHtml(stored.id)}">Publish to marketplace</button>
        </div>
      </div>
      <form id="run-form" class="card" data-id="${escapeHtml(stored.id)}">
        <h2 style="margin:0 0 12px;font-size:1.05rem">Run</h2>
        ${fields}
        <label class="field"><span>When a match is uncertain (hitl_mode)</span>
          <select name="hitl_mode">
            ${HITL_MODES.map((mode) => {
              const current = String((def as { hitl?: { mode?: string } }).hitl?.mode || "always_on_uncertain");
              const labels: Record<string, string> = {
                always_on_uncertain: "always_on_uncertain — pause for a person",
                llm_then_human: "llm_then_human — try AI first, then pause",
                llm_only: "llm_only — never pause, use AI",
              };
              const selected = current === mode ? "selected" : "";
              return `<option value="${mode}" ${selected}>${labels[mode]}</option>`;
            }).join("")}
          </select>
        </label>
        <button class="btn primary" type="submit">Run now</button>
      </form>`,
    );
  } catch (err) {
    return shell("Workflow", "Error", bannerError(err));
  }
}

function renderStep(step: StepTelemetry, pending = false): string {
  const state = pending ? "run" : step.success ? "ok" : "bad";
  const mark = pending ? "…" : step.success ? "✓" : "!";
  return `
    <div class="step">
      <div class="idx ${state}">${mark}</div>
      <div>
        <strong>${escapeHtml(step.step_type)}${step.description ? " · " + escapeHtml(step.description) : ""}</strong>
        <div class="meta">${fmtMs(step.latency_ms)} · ${fmtTokens(step)}${step.resolution ? " · " + escapeHtml(step.resolution) : ""}${step.error ? " · " + escapeHtml(step.error) : ""}</div>
      </div>
      ${step.tier ? `<span class="tier ${escapeHtml(step.tier)}">${escapeHtml(step.tier)}</span>` : `<span class="tier">${pending ? "running" : "—"}</span>`}
    </div>`;
}

function runView(run: RunRecord): string {
  const live = isLiveStatus(run.status);
  const duration =
    run.started_at && run.finished_at
      ? fmtMs(new Date(run.finished_at).getTime() - new Date(run.started_at).getTime())
      : live
        ? "in progress"
        : "—";
  const steps = (run.steps ?? []).map((s) => renderStep(s)).join("") || `<p class="muted">Waiting for the first step…</p>`;
  const outputs = run.outputs && Object.keys(run.outputs).length
    ? `<pre class="output">${escapeHtml(JSON.stringify(run.outputs, null, 2))}</pre>`
    : `<p class="muted">No outputs yet.</p>`;
  const prompt = extractConfirmation(run);
  const confirm = prompt ? renderConfirmation(run, prompt) : "";
  const spin = live ? ' <span class="spinner"></span>' : "";
  return shell(
    escapeHtml(run.workflow_name || "Run"),
    "Live run",
    `${confirm}<div class="card">
      <div class="row">
        <span class="pill ${statusClass(run.status)}"><span class="dot"></span>${escapeHtml(run.status)}${spin}</span>
        <span class="mono muted">${escapeHtml(run.id.slice(0, 10))}</span>
      </div>
      <div class="status-grid">
        <div class="stat"><b>${escapeHtml(duration)}</b><span>Duration</span></div>
        <div class="stat"><b>${escapeHtml(totalTokens(run))}</b><span>Tokens</span></div>
      </div>
      ${run.error ? `<div class="banner err" style="margin:12px 0 0">${escapeHtml(run.error)}</div>` : ""}
      <div style="margin-top:16px">${steps}</div>
    </div>
    <div class="card">
      <h2 style="margin:0 0 10px;font-size:1.05rem">Outputs</h2>
      ${outputs}
    </div>`,
    run.model ? `Model for repairs: ${escapeHtml(run.model)}` : "Deterministic first, AI only if a step breaks.",
  );
}

async function renderRun(id: string): Promise<string> {
  const setup = needSetup();
  if (setup) return setup;
  try {
    const run = await api.getRun(id);
    return runView(run);
  } catch (err) {
    return shell("Run", "Error", bannerError(err));
  }
}

async function renderRuns(): Promise<string> {
  const setup = needSetup();
  if (setup) return setup;
  try {
    const { runs } = await api.listRuns();
    const list = runs.length
      ? runs
          .map((run) => {
            const when = run.started_at ? new Date(run.started_at).toLocaleString() : "";
            return `<a class="card card-link" href="#/runs/${encodeURIComponent(run.id)}">
              <div class="row">
                <div class="grow">
                  <strong>${escapeHtml(run.workflow_name)}</strong>
                  <div class="meta">${escapeHtml(when)} · ${escapeHtml(totalTokens(run))}</div>
                </div>
                <span class="pill ${statusClass(run.status)}">${escapeHtml(run.status)}</span>
              </div>
            </a>`;
          })
          .join("")
      : `<div class="card empty">No runs yet. Open a workflow and press Run now.</div>`;
    return shell("Run history", "Telemetry", list, "Per-step tier, latency and tokens from earlier jobs.");
  } catch (err) {
    return shell("Run history", "Telemetry", bannerError(err));
  }
}


function mintJobId(job: MintJob): string {
  return String(job.job_id || job.id || "");
}

function mintSteps(job: MintJob): MintTraceStep[] {
  if (Array.isArray(job.trace?.steps)) return job.trace!.steps as MintTraceStep[];
  if (Array.isArray(job.steps)) return job.steps;
  return [];
}

function isLiveMint(status: string | undefined): boolean {
  const value = (status || "").toLowerCase();
  return value === "running" || value === "queued" || value === "needs_input" || value === "input_required";
}

function mintStatusLabel(job: MintJob): string {
  return String(job.status || "unknown");
}

function fmtWhen(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function renderMintStep(step: MintTraceStep): string {
  const state = step.success === false || step.error ? "bad" : step.phase === "note" ? "run" : "ok";
  const mark = state === "bad" ? "!" : state === "run" ? "…" : "✓";
  const title = step.title || step.description || step.phase || step.step_type || "step";
  const when = step.ts ? fmtWhen(step.ts) : "";
  const bits = [step.phase || step.step_type, when, step.source, step.error || step.detail].filter(Boolean);
  return `
    <div class="step">
      <div class="idx ${state}">${mark}</div>
      <div>
        <strong>${escapeHtml(title)}</strong>
        <div class="meta">${escapeHtml(bits.join(" · "))}</div>
      </div>
      <span class="tier ${escapeHtml(step.tier || step.phase || "pomerado")}">${escapeHtml(step.tier || step.phase || "pomerado")}</span>
    </div>`;
}

function mintJobView(job: MintJob): string {
  const id = mintJobId(job);
  const live = isLiveMint(job.status);
  const steps = mintSteps(job);
  const timeline = steps.map(renderMintStep).join("") || `<p class="muted">${live ? "Waiting for the first mint event…" : "No trace steps were saved."}</p>`;
  const partial = job.partial || job.trace?.partial;
  const duration =
    job.started_at && job.finished_at
      ? fmtMs(new Date(job.finished_at).getTime() - new Date(job.started_at).getTime())
      : live
        ? "in progress"
        : "—";
  const workflow = job.workflow && typeof job.workflow === "object" ? job.workflow : null;
  const workflowId = workflow?.id || (typeof job.workflow_id === "string" ? job.workflow_id : "");
  const spin = live ? ' <span class="spinner"></span>' : "";
  const banner = partial
    ? `<div class="banner info" style="margin:0 0 12px">This trace is partial — reconstructed from saved artifacts and the mint report. Mid-mint tool calls, stdout/stderr, and Guardian step logs were not captured live.</div>`
    : "";
  const wfLink = workflowId
    ? `<a class="btn" href="#/workflows/${encodeURIComponent(workflowId)}">Open workflow</a>`
    : "";
  return shell(
    escapeHtml(job.name || "Mint"),
    "Mint trace",
    `${banner}<div class="card">
      <div class="row">
        <span class="pill ${statusClass(mintStatusLabel(job))}"><span class="dot"></span>${escapeHtml(mintStatusLabel(job))}${spin}</span>
        <span class="mono muted">${escapeHtml(id.slice(0, 10))}</span>
      </div>
      <div class="status-grid">
        <div class="stat"><b>${escapeHtml(duration)}</b><span>Duration</span></div>
        <div class="stat"><b>${steps.length}</b><span>Events</span></div>
      </div>
      ${job.intent ? `<p class="sub" style="margin:12px 0 0">${escapeHtml(job.intent)}</p>` : ""}
      ${job.url ? `<div class="meta" style="margin-top:8px">${escapeHtml(job.url)}</div>` : ""}
      ${job.error ? `<div class="banner err" style="margin:12px 0 0">${escapeHtml(String(job.error))}</div>` : ""}
      <div style="margin-top:16px">${timeline}</div>
    </div>
    <div class="row" style="margin-top:12px;gap:8px">
      <a class="btn" href="#/mint">All mints</a>
      ${wfLink}
    </div>`,
    live ? "Live mint — this timeline updates as Pomerado reports progress." : job.trace_source === "backfill" || partial ? "Backfilled from saved Pomerado artifacts." : "Official Pomerado mint trace.",
  );
}

async function renderMint(): Promise<string> {
  const setup = needSetup();
  if (setup) return setup;
  let jobs: MintJob[] = [];
  let listError = "";
  let openaiConfigured = true;
  try {
    const status = await api.getKeyStatus();
    openaiConfigured = Boolean(status.openai?.configured);
  } catch {
    openaiConfigured = true;
  }
  try {
    jobs = (await api.listMintJobs()).jobs ?? [];
  } catch (err) {
    listError = err instanceof Error ? err.message : String(err);
  }
  const list = jobs.length
    ? jobs
        .map((job) => {
          const id = mintJobId(job);
          const when = fmtWhen(job.started_at);
          const count = job.step_count ?? mintSteps(job).length;
          const partial = job.partial ? " · partial" : "";
          return `<a class="card card-link" href="#/mint/${encodeURIComponent(id)}">
            <div class="row">
              <div class="grow">
                <strong>${escapeHtml(job.name || id || "Mint")}</strong>
                <div class="meta">${escapeHtml(when)}${when && count ? " · " : ""}${count ? escapeHtml(String(count) + " events") : ""}${escapeHtml(partial)}</div>
              </div>
              <span class="pill ${statusClass(mintStatusLabel(job))}">${escapeHtml(mintStatusLabel(job))}</span>
            </div>
          </a>`;
        })
        .join("")
    : listError
      ? bannerError(listError)
      : `<div class="card empty">No mint jobs yet. Start one below — the trace will show up here the same way a run does.</div>`;
  return shell(
    "Mint a Pomerado tool",
    "Deterministic",
    `${!openaiConfigured ? `<div class="banner info">Add an OpenAI key in <a href="#/settings">Settings</a> before minting. It stays on this computer and is never written to the zip or logs.</div>` : ""}
    ${list}
    <form id="mint-form" class="card">
      <p class="sub" style="margin-top:0">Pomerado explores a site once with the OpenAI key saved in Settings, then later runs cost no tokens. This can take a while. Open a job to watch its trace, like a workflow run.</p>
      <label class="field"><span>Tool name</span><input name="name" required type="text" placeholder="example_reader" /></label>
      <label class="field"><span>URL</span><input name="url" required type="url" placeholder="https://example.com" /></label>
      <label class="field"><span>Intent</span><textarea name="intent" required placeholder="Read the main heading" style="min-height:88px;font-family:var(--font);font-size:16px"></textarea></label>
      <label class="field"><span>Effect</span>
        <select name="effect">
          <option value="read" selected>read</option>
          <option value="write">write</option>
        </select>
      </label>
      <div class="field">
        <span>Inputs</span>
        <p class="muted" style="margin:0 0 8px">Named values the minted tool should ask for on every run (for example a search term). You can rename or add more after you start, and they become required fields on the Run screen.</p>
        <div id="mint-inputs" class="var-list">
          <label class="var-row">
            <input type="text" name="input-name-0" value="term" placeholder="name" aria-label="Input name" />
            <input type="text" name="input-example-0" value="" placeholder="example value" aria-label="Example value" />
          </label>
        </div>
        <button type="button" class="btn" data-action="mint-add-input">Add input</button>
      </div>
      <button class="btn primary" type="submit">Start mint</button>
    </form>
    <div id="mint-status"></div>`,
    "Past mint jobs and a live timeline of the one you open.",
  );
}

async function renderMintJob(id: string): Promise<string> {
  const setup = needSetup();
  if (setup) return setup;
  try {
    const job = await api.getMintJob(id);
    return mintJobView(job);
  } catch (err) {
    return shell("Mint", "Error", `${bannerError(err)}<a class="btn" href="#/mint">All mints</a>`);
  }
}


function formatMarketDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "";
  return date.toLocaleString();
}

function inputSummary(listing: MarketListing): string {
  if (!listing.inputs.length) return "No declared inputs";
  return listing.inputs
    .map((input) => {
      const label = input.label || input.name;
      return input.required === false ? label : `${label} (required)`;
    })
    .join(", ");
}

function listingCard(listing: MarketListing): string {
  const warning = listing.contains_code
    ? `<div class="banner info" style="margin-top:10px">This package contains executable automation code. Review it before running.</div>`
    : "";
  const download = packageUrl(listing.id);
  return `<article class="card" data-listing-id="${escapeHtml(listing.id)}">
    <h2 style="margin:0 0 6px;font-size:1.05rem">${escapeHtml(listing.name)}</h2>
    <div class="mono muted">${escapeHtml(listing.type)}${listing.tier && listing.tier !== listing.type ? ` · ${escapeHtml(listing.tier)}` : ""} · v${escapeHtml(listing.version)} · ${escapeHtml(formatMarketDate(listing.created_at))}</div>
    <p class="sub" style="margin:10px 0">${escapeHtml(listing.description || "No description")}</p>
    <p class="muted" style="margin:0">Inputs: ${escapeHtml(inputSummary(listing))}</p>
    <p class="mono muted" style="margin:8px 0 0;word-break:break-all">sha256 ${escapeHtml(listing.sha256)}</p>
    ${warning}
    <div class="btn-row" style="margin-top:14px">
      <a class="btn" href="${escapeHtml(download)}" data-action="market-download" data-id="${escapeHtml(listing.id)}">Download package</a>
      <button type="button" class="btn danger" data-action="market-delete" data-id="${escapeHtml(listing.id)}">Delete</button>
    </div>
  </article>`;
}

async function renderMarketplace(): Promise<string> {
  const s = loadSettings();
  if (!s.accessToken) {
    return shell(
      "Marketplace",
      "Private automations",
      `<div class="card">
        <p class="sub" style="margin-top:0">Listings are private and need the same shared access token you use for the phone site. Add it in Settings, then come back here to browse or publish.</p>
        <div style="margin-top:16px"><a class="btn primary" href="#/settings">Open settings</a></div>
      </div>`,
    );
  }
  let listings: MarketListing[] = [];
  let library: WorkflowSummary[] = [];
  let errHtml = "";
  try {
    listings = (await listMarket()).listings;
  } catch (err) {
    errHtml = bannerError(err);
  }
  let libraryErr = "";
  try {
    library = (await api.listWorkflows()).workflows;
  } catch (err) {
    library = [];
    libraryErr = bannerError(err);
  }
  const hashQuery = new URLSearchParams((location.hash.split("?")[1] || "").replace(/^\?/, ""));
  const preselect = hashQuery.get("publish") || "";
  const cards = listings.length
    ? listings.map(listingCard).join("")
    : `<div class="card"><p class="sub" style="margin:0">No automations published yet. Choose one of your workflows below and tap Publish.</p></div>`;
  const options = library.length
    ? library
        .map((w) => {
          const existing = matchListing(listings, w.name, listingIdForName(w.name));
          const label = `${w.name}${existing ? ` (published v${existing.version} — Update)` : ""}`;
          const selected = w.id === preselect ? "selected" : "";
          return `<option value="${escapeHtml(w.id)}" ${selected}>${escapeHtml(label)}</option>`;
        })
        .join("")
    : "";
  const form = `<form id="market-publish-form" class="card">
    <h2 style="margin:0 0 12px;font-size:1.05rem">Publish</h2>
    <p class="muted" style="margin:0 0 12px">Select one of your automations, edit the details if you want, then publish. Republishing bumps the version. No zip is needed for workflow-use or from-task (Pomerado code packages still use a zip).</p>
    ${
      library.length
        ? `<label class="field"><span>Your automations</span>
      <select name="workflowId" id="market-source">
        <option value="">Choose a workflow…</option>
        ${options}
      </select>
    </label>
    <div id="market-publish-fields" hidden>
      <label class="field"><span>Name</span><input name="name" required placeholder="Wikipedia search reader" /></label>
      <label class="field"><span>Description</span><textarea name="description" rows="3" placeholder="What this automation does"></textarea></label>
      <label class="field"><span>Type</span>
        <select name="type">
          <option value="workflow-use">workflow-use</option>
          <option value="from-task">from-task</option>
        </select>
      </label>
      <label class="field"><span>Declared inputs (editable)</span>
        <textarea name="inputs" rows="3" placeholder='[{"name":"term","label":"Search term","required":true,"type":"string"}]'></textarea>
      </label>
    </div>
    <p id="market-pomerado-note" class="muted" hidden>This is a Pomerado tool. Publishing still needs its integration zip — zip upload is only for those code packages.</p>`
        : `<p class="muted">Connect the backend in Settings to publish from your workflow library. ${isLocalOrigin() ? "This page should already be talking to the local app." : "Add the tunnel URL if you are on the phone site."}</p>`
    }
    <div id="market-publish-status" style="margin-top:12px"></div>
    <div class="btn-row" style="margin-top:16px">
      <button class="btn primary wide" id="market-publish-submit" type="submit" disabled>Publish</button>
    </div>
  </form>`;
  const flash = flashMessage ? `<div class="banner ${flashMessage.kind}">${escapeHtml(flashMessage.text)}</div>` : "";
  flashMessage = null;
  const html = shell("Marketplace", "Private automations", `${flash}${errHtml}${libraryErr}${form}${cards}`);
  queueMicrotask(() => {
    const select = document.getElementById("market-source") as HTMLSelectElement | null;
    if (select && preselect) {
      select.value = preselect;
      void fillPublishFields(select.value, listings).catch(() => undefined);
    }
  });
  return html;
}

function inputPayload(inputs: WorkflowInput[]): MarketInput[] {
  return inputs.map((input) => ({
    name: input.name,
    label: prettyInputLabel(input.name, input.label),
    type: input.type || "string",
    required: input.required !== false,
    description: input.description,
  }));
}

async function fillPublishFields(workflowId: string, listings?: MarketListing[]): Promise<void> {
  const fields = document.getElementById("market-publish-fields");
  const note = document.getElementById("market-pomerado-note");
  const submit = document.getElementById("market-publish-submit") as HTMLButtonElement | null;
  const form = document.getElementById("market-publish-form") as HTMLFormElement | null;
  if (!form || !fields || !submit) return;
  if (!workflowId) {
    fields.hidden = true;
    if (note) note.hidden = true;
    submit.disabled = true;
    submit.textContent = "Publish";
    return;
  }
  const stored = await api.getWorkflow(workflowId);
  const def = stored.definition;
  const typ = inferWorkflowType(def as unknown as Record<string, unknown>);
  if (note) note.hidden = typ !== "pomerado";
  if (typ === "pomerado") {
    fields.hidden = true;
    submit.disabled = true;
    submit.textContent = "Publish";
    return;
  }
  fields.hidden = false;
  const published = (listings || []).length
    ? matchListing(listings || [], def.name, listingIdForName(def.name))
    : undefined;
  (form.elements.namedItem("name") as HTMLInputElement).value = def.name;
  (form.elements.namedItem("description") as HTMLTextAreaElement).value = def.description || "";
  (form.elements.namedItem("type") as HTMLSelectElement).value = typ;
  (form.elements.namedItem("inputs") as HTMLTextAreaElement).value = JSON.stringify(inputPayload(deriveWorkflowInputs(def)), null, 2);
  form.dataset.workflowId = stored.id;
  submit.disabled = false;
  submit.textContent = published ? "Update" : "Publish";
}

async function renderSettings(): Promise<string> {


  const s = loadSettings();
  let openaiConfigured = false;
  try {
    const status = await api.getKeyStatus();
    openaiConfigured = Boolean(status.openai?.configured);
  } catch {
    /* backend may still be starting */
  }
  const modelBlock = [DEFAULT_MODEL, ALT_MODEL]
    .map((model) => {
      const label = model === DEFAULT_MODEL ? "Fast and cheap. Best default for repairs." : "Alternative OpenRouter model.";
      const checked = s.model === model ? "checked" : "";
      return `<label class="model"><input type="radio" name="model" value="${escapeHtml(model)}" ${checked} /><div><b>${escapeHtml(model)}</b><small>${label}</small></div></label>`;
    })
    .join("");
  return shell(
    "Settings",
    "This device",
    `<form id="settings-form" class="card">
      <label class="field"><span>Backend URL</span>
        <input name="backendUrl" type="url" placeholder="${isLocalOrigin() ? "http://localhost:8745" : "auto (published tunnel)"}" value="${escapeHtml(s.backendUrl)}" />
      </label>
      <p class="muted" style="margin-top:-6px">${isLocalOrigin() ? "Served on this computer — defaults to this page's origin. Leave this as-is unless you moved the backend." : isManagedBackendUrl(s.backendUrl) || !s.backendUrl ? "The current tunnel URL is picked up automatically. You can leave this blank or ignore a trycloudflare.com URL — you do not need to paste a new one when the tunnel restarts. Only set a custom URL if you use something else." : "Custom backend URL (not auto-updated). Clear this field to go back to automatic discovery."}</p>
      <label class="field"><span>Shared access token${isLocalOrigin() ? " (optional on localhost)" : ""}</span>
        <div class="secret-wrap">
          <input name="accessToken" type="password" autocomplete="off" value="${escapeHtml(s.accessToken)}" />
          <button type="button" class="btn peek" data-peek="accessToken">Show</button>
        </div>
      </label>
      <p class="muted" style="margin-top:-6px">The same token unlocks the private Marketplace (list, publish, and download).</p>
      ${isLocalOrigin() && !openaiConfigured ? `<div class="banner info">Add an OpenAI key to mint Pomerado tools. It is stored only on this computer, never in the app zip or logs.</div>` : ""}
      <label class="field"><span>OpenAI key (Pomerado minting)</span>
        <div class="secret-wrap">
          <input name="openaiKey" type="password" autocomplete="off" placeholder="${openaiConfigured ? "A key is saved on this computer — paste to replace" : "sk-…"}" value="" />
          <button type="button" class="btn peek" data-peek="openaiKey">Show</button>
        </div>
      </label>
      <p class="muted" style="margin-top:-6px">${openaiConfigured ? "A key is already saved on this computer (not in this browser). Leave the box blank to keep it, or paste a new one to replace it." : "Saved only on this computer (not in localStorage or the zip). Needed for Pomerado minting on this machine."}${isLocalOrigin() ? ` <button type="button" class="btn" data-action="clear-openai-key" ${openaiConfigured ? "" : "disabled"}>Remove saved key</button>` : ""}</p>
      <label class="field"><span>OpenRouter key</span>
        <div class="secret-wrap">
          <input name="openrouterKey" type="password" autocomplete="off" placeholder="sk-or-v1-…" value="${escapeHtml(s.openrouterKey)}" />
          <button type="button" class="btn peek" data-peek="openrouterKey">Show</button>
        </div>
      </label>
      <p class="muted" style="margin-top:-6px">Stored only in this browser's localStorage. Used for from-task, repair, and browser-use — not for Pomerado minting.</p>
      <span class="field"><span>Repair model</span></span>
      <div class="models">${modelBlock}</div>
      <div class="btn-row" style="margin-top:16px">
        <button class="btn" type="button" data-action="test-connection">Test connection</button>
        <button class="btn primary" type="submit">Save</button>
      </div>
      <div id="settings-status" style="margin-top:12px">${flashMessage ? `<div class="banner ${flashMessage.kind}">${escapeHtml(flashMessage.text)}</div>` : ""}</div>
    </form>`,
  );
  flashMessage = null;
}


function suggestionRows(job: FromTaskJob): FromTaskSuggestion[] {
  const confirmation = job.confirmation;
  if (confirmation?.suggestions?.length) return confirmation.suggestions;
  return job.suggestions ?? [];
}


function renderRecordPage(): string {
  const setup = needSetup();
  if (setup) return setup;
  const s = recordState;
  const live = s.phase === "live";
  const cleaning = s.phase === "cleaning";
  const sourceToggle = `<div class="source-toggle" role="tablist">
      <button type="button" class="btn ${s.source === "remote" ? "primary" : ""}" data-action="record-source" data-source="remote" ${live || cleaning ? "disabled" : ""}>Remote browser</button>
      <button type="button" class="btn ${s.source === "extension" ? "primary" : ""}" data-action="record-source" data-source="extension" ${live || cleaning ? "disabled" : ""}>Record in my Chrome</button>
    </div>`;
  const extNote = s.source === "extension"
    ? (s.extensionReady
      ? `<p class="muted">The Browser Platform extension is available in this Chrome. Start, do the task in your own tabs, then stop — the cleaned steps come back here to review, save, and publish.</p>`
      : `<div class="banner info">This page can't record your phone's browser, and the extension is not visible here. On a computer with the extension installed, choose this option and reload. Otherwise use <strong>Remote browser</strong>, or open the extension's <strong>Record</strong> tab, then publish from Market after you save.</div>`)
    : `<p class="muted">A Chromium session starts on the backend and is streamed here. Tap or click the picture to click; use the box below to type (needed on a phone). Idle sessions close after 3 minutes, and at most two can run at once.</p>`;
  const controls = `<div class="card" id="record-setup">
      ${sourceToggle}
      ${extNote}
      <label class="field"><span>Goal (optional, one line)</span>
        <input id="record-goal" value="${escapeHtml(s.goal)}" placeholder="Find the fixtures for a football team" ${live || cleaning ? "disabled" : ""} />
      </label>
      <label class="field"><span>Name</span>
        <input id="record-name" value="${escapeHtml(s.name)}" placeholder="football_fixtures_recorded" ${live || cleaning ? "disabled" : ""} />
      </label>
      <div class="btn-row">
        ${
          live
            ? `<button class="btn danger wide" data-action="record-stop" ${cleaning ? "disabled" : ""} id="record-stop">Stop recording</button>`
            : `<button class="btn primary wide" data-action="record-start" ${cleaning ? "disabled" : ""} id="record-start">Start recording</button>`
        }
        ${live && s.source === "remote" ? `<button class="btn" data-action="record-cancel">Cancel session</button>` : ""}
      </div>
      ${cleaning ? `<p class="muted">Cleaning the recording…</p>` : ""}
      ${s.error ? `<div class="banner err" style="margin-top:10px">${escapeHtml(s.error)}</div>` : ""}
    </div>`;
  const remote = s.source === "remote" && live
    ? `<div class="card record-stage">
        <form id="record-url-form" class="url-bar">
          <input id="record-url" type="url" inputmode="url" value="${escapeHtml(s.url)}" placeholder="https://www.bbc.com/sport/football" />
          <button class="btn" type="submit">Go</button>
        </form>
        <div class="record-screen-wrap">
          <img id="record-screen" alt="Remote browser" src="${s.frame ? escapeHtml(s.frame) : ""}" />
          ${s.frame ? "" : `<div id="record-screen-placeholder" class="record-placeholder">Starting the remote browser…</div>`}
        </div>
        <div class="record-meta"><span id="record-event-count">${s.eventCount} event${s.eventCount === 1 ? "" : "s"}</span> · ${escapeHtml(s.url || "about:blank")}</div>
        <form id="record-type-form" class="type-bar">
          <input id="record-type" type="text" enterkeyhint="send" placeholder="Type into the focused field" />
          <button class="btn" type="submit" data-enter="0">Send</button>
          <button class="btn" type="submit" data-enter="1">Send + Enter</button>
        </form>
      </div>`
    : "";
  const extLive = s.source === "extension" && live
    ? `<div class="banner info">Recording in your Chrome. Switch to the tab you want, do the task (redundant clicks are fine), then come back and press Stop.</div>`
    : "";
  const review = s.review.length
    ? `<div class="card" id="record-review">
        <h2 style="margin:0 0 8px;font-size:1.05rem">Review steps</h2>
        <p class="muted">${escapeHtml(s.usageLabel)}</p>
        <p class="muted">${escapeHtml(s.statsLine)}</p>
        <div class="trace">${s.review.map((item, i) => {
          const dropped = !item.kept;
          const reason = item.reason ? `<div class="muted">${escapeHtml(item.reason)}</div>` : "";
          return `<div class="step rec-step ${dropped ? "dropped" : "kept"}">
            <span class="idx">${i + 1}</span>
            <div>
              <div>${escapeHtml(stepSummary(item.step))}</div>
              ${reason}
              <button type="button" class="btn" data-action="${dropped ? "record-restore" : "record-drop"}" data-rec-id="${escapeHtml(item.id)}">${dropped ? "Restore" : "Delete"}</button>
            </div>
          </div>`;
        }).join("")}</div>
      </div>
      <div class="card">
        <h2 style="margin:0 0 8px;font-size:1.05rem">Inputs</h2>
        <p class="muted">These become required labelled fields when you run the workflow. Confirm or edit them.</p>
        ${
          s.parameters.length
            ? s.parameters.map((param, i) => `<div class="param-row">
                <label class="field"><span>Name</span><input data-param="${i}" data-field="name" value="${escapeHtml(param.name)}" /></label>
                <label class="field"><span>Label</span><input data-param="${i}" data-field="label" value="${escapeHtml(param.label || param.name)}" /></label>
                <label class="field"><span>Example</span><input data-param="${i}" data-field="example" value="${escapeHtml(param.example || "")}" /></label>
                <label class="check"><input type="checkbox" data-param="${i}" data-field="required" ${param.required === false ? "" : "checked"} /> Required</label>
              </div>`).join("")
            : `<p class="muted">No parameters detected. You can still save a fixed workflow.</p>`
        }
        <div class="btn-row"><button class="btn" data-action="record-add-param">Add input</button></div>
      </div>`
    : "";
  const save = s.review.some((item) => item.kept)
    ? `<div class="btn-row">
        <button class="btn primary wide" data-action="record-save" id="record-save" ${s.phase === "cleaning" ? "disabled" : ""}>${s.workflowId ? "Saved" : "Save workflow"}</button>
        <button class="btn wide" data-action="record-publish" id="record-publish" ${s.workflowId ? "" : "disabled"}>Publish to marketplace</button>
      </div>
      ${s.workflowId ? `<p class="muted">Saved as <a href="#/workflows/${encodeURIComponent(s.workflowId)}">${escapeHtml(s.workflowId)}</a>. You can run it from the library or publish it now.</p>` : ""}
      ${s.listing ? `<div class="banner ok">Published ${escapeHtml(s.listing.name)} v${escapeHtml(s.listing.version)} to the marketplace.</div>` : ""}`
    : "";
  const flash = flashMessage ? `<div class="banner ${flashMessage.kind}">${escapeHtml(flashMessage.text)}</div>` : "";
  flashMessage = null;
  return shell(
    "Record",
    "Authoring",
    `${flash}${controls}${extLive}${remote}${review}${save}`,
    "Do the task yourself, then keep only the steps you need and publish.",
  );
}

function renderFromTaskForm(): string {
  const settings = loadSettings();
  const keyNote = settings.openrouterKey
    ? "Your OpenRouter key will be sent with this request only and is not stored on the server."
    : "Add an OpenRouter key in Settings first. The demonstration uses browser-use once, then a 0-token replay.";
  return shell(
    "Create from task",
    "Authoring",
    `${!settings.openrouterKey ? `<div class="banner info">Add an OpenRouter key in <a href="#/settings">Settings</a> so browser-use can demonstrate the task.</div>` : ""}
    <form id="from-task-form" class="card">
      <p class="sub" style="margin-top:0">Describe the task in plain language. First we extract the inputs (team, search term, and so on) so you can confirm them. The agent then has to apply those filters, and they become required labelled fields on the saved workflow.</p>
      <label class="field"><span>Task</span>
        <textarea name="task" required placeholder="Search the library for {query} and extract the first paragraph." style="min-height:110px;font-family:var(--font);font-size:16px"></textarea>
      </label>
      <label class="field"><span>Start URL (optional)</span>
        <input name="start_url" type="url" placeholder="http://127.0.0.1:8765/from_task_form.html" />
      </label>
      <label class="field"><span>Workflow name (optional)</span>
        <input name="name" type="text" placeholder="library_search" />
      </label>
      <label class="field"><span>Variables (optional, name=value per line)</span>
        <textarea name="variables" placeholder="query=browser automation" style="min-height:88px;font-family:var(--mono);font-size:14px"></textarea>
      </label>
      <label class="check">
        <input type="checkbox" name="confirm_variables" checked />
        <span>Ask me to confirm, edit, or add the extracted inputs before the demonstration starts</span>
      </label>
      <p class="muted">${escapeHtml(keyNote)}</p>
      <button class="btn primary" type="submit">Start demonstration</button>
    </form>`,
    "browser-use demonstrates once, then a deterministic replay validates the workflow.",
  );
}

function renderFromTaskProgress(job: FromTaskJob): string {
  const live = isLiveJob(job.status);
  const spin = live ? ' <span class="spinner"></span>' : "";
  const confirmation = job.status === "needs_confirmation" ? renderFromTaskConfirmation(job) : "";
  const result = job.status === "succeeded" ? renderFromTaskResult(job) : "";
  const error = job.error && job.status !== "succeeded" ? `<div class="banner err">${escapeHtml(job.error)}</div>` : "";
  const creation = job.creation;
  return shell(
    escapeHtml(job.name || "From task"),
    "Live job",
    `${confirmation}
    <div class="card">
      <div class="row">
        <span class="pill ${statusClass(job.status)}"><span class="dot"></span>${escapeHtml(job.status)}${spin}</span>
        <span class="mono muted">${escapeHtml(job.id.slice(0, 10))}</span>
      </div>
      <p class="sub">${escapeHtml(job.task)}</p>
      ${error}
      <div class="status-grid">
        <div class="stat"><b>${escapeHtml(formatLatency(creation?.latency_ms))}</b><span>Creation time</span></div>
        <div class="stat"><b>${escapeHtml(formatTokens(creation))}</b><span>Creation tokens</span></div>
      </div>
    </div>
    ${result}
    <a class="btn" href="#/from-task" style="margin-top:12px">Start another</a>`,
    live ? "The agent is working in the box browser. This page will update as the job moves." : "",
  );
}

function renderFromTaskConfirmation(job: FromTaskJob): string {
  const payload = job.confirmation || {};
  const kind = payload.kind || "variables";
  if (kind === "parameters") {
    const params = (payload.parameters || job.parameters || []) as { name?: string; label?: string; example?: string; required?: boolean }[];
    const rows = params.length
      ? params
          .map((item, i) => {
            const name = item.name || `input_${i + 1}`;
            const label = item.label || name;
            const existing = ((payload.variables as Record<string, string> | undefined) || {})[name] || "";
            const value = item.example ?? existing;
            const required = item.required !== false;
            return `<label class="var-row var-row-params">
            <input type="checkbox" name="required-${i}" ${required ? "checked" : ""} data-index="${i}" title="Required" />
            <input type="text" name="name-${i}" value="${escapeHtml(name)}" aria-label="Input name" placeholder="name" />
            <input type="text" name="label-${i}" value="${escapeHtml(label)}" aria-label="Label" placeholder="Label" />
            <input type="text" name="value-${i}" value="${escapeHtml(String(value))}" aria-label="Example value" placeholder="example value" />
          </label>`;
          })
          .join("")
      : `<p class="muted">No inputs were detected. Add one below if this task should be reusable with a filter or search term.</p>`;
    return `<div class="card confirm-panel" id="from-task-confirm" data-job-id="${escapeHtml(job.id)}" data-kind="parameters">
    <p class="eyebrow">Needs confirmation · inputs</p>
    <h2>Confirm the inputs this workflow will use</h2>
    <p class="sub">These become required labelled fields, and the agent must actually apply each one (not just mention it). Edit, uncheck required, or add a missing input before the demonstration starts.</p>
    <div class="var-list-head muted">Required · name · label · example</div>
    <div class="var-list">${rows}</div>
    <button type="button" class="btn" data-action="from-task-add-input" style="margin-bottom:12px">Add input</button>
    <div class="btn-row">
      <button type="button" class="btn danger" data-action="from-task-cancel">Cancel</button>
      <button type="button" class="btn primary" data-action="from-task-confirm">Confirm and start</button>
    </div>
    <div id="from-task-confirm-status" style="margin-top:12px"></div>
  </div>`;
  }
  if (kind !== "variables") {
    const message = payload.message || payload.prompt || "The agent is waiting for a confirmation.";
    return `<div class="card confirm-panel" id="from-task-confirm" data-job-id="${escapeHtml(job.id)}" data-kind="${escapeHtml(kind)}">
      <p class="eyebrow">Needs confirmation</p>
      <h2>The agent has a question</h2>
      <p class="sub">${escapeHtml(message)}</p>
      <div class="btn-row">
        <button type="button" class="btn danger" data-action="from-task-cancel">Cancel</button>
        <button type="button" class="btn primary" data-action="from-task-confirm">Confirm</button>
      </div>
      <div id="from-task-confirm-status" style="margin-top:12px"></div>
    </div>`;
  }
  const suggestions = suggestionRows(job);
  const already = payload.variables || job.variables || {};
  const alreadyBits = Object.keys(already).length
    ? `<p class="meta">Already in the workflow: ${escapeHtml(Object.entries(already).map(([k, v]) => `${k}=${v}`).join(", "))}</p>`
    : "";
  const rows = suggestions.length
    ? suggestions
        .map((item, i) => {
          const name = item.suggested_name || `var_${i + 1}`;
          const value = item.value ?? "";
          const hint = item.element && (item.element.name || item.element.placeholder || item.element.role);
          return `<label class="var-row">
            <input type="checkbox" name="accept-${i}" checked data-index="${i}" />
            <input type="text" name="name-${i}" value="${escapeHtml(name)}" aria-label="Variable name" />
            <input type="text" name="value-${i}" value="${escapeHtml(value)}" aria-label="Detected value" />
            ${hint || item.type ? `<span class="muted" style="grid-column:2 / -1">${escapeHtml(item.type || "")}${hint ? " · " + escapeHtml(String(hint)) : ""}</span>` : ""}
          </label>`;
        })
        .join("")
    : `<p class="muted">No extra variables were suggested. Add one below, or confirm to continue.</p>`;
  return `<div class="card confirm-panel" id="from-task-confirm" data-job-id="${escapeHtml(job.id)}" data-kind="variables">
    <p class="eyebrow">Needs confirmation · variables</p>
    <h2>Accept or rename detected variables</h2>
    <p class="sub">Literals typed during the demonstration can become workflow inputs. Uncheck any you want left as fixed text.</p>
    ${alreadyBits}
    <div class="var-list">${rows}</div>
    <button type="button" class="btn" data-action="from-task-add-input" style="margin-bottom:12px">Add input</button>
    <div class="btn-row">
      <button type="button" class="btn danger" data-action="from-task-cancel">Cancel</button>
      <button type="button" class="btn primary" data-action="from-task-confirm">Confirm variables</button>
    </div>
    <div id="from-task-confirm-status" style="margin-top:12px"></div>
  </div>`;
}

function renderFromTaskResult(job: FromTaskJob): string {
  const def = job.definition;
  const wfId = job.workflow_id;
  const steps = def?.steps
    ?.map(
      (step, i) => `
      <div class="step">
        <div class="idx">${i + 1}</div>
        <div>
          <strong>${escapeHtml(step.type)}</strong>
          <div class="muted">${escapeHtml(step.description || step.url || step.value || step.extractionGoal || "")}</div>
        </div>
      </div>`,
    )
    .join("") || `<p class="muted">No steps were saved.</p>`;
  const replay = job.replay;
  const replayStatus = replay?.status ? `<span class="pill ${statusClass(String(replay.status))}">${escapeHtml(String(replay.status))}</span>` : "";
  const outputs = replay?.outputs && Object.keys(replay.outputs).length
    ? `<pre class="output">${escapeHtml(JSON.stringify(replay.outputs, null, 2))}</pre>`
    : "";
  return `<div class="card" id="from-task-result">
    <h2 style="margin:0 0 8px;font-size:1.08rem">${escapeHtml(def?.name || job.name)}</h2>
    <div class="mono muted">${wfId ? "workflow " + escapeHtml(wfId) : "no workflow id yet"}</div>
    <div class="usage-compare">
      <div class="stat">
        <b>${escapeHtml(formatLatency(job.creation?.latency_ms))}</b>
        <span>Creation · ${escapeHtml(formatTokens(job.creation))}</span>
      </div>
      <div class="stat">
        <b>${escapeHtml(formatLatency(replay?.latency_ms))}</b>
        <span>Replay · ${escapeHtml(formatTokens(replay))} ${replayStatus}</span>
      </div>
    </div>
    <div style="margin-top:16px">${steps}</div>
    ${outputs ? `<div style="margin-top:12px">${outputs}</div>` : ""}
    <div class="btn-row" style="margin-top:16px">
      ${wfId ? `<a class="btn" href="#/workflows/${encodeURIComponent(wfId)}">Open workflow</a>
      <a class="btn primary" href="#/workflows/${encodeURIComponent(wfId)}" data-action="from-task-run" data-workflow-id="${escapeHtml(wfId)}">Run</a>` : `<span class="muted">Workflow was not saved.</span>`}
    </div>
  </div>`;
}

async function renderFromTaskPage(jobId?: string): Promise<string> {
  const setup = needSetup();
  if (setup) return setup;
  if (!jobId) return renderFromTaskForm();
  try {
    const job = await getFromTaskJob(jobId);
    return renderFromTaskProgress(job);
  } catch (err) {
    return shell("Create from task", "Job", `${bannerError(err)}<a class="btn" href="#/from-task">Start another</a>`);
  }
}

async function render(): Promise<void> {
  if (pollTimer) {
    window.clearInterval(pollTimer);
    pollTimer = undefined;
  }
  if (runSocket) {
    runSocket.close();
    runSocket = null;
  }
  const route = parseRoute();
  let html: string;
  if (route.name === "settings") html = await renderSettings();
  else if (route.name === "marketplace") html = await renderMarketplace();
  else if (route.name === "record") html = renderRecordPage();
  else if (route.name === "from-task") html = await renderFromTaskPage();
  else if (route.name === "from-task-job") html = await renderFromTaskPage(route.id);
  else if (route.name === "mint") html = await renderMint();
  else if (route.name === "mint-job") html = await renderMintJob(route.id);
  else if (route.name === "runs") html = await renderRuns();
  else if (route.name === "run") html = await renderRun(route.id);
  else if (route.name === "new") html = await renderNew();
  else if (route.name === "workflow") html = await renderWorkflow(route.id);
  else html = await renderWorkflows();
  root.innerHTML = html;

  if (route.name === "run") startRunPolling(route.id);
  if (route.name === "mint-job") startMintPolling(route.id);
  if (route.name === "from-task-job") startFromTaskPolling(route.id);
  if (route.name === "record") {
    bindRecordScreen();
    resumeLiveSession();
    void refreshExtensionProbe().then(() => {
      if (parseRoute().name === "record" && recordState.source === "extension") {
        const html2 = renderRecordPage();
        root.innerHTML = html2;
        bindRecordScreen();
      }
    });
  } else {
    teardownRecordSockets();
  }
}

function startRunPolling(id: string): void {
  const apply = (run: RunRecord) => {
    if (parseRoute().name !== "run") return;
    const panel = document.getElementById("hitl-panel");
    const prompt = extractConfirmation(run);
    const samePrompt =
      run.status === "needs_confirmation" &&
      Boolean(panel) &&
      panel?.dataset.runId === run.id &&
      prompt?.id === panel?.dataset.promptId;
    const focused = document.activeElement as HTMLElement | null;
    const interacting = Boolean(focused?.closest?.("#hitl-panel"));
    const shouldRefresh = !samePrompt && !interacting && (!focused || focused === document.body || focused.tagName === "A");
    if (shouldRefresh) root.innerHTML = runView(run);
    if (!isLiveStatus(run.status) && pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = undefined;
      if (runSocket) {
        runSocket.close();
        runSocket = null;
      }
      root.innerHTML = runView(run);
    }
  };

  const enrich = async (run: RunRecord): Promise<RunRecord> => {
    if (run.status === "needs_confirmation" && !extractConfirmation(run)) {
      try {
        const prompt = await getConfirmation(run.id);
        if (prompt) run = { ...run, confirmation: prompt };
      } catch {
        /* poll will retry */
      }
    }
    return run;
  };

  runSocket = openRunSocket(id, (event) => {
    if (event.type === "needs_confirmation" || event.type === "run_resumed" || event.type === "run_updated") {
      void api.getRun(id).then(enrich).then(apply);
    }
  });

  const tick = async () => {
    if (parseRoute().name !== "run") return;
    try {
      apply(await enrich(await api.getRun(id)));
    } catch {
      /* keep last view; next tick retries */
    }
  };
  pollTimer = window.setInterval(tick, 800);
}


function startMintPolling(id: string): void {
  const apply = (job: MintJob) => {
    if (parseRoute().name !== "mint-job") return;
    root.innerHTML = mintJobView(job);
    if (!isLiveMint(job.status) && pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = undefined;
      if (runSocket) {
        runSocket.close();
        runSocket = null;
      }
      root.innerHTML = mintJobView(job);
    }
  };
  runSocket = openRunSocket(id, (event) => {
    const typ = String(event.type || "");
    if (typ === "mint_updated" || typ === "mint_step" || typ === "mint_finished" || typ === "hello_ack") {
      void api.getMintJob(id).then(apply).catch(() => undefined);
    }
  });
  const tick = async () => {
    if (parseRoute().name !== "mint-job") return;
    try {
      apply(await api.getMintJob(id));
    } catch {
      /* keep last view; next tick retries */
    }
  };
  pollTimer = window.setInterval(tick, 1500);
}


function startFromTaskPolling(id: string): void {
  const tick = async () => {
    if (parseRoute().name !== "from-task-job") return;
    try {
      const job = await getFromTaskJob(id);
      const panel = document.getElementById("from-task-confirm");
      const focused = document.activeElement as HTMLElement | null;
      const interacting = Boolean(focused?.closest?.("#from-task-confirm"));
      const keepConfirm = Boolean(panel) && job.status === "needs_confirmation";
      if (!interacting && !keepConfirm) root.innerHTML = renderFromTaskProgress(job);
      if (!isLiveJob(job.status) && pollTimer) {
        window.clearInterval(pollTimer);
        pollTimer = undefined;
        root.innerHTML = renderFromTaskProgress(job);
      }
      void panel;
    } catch {
      /* next tick */
    }
  };
  pollTimer = window.setInterval(tick, 900);
}


async function seedExample(): Promise<void> {
  const created = await api.createWorkflow({
    name: "example_com_heading",
    description: "Read the title and page text on example.com (read-only smoke workflow).",
    version: "1",
    cache_results: true,
    steps: [
      {
        type: "navigation",
        description: "Open example.com",
        url: "https://example.com",
        expected_state: { url_includes: "example.com", title_contains: "Example Domain" },
      },
      {
        type: "extract",
        description: "Read the page title",
        extractionGoal: "the page title",
        expected_state: { title_contains: "Example Domain" },
        output: "heading",
      },
    ],
  });
  go(`/workflows/${created.id}`);
}

function formValues(form: HTMLFormElement): Record<string, string> {
  const data = new FormData(form);
  const out: Record<string, string> = {};
  for (const [key, value] of data.entries()) out[key] = String(value);
  return out;
}

function llmPayload(settings: Settings): Record<string, string> | undefined {
  if (!settings.openrouterKey) return undefined;
  return { api_key: settings.openrouterKey, model: settings.model || DEFAULT_MODEL };
}

async function onClick(event: Event): Promise<void> {
  const target = event.target as HTMLElement | null;
  if (!target) return;
  const peek = target.closest("[data-peek]") as HTMLElement | null;
  if (peek) {
    event.preventDefault();
    const name = peek.dataset.peek!;
    const input = document.querySelector<HTMLInputElement>(`input[name="${name}"]`);
    if (!input) return;
    input.type = input.type === "password" ? "text" : "password";
    peek.textContent = input.type === "password" ? "Show" : "Hide";
    return;
  }
  const action = (target.closest("[data-action]") as HTMLElement | null)?.dataset.action;

  if (action === "record-source") {
    event.preventDefault();
    const source = (target.closest("[data-source]") as HTMLElement | null)?.dataset.source;
    if (source === "remote" || source === "extension") {
      recordState.source = source;
      recordState.error = "";
      await render();
    }
    return;
  }
  if (action === "record-start") {
    event.preventDefault();
    recordState.goal = (document.getElementById("record-goal") as HTMLInputElement | null)?.value || recordState.goal;
    recordState.name = (document.getElementById("record-name") as HTMLInputElement | null)?.value || recordState.name;
    try {
      if (recordState.source === "extension") await startExtension();
      else await startRemote();
    } catch (err) {
      recordState.phase = "idle";
      recordState.error = err instanceof Error ? err.message : String(err);
    }
    await render();
    return;
  }
  if (action === "record-stop") {
    event.preventDefault();
    recordState.goal = (document.getElementById("record-goal") as HTMLInputElement | null)?.value || recordState.goal;
    recordState.name = (document.getElementById("record-name") as HTMLInputElement | null)?.value || recordState.name;
    try {
      if (recordState.source === "extension") await stopExtension();
      else await stopRemote();
    } catch (err) {
      recordState.phase = recordState.sessionId ? "live" : "idle";
      recordState.error = err instanceof Error ? err.message : String(err);
    }
    await render();
    return;
  }
  if (action === "record-cancel") {
    event.preventDefault();
    await cancelRemote();
    await render();
    return;
  }
  if (action === "record-drop" || action === "record-restore") {
    event.preventDefault();
    const id = (target.closest("[data-rec-id]") as HTMLElement | null)?.dataset.recId || "";
    recordState.review = recordState.review.map((item) => item.id === id ? { ...item, kept: action === "record-restore" } : item);
    await render();
    return;
  }
  if (action === "record-add-param") {
    event.preventDefault();
    recordState.parameters = [...recordState.parameters, { name: "value", label: "Value", example: "", required: true }];
    await render();
    return;
  }
  if (action === "record-save") {
    event.preventDefault();
    const btn = target.closest("button") as HTMLButtonElement | null;
    if (btn) btn.disabled = true;
    try {
      await saveRecording();
      flashMessage = { kind: "ok", text: "Saved to your library." };
    } catch (err) {
      recordState.error = err instanceof Error ? err.message : String(err);
    }
    await render();
    return;
  }
  if (action === "record-publish") {
    event.preventDefault();
    const btn = target.closest("button") as HTMLButtonElement | null;
    if (btn) btn.disabled = true;
    try {
      const listing = await publishRecording();
      flashMessage = { kind: "ok", text: `Published ${listing.name} v${listing.version} to the marketplace.` };
    } catch (err) {
      recordState.error = err instanceof Error ? err.message : String(err);
    }
    await render();
    return;
  }
  if (action === "hitl-select") {
    event.preventDefault();
    const id = (target.closest("[data-candidate-id]") as HTMLElement | null)?.dataset.candidateId;
    if (!id) return;
    selectedCandidateId = id;
    document.querySelectorAll(".candidate").forEach((el) => {
      el.classList.toggle("selected", (el as HTMLElement).dataset.candidateId === id);
    });
    const confirmBtn = document.querySelector<HTMLButtonElement>("[data-action=hitl-confirm]");
    if (confirmBtn) confirmBtn.disabled = false;
    return;
  }
  if (action === "hitl-confirm" || action === "hitl-cancel") {
    event.preventDefault();
    const panel = document.getElementById("hitl-panel");
    const status = document.getElementById("hitl-status");
    if (!panel) return;
    const runId = panel.dataset.runId!;
    const promptId = panel.dataset.promptId!;
    const prompt: ConfirmationPrompt = { id: promptId, runId, candidates: [] };
    const btn = target.closest("button") as HTMLButtonElement | null;
    if (btn) btn.disabled = true;
    if (status) status.innerHTML = `<div class="banner info">${action === "hitl-cancel" ? "Cancelling…" : "Confirming…"}</div>`;
    void (async () => {
      try {
        if (action === "hitl-cancel") await cancelRun(runId, prompt);
        else {
          if (!selectedCandidateId) throw new Error("Select a candidate first.");
          await confirmRun(runId, prompt, selectedCandidateId);
        }
        selectedCandidateId = null;
        selectedCandidateRunId = null;
        await render();
      } catch (err) {
        if (btn) btn.disabled = false;
        if (status) status.innerHTML = bannerError(err);
      }
    })();
    return;
  }
  if (action === "publish-workflow") {
    event.preventDefault();
    const id = (target.closest("[data-id]") as HTMLElement | null)?.dataset.id;
    if (!id) return;
    location.hash = `#/marketplace?publish=${encodeURIComponent(id)}`;
    return;
  }
  if (action === "market-delete") {
    event.preventDefault();
    const id = (target.closest("[data-id]") as HTMLElement | null)?.dataset.id;
    if (!id) return;
    if (!window.confirm("Delete this marketplace listing? The packaged automation will be removed.")) return;
    const btn = target.closest("button") as HTMLButtonElement | null;
    if (btn) btn.disabled = true;
    try {
      await deleteMarketListing(id);
      flashMessage = { kind: "ok", text: "Listing deleted." };
      await render();
    } catch (err) {
      if (btn) btn.disabled = false;
      flashMessage = { kind: "err", text: err instanceof Error ? err.message : String(err) };
      await render();
    }
    return;
  }
  if (action === "market-download") {
    event.preventDefault();
    const id = (target.closest("[data-id]") as HTMLElement | null)?.dataset.id;
    if (!id) return;
    const settings = loadSettings();
    try {
      const response = await fetch(packageUrl(id), {
        headers: settings.accessToken ? { "X-Browser-Platform-Token": settings.accessToken } : {},
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        const detail = data && typeof data === "object" && "error" in data ? String(data.error) : `Download failed (${response.status})`;
        throw new Error(detail);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      const disposition = response.headers.get("content-disposition") || "";
      const match = disposition.match(/filename="([^"]+)"/);
      a.href = url;
      a.download = match?.[1] || `${id}.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      flashMessage = { kind: "err", text: err instanceof Error ? err.message : String(err) };
      await render();
    }
    return;
  }
  if (action === "clear-openai-key") {
    event.preventDefault();
    if (!window.confirm("Remove the OpenAI key saved on this computer?")) return;
    try {
      await api.setOpenAIKey("");
      flashMessage = { kind: "ok", text: "Removed the OpenAI key saved on this computer." };
    } catch (err) {
      flashMessage = { kind: "err", text: err instanceof Error ? err.message : String(err) };
    }
    await render();
    return;
  }
  if (action === "mint-add-input") {
    event.preventDefault();
    const list = document.getElementById("mint-inputs");
    if (!list) return;
    const i = list.querySelectorAll(".var-row").length;
    const row = document.createElement("label");
    row.className = "var-row";
    row.innerHTML = `<input type="text" name="input-name-${i}" value="" placeholder="name" aria-label="Input name" /><input type="text" name="input-example-${i}" value="" placeholder="example value" aria-label="Example value" />`;
    list.appendChild(row);
    row.querySelector("input")?.focus();
    return;
  }
  if (action === "from-task-add-input") {
    event.preventDefault();
    const list = document.querySelector("#from-task-confirm .var-list");
    const panel = document.getElementById("from-task-confirm");
    if (!list) return;
    const i = list.querySelectorAll(".var-row").length;
    const row = document.createElement("label");
    const params = panel?.dataset.kind === "parameters";
    row.className = params ? "var-row var-row-params" : "var-row";
    row.innerHTML = params
      ? `<input type="checkbox" name="required-${i}" checked data-index="${i}" title="Required" /><input type="text" name="name-${i}" value="" placeholder="name" aria-label="Input name" /><input type="text" name="label-${i}" value="" placeholder="Label" aria-label="Label" /><input type="text" name="value-${i}" value="" placeholder="example value" aria-label="Example value" />`
      : `<input type="checkbox" name="accept-${i}" checked data-index="${i}" /><input type="text" name="name-${i}" value="" placeholder="input name" aria-label="Variable name" /><input type="text" name="value-${i}" value="" placeholder="example value" aria-label="Detected value" />`;
    list.appendChild(row);
    row.querySelector<HTMLInputElement>('input[name^="name"]')?.focus();
    return;
  }
  if (action === "from-task-confirm" || action === "from-task-cancel") {
    event.preventDefault();
    const panel = document.getElementById("from-task-confirm");
    const status = document.getElementById("from-task-confirm-status");
    if (!panel) return;
    const jobId = panel.dataset.jobId!;
    const btn = target.closest("button") as HTMLButtonElement | null;
    if (btn) btn.disabled = true;
    if (status) status.innerHTML = `<div class="banner info">${action === "from-task-cancel" ? "Cancelling…" : "Saving inputs…"}</div>`;
    void (async () => {
      try {
        if (action === "from-task-cancel") await cancelFromTask(jobId);
        else {
          const variables: Record<string, string> = {};
          const parameters: { name: string; label?: string; example?: string; required?: boolean }[] = [];
          const isParams = panel.dataset.kind === "parameters";
          panel.querySelectorAll(".var-row").forEach((row) => {
            const accept = row.querySelector<HTMLInputElement>('input[type="checkbox"]');
            const name = row.querySelector<HTMLInputElement>('input[name^="name"]');
            const value = row.querySelector<HTMLInputElement>('input[name^="value"]');
            const label = row.querySelector<HTMLInputElement>('input[name^="label"]');
            if (!isParams && accept && !accept.checked) return;
            if (name?.value.trim() && value) {
              variables[name.value.trim()] = value.value;
              if (isParams) {
                parameters.push({
                  name: name.value.trim(),
                  label: label?.value.trim() || name.value.trim(),
                  example: value.value,
                  required: accept ? accept.checked : true,
                });
              }
            }
          });
          await confirmFromTaskVariables(jobId, variables, isParams ? parameters : undefined);
        }
        const job = await getFromTaskJob(jobId);
        root.innerHTML = renderFromTaskProgress(job);
        if (isLiveJob(job.status)) startFromTaskPolling(jobId);
      } catch (err) {
        if (btn) btn.disabled = false;
        if (status) status.innerHTML = bannerError(err);
      }
    })();
    return;
  }
  if (action === "seed-example") {
    event.preventDefault();
    try {
      await seedExample();
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err));
    }
  }
  if (action === "test-connection") {
    event.preventDefault();
    const form = document.getElementById("settings-form") as HTMLFormElement | null;
    const status = document.getElementById("settings-status");
    if (!form || !status) return;
    const values = formValues(form);
    const trial: Settings = {
      backendUrl: values.backendUrl ?? "",
      accessToken: values.accessToken ?? "",
      openrouterKey: values.openrouterKey ?? "",
      model: values.model || DEFAULT_MODEL,
    };
    saveSettings(trial);
    status.innerHTML = `<div class="banner info">Checking…</div>`;
    try {
      const h = await api.health(trial);
      const listed = await api.listWorkflows();
      connected = true;
      flashMessage = {
        kind: "ok",
        text: `Connected to ${h.service || "backend"}. ${listed.workflows.length} workflow(s) visible.`,
      };
      await render();
    } catch (err) {
      connected = false;
      status.innerHTML = bannerError(err);
    }
  }
}

async function onSubmit(event: Event): Promise<void> {
  const form = event.target as HTMLFormElement;
  if (!(form instanceof HTMLFormElement)) return;
  event.preventDefault();
  if (form.id === "record-url-form") {
    const input = document.getElementById("record-url") as HTMLInputElement | null;
    try {
      await submitUrlBar(input?.value || "");
    } catch (err) {
      recordState.error = err instanceof Error ? err.message : String(err);
      await render();
    }
    return;
  }
  if (form.id === "record-type-form") {
    const input = document.getElementById("record-type") as HTMLInputElement | null;
    const submitter = (event as SubmitEvent).submitter as HTMLElement | null;
    const pressEnter = submitter?.dataset.enter === "1";
    const text = input?.value || "";
    try {
      await submitTypeBox(text, pressEnter);
      if (input) input.value = "";
    } catch (err) {
      recordState.error = err instanceof Error ? err.message : String(err);
      await render();
    }
    return;
  }
  if (form.id === "market-publish-form") {
    event.preventDefault();
    const status = document.getElementById("market-publish-status");
    const button = form.querySelector("button[type=submit]") as HTMLButtonElement | null;
    const workflowId = String((form.elements.namedItem("workflowId") as HTMLSelectElement | null)?.value || form.dataset.workflowId || "");
    if (!workflowId) {
      if (status) status.innerHTML = `<div class="banner err">Choose a workflow to publish.</div>`;
      return;
    }
    const inputsRaw = String((form.elements.namedItem("inputs") as HTMLTextAreaElement | null)?.value || "").trim();
    let inputs: MarketInput[] | undefined;
    if (inputsRaw) {
      try {
        const parsed = JSON.parse(inputsRaw);
        if (!Array.isArray(parsed)) throw new Error("inputs must be a JSON array");
        inputs = parsed as MarketInput[];
      } catch (err) {
        if (status) status.innerHTML = `<div class="banner err">${escapeHtml(err instanceof Error ? err.message : "inputs must be a JSON array")}</div>`;
        return;
      }
    }
    if (button) button.disabled = true;
    if (status) status.innerHTML = `<div class="banner info">Publishing…</div>`;
    try {
      const stored = await api.getWorkflow(workflowId);
      const typ = String((form.elements.namedItem("type") as HTMLSelectElement | null)?.value || inferWorkflowType(stored.definition as unknown as Record<string, unknown>));
      const name = String((form.elements.namedItem("name") as HTMLInputElement | null)?.value || stored.definition.name).trim();
      const result = await publishWorkflowJson({
        id: listingIdForName(name),
        name,
        description: String((form.elements.namedItem("description") as HTMLTextAreaElement | null)?.value || stored.definition.description || ""),
        type: typ,
        inputs: inputs || inputPayload(deriveWorkflowInputs(stored.definition)),
        workflow: stored.definition as unknown as Record<string, unknown>,
      });
      flashMessage = { kind: "ok", text: `Published ${result.listing.name} v${result.listing.version}.` };
      await render();
    } catch (err) {
      if (button) button.disabled = false;
      if (status) status.innerHTML = bannerError(err);
    }
    return;
  }
  if (form.id === "settings-form") {
    const values = formValues(form);
    const openaiKey = (values.openaiKey || "").trim();
    saveSettings({
      backendUrl: values.backendUrl ?? "",
      accessToken: values.accessToken ?? "",
      openrouterKey: values.openrouterKey ?? "",
      model: values.model || DEFAULT_MODEL,
    });
    if (!(values.backendUrl ?? "").trim()) await refreshDiscovery();
    try {
      if (openaiKey) await api.setOpenAIKey(openaiKey);
      flashMessage = { kind: "ok", text: openaiKey ? "Saved. The OpenAI key is stored only on this computer." : "Saved on this device." };
    } catch (err) {
      flashMessage = { kind: "err", text: err instanceof Error ? err.message : String(err) };
    }
    await ping();
    await render();
    return;
  }
  if (form.id === "create-form") {
    const values = formValues(form);
    try {
      let payload: Record<string, unknown>;
      if (values.json?.trim()) {
        payload = JSON.parse(values.json);
        if (values.name) payload.name = values.name;
        if (values.description) payload.description = values.description;
      } else {
        const url = values.url?.trim();
        const goal = values.goal?.trim() || "the main heading";
        if (!url) throw new Error("Paste workflow JSON, or enter a URL for the simple form.");
        payload = {
          name: values.name || "untitled",
          description: values.description || "",
          version: "1",
          steps: [
            { type: "navigation", url, description: `Open ${url}` },
            { type: "extract", extractionGoal: goal, description: "Extract", output: "extract" },
          ],
        };
      }
      const created = await api.createWorkflow(payload);
      go(`/workflows/${created.id}`);
    } catch (err) {
      alert(err instanceof Error ? err.message : String(err));
    }
    return;
  }
  if (form.id === "run-form") {
    const id = form.dataset.id!;
    const values = formValues(form);
    const settings = loadSettings();
    const hitlMode = (values.hitl_mode || "always_on_uncertain") as HitlMode;
    for (const key of RESERVED_RUN_FIELDS) delete values[key];
    const empty = Object.entries(values).filter(([, value]) => !String(value || "").trim());
    if (empty.length) {
      alert("Fill in every required input before running.");
      const first = form.querySelector<HTMLInputElement>(`input[name="${empty[0][0]}"]`);
      first?.focus();
      return;
    }
    const body: Record<string, unknown> = {
      variables: values,
      write_back: true,
      hitl_mode: hitlMode,
      hitl: { mode: hitlMode },
    };
    const llm = llmPayload(settings);
    if (llm) body.llm = llm;
    const button = form.querySelector("button[type=submit]") as HTMLButtonElement | null;
    if (button) {
      button.disabled = true;
      button.textContent = "Starting…";
    }
    try {
      const run = await api.startRun(id, body);
      saveRunInputs(id, values);
      go(`/runs/${run.id}`);
    } catch (err) {
      if (button) {
        button.disabled = false;
        button.textContent = "Run now";
      }
      alert(err instanceof Error ? err.message : String(err));
    }
    return;
  }
  if (form.id === "from-task-form") {
    const values = formValues(form);
    const settings = loadSettings();
    const confirmBox = form.querySelector<HTMLInputElement>('input[name="confirm_variables"]');
    const payload: Parameters<typeof startFromTask>[0] = {
      task: values.task || "",
      confirm_variables: Boolean(confirmBox?.checked),
    };
    const startUrl = values.start_url?.trim();
    if (startUrl) payload.start_url = startUrl;
    const name = values.name?.trim();
    if (name) payload.name = name;
    const variables = parseVariableLines(values.variables || "");
    if (Object.keys(variables).length) payload.variables = variables;
    const llm = llmPayload(settings);
    if (llm) payload.llm = llm;
    const button = form.querySelector("button[type=submit]") as HTMLButtonElement | null;
    if (button) {
      button.disabled = true;
      button.textContent = "Starting…";
    }
    try {
      const job = await startFromTask(payload);
      go(`/from-task/${job.id}`);
    } catch (err) {
      if (button) {
        button.disabled = false;
        button.textContent = "Start demonstration";
      }
      alert(err instanceof Error ? err.message : String(err));
    }
    return;
  }
  if (form.id === "mint-form") {
    const values = formValues(form);
    const settings = loadSettings();
    const payload: Record<string, unknown> = {
      name: values.name,
      url: values.url,
      intent: values.intent,
      effect: values.effect || "read",
    };
    const declared: Record<string, string> = {};
    form.querySelectorAll("#mint-inputs .var-row").forEach((row) => {
      const name = row.querySelector<HTMLInputElement>('input[name^="input-name"]');
      const example = row.querySelector<HTMLInputElement>('input[name^="input-example"]');
      if (name?.value.trim()) declared[name.value.trim()] = example?.value ?? "";
    });
    if (Object.keys(declared).length) payload.input = declared;
    const llm = llmPayload(settings);
    if (llm) payload.llm = llm;
    const status = document.getElementById("mint-status");
    const button = form.querySelector("button[type=submit]") as HTMLButtonElement | null;
    if (button) button.disabled = true;
    if (status) status.innerHTML = `<div class="card"><div class="banner info">Mint started. This can take several minutes…</div></div>`;
    try {
      const { result } = await api.mint(payload);
      const jobId = mintJobId(result);
      if (jobId) {
        go(`/mint/${jobId}`);
        return;
      }
      if (status) {
        status.innerHTML = `<div class="card"><h2 style="margin:0 0 8px;font-size:1.05rem">Job</h2><pre class="output">${escapeHtml(JSON.stringify(result, null, 2))}</pre></div>`;
      }
    } catch (err) {
      if (status) status.innerHTML = bannerError(err);
    } finally {
      if (button) button.disabled = false;
    }
  }
}

export function mount(el: HTMLElement): void {
  root = el;
  window.addEventListener("hashchange", () => void render());
  document.addEventListener("click", (event) => void onClick(event));
  document.addEventListener("submit", (event) => void onSubmit(event));
  document.addEventListener("input", (event) => {
    const el = event.target as HTMLInputElement | null;
    if (!el) return;
    if (el.id === "record-goal") recordState.goal = el.value;
    if (el.id === "record-name") recordState.name = el.value;
    if (el.id === "record-url") recordState.url = el.value;
    if (el.dataset.param != null && el.dataset.field && el.dataset.field !== "required") {
      const i = Number(el.dataset.param);
      const field = el.dataset.field as "name" | "label" | "example";
      recordState.parameters = recordState.parameters.map((item, idx) => (idx === i ? { ...item, [field]: el.value } : item));
    }
  });
  document.addEventListener("change", (event) => {
    const target = event.target as HTMLElement | null;
    const input = event.target as HTMLInputElement | null;
    if (input && input.dataset.param != null && input.dataset.field === "required") {
      const i = Number(input.dataset.param);
      recordState.parameters = recordState.parameters.map((item, idx) => (idx === i ? { ...item, required: input.checked } : item));
      return;
    }
    if (target && target.id === "market-source") {
      const value = (target as HTMLSelectElement).value;
      void listMarket()
        .then((res) => fillPublishFields(value, res.listings))
        .catch(() => fillPublishFields(value));
    }
  });
  void (async () => {
    await refreshDiscovery();
    await ping();
    await render();
  })();
  healthTimer = window.setInterval(() => void ping().then(() => {
    const route = parseRoute();
    if (route.name !== "settings") return;
  }), 15000);
  void healthTimer;
}


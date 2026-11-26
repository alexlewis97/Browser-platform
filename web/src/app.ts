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
import { bindChatPage, handleChatClick, renderChatPage } from "./chat";
import { findingsCard, validateInputs, type InputValidation } from "./validation";
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
import { he } from "./copy/he";

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
  | { name: "chat" }
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
  chat: `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M5 6h14v9H8l-3 3V6z"/></svg>`,
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
  if (parts[0] === "chat") return { name: "chat" };
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
  if (ms == null || Number.isNaN(ms)) return he.common.dash;
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
  if (!inn && !out) return he.validation.zeroTokens;
  return `${inn + out} ${he.validation.tokens}`;
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
  if (reason === "ambiguous_margin") return he.confirm.ambiguous;
  if (reason === "below_threshold") return he.confirm.below;
  return he.confirm.needs;
}

function renderConfirmation(run: RunRecord, prompt: ConfirmationPrompt): string {
  if (selectedCandidateRunId !== run.id) {
    selectedCandidateId = prompt.candidates[0]?.id ?? null;
    selectedCandidateRunId = run.id;
  }
  const src = resolveScreenshotSrc(prompt.screenshot);
  const shot = src ? `<img class="shot" alt="${he.confirm.screenshot}" src="${escapeHtml(src)}" />` : "";
  const query = prompt.query ? `“${prompt.query}”` : he.hitl.thisStep;
  const stepBit = [prompt.stepType, prompt.stepIndex != null ? `${he.hitl.step} ${prompt.stepIndex + 1}` : "", prompt.stepDescription].filter(Boolean).join(" · ");
  const scores = [
    prompt.threshold != null ? `${he.hitl.threshold} ${formatScore(prompt.threshold)}` : "",
    prompt.margin != null ? `${he.hitl.margin} ${formatScore(prompt.margin)}` : "",
    prompt.winnerScore != null ? `${he.hitl.top} ${formatScore(prompt.winnerScore)}` : "",
    prompt.runnerUpScore != null ? `${he.hitl.runnerUp} ${formatScore(prompt.runnerUpScore)}` : "",
  ].filter(Boolean).join(" · ");
  const timeout = prompt.timeoutAt
    ? `${he.hitl.timeout} ${new Date(prompt.timeoutAt).toLocaleTimeString("he-IL")} (${escapeHtml(prompt.timeoutPolicy || "fail")})`
    : prompt.timeoutS
      ? `${he.hitl.timeout} ${prompt.timeoutS}s (${escapeHtml(prompt.timeoutPolicy || "fail")})`
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
    : `<p class="muted">${he.hitl.noCandidates}</p>`;
  return `<div class="card confirm-panel" id="hitl-panel" data-run-id="${escapeHtml(run.id)}" data-prompt-id="${escapeHtml(prompt.id)}">
    <p class="eyebrow">${escapeHtml(reasonLabel(prompt.reason))}${prompt.hitlMode ? " · " + escapeHtml(prompt.hitlMode) : ""}</p>
    <h2>${he.hitl.chooseMatch} ${escapeHtml(query)}</h2>
    <p class="sub">${escapeHtml(prompt.message || he.confirm.pick)}</p>
    ${stepBit ? `<div class="meta">${escapeHtml(stepBit)}</div>` : ""}
    ${scores ? `<div class="meta">${escapeHtml(scores)}</div>` : ""}
    ${timeout ? `<div class="meta">${timeout}</div>` : ""}
    ${shot}
    <div class="candidates">${cards}</div>
    <div class="btn-row">
      <button type="button" class="btn danger" data-action="hitl-cancel">${he.confirm.cancel}</button>
      <button type="button" class="btn primary" data-action="hitl-confirm" ${selectedCandidateId ? "" : "disabled"}>${he.confirm.confirm}</button>
    </div>
    <div id="hitl-status" style="margin-top:12px"></div>
  </div>`;
}

function shell(title: string, eyebrow: string, body: string, subtitle = ""): string {
  const settings = loadSettings();
  const ready = settingsComplete(settings);
  const pill = connected
    ? `<span class="pill ok"><span class="dot"></span>${he.status.connected}</span>`
    : ready
      ? `<span class="pill"><span class="dot"></span>${he.status.checking}</span>`
      : `<span class="pill bad"><span class="dot"></span>${he.status.setupNeeded}</span>`;
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
      <a href="#/workflows" class="${active("workflows")}">${ICONS.workflows}${he.nav.workflows}</a>
      <a href="#/from-task" class="${active("from-task")}">${ICONS.fromTask}${he.nav.fromTask}</a>
      <a href="#/record" class="${active("record")}">${ICONS.record}${he.nav.record}</a>
      <a href="#/chat" class="${active("chat")}">${ICONS.chat}${he.nav.chat}</a>
      <a href="#/runs" class="${active("runs")}">${ICONS.runs}${he.nav.runs}</a>
      <a href="#/mint" class="${active("mint")}">${ICONS.mint}${he.nav.mint}</a>
      <a href="#/marketplace" class="${active("marketplace")}">${ICONS.market}${he.nav.market}</a>
      <a href="#/settings" class="${active("settings")}">${ICONS.settings}${he.nav.settings}</a>
    </nav>
  `;
}

function needSetup(): string | null {
  const s = loadSettings();
  if (!settingsComplete(s)) {
    return shell(
      he.setup.title,
      he.setup.eyebrow,
      `<div class="card">
        <p class="sub" style="margin-top:0">${he.setup.body}</p>
        <div style="margin-top:16px"><a class="btn primary" href="#/settings">${he.setup.openSettings}</a></div>
      </div>`,
    );
  }
  return null;
}

async function ping(): Promise<void> {
  const s = loadSettings();
  if (!s.backendUrl) {
    connected = false;
    lastConnectError = he.connect.noUrl;
    return;
  }
  try {
    const result = await api.health(s);
    connected = Boolean(result.ok);
    lastConnectError = connected ? null : he.connect.notHealthy;
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
  const detail = lastConnectError || he.connectErrors.cantReach;
  return `<div class="banner err">${escapeHtml(detail)}</div>`;
}

async function renderWorkflows(): Promise<string> {
  const setup = needSetup();
  if (setup) return setup;
  if (!connected) {
    return shell(he.workflows.title, he.workflows.eyebrowSaved, `${backendDownBanner()}<div class="card"><p class="sub" style="margin:0">${he.workflows.down}</p></div>`);
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
                <span class="muted">${he.common.open}</span>
              </div>
            </a>
            <div class="btn-row" style="margin-top:10px">
              <button type="button" class="btn wide" data-action="publish-workflow" data-id="${escapeHtml(w.id)}">${he.workflows.publish}</button>
            </div>
          </div>`,
          )
          .join("")
      : `<div class="card empty">${he.workflows.empty}</div>`;
    return shell(
      he.workflows.title,
      he.workflows.eyebrow,
      `${list}
       <div class="btn-row" style="margin-top:14px">
         <a class="btn" href="#/workflows/new">${he.workflows.new}</a>
         <a class="btn primary" href="#/from-task">${he.workflows.fromTask}</a>
         <a class="btn" href="#/record">${he.workflows.record}</a>
         <button class="btn wide" data-action="seed-example">${he.workflows.seed}</button>
       </div>`,
      he.workflows.subtitle,
    );
  } catch (err) {
    return shell(he.workflows.title, he.workflows.eyebrow, `${bannerError(err)}<a class="btn" href="#/settings">${he.workflows.checkSettings}</a>`);
  }
}

async function renderNew(): Promise<string> {
  const setup = needSetup();
  if (setup) return setup;
  return shell(
    he.newWorkflow.title,
    he.newWorkflow.eyebrow,
    `<form id="create-form" class="card">
      <label class="field"><span>${he.newWorkflow.name}</span><input name="name" type="text" required placeholder="example_com_heading" /></label>
      <label class="field"><span>${he.newWorkflow.description}</span><input name="description" type="text" placeholder="${he.newWorkflow.descriptionPh}" /></label>
      <label class="field"><span>${he.newWorkflow.pasteJson}</span>
        <textarea name="json" placeholder='{"name":"...","steps":[{"type":"navigation","url":"https://example.com"},{"type":"extract","extractionGoal":"the page title","output":"heading"}]}'></textarea>
      </label>
      <p class="muted" style="margin-top:-4px">${he.newWorkflow.jsonHint}</p>
      <label class="field"><span>${he.newWorkflow.orUrl}</span><input name="url" type="url" placeholder="https://example.com" /></label>
      <label class="field"><span>${he.newWorkflow.extract}</span><input name="goal" type="text" placeholder="${he.newWorkflow.mainHeading}" /></label>
      <button class="btn primary" type="submit">${he.newWorkflow.create}</button>
    </form>`,
  );
}


const INPUT_LABELS: Record<string, string> = {
  term: he.labels.term,
  query: he.labels.query,
  q: he.labels.q,
  search: he.labels.search,
  name: he.labels.name,
  status: he.labels.status,
  url: he.labels.url,
  email: he.labels.email,
  title: he.labels.title,
};
const RESERVED_RUN_FIELDS = new Set(["hitl_mode", "instructions"]);

function prettyInputLabel(name: string, label?: string): string {
  if (label && label.trim()) return label.trim();
  const key = (name || "").trim();
  if (INPUT_LABELS[key.toLowerCase()]) return INPUT_LABELS[key.toLowerCase()];
  const text = key.replace(/_/g, " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : he.labels.input;
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
    return `<p class="muted" style="margin-top:0">${he.runPage.noVariables}</p>`;
  }
  const lastUsed = loadRunInputs(id);
  return inputs
    .map((input) => {
      const label = prettyInputLabel(input.name, input.label);
      const saved = lastUsed[input.name];
      const value = saved != null ? saved : "";
      const example = input.default == null ? "" : String(input.default);
      const hint = input.description || (example ? `${he.runPage.examplePrefix} ${example}` : "");
      return `<label class="field">
        <span>${escapeHtml(label)} <span class="muted">${he.workflow.required}</span></span>
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
      he.workflow.title,
      `<div class="card">
        <p class="sub" style="margin:0 0 12px">${escapeHtml(def.description || he.workflows.noDescription)}</p>
        <div class="mono muted">id ${escapeHtml(stored.id)} · v${escapeHtml(def.version || "1")}</div>
        <div style="margin-top:14px">${steps}</div>
        ${typeof def.mint_job_id === "string" && def.mint_job_id ? `<p style="margin:16px 0 0"><a class="btn" href="#/mint/${encodeURIComponent(String(def.mint_job_id))}">${he.workflow.mintTrace}</a></p>` : ""}
        <div class="btn-row" style="margin-top:14px">
          <button type="button" class="btn wide" data-action="publish-workflow" data-id="${escapeHtml(stored.id)}">${he.workflows.publish}</button>
        </div>
      </div>
      <form id="run-form" class="card" data-id="${escapeHtml(stored.id)}">
        <h2 style="margin:0 0 12px;font-size:1.05rem">${he.workflow.run}</h2>
        ${fields}
        <label class="field"><span>${he.runPage.hitlMode}</span>
          <select name="hitl_mode">
            ${HITL_MODES.map((mode) => {
              const current = String((def as { hitl?: { mode?: string } }).hitl?.mode || "always_on_uncertain");
              const labels: Record<string, string> = {
                always_on_uncertain: he.hitlModes.always,
                llm_then_human: he.hitlModes.llmThenHuman,
                llm_only: he.hitlModes.llmOnly,
              };
              const selected = current === mode ? "selected" : "";
              return `<option value="${mode}" ${selected}>${labels[mode]}</option>`;
            }).join("")}
          </select>
        </label>
        <button class="btn primary" type="submit">${he.runPage.runNow}</button>
      </form>`,
    );
  } catch (err) {
    return shell(he.workflow.title, he.workflows.error, bannerError(err));
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
      ${step.tier ? `<span class="tier ${escapeHtml(step.tier)}">${escapeHtml(step.tier)}</span>` : `<span class="tier">${pending ? he.runPage.runningTier : he.common.dash}</span>`}
    </div>`;
}

function runView(run: RunRecord): string {
  const live = isLiveStatus(run.status);
  const duration =
    run.started_at && run.finished_at
      ? fmtMs(new Date(run.finished_at).getTime() - new Date(run.started_at).getTime())
      : live
        ? he.runPage.inProgress
        : he.common.dash;
  const steps = (run.steps ?? []).map((s) => renderStep(s)).join("") || `<p class="muted">${he.runPage.waitingStep}</p>`;
  const outputs = run.outputs && Object.keys(run.outputs).length
    ? `<pre class="output">${escapeHtml(JSON.stringify(run.outputs, null, 2))}</pre>`
    : `<p class="muted">${he.runPage.noOutputs}</p>`;
  const prompt = extractConfirmation(run);
  const confirm = prompt ? renderConfirmation(run, prompt) : "";
  const spin = live ? ' <span class="spinner"></span>' : "";
  return shell(
    escapeHtml(run.workflow_name || he.workflow.run),
    he.runPage.live,
    `${confirm}<div class="card">
      <div class="row">
        <span class="pill ${statusClass(run.status)}"><span class="dot"></span>${escapeHtml(run.status)}${spin}</span>
        <span class="mono muted">${escapeHtml(run.id.slice(0, 10))}</span>
      </div>
      <div class="status-grid">
        <div class="stat"><b>${escapeHtml(duration)}</b><span>${he.runPage.duration}</span></div>
        <div class="stat"><b>${escapeHtml(totalTokens(run))}</b><span>${he.runPage.tokens}</span></div>
      </div>
      ${run.error ? `<div class="banner err" style="margin:12px 0 0">${escapeHtml(run.error)}</div>` : ""}
      <div style="margin-top:16px">${steps}</div>
    </div>
    <div class="card">
      <h2 style="margin:0 0 10px;font-size:1.05rem">${he.runPage.outputs}</h2>
      ${outputs}
    </div>`,
    run.model ? `${he.runs.modelRepairs} ${escapeHtml(run.model)}` : he.runs.deterministic,
  );
}

async function renderRun(id: string): Promise<string> {
  const setup = needSetup();
  if (setup) return setup;
  try {
    const run = await api.getRun(id);
    return runView(run);
  } catch (err) {
    return shell(he.workflow.run, he.workflows.error, bannerError(err));
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
            const when = run.started_at ? new Date(run.started_at).toLocaleString("he-IL") : "";
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
      : `<div class="card empty">${he.runPage.runHistoryEmpty}</div>`;
    return shell(he.runs.title, he.runs.eyebrow, list, he.runs.subtitle);
  } catch (err) {
    return shell(he.runs.title, he.runs.eyebrow, bannerError(err));
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
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("he-IL");
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
  const timeline = steps.map(renderMintStep).join("") || `<p class="muted">${live ? he.mint.waiting : he.mint.noSteps}</p>`;
  const partial = job.partial || job.trace?.partial;
  const duration =
    job.started_at && job.finished_at
      ? fmtMs(new Date(job.finished_at).getTime() - new Date(job.started_at).getTime())
      : live
        ? he.runPage.inProgress
        : he.common.dash;
  const workflow = job.workflow && typeof job.workflow === "object" ? job.workflow : null;
  const workflowId = workflow?.id || (typeof job.workflow_id === "string" ? job.workflow_id : "");
  const spin = live ? ' <span class="spinner"></span>' : "";
  const banner = partial
    ? `<div class="banner info" style="margin:0 0 12px">${he.mintExtra.partialBanner}</div>`
    : "";
  const wfLink = workflowId
    ? `<a class="btn" href="#/workflows/${encodeURIComponent(workflowId)}">${he.mint.openWorkflow}</a>`
    : "";
  return shell(
    escapeHtml(job.name || he.mint.title),
    he.mint.trace,
    `${banner}<div class="card">
      <div class="row">
        <span class="pill ${statusClass(mintStatusLabel(job))}"><span class="dot"></span>${escapeHtml(mintStatusLabel(job))}${spin}</span>
        <span class="mono muted">${escapeHtml(id.slice(0, 10))}</span>
      </div>
      <div class="status-grid">
        <div class="stat"><b>${escapeHtml(duration)}</b><span>${he.mintExtra.duration}</span></div>
        <div class="stat"><b>${steps.length}</b><span>${he.mintExtra.events}</span></div>
      </div>
      ${job.intent ? `<p class="sub" style="margin:12px 0 0">${escapeHtml(job.intent)}</p>` : ""}
      ${job.url ? `<div class="meta" style="margin-top:8px">${escapeHtml(job.url)}</div>` : ""}
      ${job.error ? `<div class="banner err" style="margin:12px 0 0">${escapeHtml(String(job.error))}</div>` : ""}
      <div style="margin-top:16px">${timeline}</div>
    </div>
    <div class="row" style="margin-top:12px;gap:8px">
      <a class="btn" href="#/mint">${he.mint.all}</a>
      ${wfLink}
    </div>`,
    live ? he.mint.live : job.trace_source === "backfill" || partial ? he.mint.backfill : he.mint.official,
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
          const partial = job.partial ? he.mintExtra.partial : "";
          return `<a class="card card-link" href="#/mint/${encodeURIComponent(id)}">
            <div class="row">
              <div class="grow">
                <strong>${escapeHtml(job.name || id || he.mint.title)}</strong>
                <div class="meta">${escapeHtml(when)}${when && count ? " · " : ""}${count ? escapeHtml(String(count) + " " + he.mintExtra.events) : ""}${escapeHtml(partial)}</div>
              </div>
              <span class="pill ${statusClass(mintStatusLabel(job))}">${escapeHtml(mintStatusLabel(job))}</span>
            </div>
          </a>`;
        })
        .join("")
    : listError
      ? bannerError(listError)
      : `<div class="card empty">${he.mintExtra.emptyJobs}</div>`;
  return shell(
    he.mint.title,
    he.mintExtra.deterministic,
    `${!openaiConfigured ? `<div class="banner info">${he.mint.needOpenai}<a href="#/settings">${he.nav.settings}</a>${he.mint.beforeMint}</div>` : ""}
    ${list}
    <form id="mint-form" class="card">
      <p class="sub" style="margin-top:0">${he.mintExtra.mintBody}</p>
      <label class="field"><span>${he.mintExtra.toolName}</span><input name="name" required type="text" placeholder="example_reader" /></label>
      <label class="field"><span>${he.labels.url}</span><input name="url" required type="url" placeholder="https://example.com" /></label>
      <label class="field"><span>${he.mint.intent}</span><textarea name="intent" required placeholder="${he.mint.intentPh}" style="min-height:88px;font-family:var(--font);font-size:16px"></textarea></label>
      <label class="field"><span>${he.mintExtra.effect}</span>
        <select name="effect">
          <option value="read" selected>read</option>
          <option value="write">write</option>
        </select>
      </label>
      <div class="field">
        <span>${he.mint.inputs}</span>
        <p class="muted" style="margin:0 0 8px">${he.mintExtra.inputsHelp}</p>
        <div id="mint-inputs" class="var-list">
          <label class="var-row">
            <input type="text" name="input-name-0" value="term" placeholder="name" aria-label="${he.aria.inputName}" />
            <input type="text" name="input-example-0" value="" placeholder="${he.mint.exampleValue}" aria-label="${he.aria.exampleValue}" />
          </label>
        </div>
        <button type="button" class="btn" data-action="mint-add-input">${he.mintExtra.addInput}</button>
      </div>
      <button class="btn primary" type="submit">${he.mintExtra.startMint}</button>
    </form>
    <div id="mint-status"></div>`,
    he.mint.subtitle,
  );
}

async function renderMintJob(id: string): Promise<string> {
  const setup = needSetup();
  if (setup) return setup;
  try {
    const job = await api.getMintJob(id);
    return mintJobView(job);
  } catch (err) {
    return shell(he.nav.mint, he.workflows.error, `${bannerError(err)}<a class="btn" href="#/mint">${he.mint.all}</a>`);
  }
}


function formatMarketDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "";
  return date.toLocaleString("he-IL");
}

function inputSummary(listing: MarketListing): string {
  if (!listing.inputs.length) return he.market.noInputs;
  return listing.inputs
    .map((input) => {
      const label = input.label || input.name;
      return input.required === false ? label : `${label} ${he.marketExtra.requiredParen}`;
    })
    .join(", ");
}

function listingCard(listing: MarketListing): string {
  const warning = listing.contains_code
    ? `<div class="banner info" style="margin-top:10px">${he.marketExtra.codeWarning}</div>`
    : "";
  const download = packageUrl(listing.id);
  return `<article class="card" data-listing-id="${escapeHtml(listing.id)}">
    <h2 style="margin:0 0 6px;font-size:1.05rem">${escapeHtml(listing.name)}</h2>
    <div class="mono muted">${escapeHtml(listing.type)}${listing.tier && listing.tier !== listing.type ? ` · ${escapeHtml(listing.tier)}` : ""} · v${escapeHtml(listing.version)} · ${escapeHtml(formatMarketDate(listing.created_at))}</div>
    <p class="sub" style="margin:10px 0">${escapeHtml(listing.description || he.workflows.noDescription)}</p>
    <p class="muted" style="margin:0">${he.marketExtra.inputsLabel} ${escapeHtml(inputSummary(listing))}</p>
    <p class="mono muted" style="margin:8px 0 0;word-break:break-all">sha256 ${escapeHtml(listing.sha256)}</p>
    ${warning}
    <div class="btn-row" style="margin-top:14px">
      <a class="btn" href="${escapeHtml(download)}" data-action="market-download" data-id="${escapeHtml(listing.id)}">${he.marketExtra.downloadPackage}</a>
      <button type="button" class="btn danger" data-action="market-delete" data-id="${escapeHtml(listing.id)}">${he.market.delete}</button>
    </div>
  </article>`;
}

async function renderMarketplace(): Promise<string> {
  const s = loadSettings();
  if (!s.accessToken) {
    return shell(
      he.market.title,
      he.market.eyebrow,
      `<div class="card">
        <p class="sub" style="margin-top:0">${he.marketExtra.tokenGate}</p>
        <div style="margin-top:16px"><a class="btn primary" href="#/settings">${he.setup.openSettings}</a></div>
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
    : `<div class="card"><p class="sub" style="margin:0">${he.marketExtra.emptyPublish}</p></div>`;
  const options = library.length
    ? library
        .map((w) => {
          const existing = matchListing(listings, w.name, listingIdForName(w.name));
          const label = `${w.name}${existing ? `${he.marketExtra.publishedSuffix}${existing.version}${he.marketExtra.updateSuffix})` : ""}`;
          const selected = w.id === preselect ? "selected" : "";
          return `<option value="${escapeHtml(w.id)}" ${selected}>${escapeHtml(label)}</option>`;
        })
        .join("")
    : "";
  const form = `<form id="market-publish-form" class="card">
    <h2 style="margin:0 0 12px;font-size:1.05rem">${he.marketExtra.publishHeading}</h2>
    <p class="muted" style="margin:0 0 12px">${he.marketExtra.publishIntro}</p>
    ${
      library.length
        ? `<label class="field"><span>${he.marketExtra.yourAutomations}</span>
      <select name="workflowId" id="market-source">
        <option value="">${he.marketExtra.chooseWorkflow}</option>
        ${options}
      </select>
    </label>
    <div id="market-publish-fields" hidden>
      <label class="field"><span>${he.market.name}</span><input name="name" required placeholder="${he.market.namePh}" /></label>
      <label class="field"><span>${he.market.description}</span><textarea name="description" rows="3" placeholder="${he.market.descriptionPh}"></textarea></label>
      <label class="field"><span>${he.marketExtra.type}</span>
        <select name="type">
          <option value="workflow-use">workflow-use</option>
          <option value="from-task">from-task</option>
        </select>
      </label>
      <label class="field"><span>${he.marketExtra.declaredInputs}</span>
        <textarea name="inputs" rows="3" placeholder='[{"name":"term","label":"${he.labels.term}","required":true,"type":"string"}]'></textarea>
      </label>
    </div>
    <p id="market-pomerado-note" class="muted" hidden>${he.marketExtra.pomeradoZipNote}</p>`
        : `<p class="muted">${he.market.connectToPublish} ${isLocalOrigin() ? he.market.localHint : he.market.tunnelHint}</p>`
    }
    <div id="market-publish-status" style="margin-top:12px"></div>
    <div class="btn-row" style="margin-top:16px">
      <button class="btn primary wide" id="market-publish-submit" type="submit" disabled>${he.market.publish}</button>
    </div>
  </form>`;
  const flash = flashMessage ? `<div class="banner ${flashMessage.kind}">${escapeHtml(flashMessage.text)}</div>` : "";
  flashMessage = null;
  const html = shell(he.market.title, he.market.eyebrow, `${flash}${errHtml}${libraryErr}${form}${cards}`);
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
    submit.textContent = he.market.publish;
    return;
  }
  const stored = await api.getWorkflow(workflowId);
  const def = stored.definition;
  const typ = inferWorkflowType(def as unknown as Record<string, unknown>);
  if (note) note.hidden = typ !== "pomerado";
  if (typ === "pomerado") {
    fields.hidden = true;
    submit.disabled = true;
    submit.textContent = he.market.publish;
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
  let blocked = false;
  try {
    const check = await validateInputs(def, def.description || def.name || "");
    form.dataset.blocksPublish = check.blocks_publish ? "1" : "0";
    const box = document.getElementById("market-input-findings");
    const html = findingsCard(check);
    if (box) box.innerHTML = html;
    else if (html) fields.insertAdjacentHTML("beforeend", `<div id="market-input-findings">${html}</div>`);
    blocked = Boolean(check.blocks_publish);
  } catch {
    form.dataset.blocksPublish = "0";
  }
  submit.disabled = blocked;
  submit.textContent = blocked ? he.market.fixToPublish : published ? he.market.update : he.market.publish;
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
      const label = model === DEFAULT_MODEL ? he.settings.modelFast : he.settings.modelAlt;
      const checked = s.model === model ? "checked" : "";
      return `<label class="model"><input type="radio" name="model" value="${escapeHtml(model)}" ${checked} /><div><b>${escapeHtml(model)}</b><small>${label}</small></div></label>`;
    })
    .join("");
  return shell(
    he.settings.title,
    he.settings.eyebrow,
    `<form id="settings-form" class="card">
      <label class="field"><span>${he.settings.backendUrl}</span>
        <input name="backendUrl" type="url" placeholder="${isLocalOrigin() ? "http://localhost:8745" : he.settingsExtra.autoTunnelPh}" value="${escapeHtml(s.backendUrl)}" />
      </label>
      <p class="muted" style="margin-top:-6px">${isLocalOrigin() ? he.settings.localBackend : isManagedBackendUrl(s.backendUrl) || !s.backendUrl ? he.settings.autoTunnel : he.settings.customBackend}</p>
      <label class="field"><span>${he.settings.accessToken}${isLocalOrigin() ? he.settingsExtra.tokenOptionalLocal : ""}</span>
        <div class="secret-wrap">
          <input name="accessToken" type="password" autocomplete="off" value="${escapeHtml(s.accessToken)}" />
          <button type="button" class="btn peek" data-peek="accessToken">${he.settings.show}</button>
        </div>
      </label>
      <p class="muted" style="margin-top:-6px">${he.settingsExtra.marketTokenNote}</p>
      ${isLocalOrigin() && !openaiConfigured ? `<div class="banner info">${he.settingsExtra.openrouterMintBanner}</div>` : ""}
      <label class="field"><span>${he.settingsExtra.openaiField}</span>
        <div class="secret-wrap">
          <input name="openaiKey" type="password" autocomplete="off" placeholder="${openaiConfigured ? he.settingsExtra.pasteReplace : "sk-…"}" value="" />
          <button type="button" class="btn peek" data-peek="openaiKey">${he.settings.show}</button>
        </div>
      </label>
      <p class="muted" style="margin-top:-6px">${openaiConfigured ? he.settings.openaiSaved : he.settings.openaiNeed}${isLocalOrigin() ? ` <button type="button" class="btn" data-action="clear-openai-key" ${openaiConfigured ? "" : "disabled"}>${he.settings.removeKey}</button>` : ""}</p>
      <label class="field"><span>${he.settingsExtra.openrouterField}</span>
        <div class="secret-wrap">
          <input name="openrouterKey" type="password" autocomplete="off" placeholder="sk-or-v1-…" value="${escapeHtml(s.openrouterKey)}" />
          <button type="button" class="btn peek" data-peek="openrouterKey">${he.settings.show}</button>
        </div>
      </label>
      <p class="muted" style="margin-top:-6px">${he.settingsExtra.openrouterNote}</p>
      <span class="field"><span>${he.settingsExtra.repairModel}</span></span>
      <div class="models">${modelBlock}</div>
      <div class="btn-row" style="margin-top:16px">
        <button class="btn" type="button" data-action="test-connection">${he.settingsExtra.testConnection}</button>
        <button class="btn primary" type="submit">${he.settings.save}</button>
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
      <button type="button" class="btn ${s.source === "remote" ? "primary" : ""}" data-action="record-source" data-source="remote" ${live || cleaning ? "disabled" : ""}>${he.record.remoteBrowser}</button>
      <button type="button" class="btn ${s.source === "extension" ? "primary" : ""}" data-action="record-source" data-source="extension" ${live || cleaning ? "disabled" : ""}>${he.recordExtra.recordInChrome}</button>
    </div>`;
  const extNote = s.source === "extension"
    ? (s.extensionReady
      ? `<p class="muted">${he.recordExtra.extensionReady}</p>`
      : `<div class="banner info">${he.recordExtra.extensionMissing}</div>`)
    : `<p class="muted">${he.recordExtra.remoteHelp}</p>`;
  const controls = `<div class="card" id="record-setup">
      ${sourceToggle}
      ${extNote}
      <label class="field"><span>${he.recordExtra.goalOptional}</span>
        <input id="record-goal" value="${escapeHtml(s.goal)}" placeholder="${he.record.goalPh}" ${live || cleaning ? "disabled" : ""} />
      </label>
      <label class="field"><span>${he.recordExtra.name}</span>
        <input id="record-name" value="${escapeHtml(s.name)}" placeholder="football_fixtures_recorded" ${live || cleaning ? "disabled" : ""} />
      </label>
      <div class="btn-row">
        ${
          live
            ? `<button class="btn danger wide" data-action="record-stop" ${cleaning ? "disabled" : ""} id="record-stop">${he.recordExtra.stopRecording}</button>`
            : `<button class="btn primary wide" data-action="record-start" ${cleaning ? "disabled" : ""} id="record-start">${he.recordExtra.startRecording}</button>`
        }
        ${live && s.source === "remote" ? `<button class="btn" data-action="record-cancel">${he.recordExtra.cancelSession}</button>` : ""}
      </div>
      ${cleaning ? `<p class="muted">${he.record.cleaning}</p>` : ""}
      ${s.error ? `<div class="banner err" style="margin-top:10px">${escapeHtml(s.error)}</div>` : ""}
    </div>`;
  const remote = s.source === "remote" && live
    ? `<div class="card record-stage">
        <form id="record-url-form" class="url-bar">
          <input id="record-url" type="url" inputmode="url" value="${escapeHtml(s.url)}" placeholder="https://www.bbc.com/sport/football" />
          <button class="btn" type="submit">${he.record.urlGo}</button>
        </form>
        <div class="record-screen-wrap">
          <img id="record-screen" alt="${he.recordExtra.remoteBrowserAlt}" src="${s.frame ? escapeHtml(s.frame) : ""}" />
          ${s.frame ? "" : `<div id="record-screen-placeholder" class="record-placeholder">${he.record.starting}</div>`}
        </div>
        <div class="record-meta"><span id="record-event-count">${s.eventCount} ${s.eventCount === 1 ? he.recordExtra.event : he.recordExtra.events}</span> · ${escapeHtml(s.url || "about:blank")}</div>
        <form id="record-type-form" class="type-bar">
          <input id="record-type" type="text" enterkeyhint="send" placeholder="${he.record.typePh}" />
          <button class="btn" type="submit" data-enter="0">${he.recordExtra.send}</button>
          <button class="btn" type="submit" data-enter="1">${he.recordExtra.sendEnter}</button>
        </form>
      </div>`
    : "";
  const extLive = s.source === "extension" && live
    ? `<div class="banner info">${he.recordExtra.extRecordingBanner}</div>`
    : "";
  const review = s.review.length
    ? `<div class="card" id="record-review">
        <h2 style="margin:0 0 8px;font-size:1.05rem">${he.recordExtra.reviewSteps}</h2>
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
              <button type="button" class="btn" data-action="${dropped ? "record-restore" : "record-drop"}" data-rec-id="${escapeHtml(item.id)}">${dropped ? he.record.restore : he.record.drop}</button>
            </div>
          </div>`;
        }).join("")}</div>
      </div>
      <div class="card">
        <h2 style="margin:0 0 8px;font-size:1.05rem">${he.recordExtra.inputsTitle}</h2>
        <p class="muted">${he.recordExtra.inputsHelp}</p>
        ${
          s.parameters.length
            ? s.parameters.map((param, i) => `<div class="param-row">
                <label class="field"><span>${he.recordExtra.name}</span><input data-param="${i}" data-field="name" value="${escapeHtml(param.name)}" /></label>
                <label class="field"><span>${he.recordExtra.label}</span><input data-param="${i}" data-field="label" value="${escapeHtml(param.label || param.name)}" /></label>
                <label class="field"><span>${he.recordExtra.example}</span><input data-param="${i}" data-field="example" value="${escapeHtml(param.example || "")}" /></label>
                <label class="check"><input type="checkbox" data-param="${i}" data-field="required" ${param.required === false ? "" : "checked"} /> ${he.recordExtra.required}</label>
              </div>`).join("")
            : `<p class="muted">${he.recordExtra.noParams}</p>`
        }
        <div class="btn-row"><button class="btn" data-action="record-add-param">${he.recordExtra.addInput}</button></div>
      </div>
      ${findingsCard(s.inputValidation, { applyAction: "record-apply-finding" })}`
    : "";
  const save = s.review.some((item) => item.kept)
    ? `<div class="btn-row">
        <button class="btn primary wide" data-action="record-save" id="record-save" ${s.phase === "cleaning" ? "disabled" : ""}>${s.workflowId ? he.record.saved : he.recordExtra.saveWorkflow}</button>
        <button class="btn wide" data-action="record-publish" id="record-publish" ${s.workflowId && !s.inputValidation?.blocks_publish ? "" : "disabled"}>${s.inputValidation?.blocks_publish ? he.recordExtra.publishBlocked : he.record.publish}</button>
      </div>
      ${s.workflowId ? `<p class="muted">${he.recordExtra.savedAs} <a href="#/workflows/${encodeURIComponent(s.workflowId)}">${escapeHtml(s.workflowId)}</a>${he.recordExtra.savedRunHint}</p>` : ""}
      ${s.listing ? `<div class="banner ok">${he.recordExtra.publishedBanner} ${escapeHtml(s.listing.name)} v${escapeHtml(s.listing.version)} ${he.recordExtra.toMarket}</div>` : ""}`
    : "";
  const flash = flashMessage ? `<div class="banner ${flashMessage.kind}">${escapeHtml(flashMessage.text)}</div>` : "";
  flashMessage = null;
  return shell(
    he.record.title,
    he.recordExtra.authoring,
    `${flash}${controls}${extLive}${remote}${review}${save}`,
    he.record.subtitle,
  );
}

function renderFromTaskForm(): string {
  const settings = loadSettings();
  const keyNote = settings.openrouterKey
    ? he.fromTaskExtra.keyWillSend
    : `${he.fromTask.needOr}<a href="#/settings">${he.nav.settings}</a>${he.fromTask.needOr2}`;
  return shell(
    he.fromTask.title,
    he.fromTaskExtra.authoring,
    `${!settings.openrouterKey ? `<div class="banner info">${he.fromTask.needOr}<a href="#/settings">${he.nav.settings}</a>${he.fromTask.needOr2}</div>` : ""}
    <form id="from-task-form" class="card">
      <p class="sub" style="margin-top:0">${he.fromTaskExtra.formIntro}</p>
      <label class="field"><span>${he.fromTask.task}</span>
        <textarea name="task" required placeholder="${he.fromTask.taskPh}" style="min-height:110px;font-family:var(--font);font-size:16px"></textarea>
      </label>
      <label class="field"><span>${he.fromTaskExtra.startUrl}</span>
        <input name="start_url" type="url" placeholder="http://127.0.0.1:8765/from_task_form.html" />
      </label>
      <label class="field"><span>${he.fromTaskExtra.workflowName}</span>
        <input name="name" type="text" placeholder="library_search" />
      </label>
      <label class="field"><span>${he.fromTaskExtra.variables}</span>
        <textarea name="variables" placeholder="query=browser automation" style="min-height:88px;font-family:var(--mono);font-size:14px"></textarea>
      </label>
      <label class="check">
        <input type="checkbox" name="confirm_variables" checked />
        <span>${he.fromTaskExtra.confirmVars}</span>
      </label>
      <p class="muted">${escapeHtml(keyNote)}</p>
      <button class="btn primary" type="submit">${he.fromTaskExtra.startDemo}</button>
    </form>`,
    he.fromTaskExtra.replaySubtitle,
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
    escapeHtml(job.name || he.fromTask.title),
    he.fromTaskExtra.liveJob,
    `${confirmation}
    <div class="card">
      <div class="row">
        <span class="pill ${statusClass(job.status)}"><span class="dot"></span>${escapeHtml(job.status)}${spin}</span>
        <span class="mono muted">${escapeHtml(job.id.slice(0, 10))}</span>
      </div>
      <p class="sub">${escapeHtml(job.task)}</p>
      ${error}
      <div class="status-grid">
        <div class="stat"><b>${escapeHtml(formatLatency(creation?.latency_ms))}</b><span>${he.fromTaskExtra.creationTime}</span></div>
        <div class="stat"><b>${escapeHtml(formatTokens(creation))}</b><span>${he.fromTaskExtra.creationTokens}</span></div>
      </div>
    </div>
    ${result}
    <a class="btn" href="#/from-task" style="margin-top:12px">${he.fromTask.another}</a>`,
    live ? he.fromTask.working : "",
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
            <input type="checkbox" name="required-${i}" ${required ? "checked" : ""} data-index="${i}" title="${he.aria.required}" />
            <input type="text" name="name-${i}" value="${escapeHtml(name)}" aria-label="${he.aria.inputName}" placeholder="name" />
            <input type="text" name="label-${i}" value="${escapeHtml(label)}" aria-label="${he.aria.label}" placeholder="${he.aria.label}" />
            <input type="text" name="value-${i}" value="${escapeHtml(String(value))}" aria-label="${he.aria.exampleValue}" placeholder="${he.mint.exampleValue}" />
          </label>`;
          })
          .join("")
      : `<p class="muted">${he.fromTaskExtra.noInputsDetected}</p>`;
    return `<div class="card confirm-panel" id="from-task-confirm" data-job-id="${escapeHtml(job.id)}" data-kind="parameters">
    <p class="eyebrow">${he.fromTaskExtra.needsConfirmInputs}</p>
    <h2>${he.fromTaskExtra.confirmInputsTitle}</h2>
    <p class="sub">${he.fromTaskExtra.confirmInputsBody}</p>
    <div class="var-list-head muted">${he.fromTaskExtra.varListHead}</div>
    <div class="var-list">${rows}</div>
    <button type="button" class="btn" data-action="from-task-add-input" style="margin-bottom:12px">${he.recordExtra.addInput}</button>
    <div class="btn-row">
      <button type="button" class="btn danger" data-action="from-task-cancel">${he.confirm.cancel}</button>
      <button type="button" class="btn primary" data-action="from-task-confirm">${he.fromTaskExtra.confirmAndStart}</button>
    </div>
    <div id="from-task-confirm-status" style="margin-top:12px"></div>
  </div>`;
  }
  if (kind !== "variables") {
    const message = payload.message || payload.prompt || he.fromTask.waitingConfirm;
    return `<div class="card confirm-panel" id="from-task-confirm" data-job-id="${escapeHtml(job.id)}" data-kind="${escapeHtml(kind)}">
      <p class="eyebrow">${he.confirm.needs}</p>
      <h2>${he.fromTaskExtra.agentQuestion}</h2>
      <p class="sub">${escapeHtml(message)}</p>
      <div class="btn-row">
        <button type="button" class="btn danger" data-action="from-task-cancel">${he.confirm.cancel}</button>
        <button type="button" class="btn primary" data-action="from-task-confirm">${he.confirm.confirm}</button>
      </div>
      <div id="from-task-confirm-status" style="margin-top:12px"></div>
    </div>`;
  }
  const suggestions = suggestionRows(job);
  const already = payload.variables || job.variables || {};
  const alreadyBits = Object.keys(already).length
    ? `<p class="meta">${he.fromTaskExtra.alreadyInWorkflow} ${escapeHtml(Object.entries(already).map(([k, v]) => `${k}=${v}`).join(", "))}</p>`
    : "";
  const rows = suggestions.length
    ? suggestions
        .map((item, i) => {
          const name = item.suggested_name || `var_${i + 1}`;
          const value = item.value ?? "";
          const hint = item.element && (item.element.name || item.element.placeholder || item.element.role);
          return `<label class="var-row">
            <input type="checkbox" name="accept-${i}" checked data-index="${i}" />
            <input type="text" name="name-${i}" value="${escapeHtml(name)}" aria-label="${he.aria.variableName}" />
            <input type="text" name="value-${i}" value="${escapeHtml(value)}" aria-label="${he.aria.detectedValue}" />
            ${hint || item.type ? `<span class="muted" style="grid-column:2 / -1">${escapeHtml(item.type || "")}${hint ? " · " + escapeHtml(String(hint)) : ""}</span>` : ""}
          </label>`;
        })
        .join("")
    : `<p class="muted">${he.fromTaskExtra.noVarsSuggested}</p>`;
  return `<div class="card confirm-panel" id="from-task-confirm" data-job-id="${escapeHtml(job.id)}" data-kind="variables">
    <p class="eyebrow">${he.fromTaskExtra.needsConfirmVars}</p>
    <h2>${he.fromTaskExtra.acceptVarsTitle}</h2>
    <p class="sub">${he.fromTaskExtra.acceptVarsBody}</p>
    ${alreadyBits}
    <div class="var-list">${rows}</div>
    <button type="button" class="btn" data-action="from-task-add-input" style="margin-bottom:12px">${he.recordExtra.addInput}</button>
    <div class="btn-row">
      <button type="button" class="btn danger" data-action="from-task-cancel">${he.confirm.cancel}</button>
      <button type="button" class="btn primary" data-action="from-task-confirm">${he.fromTaskExtra.confirmVarsBtn}</button>
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
    .join("") || `<p class="muted">${he.fromTaskExtra.noStepsSaved}</p>`;
  const replay = job.replay;
  const replayStatus = replay?.status ? `<span class="pill ${statusClass(String(replay.status))}">${escapeHtml(String(replay.status))}</span>` : "";
  const outputs = replay?.outputs && Object.keys(replay.outputs).length
    ? `<pre class="output">${escapeHtml(JSON.stringify(replay.outputs, null, 2))}</pre>`
    : "";
  return `<div class="card" id="from-task-result">
    <h2 style="margin:0 0 8px;font-size:1.08rem">${escapeHtml(def?.name || job.name)}</h2>
    <div class="mono muted">${wfId ? he.fromTaskExtra.workflowPrefix + escapeHtml(wfId) : he.fromTaskExtra.noWorkflowId}</div>
    <div class="usage-compare">
      <div class="stat">
        <b>${escapeHtml(formatLatency(job.creation?.latency_ms))}</b>
        <span>${he.fromTaskExtra.creation} · ${escapeHtml(formatTokens(job.creation))}</span>
      </div>
      <div class="stat">
        <b>${escapeHtml(formatLatency(replay?.latency_ms))}</b>
        <span>${he.fromTaskExtra.replay} · ${escapeHtml(formatTokens(replay))} ${replayStatus}</span>
      </div>
    </div>
    <div style="margin-top:16px">${steps}</div>
    ${outputs ? `<div style="margin-top:12px">${outputs}</div>` : ""}
    ${findingsCard((job as FromTaskJob & { input_validation?: InputValidation }).input_validation, { applyAction: "from-task-apply-finding" })}
    <div class="btn-row" style="margin-top:16px">
      ${wfId ? `<a class="btn" href="#/workflows/${encodeURIComponent(wfId)}">${he.fromTask.open}</a>
      <a class="btn primary" href="#/workflows/${encodeURIComponent(wfId)}" data-action="from-task-run" data-workflow-id="${escapeHtml(wfId)}">${he.fromTask.run}</a>` : `<span class="muted">${he.fromTask.notSaved}</span>`}
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
    return shell(he.fromTask.title, he.fromTask.job, `${bannerError(err)}<a class="btn" href="#/from-task">${he.fromTask.another}</a>`);
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
  else if (route.name === "chat") html = shell(he.chat.title, he.chat.eyebrow, `<div id="chat-page">${renderChatPage()}</div>`, he.chat.subtitle);
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
  if (route.name === "chat") {
    void bindChatPage();
  }
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
    peek.textContent = input.type === "password" ? he.common.show : he.common.hide;
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
    recordState.parameters = [...recordState.parameters, { name: "value", label: he.recordExtra.valueLabel, example: "", required: true }];
    await render();
    return;
  }
  if (action === "record-save") {
    event.preventDefault();
    const btn = target.closest("button") as HTMLButtonElement | null;
    if (btn) btn.disabled = true;
    try {
      await saveRecording();
      flashMessage = { kind: "ok", text: he.flash.savedLibrary };
    } catch (err) {
      recordState.error = err instanceof Error ? err.message : String(err);
    }
    await render();
    return;
  }
  if (action && handleChatClick(action, (target.closest("[data-action]") as HTMLElement | null) || target)) {
    return;
  }
  if (action === "record-apply-finding") {
    event.preventDefault();
    const id = (target.closest("[data-finding]") as HTMLElement | null)?.dataset.finding;
    const finding = (recordState.inputValidation?.findings || []).find((item) => item.id === id);
    if (!finding?.patch || !recordState.definition) return;
    try {
      const result = await validateInputs(recordState.definition as import("./api").WorkflowDefinition, recordState.goal, finding.patch);
      recordState.definition = (result.definition || recordState.definition) as Record<string, unknown>;
      recordState.inputValidation = result;
      if (Array.isArray(result.definition?.input_schema)) {
        recordState.parameters = result.definition.input_schema.map((item) => ({
          name: item.name,
          label: item.label,
          example: item.default != null ? String(item.default) : "",
          required: item.required !== false,
        }));
      }
    } catch (err) {
      recordState.error = err instanceof Error ? err.message : String(err);
    }
    await render();
    return;
  }
  if (action === "from-task-apply-finding") {
    event.preventDefault();
    const route = parseRoute();
    if (route.name !== "from-task-job") return;
    try {
      const job = await getFromTaskJob(route.id);
      const id = (target.closest("[data-finding]") as HTMLElement | null)?.dataset.finding;
      const finding = (job.input_validation?.findings || []).find((item) => item.id === id);
      if (!finding?.patch || !job.definition) return;
      const result = await validateInputs(job.definition, job.task, finding.patch);
      if (job.workflow_id && result.definition) {
        await api.updateWorkflow(job.workflow_id, result.definition);
      }
      flashMessage = { kind: "ok", text: he.flash.appliedFix };
    } catch (err) {
      flashMessage = { kind: "err", text: err instanceof Error ? err.message : String(err) };
    }
    await render();
    return;
  }
  if (action === "record-publish") {
    event.preventDefault();
    if (recordState.inputValidation?.blocks_publish) {
      recordState.error = he.recordExtra.publishBlockedErr;
      await render();
      return;
    }
    const btn = target.closest("button") as HTMLButtonElement | null;
    if (btn) btn.disabled = true;
    try {
      const listing = await publishRecording();
      flashMessage = { kind: "ok", text: `${he.flash.publishedListing} ${listing.name} v${listing.version} ${he.recordExtra.toMarket}` };
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
    if (status) status.innerHTML = `<div class="banner info">${action === "hitl-cancel" ? he.hitl.cancelling : he.hitl.confirming}</div>`;
    void (async () => {
      try {
        if (action === "hitl-cancel") await cancelRun(runId, prompt);
        else {
          if (!selectedCandidateId) throw new Error(he.hitl.selectCandidate);
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
    if (!window.confirm(he.flash.deleteMarketConfirm)) return;
    const btn = target.closest("button") as HTMLButtonElement | null;
    if (btn) btn.disabled = true;
    try {
      await deleteMarketListing(id);
      flashMessage = { kind: "ok", text: he.flash.listingDeleted };
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
    if (!window.confirm(he.flash.removeOpenaiConfirm)) return;
    try {
      await api.setOpenAIKey("");
      flashMessage = { kind: "ok", text: he.flash.removeOpenaiDone };
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
    row.innerHTML = `<input type="text" name="input-name-${i}" value="" placeholder="name" aria-label="${he.aria.inputName}" /><input type="text" name="input-example-${i}" value="" placeholder="${he.mint.exampleValue}" aria-label="${he.aria.exampleValue}" />`;
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
      ? `<input type="checkbox" name="required-${i}" checked data-index="${i}" title="${he.aria.required}" /><input type="text" name="name-${i}" value="" placeholder="name" aria-label="${he.aria.inputName}" /><input type="text" name="label-${i}" value="" placeholder="${he.aria.label}" aria-label="${he.aria.label}" /><input type="text" name="value-${i}" value="" placeholder="${he.mint.exampleValue}" aria-label="${he.aria.exampleValue}" />`
      : `<input type="checkbox" name="accept-${i}" checked data-index="${i}" /><input type="text" name="name-${i}" value="" placeholder="name" aria-label="${he.aria.variableName}" /><input type="text" name="value-${i}" value="" placeholder="${he.mint.exampleValue}" aria-label="${he.aria.detectedValue}" />`;
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
    if (status) status.innerHTML = `<div class="banner info">${action === "from-task-cancel" ? he.hitl.cancelling : he.flash.savingInputs}</div>`;
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
    status.innerHTML = `<div class="banner info">${he.flash.checking}</div>`;
    try {
      const h = await api.health(trial);
      const listed = await api.listWorkflows();
      connected = true;
      flashMessage = {
        kind: "ok",
        text: `${he.flash.connected} ${h.service || he.status.backend}. ${listed.workflows.length} ${he.flash.workflowsVisible}`,
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
      if (status) status.innerHTML = `<div class="banner err">${he.flash.chooseWorkflowPublish}</div>`;
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
        if (status) status.innerHTML = `<div class="banner err">${escapeHtml(err instanceof Error ? err.message : he.marketExtra.jsonArrayErr)}</div>`;
        return;
      }
    }
    if (form.dataset.blocksPublish === "1") {
      if (status) status.innerHTML = `<div class="banner err">${he.flash.publishingBlocked}</div>`;
      return;
    }
    if (button) button.disabled = true;
    if (status) status.innerHTML = `<div class="banner info">${he.flash.publishing}</div>`;
    try {
      const stored = await api.getWorkflow(workflowId);
      const check = await validateInputs(stored.definition, stored.definition.description || stored.definition.name || "");
      if (check.blocks_publish) {
        if (button) button.disabled = false;
        if (status) status.innerHTML = findingsCard(check) || `<div class="banner err">${he.validation.blockPublish}</div>`;
        return;
      }
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
      flashMessage = { kind: "ok", text: `${he.flash.publishedListing} ${result.listing.name} v${result.listing.version}.` };
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
      flashMessage = { kind: "ok", text: openaiKey ? he.flash.savedOpenai : he.flash.savedDevice };
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
        if (!url) throw new Error(he.newWorkflow.pasteOrUrl);
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
      alert(he.runPage.fillRequired);
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
      button.textContent = he.runPage.starting;
    }
    try {
      const run = await api.startRun(id, body);
      saveRunInputs(id, values);
      go(`/runs/${run.id}`);
    } catch (err) {
      if (button) {
        button.disabled = false;
        button.textContent = he.runPage.runNow;
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
      button.textContent = he.runPage.starting;
    }
    try {
      const job = await startFromTask(payload);
      go(`/from-task/${job.id}`);
    } catch (err) {
      if (button) {
        button.disabled = false;
        button.textContent = he.fromTaskExtra.startDemo;
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
    if (status) status.innerHTML = `<div class="card"><div class="banner info">${he.flash.mintStarted}</div></div>`;
    try {
      const { result } = await api.mint(payload);
      const jobId = mintJobId(result);
      if (jobId) {
        go(`/mint/${jobId}`);
        return;
      }
      if (status) {
        status.innerHTML = `<div class="card"><h2 style="margin:0 0 8px;font-size:1.05rem">${he.mintExtra.job}</h2><pre class="output">${escapeHtml(JSON.stringify(result, null, 2))}</pre></div>`;
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


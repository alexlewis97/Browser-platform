import { api, type CatalogWorkflow, type WorkflowInput } from "./api";
import { he } from "./copy/he";
import { probeExtension, requestRun, watchRunProgress, type ExtRun } from "./ext";
import { getRun, listRuns, loadInputs, newRunId, saveInputs, upsertRun, type LocalRun } from "./storage";

type Route =
  | { name: "catalog" }
  | { name: "workflow"; id: string }
  | { name: "runs" }
  | { name: "run"; id: string };

let root: HTMLElement;
let extensionPresent = false;
let extensionChecked = false;
let catalogCache: CatalogWorkflow[] | null = null;
let searchQuery = "";
let banner: { kind: "ok" | "err" | "info"; text: string } | null = null;
let liveRun: LocalRun | null = null;
let stopProgress: (() => void) | null = null;

function parseRoute(): Route {
  const hash = location.hash.replace(/^#/, "") || "/";
  const path = hash.split("?")[0];
  const parts = path.split("/").filter(Boolean);
  if (parts[0] === "w" && parts[1]) return { name: "workflow", id: decodeURIComponent(parts[1]) };
  if (parts[0] === "runs" && parts[1]) return { name: "run", id: decodeURIComponent(parts[1]) };
  if (parts[0] === "runs") return { name: "runs" };
  return { name: "catalog" };
}

function go(path: string): void {
  location.hash = path.startsWith("#") ? path : `#${path}`;
}

function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatWhen(value?: string | null): string {
  if (!value) return he.common.dash;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return escapeHtml(value);
  return new Intl.DateTimeFormat("he-IL", { dateStyle: "short", timeStyle: "short", timeZone: "Asia/Jerusalem" }).format(date);
}

function statusLabel(status: string): string {
  const key = status as keyof typeof he.status;
  return he.status[key] || status;
}

function statusClass(status: string): string {
  if (status === "ok" || status === "succeeded") return "ok";
  if (status === "fail" || status === "failed" || status === "cancel") return "bad";
  if (status === "running" || status === "hitl") return "warn";
  return "";
}

function shell(title: string, eyebrow: string, body: string, subtitle = ""): string {
  const extPill = !extensionChecked
    ? `<span class="pill">${he.ext.checking}</span>`
    : extensionPresent
      ? `<span class="pill ok">${he.ext.connected}</span>`
      : `<span class="pill bad">${he.ext.missing}</span>`;
  const flash = banner ? `<div class="banner ${banner.kind}">${escapeHtml(banner.text)}</div>` : "";
  banner = null;
  return `
    <div class="shell">
      <header class="top">
        <div>
          <p class="eyebrow">${escapeHtml(eyebrow)}</p>
          <h1>${escapeHtml(title)}</h1>
          ${subtitle ? `<p class="sub">${escapeHtml(subtitle)}</p>` : ""}
        </div>
        ${extPill}
      </header>
      ${flash}
      ${extensionChecked && !extensionPresent ? installPrompt() : ""}
      ${body}
    </div>
    ${nav()}`;
}

function nav(): string {
  const route = parseRoute();
  const workflowsActive = route.name === "catalog" || route.name === "workflow" ? "active" : "";
  const runsActive = route.name === "runs" || route.name === "run" ? "active" : "";
  return `<nav class="nav">
    <a class="${workflowsActive}" href="#/">${he.nav.workflows}</a>
    <a class="${runsActive}" href="#/runs">${he.nav.runs}</a>
  </nav>`;
}

function installPrompt(): string {
  return `<div class="install">
    <h2>${he.ext.installTitle}</h2>
    <p>${he.ext.installBody}</p>
    <p class="muted">${he.ext.stepsTitle}</p>
    <ol>
      <li>${he.ext.step1}</li>
      <li>${he.ext.step2}</li>
      <li>${he.ext.step3}</li>
      <li>${he.ext.step4}</li>
    </ol>
    <a class="btn primary" href="/browser-platform-extension.zip" download>${he.ext.download}</a>
  </div>`;
}

function prettyLabel(input: WorkflowInput): string {
  if (input.label && input.label.trim()) return input.label.trim();
  const text = (input.name || "").replace(/_/g, " ").trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : input.name;
}

function deriveInputs(workflow: CatalogWorkflow): WorkflowInput[] {
  const schema = workflow.definition?.input_schema || workflow.input_schema || [];
  const byName = new Map<string, WorkflowInput>();
  for (const item of schema) {
    const name = String(item.name || "").trim();
    if (!name) continue;
    byName.set(name, item);
  }
  return [...byName.values()];
}

function inputValue(workflowId: string, input: WorkflowInput): string {
  const saved = loadInputs(workflowId)[input.name];
  if (saved != null && saved !== "") return saved;
  if (input.default != null) return String(input.default);
  return "";
}

function renderInputs(workflowId: string, inputs: WorkflowInput[]): string {
  if (!inputs.length) return `<p class="muted">${he.workflow.noInputs}</p>`;
  return inputs
    .map((input) => {
      const required = input.required !== false && input.default == null;
      const hintParts = [
        input.description || "",
        input.default != null ? `${he.workflow.defaultHint} ${String(input.default)}` : "",
      ].filter(Boolean);
      return `<label class="field">
        <span>${escapeHtml(prettyLabel(input))} <span class="muted">${required ? he.workflow.required : he.workflow.optional}</span></span>
        <input name="${escapeHtml(input.name)}" type="text" ${required ? "required" : ""} value="${escapeHtml(inputValue(workflowId, input))}" autocomplete="off" />
        ${hintParts.length ? `<span class="muted">${escapeHtml(hintParts.join(" · "))}</span>` : ""}
      </label>`;
    })
    .join("");
}

function matchesQuery(item: CatalogWorkflow, query: string): boolean {
  if (!query.trim()) return true;
  const hay = `${item.name} ${item.description || ""}`.toLowerCase();
  return hay.includes(query.trim().toLowerCase());
}

async function loadCatalog(force = false): Promise<CatalogWorkflow[]> {
  if (!force && catalogCache) return catalogCache;
  const { workflows } = await api.listCatalog();
  catalogCache = workflows;
  return workflows;
}

async function renderCatalog(): Promise<string> {
  try {
    const workflows = await loadCatalog();
    const filtered = workflows.filter((item) => matchesQuery(item, searchQuery));
    const list = !workflows.length
      ? `<div class="card empty">${he.catalog.empty}</div>`
      : !filtered.length
        ? `<div class="card empty">${he.catalog.noneMatch}</div>`
        : `<div class="cards">${filtered
            .map(
              (item) => `<a class="card card-link" href="#/w/${encodeURIComponent(item.id)}">
                <div class="row">
                  <div class="grow">
                    <strong>${escapeHtml(item.name)}</strong>
                    <p class="card-desc">${escapeHtml(item.description || he.catalog.noDescription)}</p>
                    <div class="meta">v${escapeHtml(item.version || "1")}</div>
                  </div>
                  <span class="muted">${he.catalog.open}</span>
                </div>
              </a>`,
            )
            .join("")}</div>`;
    return shell(
      he.title,
      he.catalog.eyebrow,
      `<input class="search" type="search" name="q" value="${escapeHtml(searchQuery)}" placeholder="${he.catalog.search}" />
       ${list}`,
      he.catalog.subtitle,
    );
  } catch (err) {
    return shell(he.title, he.catalog.eyebrow, `<div class="banner err">${escapeHtml(err instanceof Error ? err.message : he.catalog.down)}</div>`);
  }
}

function renderTraces(run: LocalRun | ExtRun): string {
  const traces = ("traces" in run ? run.traces : []) || [];
  if (!traces.length) return "";
  return traces
    .map((step, i) => {
      const state = step.status === "ok" ? "ok" : step.status === "fail" ? "bad" : "run";
      const mark = step.status === "ok" ? "✓" : step.status === "fail" ? "!" : "…";
      return `<div class="step">
        <div class="idx ${state}">${mark}</div>
        <div>
          <strong>${escapeHtml(step.type || `${i + 1}`)}${step.description ? " · " + escapeHtml(step.description) : ""}</strong>
          <div class="meta">${escapeHtml(step.message || step.status || "")}</div>
        </div>
      </div>`;
    })
    .join("");
}

function renderOutputs(outputs?: Record<string, unknown> | null): string {
  if (!outputs || !Object.keys(outputs).length) return `<p class="muted">${he.runs.noOutput}</p>`;
  return `<pre class="output">${escapeHtml(JSON.stringify(outputs, null, 2))}</pre>`;
}

function renderLiveOrHistory(run: LocalRun): string {
  return `<div class="card">
    <div class="row">
      <div class="grow">
        <strong>${escapeHtml(run.workflowName)}</strong>
        <div class="meta">${formatWhen(run.startedAt)}</div>
      </div>
      <span class="pill ${statusClass(run.status)}">${escapeHtml(statusLabel(run.status))}</span>
    </div>
    ${Object.keys(run.values || {}).length ? `<div class="output-block"><p class="muted">${he.runs.values}</p><pre class="output">${escapeHtml(JSON.stringify(run.values, null, 2))}</pre></div>` : ""}
    <div class="output-block">${renderTraces(run)}</div>
    <div class="output-block">
      <p class="muted">${he.runs.outputs}</p>
      ${renderOutputs(run.outputs)}
    </div>
    ${run.error ? `<div class="banner err" style="margin-top:12px">${escapeHtml(run.error)}</div>` : ""}
  </div>`;
}

async function renderWorkflow(id: string): Promise<string> {
  try {
    const workflow = await api.getCatalog(id);
    const inputs = deriveInputs(workflow);
    const runCard = liveRun && liveRun.workflowId === id ? renderLiveOrHistory(liveRun) : "";
    return shell(
      workflow.name,
      he.workflow.eyebrow,
      `<div class="card">
         <p class="sub" style="margin:0">${escapeHtml(workflow.description || workflow.definition?.description || he.catalog.noDescription)}</p>
       </div>
       <form id="run-form" class="card" data-id="${escapeHtml(workflow.id)}" data-name="${escapeHtml(workflow.name)}">
         <h2 style="margin:0 0 12px;font-size:1.05rem">${he.workflow.run}</h2>
         ${renderInputs(workflow.id, inputs)}
         <p class="muted">${he.workflow.confirmHint}</p>
         <button class="btn primary" type="submit" ${extensionPresent ? "" : "disabled"}>${extensionPresent ? he.workflow.run : he.ext.missing}</button>
       </form>
       ${runCard}
       <p style="margin-top:14px"><a class="btn" href="#/">${he.workflow.back}</a></p>`,
    );
  } catch (err) {
    return shell(he.workflow.eyebrow, he.common.error, `<div class="banner err">${escapeHtml(err instanceof Error ? err.message : he.workflow.notFound)}</div><a class="btn" href="#/">${he.workflow.back}</a>`);
  }
}

async function renderRuns(): Promise<string> {
  const runs = listRuns();
  const list = !runs.length
    ? `<div class="card empty">${he.runs.empty}</div>`
    : runs
        .map(
          (run) => `<a class="card card-link" href="#/runs/${encodeURIComponent(run.id)}">
            <div class="row">
              <div class="grow">
                <strong>${escapeHtml(run.workflowName)}</strong>
                <div class="meta">${formatWhen(run.startedAt)}</div>
              </div>
              <span class="pill ${statusClass(run.status)}">${escapeHtml(statusLabel(run.status))}</span>
            </div>
          </a>`,
        )
        .join("");
  return shell(he.runs.title, he.runs.eyebrow, list, he.runs.subtitle);
}

async function renderRun(id: string): Promise<string> {
  const run = (liveRun && liveRun.id === id ? liveRun : getRun(id)) || getRun(id);
  if (!run) {
    return shell(he.runs.detail, he.common.error, `<div class="card empty">${he.runs.empty}</div><a class="btn" href="#/runs">${he.nav.runs}</a>`);
  }
  return shell(run.workflowName, he.runs.detail, `${renderLiveOrHistory(run)}<p style="margin-top:14px"><a class="btn" href="#/runs">${he.nav.runs}</a></p>`);
}

async function render(): Promise<void> {
  const route = parseRoute();
  let html = "";
  if (route.name === "workflow") html = await renderWorkflow(route.id);
  else if (route.name === "runs") html = await renderRuns();
  else if (route.name === "run") html = await renderRun(route.id);
  else html = await renderCatalog();
  root.innerHTML = html;
  const search = root.querySelector<HTMLInputElement>("input.search");
  if (search) {
    search.focus();
    const value = search.value;
    search.setSelectionRange(value.length, value.length);
  }
}

async function refreshExtension(): Promise<void> {
  extensionPresent = await probeExtension();
  extensionChecked = true;
}

function formValues(form: HTMLFormElement): Record<string, string> {
  const data = new FormData(form);
  const values: Record<string, string> = {};
  for (const [key, value] of data.entries()) {
    values[key] = String(value);
  }
  return values;
}

function applyExtRun(local: LocalRun, ext: ExtRun): LocalRun {
  const outputs: Record<string, unknown> = { ...(local.outputs || {}) };
  for (const step of ext.traces || []) {
    if (step.output != null && step.type) outputs[String(step.description || step.type)] = step.output;
  }
  if (ext.outputs) Object.assign(outputs, ext.outputs);
  const finished = ext.status && ext.status !== "running" && ext.status !== "hitl";
  return {
    ...local,
    status: ext.status || local.status,
    traces: ext.traces || local.traces,
    outputs,
    error: ext.error || (ext.status === "fail" ? local.error : null),
    finishedAt: finished ? new Date().toISOString() : local.finishedAt,
  };
}

async function onSubmit(event: Event): Promise<void> {
  const form = event.target as HTMLFormElement | null;
  if (!form || form.id !== "run-form") return;
  event.preventDefault();
  if (!extensionPresent) {
    banner = { kind: "err", text: he.ext.installTitle };
    await render();
    return;
  }
  const id = form.dataset.id || "";
  const name = form.dataset.name || "";
  const values = formValues(form);
  saveInputs(id, values);
  const btn = form.querySelector("button[type='submit']") as HTMLButtonElement | null;
  if (btn) {
    btn.disabled = true;
    btn.textContent = he.ext.waiting;
  }
  let workflow: CatalogWorkflow;
  try {
    workflow = await api.getCatalog(id);
  } catch (err) {
    banner = { kind: "err", text: err instanceof Error ? err.message : he.workflow.notFound };
    await render();
    return;
  }
  const local: LocalRun = {
    id: newRunId(),
    workflowId: id,
    workflowName: name || workflow.name,
    status: "running",
    startedAt: new Date().toISOString(),
    values,
    traces: [],
  };
  liveRun = local;
  upsertRun(local);
  stopProgress?.();
  stopProgress = watchRunProgress((ext) => {
    if (!liveRun) return;
    liveRun = applyExtRun(liveRun, ext);
    upsertRun(liveRun);
    void render();
  });
  await render();
  try {
    const result = await requestRun({
      id,
      name: local.workflowName,
      definition: (workflow.definition || workflow) as Record<string, unknown>,
      values,
    });
    if (result.cancelled) {
      liveRun = { ...local, status: "cancel", error: he.ext.cancelled, finishedAt: new Date().toISOString() };
    } else if (result.run) {
      liveRun = applyExtRun(liveRun, result.run);
      if (!result.ok && !liveRun.error) liveRun.error = result.error || he.common.error;
      if (!result.ok && liveRun.status === "running") liveRun.status = "fail";
    } else {
      liveRun = {
        ...local,
        status: result.ok ? "ok" : "fail",
        error: result.ok ? null : result.error || he.common.error,
        finishedAt: new Date().toISOString(),
      };
    }
    upsertRun(liveRun);
    go(`#/runs/${encodeURIComponent(liveRun.id)}`);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    liveRun = { ...local, status: "fail", error: message, finishedAt: new Date().toISOString() };
    upsertRun(liveRun);
    banner = { kind: "err", text: message };
    await render();
  } finally {
    stopProgress?.();
    stopProgress = null;
  }
}

function onInput(event: Event): void {
  const target = event.target as HTMLInputElement | null;
  if (!target || !target.classList.contains("search")) return;
  searchQuery = target.value;
  window.clearTimeout((onInput as unknown as { t?: number }).t);
  (onInput as unknown as { t?: number }).t = window.setTimeout(() => void render(), 120);
}

export function mount(el: HTMLElement): void {
  root = el;
  window.addEventListener("hashchange", () => void render());
  el.addEventListener("submit", (event) => void onSubmit(event));
  el.addEventListener("input", onInput);
  void (async () => {
    await refreshExtension();
    await render();
  })();
}

export const __test = { parseRoute, deriveInputs, inputValue, matchesQuery, applyExtRun };

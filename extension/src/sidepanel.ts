import { deriveInputs, missingRequired, prettyLabel } from "./lib/inputs.ts";
import { listingIdForName } from "./lib/market.ts";
import type { Candidate, ExtensionSettings, InstalledWorkflow, MarketListing, RunState, WorkflowInput } from "./lib/types.ts";
import { DEFAULT_BACKEND_URL, DEFAULT_MARKET_ORIGIN } from "./lib/types.ts";

type View = "installed" | "record" | "market" | "run" | "settings";
type MarketItem = MarketListing & { installed?: boolean; installedVersion?: string; updateAvailable?: boolean };

let view: View = "installed";
let settings: ExtensionSettings = { marketToken: "", marketOrigin: DEFAULT_MARKET_ORIGIN, backendUrl: DEFAULT_BACKEND_URL };
let workflows: InstalledWorkflow[] = [];
let listings: MarketItem[] = [];
let run: RunState | null = null;
let selectedId = "";
let inputValues: Record<string, string> = {};
let flash: { kind: "ok" | "err" | "info"; text: string } | null = null;
let hitlChoice: string | null = null;
let busy = false;
let publishId = "";

type ReviewItem = { id: string; kept: boolean; reason?: string; stage?: string; step: Record<string, unknown> };
type ParamDraft = { name: string; label?: string; example?: string; required?: boolean; description?: string };
let recordingActive = false;
let recordingCount = 0;
let recordGoal = "";
let recordName = "";
let recordBusy = false;
let recordEvents: Record<string, unknown>[] = [];
let reviewItems: ReviewItem[] = [];
let recordParams: ParamDraft[] = [];
let recordUsage: { tokens_in?: number; tokens_out?: number; used?: boolean; extract_used?: boolean; prune_used?: boolean; label?: string } | null = null;
let recordStats: Record<string, unknown> | null = null;

const app = () => document.getElementById("app")!;

function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function send<T = Record<string, unknown>>(kind: string, payload: Record<string, unknown> = {}): Promise<T & { ok: boolean; error?: string }> {
  return (await chrome.runtime.sendMessage({ kind, ...payload })) as T & { ok: boolean; error?: string };
}

async function refreshState(): Promise<void> {
  const res = await send<{ settings: ExtensionSettings; workflows: InstalledWorkflow[]; run: RunState | null; recording?: { active: boolean; count: number } }>("bp-get-state");
  if (!res.ok) throw new Error(res.error || "Failed to read extension state");
  settings = res.settings;
  workflows = res.workflows;
  run = res.run;
  if (res.recording) {
    recordingActive = res.recording.active;
    recordingCount = res.recording.count;
  }
}

function banner(): string {
  return flash ? `<div class="banner ${flash.kind}">${escapeHtml(flash.text)}</div>` : "";
}

function shell(title: string, body: string): string {
  const tab = (name: View, label: string) =>
    `<button type="button" data-view="${name}" class="${view === name || (view === "run" && name === "installed" && selectedId) ? "active" : ""}">${label}</button>`;
  return `
    <header class="top"><div><p class="eyebrow">Browser Platform</p><h1>${title}</h1></div></header>
    <nav class="tabs">
      ${tab("installed", "Installed")}
      ${tab("record", "Record")}
      ${tab("market", "Market")}
      ${tab("run", "Run")}
      ${tab("settings", "Settings")}
    </nav>
    <div class="page">${banner()}${body}</div>
  `;
}

function renderInstalled(): string {
  const cards = workflows.length
    ? workflows
        .map(
          (w) => `<article class="card">
            <h2 style="margin:0 0 4px;font-size:1rem">${escapeHtml(w.name)}</h2>
            <div class="mono muted">v${escapeHtml(w.version)} · ${escapeHtml(w.type)} · ${w.source === "market" ? "marketplace" : w.source === "record" ? "recorded" : "imported"}</div>
            <p class="sub">${escapeHtml(w.description || "")}</p>
            <div class="btn-row">
              <button class="btn primary" data-action="open-run" data-id="${escapeHtml(w.id)}">Run</button>
              <button class="btn" data-action="goto-publish" data-id="${escapeHtml(w.id)}">Publish</button>
              <button class="btn danger" data-action="remove" data-id="${escapeHtml(w.id)}">Remove</button>
            </div>
          </article>`,
        )
        .join("")
    : `<div class="card"><p class="sub" style="margin:0">No workflows yet. Record a flow, import JSON, or install one from the Marketplace tab.</p></div>`;
  return shell(
    "Installed",
    `${cards}
    <div class="card">
      <h2 style="margin:0 0 8px;font-size:1rem">Import workflow JSON</h2>
      <input type="file" accept="application/json,.json,.workflow.json" id="import-file" />
    </div>`,
  );
}

function renderMarket(): string {
  if (!settings.marketToken) {
    return shell(
      "Marketplace",
      `<div class="card"><p class="sub" style="margin:0">Listings are private. Add the site access token in Settings, then refresh this tab.</p>
      <div class="btn-row"><button class="btn primary wide" data-view="settings">Open settings</button></div></div>`,
    );
  }
  const cards = listings.length
    ? listings
        .map((l) => {
          const warning = l.contains_code
            ? `<div class="banner info">This package contains executable automation code. Review it before running.</div>`
            : "";
          const update = l.updateAvailable ? `<div class="banner info">Update available (installed v${escapeHtml(l.installedVersion || "")} → v${escapeHtml(l.version)}).</div>` : "";
          const unsupported = l.type === "pomerado";
          return `<article class="card">
            <h2 style="margin:0 0 4px;font-size:1rem">${escapeHtml(l.name)}</h2>
            <div class="mono muted">${escapeHtml(l.type)} · v${escapeHtml(l.version)} · ${escapeHtml(l.created_at)}</div>
            <p class="sub">${escapeHtml(l.description || "")}</p>
            <p class="muted">Inputs: ${escapeHtml(l.inputs.length ? l.inputs.map((i) => i.label || i.name).join(", ") : "none declared")}</p>
            <p class="mono muted">sha256 ${escapeHtml(l.sha256)}</p>
            ${warning}${update}
            ${unsupported ? `<p class="muted">Pomerado packages are not supported in this extension (workflow-use only).</p>` : ""}
            <div class="btn-row">
              <button class="btn primary wide" data-action="install" data-id="${escapeHtml(l.id)}" ${unsupported ? "disabled" : ""}>${l.updateAvailable ? "Update" : l.installed ? "Reinstall" : "Install"}</button>
            </div>
          </article>`;
        })
        .join("")
    : `<div class="card"><p class="sub" style="margin:0">No listings (or the marketplace is empty). Publish workflow-use packages from the website, then refresh.</p></div>`;
  const publishedFor = (w: InstalledWorkflow) =>
    listings.find((l) => l.id === w.listingId || l.id === listingIdForName(w.name) || l.name === w.name);
  const options = workflows
    .map((w) => {
      const published = publishedFor(w);
      const label = `${w.name}${published ? ` (published v${published.version} — Update)` : ""}`;
      return `<option value="${escapeHtml(w.id)}" ${w.id === publishId ? "selected" : ""}>${escapeHtml(label)}</option>`;
    })
    .join("");
  const selected = workflows.find((w) => w.id === publishId);
  const published = selected ? publishedFor(selected) : undefined;
  const selectedInputs = selected
    ? JSON.stringify(
        deriveInputs(selected.definition).map((item) => ({
          name: item.name,
          label: prettyLabel(item.name, item.label),
          type: item.type || "string",
          required: item.required !== false,
        })),
        null,
        2,
      )
    : "";
  const form = `<form id="ext-publish-form" class="card">
      <h2 style="margin:0 0 8px;font-size:1rem">Publish</h2>
      <p class="muted">Select an installed or imported workflow, edit the details if you want, then publish. Learned HITL aliases are included. Republishing bumps the version.</p>
      ${
        workflows.length
          ? `<label class="field"><span>Your automations</span>
        <select name="workflowId" id="ext-market-source">
          <option value="">Choose a workflow…</option>
          ${options}
        </select>
      </label>`
          : `<p class="muted">Import or install a workflow first, then you can publish it here.</p>`
      }
      ${
        selected
          ? `<label class="field"><span>Name</span><input name="name" required value="${escapeHtml(selected.name)}" /></label>
      <label class="field"><span>Description</span><textarea name="description" rows="3">${escapeHtml(selected.description || "")}</textarea></label>
      <label class="field"><span>Declared inputs (editable)</span><textarea name="inputs" rows="3">${escapeHtml(selectedInputs)}</textarea></label>
      <div class="btn-row"><button class="btn primary wide" type="submit">${published ? "Update" : "Publish"}</button></div>`
          : ""
      }
    </form>`;
  return shell("Marketplace", `${form}<div class="btn-row" style="margin-bottom:10px"><button class="btn wide" data-action="refresh-market">Refresh listings</button></div>${cards}`);
}

function renderRun(): string {
  const wf = workflows.find((w) => w.id === selectedId);
  if (!wf) {
    return shell("Run", `<div class="card"><p class="sub" style="margin:0">Choose an installed workflow first.</p></div>`);
  }
  const inputs = deriveInputs(wf.definition);
  const missing = missingRequired(inputs, inputValues);
  const fields = inputs
    .map((input: WorkflowInput) => {
      const label = prettyLabel(input.name, input.label);
      const req = input.required === false ? "" : " *";
      const value = inputValues[input.name] ?? (input.default != null ? String(input.default) : "");
      if (!(input.name in inputValues) && input.default != null) inputValues[input.name] = String(input.default);
      return `<label class="field"><span>${escapeHtml(label)}${req}</span>
        <input name="${escapeHtml(input.name)}" value="${escapeHtml(value)}" ${input.required === false ? "" : "required"} /></label>`;
    })
    .join("");
  const traces = (run && run.workflowId === wf.id ? run.traces : [])
    .map((t) => {
      const extra = t.output != null ? `<div class="muted">${escapeHtml(typeof t.output === "string" ? t.output.slice(0, 280) : JSON.stringify(t.output))}</div>` : "";
      const attempt = t.attempt ? `<div class="mono muted">${escapeHtml(t.attempt.type)}: ${escapeHtml(t.attempt.value)}</div>` : "";
      return `<div class="step ${t.status}"><div class="idx">${t.index + 1}</div><div>
        <strong>${escapeHtml(t.type)}</strong> <span class="muted">${escapeHtml(t.description || t.status)}</span>
        ${t.message ? `<div class="muted">${escapeHtml(t.message)}</div>` : ""}
        ${attempt}${extra}
      </div></div>`;
    })
    .join("");
  const hitl = run?.status === "hitl" && run.hitl
    ? `<div class="card">
        <h2 style="margin:0 0 6px;font-size:1rem">Choose a match</h2>
        <p class="sub">Query “${escapeHtml(run.hitl.query)}” was ${escapeHtml(run.hitl.reason.replaceAll("_", " "))}. Pick one to continue. The selector is saved on this workflow for next time.</p>
        ${run.hitl.candidates
          .map(
            (c: Candidate) => `<button type="button" class="candidate ${hitlChoice === c.id ? "selected" : ""}" data-action="hitl-select" data-id="${escapeHtml(c.id)}">
              <strong>${escapeHtml(c.text)}</strong>
              <div class="mono muted">${escapeHtml(c.role || "")} · score ${c.score ?? "?"} · ${escapeHtml(c.selector || "")}</div>
            </button>`,
          )
          .join("")}
        <div class="btn-row">
          <button class="btn danger" data-action="hitl-cancel">Cancel</button>
          <button class="btn primary" data-action="hitl-confirm" ${hitlChoice ? "" : "disabled"}>Use this match</button>
        </div>
      </div>`
    : "";
  const running = run?.status === "running" || run?.status === "hitl";
  return shell(
    escapeHtml(wf.name),
    `<form id="run-form" class="card" data-id="${escapeHtml(wf.id)}" novalidate>
      <p class="sub" style="margin-top:0">Plays back in your current Chrome tab. Required fields must be filled.</p>
      ${fields || `<p class="muted">This workflow has no inputs.</p>`}
      ${missing.length && busy === false ? "" : ""}
      <div class="btn-row">
        <button class="btn primary wide" type="submit" ${running ? "disabled" : ""}>Run in current tab</button>
      </div>
    </form>
    ${hitl}
    <div class="card">
      <h2 style="margin:0 0 6px;font-size:1rem">Trace</h2>
      <div class="trace">${traces || `<p class="muted">No steps yet.</p>`}</div>
    </div>`,
  );
}


function stepSummary(step: Record<string, unknown>): string {
  const typ = String(step.type || "");
  if (typ === "navigation") return `Go to ${String(step.url || "")}`;
  if (typ === "input") return `Type ${String(step.value || "")} in ${String(step.cssSelector || step.target_text || "field")}`;
  if (typ === "select_change" || typ === "select") return `Choose ${String(step.value || step.target_text || "")}`;
  if (typ === "key_press" || typ === "key") return `Press ${String(step.value || "Enter")}`;
  if (typ === "click") return `Click ${String(step.target_text || step.cssSelector || "element")}`;
  if (typ === "extract") return `Extract ${String(step.extractionGoal || "result")}`;
  return typ || "step";
}

function renderRecord(): string {
  const tokenReady = Boolean(settings.marketToken && (settings.backendUrl || DEFAULT_BACKEND_URL));
  const setup = tokenReady
    ? ""
    : `<div class="banner info">Add the site access token (and backend URL if needed) in Settings so a recording can be cleaned and saved to your library.</div>`;
  const controls = `<div class="card">
      <p class="sub" style="margin-top:0">Do the task yourself in this Chrome window. Start recording, click and type as you normally would, then stop. Redundant clicks are removed; you can restore or delete steps before saving.</p>
      <label class="field"><span>Goal (optional, one line)</span>
        <input id="record-goal" value="${escapeHtml(recordGoal)}" placeholder="Find the fixtures for a football team" ${recordingActive || recordBusy ? "disabled" : ""} />
      </label>
      <label class="field"><span>Name</span>
        <input id="record-name" value="${escapeHtml(recordName)}" placeholder="football_fixtures" ${recordingActive || recordBusy ? "disabled" : ""} />
      </label>
      <div class="btn-row">
        ${
          recordingActive
            ? `<button class="btn danger wide" data-action="record-stop" ${recordBusy ? "disabled" : ""}>Stop recording${recordingCount ? ` (${recordingCount})` : ""}</button>`
            : `<button class="btn primary wide" data-action="record-start" ${recordBusy ? "disabled" : ""}>Start recording</button>`
        }
      </div>
      ${recordingActive ? `<p class="muted" data-record-status="recording">Recording in your current tab… ${recordingCount} event${recordingCount === 1 ? "" : "s"} captured.</p>` : ""}
      ${recordBusy && !recordingActive ? `<p class="muted" data-record-status="${recordEvents.length ? "cleaning" : "starting"}">${recordEvents.length ? "Cleaning the recording…" : "Starting…"}</p>` : ""}
    </div>`;
  const usage = recordUsage
    ? `<p class="muted">${escapeHtml(recordUsage.label || (recordUsage.prune_used ? `LLM prune used ${recordUsage.tokens_in || 0} in / ${recordUsage.tokens_out || 0} out tokens.` : recordUsage.extract_used ? `Parameter extract used ${recordUsage.tokens_in || 0} in / ${recordUsage.tokens_out || 0} out tokens (deterministic prune only).` : recordUsage.used ? `LLM used ${recordUsage.tokens_in || 0} in / ${recordUsage.tokens_out || 0} out tokens.` : "Deterministic prune only (no LLM tokens)."))}</p>`
    : "";
  const stats = recordStats
    ? `<p class="muted">${escapeHtml(String(recordStats.steps_before || 0))} recorded steps → ${escapeHtml(String(recordStats.steps_after || reviewItems.filter((i) => i.kept).length))} kept.</p>`
    : "";
  const steps = reviewItems.length
    ? `<div class="card">
        <h2 style="margin:0 0 8px;font-size:1rem">Review steps</h2>
        ${usage}${stats}
        <div class="trace">${reviewItems
          .map((item, i) => {
            const dropped = !item.kept;
            const reason = item.reason ? `<div class="muted">${escapeHtml(item.reason)}${item.stage ? ` · ${escapeHtml(item.stage)}` : ""}</div>` : "";
            return `<div class="step rec-step ${dropped ? "dropped" : "kept"}" data-rec-id="${escapeHtml(item.id)}">
              <span class="idx">${i + 1}</span>
              <div>
                <div>${escapeHtml(stepSummary(item.step))}</div>
                ${reason}
                <button type="button" class="btn" data-action="${dropped ? "record-restore" : "record-drop"}" data-rec-id="${escapeHtml(item.id)}">${dropped ? "Restore" : "Delete"}</button>
              </div>
            </div>`;
          })
          .join("")}</div>
      </div>`
    : "";
  const params = reviewItems.length
    ? `<div class="card">
        <h2 style="margin:0 0 8px;font-size:1rem">Inputs</h2>
        <p class="muted">These become required labelled fields when you run the workflow. Confirm or edit them.</p>
        ${
          recordParams.length
            ? recordParams
                .map(
                  (p, i) => `<div class="param-row" data-param-index="${i}">
                    <label class="field"><span>Name</span><input data-param="${i}" data-field="name" value="${escapeHtml(p.name)}" /></label>
                    <label class="field"><span>Label</span><input data-param="${i}" data-field="label" value="${escapeHtml(p.label || p.name)}" /></label>
                    <label class="field"><span>Example</span><input data-param="${i}" data-field="example" value="${escapeHtml(p.example || "")}" /></label>
                    <label class="field check"><span><input type="checkbox" data-param="${i}" data-field="required" ${p.required === false ? "" : "checked"} /> Required</span></label>
                  </div>`,
                )
                .join("")
            : `<p class="muted">No parameters detected. You can still save a fixed workflow, or add a name like team in the goal and record again.</p>`
        }
        <div class="btn-row"><button class="btn" data-action="record-add-param">Add input</button></div>
      </div>`
    : "";
  const save = reviewItems.some((i) => i.kept)
    ? `<div class="btn-row"><button class="btn primary wide" data-action="record-save" ${recordBusy || !tokenReady ? "disabled" : ""}>Save workflow</button></div>`
    : "";
  return shell("Record", `${setup}${controls}${steps}${params}${save}`);
}

function renderSettings(): string {
  return shell(
    "Settings",
    `<form id="settings-form" class="card">
      <label class="field"><span>Marketplace origin</span>
        <input name="marketOrigin" value="${escapeHtml(settings.marketOrigin || DEFAULT_MARKET_ORIGIN)}" placeholder="${DEFAULT_MARKET_ORIGIN}" />
      </label>
      <label class="field"><span>Backend URL</span>
        <input name="backendUrl" value="${escapeHtml(settings.backendUrl || DEFAULT_BACKEND_URL)}" placeholder="${DEFAULT_BACKEND_URL}" />
      </label>
      <label class="field"><span>Site access token</span>
        <div class="secret-wrap">
          <input name="marketToken" type="password" autocomplete="off" value="${escapeHtml(settings.marketToken)}" />
          <button type="button" class="btn peek" data-action="peek">Show</button>
        </div>
      </label>
      <p class="muted">Same shared token as the phone website. Stored only in this browser profile (chrome.storage.local). Needed to list and install marketplace packages, and to clean and save a recording to the library.</p>
      <div class="btn-row"><button class="btn primary wide" type="submit">Save</button></div>
    </form>`,
  );
}

function render(): void {
  const usedFlash = flash;
  if (view === "market") app().innerHTML = renderMarket();
  else if (view === "record") app().innerHTML = renderRecord();
  else if (view === "run") app().innerHTML = renderRun();
  else if (view === "settings") app().innerHTML = renderSettings();
  else app().innerHTML = renderInstalled();
  if (usedFlash) flash = null;
}

async function loadMarket(): Promise<void> {
  const res = await send<{ listings: MarketItem[] }>("bp-list-market");
  if (!res.ok) throw new Error(res.error || "Could not list marketplace");
  listings = res.listings;
}

document.addEventListener("click", (event) => {
  const target = event.target as HTMLElement | null;
  if (!target) return;
  const tab = target.closest("[data-view]") as HTMLElement | null;
  if (tab?.dataset.view) {
    view = tab.dataset.view as View;
    if (view === "market") {
      void loadMarket()
        .catch((err) => {
          flash = { kind: "err", text: err instanceof Error ? err.message : String(err) };
        })
        .finally(() => render());
      return;
    }
    render();
    return;
  }
  const action = (target.closest("[data-action]") as HTMLElement | null)?.dataset.action;
  if (action === "peek") {
    const input = document.querySelector<HTMLInputElement>('input[name="marketToken"]');
    if (!input) return;
    input.type = input.type === "password" ? "text" : "password";
    (target.closest("button") as HTMLElement).textContent = input.type === "password" ? "Show" : "Hide";
    return;
  }
  if (action === "open-run") {
    selectedId = (target.closest("[data-id]") as HTMLElement).dataset.id || "";
    view = "run";
    void send<{ values: Record<string, string> }>("bp-last-inputs", { id: selectedId }).then((res) => {
      inputValues = res.values || {};
      render();
    });
    return;
  }
  if (action === "goto-publish") {
    publishId = (target.closest("[data-id]") as HTMLElement).dataset.id || "";
    view = "market";
    if (settings.marketToken) {
      void loadMarket().catch((err) => {
        flash = { kind: "err", text: err instanceof Error ? err.message : String(err) };
      }).finally(() => render());
    } else {
      render();
    }
    return;
  }
  if (action === "remove") {
    const id = (target.closest("[data-id]") as HTMLElement).dataset.id || "";
    if (!id || !confirm("Remove this installed workflow from the extension?")) return;
    void send("bp-remove", { id }).then(async () => {
      if (selectedId === id) selectedId = "";
      await refreshState();
      render();
    });
    return;
  }
  if (action === "refresh-market") {
    void loadMarket()
      .then(() => {
        flash = { kind: "ok", text: "Listings refreshed." };
      })
      .catch((err) => {
        flash = { kind: "err", text: err instanceof Error ? err.message : String(err) };
      })
      .finally(() => render());
    return;
  }
  if (action === "install") {
    const id = (target.closest("[data-id]") as HTMLElement).dataset.id || "";
    const listing = listings.find((l) => l.id === id);
    if (listing?.contains_code && !confirm("This package contains executable automation code. Review it before running. Install anyway?")) return;
    const btn = target.closest("button") as HTMLButtonElement | null;
    if (btn) btn.disabled = true;
    void send("bp-install", { id })
      .then(async (res) => {
        if (!res.ok) throw new Error(res.error || "Install failed");
        flash = { kind: "ok", text: listing?.updateAvailable ? "Updated." : "Installed. Open it from the Installed tab to run." };
        await refreshState();
        await loadMarket().catch(() => undefined);
        render();
      })
      .catch((err) => {
        flash = { kind: "err", text: err instanceof Error ? err.message : String(err) };
        render();
      });
    return;
  }

  if (action === "record-start") {
    recordGoal = (document.getElementById("record-goal") as HTMLInputElement | null)?.value || recordGoal;
    recordName = (document.getElementById("record-name") as HTMLInputElement | null)?.value || recordName;
    reviewItems = [];
    recordParams = [];
    recordEvents = [];
    recordUsage = null;
    recordStats = null;
    recordBusy = true;
    render();
    void send("bp-record-start")
      .then(async (res) => {
        if (!res.ok) throw new Error(res.error || "Could not start recording");
        recordingActive = true;
        recordingCount = Number((res as { count?: number }).count || 0);
        recordBusy = false;
        flash = { kind: "info", text: "Recording. Use this Chrome window to do the task, then stop." };
        render();
      })
      .catch((err) => {
        recordBusy = false;
        recordingActive = false;
        flash = { kind: "err", text: err instanceof Error ? err.message : String(err) };
        render();
      });
    return;
  }
  if (action === "record-stop") {
    recordBusy = true;
    render();
    void send<{ events: Record<string, unknown>[] }>("bp-record-stop")
      .then(async (res) => {
        if (!res.ok) throw new Error(res.error || "Could not stop recording");
        recordingActive = false;
        recordEvents = res.events || [];
        recordingCount = recordEvents.length;
        const clean = await send<{ result: {
          review: ReviewItem[];
          parameters: ParamDraft[];
          usage?: { tokens_in?: number; tokens_out?: number; used?: boolean; extract_used?: boolean; prune_used?: boolean; label?: string };
          stats?: Record<string, unknown>;
          name?: string;
        } }>("bp-record-clean", {
          events: recordEvents,
          goal: recordGoal,
          name: recordName,
        });
        if (!clean.ok) throw new Error(clean.error || "Could not clean recording");
        const result = clean.result;
        reviewItems = result.review || [];
        recordParams = result.parameters || [];
        recordUsage = result.usage || null;
        recordStats = result.stats || null;
        if (!recordName && result.name) recordName = result.name;
        recordBusy = false;
        flash = { kind: "ok", text: "Recording cleaned. Review the steps, then save." };
        render();
      })
      .catch((err) => {
        recordBusy = false;
        recordingActive = false;
        flash = { kind: "err", text: err instanceof Error ? err.message : String(err) };
        render();
      });
    return;
  }
  if (action === "record-drop" || action === "record-restore") {
    const id = (target.closest("[data-rec-id]") as HTMLElement | null)?.dataset.recId || "";
    reviewItems = reviewItems.map((item) => item.id === id ? { ...item, kept: action === "record-restore" } : item);
    render();
    return;
  }
  if (action === "record-add-param") {
    recordParams = [...recordParams, { name: "value", label: "Value", example: "", required: true }];
    render();
    return;
  }
  if (action === "record-save") {
    recordName = (document.getElementById("record-name") as HTMLInputElement | null)?.value || recordName || "Recorded workflow";
    recordGoal = (document.getElementById("record-goal") as HTMLInputElement | null)?.value || recordGoal;
    const kept = reviewItems.filter((i) => i.kept);
    if (!kept.length) {
      flash = { kind: "err", text: "Keep at least one step before saving." };
      render();
      return;
    }
    recordBusy = true;
    render();
    void send<{ workflow?: InstalledWorkflow }>("bp-record-save", {
      payload: {
        name: recordName || "Recorded workflow",
        goal: recordGoal,
        review: reviewItems,
        parameters: recordParams,
      },
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(res.error || "Save failed");
        recordBusy = false;
        flash = { kind: "ok", text: "Saved to your library. It also appears in the Publish dropdown." };
        await refreshState();
        if (res.workflow?.id) {
          selectedId = res.workflow.id;
          view = "run";
          inputValues = {};
          for (const p of recordParams) {
            if (p.example) inputValues[p.name] = String(p.example);
          }
        }
        render();
      })
      .catch((err) => {
        recordBusy = false;
        flash = { kind: "err", text: err instanceof Error ? err.message : String(err) };
        render();
      });
    return;
  }
  if (action === "hitl-select") {
    hitlChoice = (target.closest("[data-id]") as HTMLElement).dataset.id || null;
    render();
    return;
  }
  if (action === "hitl-confirm" || action === "hitl-cancel") {
    const candidate = action === "hitl-confirm" ? run?.hitl?.candidates.find((c) => c.id === hitlChoice) || null : null;
    hitlChoice = null;
    void send("bp-hitl-choice", { candidate }).then(() => undefined);
    return;
  }
});

document.addEventListener("change", (event) => {
  const el = event.target as HTMLElement | null;
  if (el && el.id === "ext-market-source") {
    publishId = (el as HTMLSelectElement).value;
    render();
    return;
  }
  const input = event.target as HTMLInputElement | null;
  if (input?.id === "import-file" && input.files?.[0]) {
    const file = input.files[0];
    void file.text().then(async (json) => {
      const res = await send("bp-import", { json });
      if (!res.ok) throw new Error(res.error || "Import failed");
      flash = { kind: "ok", text: "Imported. You can run it from this list." };
      await refreshState();
      render();
    }).catch((err) => {
      flash = { kind: "err", text: err instanceof Error ? err.message : String(err) };
      render();
    });
  }
});

document.addEventListener("input", (event) => {
  const el = event.target as HTMLInputElement | null;
  if (!el) return;
  if (el.id === "record-goal") recordGoal = el.value;
  if (el.id === "record-name") recordName = el.value;
  if (el.dataset.param != null && el.dataset.field) {
    const i = Number(el.dataset.param);
    const field = el.dataset.field as keyof ParamDraft;
    const next = { ...recordParams[i] };
    if (field === "required") next.required = el.type === "checkbox" ? el.checked : el.value === "true";
    else (next as Record<string, unknown>)[field] = el.value;
    recordParams = recordParams.map((p, idx) => (idx === i ? next : p));
    return;
  }
  if (view !== "run") return;
  if (el.name) inputValues[el.name] = el.value;
});

document.addEventListener("change", (event) => {
  const el = event.target as HTMLInputElement | null;
  if (!el || el.dataset.param == null || el.dataset.field !== "required") return;
  const i = Number(el.dataset.param);
  recordParams = recordParams.map((p, idx) => (idx === i ? { ...p, required: el.checked } : p));
}, true);


document.addEventListener("submit", (event) => {
  const form = event.target as HTMLFormElement | null;
  if (!form) return;
  event.preventDefault();
  if (form.id === "ext-publish-form") {
    const data = new FormData(form);
    const id = String(data.get("workflowId") || publishId || "");
    if (!id) return;
    const inputsRaw = String(data.get("inputs") || "").trim();
    let inputs: unknown;
    if (inputsRaw) {
      try {
        inputs = JSON.parse(inputsRaw);
        if (!Array.isArray(inputs)) throw new Error("inputs must be a JSON array");
      } catch (err) {
        flash = { kind: "err", text: err instanceof Error ? err.message : "inputs must be a JSON array" };
        render();
        return;
      }
    }
    const button = form.querySelector("button[type=submit]") as HTMLButtonElement | null;
    if (button) button.disabled = true;
    void send("bp-publish", {
      id,
      name: String(data.get("name") || ""),
      description: String(data.get("description") || ""),
      inputs,
    }).then(async (res) => {
      if (!res.ok) throw new Error(res.error || "Publish failed");
      const listing = (res as { listing?: { name?: string; version?: string } }).listing;
      flash = { kind: "ok", text: `Published ${listing?.name || "workflow"} v${listing?.version || ""}.` };
      await refreshState();
      await loadMarket().catch(() => undefined);
      render();
    }).catch((err) => {
      flash = { kind: "err", text: err instanceof Error ? err.message : String(err) };
      render();
    });
    return;
  }
  if (form.id === "settings-form") {
    const data = new FormData(form);
    void send("bp-save-settings", {
      settings: {
        marketOrigin: String(data.get("marketOrigin") || DEFAULT_MARKET_ORIGIN),
        marketToken: String(data.get("marketToken") || ""),
        backendUrl: String(data.get("backendUrl") || DEFAULT_BACKEND_URL),
      },
    }).then(async (res) => {
      if (!res.ok) throw new Error(res.error || "Save failed");
      await refreshState();
      flash = { kind: "ok", text: "Saved on this computer only." };
      render();
    });
    return;
  }
  if (form.id === "run-form") {
    const wf = workflows.find((w) => w.id === selectedId);
    if (!wf) return;
    const inputs = deriveInputs(wf.definition);
    const data = new FormData(form);
    for (const [k, v] of data.entries()) inputValues[k] = String(v);
    const missing = missingRequired(inputs, inputValues);
    if (missing.length) {
      flash = { kind: "err", text: `Fill required fields: ${missing.map((n) => prettyLabel(n)).join(", ")}` };
      render();
      return;
    }
    busy = true;
    render();
    void send("bp-run-workflow", { id: wf.id, values: inputValues })
      .then(async () => {
        await refreshState();
        busy = false;
        render();
      })
      .catch((err) => {
        busy = false;
        flash = { kind: "err", text: err instanceof Error ? err.message : String(err) };
        render();
      });
  }
});

chrome.runtime.onMessage.addListener((message) => {
  if (message && message.kind === "bp-run") {
    run = message.run as RunState;
    if (view === "run") render();
  }
  if (message && message.kind === "bp-record-state") {
    const rec = (message as { recording?: { active: boolean; count: number } }).recording;
    if (rec) {
      recordingActive = rec.active;
      recordingCount = rec.count;
      if (view === "record" && !recordBusy) render();
    }
  }
});

void refreshState()
  .then(() => render())
  .catch((err) => {
    flash = { kind: "err", text: err instanceof Error ? err.message : String(err) };
    render();
  });

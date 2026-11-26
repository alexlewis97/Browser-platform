import { he } from "./copy/he.ts";
import { deriveInputs, missingRequired, prettyLabel } from "./lib/inputs.ts";
import { listingIdForName } from "./lib/market.ts";
import type { Candidate, ExtensionSettings, InstalledWorkflow, MarketListing, RunState, WorkflowInput } from "./lib/types.ts";
import { DEFAULT_BACKEND_URL, DEFAULT_MARKET_ORIGIN } from "./lib/types.ts";

type View = "installed" | "record" | "market" | "run" | "chat" | "settings";
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
let chatDraft = "";
let chatBusy = false;
let chatLog: { role: "user" | "assistant"; content: string }[] = [];
let chatStatus = "";


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
  if (!res.ok) throw new Error(res.error || he.flash.stateFailed);
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
    <header class="top"><div><p class="eyebrow">${he.brand}</p><h1>${title}</h1></div></header>
    <nav class="tabs">
      ${tab("installed", he.tabs.installed)}
      ${tab("market", he.tabs.market)}
      ${tab("record", he.tabs.record)}
      ${tab("run", he.tabs.run)}
      ${tab("chat", he.tabs.chat)}
      ${tab("settings", he.tabs.settings)}
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
            <div class="mono muted">v${escapeHtml(w.version)} · ${escapeHtml(w.type)} · ${w.source === "market" ? he.installed.sourceMarket : w.source === "record" ? he.installed.sourceRecord : he.installed.sourceImport}</div>
            <p class="sub">${escapeHtml(w.description || "")}</p>
            <div class="btn-row">
              <button class="btn primary" data-action="open-run" data-id="${escapeHtml(w.id)}">${he.installed.run}</button>
              <button class="btn" data-action="goto-publish" data-id="${escapeHtml(w.id)}">${he.installed.publish}</button>
              <button class="btn danger" data-action="remove" data-id="${escapeHtml(w.id)}">${he.installed.remove}</button>
            </div>
          </article>`,
        )
        .join("")
    : `<div class="card"><p class="sub" style="margin:0">${he.installed.empty}</p></div>`;
  return shell(
    he.installed.title,
    `${cards}
    <div class="card">
      <h2 style="margin:0 0 8px;font-size:1rem">${he.installed.importTitle}</h2>
      <input type="file" accept="application/json,.json,.workflow.json" id="import-file" />
    </div>`,
  );
}

function renderMarket(): string {
  if (!settings.marketToken) {
    return shell(
      he.market.title,
      `<div class="card"><p class="sub" style="margin:0">${he.market.tokenRequired}</p>
      <div class="btn-row"><button class="btn primary wide" data-view="settings">${he.market.openSettings}</button></div></div>`,
    );
  }
  const cards = listings.length
    ? listings
        .map((l) => {
          const warning = l.contains_code
            ? `<div class="banner info">${he.market.codeWarning}</div>`
            : "";
          const update = l.updateAvailable
            ? `<div class="banner info">${escapeHtml(he.market.updateAvailable(l.installedVersion || "", l.version))}</div>`
            : "";
          const unsupported = l.type === "pomerado";
          return `<article class="card">
            <h2 style="margin:0 0 4px;font-size:1rem">${escapeHtml(l.name)}</h2>
            <div class="mono muted">${escapeHtml(l.type)} · v${escapeHtml(l.version)} · ${escapeHtml(l.created_at)}</div>
            <p class="sub">${escapeHtml(l.description || "")}</p>
            <p class="muted">${he.market.inputs} ${escapeHtml(l.inputs.length ? l.inputs.map((i) => i.label || i.name).join(", ") : he.market.inputsNone)}</p>
            <p class="mono muted">${he.common.sha256} ${escapeHtml(l.sha256)}</p>
            ${warning}${update}
            ${unsupported ? `<p class="muted">${he.market.pomeradoUnsupported}</p>` : ""}
            <div class="btn-row">
              <button class="btn primary wide" data-action="install" data-id="${escapeHtml(l.id)}" ${unsupported ? "disabled" : ""}>${l.updateAvailable ? he.market.update : l.installed ? he.market.reinstall : he.market.install}</button>
            </div>
          </article>`;
        })
        .join("")
    : `<div class="card"><p class="sub" style="margin:0">${he.market.empty}</p></div>`;
  const publishedFor = (w: InstalledWorkflow) =>
    listings.find((l) => l.id === w.listingId || l.id === listingIdForName(w.name) || l.name === w.name);
  const options = workflows
    .map((w) => {
      const published = publishedFor(w);
      const label = published ? he.market.publishedOption(w.name, published.version) : w.name;
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
      <h2 style="margin:0 0 8px;font-size:1rem">${he.market.publishTitle}</h2>
      <p class="muted">${he.market.publishHint}</p>
      ${
        workflows.length
          ? `<label class="field"><span>${he.market.yourAutomations}</span>
        <select name="workflowId" id="ext-market-source">
          <option value="">${he.market.chooseWorkflow}</option>
          ${options}
        </select>
      </label>`
          : `<p class="muted">${he.market.importFirst}</p>`
      }
      ${
        selected
          ? `<label class="field"><span>${he.market.name}</span><input name="name" required value="${escapeHtml(selected.name)}" /></label>
      <label class="field"><span>${he.market.description}</span><textarea name="description" rows="3">${escapeHtml(selected.description || "")}</textarea></label>
      <label class="field"><span>${he.market.declaredInputs}</span><textarea name="inputs" rows="3">${escapeHtml(selectedInputs)}</textarea></label>
      <div class="btn-row"><button class="btn primary wide" type="submit">${published ? he.market.update : he.installed.publish}</button></div>`
          : ""
      }
    </form>`;
  return shell(he.market.title, `${form}<div class="btn-row" style="margin-bottom:10px"><button class="btn wide" data-action="refresh-market">${he.market.refresh}</button></div>${cards}`);
}

function renderRun(): string {
  const wf = workflows.find((w) => w.id === selectedId);
  if (!wf) {
    return shell(he.tabs.run, `<div class="card"><p class="sub" style="margin:0">${he.run.chooseFirst}</p></div>`);
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
        <h2 style="margin:0 0 6px;font-size:1rem">${he.run.hitlTitle}</h2>
        <p class="sub">${escapeHtml(he.run.hitlBody(run.hitl.query, run.hitl.reason))}</p>
        ${run.hitl.candidates
          .map(
            (c: Candidate) => `<button type="button" class="candidate ${hitlChoice === c.id ? "selected" : ""}" data-action="hitl-select" data-id="${escapeHtml(c.id)}">
              <strong>${escapeHtml(c.text)}</strong>
              <div class="mono muted">${escapeHtml(c.role || "")} · ${he.run.hitlScore(c.score ?? "?")} · ${escapeHtml(c.selector || "")}</div>
            </button>`,
          )
          .join("")}
        <div class="btn-row">
          <button class="btn danger" data-action="hitl-cancel">${he.run.hitlCancel}</button>
          <button class="btn primary" data-action="hitl-confirm" ${hitlChoice ? "" : "disabled"}>${he.run.hitlConfirm}</button>
        </div>
      </div>`
    : "";
  const running = run?.status === "running" || run?.status === "hitl";
  return shell(
    escapeHtml(wf.name),
    `<form id="run-form" class="card" data-id="${escapeHtml(wf.id)}" novalidate>
      <p class="sub" style="margin-top:0">${he.run.playbackHint}</p>
      ${fields || `<p class="muted">${he.run.noInputs}</p>`}
      ${missing.length && busy === false ? "" : ""}
      <div class="btn-row">
        <button class="btn primary wide" type="submit" ${running ? "disabled" : ""}>${he.run.runInTab}</button>
      </div>
    </form>
    ${hitl}
    <div class="card">
      <h2 style="margin:0 0 6px;font-size:1rem">${he.run.trace}</h2>
      <div class="trace">${traces || `<p class="muted">${he.run.noSteps}</p>`}</div>
    </div>`,
  );
}


function stepSummary(step: Record<string, unknown>): string {
  const typ = String(step.type || "");
  if (typ === "navigation") return he.record.stepNav(String(step.url || ""));
  if (typ === "input") return he.record.stepType(String(step.value || ""), String(step.cssSelector || step.target_text || "field"));
  if (typ === "select_change" || typ === "select") return he.record.stepChoose(String(step.value || step.target_text || ""));
  if (typ === "key_press" || typ === "key") return he.record.stepKey(String(step.value || "Enter"));
  if (typ === "click") return he.record.stepClick(String(step.target_text || step.cssSelector || "element"));
  if (typ === "extract") return he.record.stepExtract(String(step.extractionGoal || "result"));
  return typ || he.record.stepGeneric;
}

function renderRecord(): string {
  const tokenReady = Boolean(settings.marketToken && (settings.backendUrl || DEFAULT_BACKEND_URL));
  const setup = tokenReady
    ? ""
    : `<div class="banner info">${he.record.tokenBanner}</div>`;
  const controls = `<div class="card">
      <p class="sub" style="margin-top:0">${he.record.intro}</p>
      <label class="field"><span>${he.record.goal}</span>
        <input id="record-goal" value="${escapeHtml(recordGoal)}" placeholder="${he.record.goalPh}" ${recordingActive || recordBusy ? "disabled" : ""} />
      </label>
      <label class="field"><span>${he.record.name}</span>
        <input id="record-name" value="${escapeHtml(recordName)}" placeholder="${he.record.namePh}" ${recordingActive || recordBusy ? "disabled" : ""} />
      </label>
      <div class="btn-row">
        ${
          recordingActive
            ? `<button class="btn danger wide" data-action="record-stop" ${recordBusy ? "disabled" : ""}>${recordingCount ? he.record.stopCount(recordingCount) : he.record.stop}</button>`
            : `<button class="btn primary wide" data-action="record-start" ${recordBusy ? "disabled" : ""}>${he.record.start}</button>`
        }
      </div>
      ${recordingActive ? `<p class="muted" data-record-status="recording">${he.record.recording(recordingCount)}</p>` : ""}
      ${recordBusy && !recordingActive ? `<p class="muted" data-record-status="${recordEvents.length ? "cleaning" : "starting"}">${recordEvents.length ? he.record.cleaning : he.record.starting}</p>` : ""}
    </div>`;
  const usage = recordUsage
    ? `<p class="muted">${escapeHtml(
        recordUsage.label ||
          (recordUsage.prune_used
            ? he.record.usagePrune(recordUsage.tokens_in || 0, recordUsage.tokens_out || 0)
            : recordUsage.extract_used
              ? he.record.usageExtract(recordUsage.tokens_in || 0, recordUsage.tokens_out || 0)
              : recordUsage.used
                ? he.record.usageLlm(recordUsage.tokens_in || 0, recordUsage.tokens_out || 0)
                : he.record.usageDeterministic),
      )}</p>`
    : "";
  const stats = recordStats
    ? `<p class="muted">${escapeHtml(he.record.stats(String(recordStats.steps_before || 0), String(recordStats.steps_after || reviewItems.filter((i) => i.kept).length)))}</p>`
    : "";
  const steps = reviewItems.length
    ? `<div class="card">
        <h2 style="margin:0 0 8px;font-size:1rem">${he.record.reviewTitle}</h2>
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
                <button type="button" class="btn" data-action="${dropped ? "record-restore" : "record-drop"}" data-rec-id="${escapeHtml(item.id)}">${dropped ? he.record.restore : he.record.delete}</button>
              </div>
            </div>`;
          })
          .join("")}</div>
      </div>`
    : "";
  const params = reviewItems.length
    ? `<div class="card">
        <h2 style="margin:0 0 8px;font-size:1rem">${he.record.inputsTitle}</h2>
        <p class="muted">${he.record.inputsHint}</p>
        ${
          recordParams.length
            ? recordParams
                .map(
                  (p, i) => `<div class="param-row" data-param-index="${i}">
                    <label class="field"><span>${he.record.paramName}</span><input data-param="${i}" data-field="name" value="${escapeHtml(p.name)}" /></label>
                    <label class="field"><span>${he.record.paramLabel}</span><input data-param="${i}" data-field="label" value="${escapeHtml(p.label || p.name)}" /></label>
                    <label class="field"><span>${he.record.paramExample}</span><input data-param="${i}" data-field="example" value="${escapeHtml(p.example || "")}" /></label>
                    <label class="field check"><span><input type="checkbox" data-param="${i}" data-field="required" ${p.required === false ? "" : "checked"} /> ${he.record.required}</span></label>
                  </div>`,
                )
                .join("")
            : `<p class="muted">${he.record.noParams}</p>`
        }
        <div class="btn-row"><button class="btn" data-action="record-add-param">${he.record.addInput}</button></div>
      </div>`
    : "";
  const save = reviewItems.some((i) => i.kept)
    ? `<div class="btn-row"><button class="btn primary wide" data-action="record-save" ${recordBusy || !tokenReady ? "disabled" : ""}>${he.record.save}</button></div>`
    : "";
  return shell(he.record.title, `${setup}${controls}${steps}${params}${save}`);
}

function renderSettings(): string {
  return shell(
    he.settings.title,
    `<form id="settings-form" class="card">
      <label class="field"><span>${he.settings.marketOrigin}</span>
        <input name="marketOrigin" value="${escapeHtml(settings.marketOrigin || DEFAULT_MARKET_ORIGIN)}" placeholder="${DEFAULT_MARKET_ORIGIN}" />
      </label>
      <label class="field"><span>${he.settings.backendUrl}</span>
        <input name="backendUrl" value="${escapeHtml(settings.backendUrl || DEFAULT_BACKEND_URL)}" placeholder="${DEFAULT_BACKEND_URL}" />
      </label>
      <label class="field"><span>${he.settings.accessToken}</span>
        <div class="secret-wrap">
          <input name="marketToken" type="password" autocomplete="off" value="${escapeHtml(settings.marketToken)}" />
          <button type="button" class="btn peek" data-action="peek">${he.settings.show}</button>
        </div>
      </label>
      <p class="muted">${he.settings.hint}</p>
      <div class="btn-row"><button class="btn primary wide" type="submit">${he.settings.save}</button></div>
    </form>`,
  );
}


async function backendChat(payload: Record<string, unknown>): Promise<Record<string, unknown>> {
  const base = (settings.backendUrl || DEFAULT_BACKEND_URL).replace(/\/+$/, "");
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (settings.marketToken) headers["X-Browser-Platform-Token"] = settings.marketToken;
  const res = await fetch(`${base}/api/chat`, { method: "POST", body: JSON.stringify(payload), headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(String((data as { detail?: string }).detail || res.statusText || he.chat.failed));
  return data as Record<string, unknown>;
}

function renderChat(): string {
  const roleLabel = (role: "user" | "assistant") => (role === "user" ? he.chat.roleUser : he.chat.roleAssistant);
  const history = chatLog.map((msg) => `<div class="card"><strong>${escapeHtml(roleLabel(msg.role))}</strong><p style="white-space:pre-wrap">${escapeHtml(msg.content)}</p></div>`).join("")
    || `<p class="muted">${he.chat.empty}</p>`;
  return shell(
    he.chat.title,
    `${history}
    ${chatStatus ? `<p class="muted">${escapeHtml(chatStatus)}</p>` : ""}
    <form id="ext-chat-form" class="card">
      <label class="field"><span>${he.chat.message}</span>
        <textarea name="message" required style="min-height:80px">${escapeHtml(chatDraft)}</textarea>
      </label>
      <button class="btn primary wide" type="submit" ${chatBusy ? "disabled" : ""}>${he.chat.send}</button>
    </form>`,
  );
}

function render(): void {
  const usedFlash = flash;
  if (view === "market") app().innerHTML = renderMarket();
  else if (view === "record") app().innerHTML = renderRecord();
  else if (view === "run") app().innerHTML = renderRun();
  else if (view === "chat") app().innerHTML = renderChat();
  else if (view === "settings") app().innerHTML = renderSettings();
  else app().innerHTML = renderInstalled();
  if (usedFlash) flash = null;
}

async function loadMarket(): Promise<void> {
  const res = await send<{ listings: MarketItem[] }>("bp-list-market");
  if (!res.ok) throw new Error(res.error || he.flash.marketFailed);
  listings = res.listings;
}

document.addEventListener("submit", (event) => {
  const form = event.target as HTMLFormElement | null;
  if (!form || form.id !== "ext-chat-form") return;
  event.preventDefault();
  const message = String((form.elements.namedItem("message") as HTMLTextAreaElement | null)?.value || "").trim();
  if (!message || chatBusy) return;
  chatDraft = "";
  chatLog.push({ role: "user", content: message });
  chatBusy = true;
  chatStatus = he.chat.planning;
  render();
  void (async () => {
    try {
      const history = chatLog.slice(0, -1);
      let job = await backendChat({ message, history, target: "extension", stream: false });
      if (job.status === "needs_input" || job.status === "needs_confirmation") {
        if (job.status === "needs_confirmation") {
          if (!confirm(String(job.confirm_reason || he.chat.confirmRun))) {
            chatLog.push({ role: "assistant", content: String(job.confirm_reason || he.chat.cancelled) });
            return;
          }
          job = await backendChat({ message, job_id: job.id, confirm: true, target: "extension", stream: false });
        } else {
          chatLog.push({ role: "assistant", content: String(job.question || job.answer || he.chat.missingInputs) });
          return;
        }
      }
      if (job.status === "awaiting_results" || Array.isArray(job.calls)) {
        const calls = (job.calls || []) as Array<{ id: string; definition?: unknown; arguments?: Record<string, string>; workflow_id?: string; workflow_name?: string; tool?: string; action_ending?: boolean }>;
        chatStatus = he.chat.runningTools(calls.length);
        render();
        const results = [];
        for (const call of calls) {
          const res = await send<{ ok: boolean; run?: Record<string, unknown>; error?: string }>("bp-run-definition", {
            definition: call.definition,
            values: call.arguments || {},
            id: call.workflow_id,
            name: call.workflow_name,
          });
          results.push({
            id: call.id,
            tool: call.tool,
            workflow_id: call.workflow_id,
            workflow_name: call.workflow_name,
            inputs: call.arguments,
            status: res.ok ? "succeeded" : "failed",
            error: res.error,
            outputs: (res.run && (res.run as { outputs?: Record<string, unknown> }).outputs) || {},
            action_ending: call.action_ending,
          });
        }
        job = await backendChat({ message, job_id: job.id, results, stream: false });
      }
      chatLog.push({ role: "assistant", content: String(job.answer || job.question || job.error || he.chat.done) });
    } catch (err) {
      flash = { kind: "err", text: err instanceof Error ? err.message : String(err) };
    } finally {
      chatBusy = false;
      chatStatus = "";
      render();
    }
  })();
});

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
    (target.closest("button") as HTMLElement).textContent = input.type === "password" ? he.settings.show : he.settings.hide;
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
    if (!id || !confirm(he.flash.removeConfirm)) return;
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
        flash = { kind: "ok", text: he.flash.listingsRefreshed };
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
    if (listing?.contains_code && !confirm(he.flash.codeInstallConfirm)) return;
    const btn = target.closest("button") as HTMLButtonElement | null;
    if (btn) btn.disabled = true;
    void send("bp-install", { id })
      .then(async (res) => {
        if (!res.ok) throw new Error(res.error || he.flash.installFailed);
        flash = { kind: "ok", text: listing?.updateAvailable ? he.flash.updated : he.flash.installedRun };
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
        if (!res.ok) throw new Error(res.error || he.flash.recordStartFailed);
        recordingActive = true;
        recordingCount = Number((res as { count?: number }).count || 0);
        recordBusy = false;
        flash = { kind: "info", text: he.flash.recordingInfo };
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
        if (!res.ok) throw new Error(res.error || he.flash.recordStopFailed);
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
        if (!clean.ok) throw new Error(clean.error || he.flash.recordCleanFailed);
        const result = clean.result;
        reviewItems = result.review || [];
        recordParams = result.parameters || [];
        recordUsage = result.usage || null;
        recordStats = result.stats || null;
        if (!recordName && result.name) recordName = result.name;
        recordBusy = false;
        flash = { kind: "ok", text: he.flash.recordCleaned };
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
    recordParams = [...recordParams, { name: he.record.defaultParamName, label: he.record.defaultParamLabel, example: "", required: true }];
    render();
    return;
  }
  if (action === "record-save") {
    recordName = (document.getElementById("record-name") as HTMLInputElement | null)?.value || recordName || he.record.defaultName;
    recordGoal = (document.getElementById("record-goal") as HTMLInputElement | null)?.value || recordGoal;
    const kept = reviewItems.filter((i) => i.kept);
    if (!kept.length) {
      flash = { kind: "err", text: he.flash.keepOneStep };
      render();
      return;
    }
    recordBusy = true;
    render();
    void send<{ workflow?: InstalledWorkflow }>("bp-record-save", {
      payload: {
        name: recordName || he.record.defaultName,
        goal: recordGoal,
        review: reviewItems,
        parameters: recordParams,
      },
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(res.error || he.flash.saveFailed);
        recordBusy = false;
        flash = { kind: "ok", text: he.flash.savedLibrary };
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
      if (!res.ok) throw new Error(res.error || he.flash.importFailed);
      flash = { kind: "ok", text: he.flash.imported };
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
        if (!Array.isArray(inputs)) throw new Error(he.flash.inputsMustBeArray);
      } catch (err) {
        flash = { kind: "err", text: err instanceof Error ? err.message : he.flash.inputsMustBeArray };
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
      if (!res.ok) throw new Error(res.error || he.flash.publishFailed);
      const listing = (res as { listing?: { name?: string; version?: string } }).listing;
      flash = { kind: "ok", text: he.flash.published(listing?.name || "workflow", listing?.version || "") };
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
      if (!res.ok) throw new Error(res.error || he.flash.settingsSaveFailed);
      await refreshState();
      flash = { kind: "ok", text: he.flash.settingsSaved };
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
      flash = { kind: "err", text: he.run.fillRequired(missing.map((n) => prettyLabel(n)).join(", ")) };
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

import { findAlias, makeAlias, aliasToStrategy } from "./lib/aliases.ts";
import { interpolate, interpolateUrl } from "./lib/interpolate.ts";
import { deriveInputs } from "./lib/inputs.ts";
import { downloadPackage, listMarket, listingIdForName, publishWorkflow, sha256Hex } from "./lib/market.ts";
import { parseWorkflowJson, parseWorkflowPackage } from "./lib/package.ts";
import { queryForStep, scoreCandidates } from "./lib/score.ts";
import { buildSelectorPlan, fuzzyApplies, normalizeWikipediaUrl } from "./lib/selectors.ts";
import {
  getInstalled,
  listInstalled,
  loadLastInputs,
  loadSettings,
  putInstalled,
  removeInstalled,
  saveLastInputs,
  saveSettings,
} from "./lib/storage.ts";
import type {
  AliasMapping,
  Candidate,
  ExtensionSettings,
  InstalledWorkflow,
  RunState,
  SelectorAttempt,
  TraceStep,
  WorkflowDefinition,
  WorkflowStep,
} from "./lib/types.ts";
import { DEFAULT_BACKEND_URL, DEFAULT_FUZZY, DEFAULT_MARKET_ORIGIN } from "./lib/types.ts";

type ContentResult = { ok: true; data?: unknown } | { ok: false; error: string };

let currentRun: RunState | null = null;
let hitlWaiter: { resolve: (c: Candidate | null) => void; reject: (e: Error) => void } | null = null;
type RunRequestPayload = {
  definition: InstalledWorkflow["definition"];
  values?: Record<string, string>;
  id?: string;
  name?: string;
  tabId?: number;
};
type PendingRunConfirm = {
  payload: RunRequestPayload;
  resolve: (accepted: boolean) => void;
  windowId?: number;
  onRemoved?: (id: number) => void;
};
const pendingRunConfirms = new Map<string, PendingRunConfirm>();

function settleRunConfirm(requestId: string, accepted: boolean): boolean {
  const pending = pendingRunConfirms.get(requestId);
  if (!pending) return false;
  pendingRunConfirms.delete(requestId);
  if (pending.onRemoved) chrome.windows.onRemoved.removeListener(pending.onRemoved);
  if (pending.windowId != null) {
    void chrome.windows.remove(pending.windowId).catch(() => undefined);
  }
  pending.resolve(accepted);
  return true;
}

async function confirmRunRequest(payload: RunRequestPayload): Promise<boolean> {
  const requestId = crypto.randomUUID();
  const created = await chrome.windows.create({
    url: chrome.runtime.getURL(`confirm.html?id=${encodeURIComponent(requestId)}`),
    type: "popup",
    width: 420,
    height: 620,
    focused: true,
  });
  return await new Promise<boolean>((resolve) => {
    const windowId = created.id;
    const pending: PendingRunConfirm = { payload, resolve, windowId };
    const onRemoved = (id: number) => {
      if (windowId != null && id === windowId) settleRunConfirm(requestId, false);
    };
    pending.onRemoved = onRemoved;
    pendingRunConfirms.set(requestId, pending);
    chrome.windows.onRemoved.addListener(onRemoved);
  });
}
const injected = new Set<number>();

chrome.runtime.onInstalled.addListener(() => {
  void chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
});
void chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });

chrome.action.onClicked.addListener((tab) => {
  if (tab.id != null) void chrome.sidePanel.open({ tabId: tab.id });
});

function broadcast(message: unknown): void {
  chrome.runtime.sendMessage(message).catch(() => undefined);
}

function emitRun(): void {
  broadcast({ kind: "bp-run", run: currentRun });
}

async function sendAction(tabId: number, action: unknown): Promise<ContentResult> {
  await ensureContent(tabId);
  try {
    return (await chrome.tabs.sendMessage(tabId, { kind: "bp-action", action })) as ContentResult;
  } catch {
    injected.delete(tabId);
    await ensureContent(tabId);
    return (await chrome.tabs.sendMessage(tabId, { kind: "bp-action", action })) as ContentResult;
  }
}

async function ensureContent(tabId: number): Promise<void> {
  if (injected.has(tabId)) {
    try {
      const ping = (await chrome.tabs.sendMessage(tabId, { kind: "bp-action", action: { op: "ping" } })) as ContentResult;
      if (ping?.ok) return;
    } catch {
      injected.delete(tabId);
    }
  }
  await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
  injected.add(tabId);
}

function waitForTabLoad(tabId: number, timeoutMs = 30000): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("Timed out waiting for the page to load"));
    }, timeoutMs);
    const listener = (id: number, info: chrome.tabs.TabChangeInfo) => {
      if (id === tabId && info.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    };
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then((tab) => {
      if (tab.status === "complete") {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }).catch(() => undefined);
  });
}

async function navigate(tabId: number, url: string, waitS?: number): Promise<{ url: string }> {
  await chrome.tabs.update(tabId, { url });
  injected.delete(tabId);
  await waitForTabLoad(tabId);
  if (waitS) await new Promise((r) => setTimeout(r, Math.round(waitS * 1000)));
  for (let i = 0; i < 2; i++) {
    const info = await sendAction(tabId, { op: "pageInfo" });
    const data = (info.ok ? info.data : null) as { wikimediaError?: boolean; url?: string } | null;
    if (!data?.wikimediaError) return { url: data?.url || url };
    await new Promise((r) => setTimeout(r, 2000));
    await chrome.tabs.reload(tabId);
    injected.delete(tabId);
    await waitForTabLoad(tabId);
  }
  const info = await sendAction(tabId, { op: "pageInfo" });
  const data = (info.ok ? info.data : null) as { url?: string } | null;
  return { url: data?.url || url };
}


async function afterAction(tabId: number): Promise<void> {
  await new Promise((r) => setTimeout(r, 150));
  try {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status && tab.status !== "complete") {
      injected.delete(tabId);
      await waitForTabLoad(tabId);
    }
  } catch {
    injected.delete(tabId);
    await waitForTabLoad(tabId).catch(() => undefined);
  }
  await new Promise((r) => setTimeout(r, 400));
}

async function tryPlan(tabId: number, typ: string, plan: SelectorAttempt[], value?: string, selected?: string, key?: string): Promise<{ ok: boolean; error?: string; attempt?: SelectorAttempt }> {
  let last = "no selector strategies to try";
  for (const attempt of plan) {
    const res = await sendAction(tabId, { op: "act", typ, strategy: attempt.strategy, value, selected, key });
    if (res.ok) return { ok: true, attempt };
    last = res.error;
  }
  return { ok: false, error: last };
}

async function collect(tabId: number, step: WorkflowStep): Promise<Candidate[]> {
  const res = await sendAction(tabId, { op: "collect", typ: step.type, containerHint: step.container_hint, limit: 80 });
  if (!res.ok || !Array.isArray(res.data)) return [];
  return (res.data as Array<Record<string, unknown>>).map((raw, i) => ({
    id: String(raw.id || `c_${i}`),
    text: String(raw.text || ""),
    role: (raw.role as string) || null,
    selector: (raw.selector as string) || null,
    selector_type: "css",
    value: raw.value == null ? null : String(raw.value),
  })).filter((c) => c.text);
}

function mergeFuzzy(definition: WorkflowDefinition, step: WorkflowStep) {
  const base = { ...DEFAULT_FUZZY, ...(definition.fuzzy || {}), ...(step.fuzzy || {}) };
  return { enabled: base.enabled !== false, threshold: base.threshold ?? 90, margin: base.margin ?? 8, top_n: base.top_n ?? 5 };
}

function waitForHitl(timeoutS: number): Promise<Candidate | null> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      hitlWaiter = null;
      reject(new Error("HITL timed out"));
    }, Math.max(1, timeoutS) * 1000);
    hitlWaiter = {
      resolve: (c) => {
        clearTimeout(timer);
        hitlWaiter = null;
        resolve(c);
      },
      reject: (e) => {
        clearTimeout(timer);
        hitlWaiter = null;
        reject(e);
      },
    };
  });
}

async function persistAlias(workflowId: string, alias: AliasMapping): Promise<void> {
  const installed = await getInstalled(workflowId);
  if (!installed) return;
  const aliases = [...(installed.definition.aliases || [])].filter(
    (a) => !(a.step_index === alias.step_index && a.query_normalized === alias.query_normalized),
  );
  aliases.push(alias);
  const steps = installed.definition.steps.map((step, i) => {
    if (i !== alias.step_index) return step;
    if (alias.cssSelector) return { ...step, cachedSelector: { type: "css", value: alias.cssSelector, priority: 0 } };
    return step;
  });
  await putInstalled({
    ...installed,
    definition: { ...installed.definition, aliases, steps },
  });
}

async function runWorkflow(tabId: number, installed: InstalledWorkflow, variables: Record<string, string>): Promise<RunState> {
  const runId = crypto.randomUUID();
  const traces: TraceStep[] = [];
  currentRun = { runId, workflowId: installed.id, status: "running", currentIndex: 0, traces };
  emitRun();
  const definition = installed.definition;
  try {
    for (let index = 0; index < definition.steps.length; index++) {
      if (currentRun.status === "cancel") throw new Error("Cancelled");
      const rawStep = definition.steps[index];
      const step: WorkflowStep = {
        ...rawStep,
        url: interpolateUrl(rawStep.url, variables) as string | undefined,
        value: interpolate(rawStep.value ?? rawStep.default_value, variables) as string | undefined,
        selectedText: interpolate(rawStep.selectedText, variables) as string | undefined,
        key: interpolate(rawStep.key, variables) as string | undefined,
        target_text: interpolate(rawStep.target_text, variables) as string | undefined,
        fuzzy_query: interpolate(rawStep.fuzzy_query, variables) as string | undefined,
        cssSelector: interpolate(rawStep.cssSelector, variables) as string | undefined,
        xpath: interpolate(rawStep.xpath, variables) as string | undefined,
        selectorStrategies: (rawStep.selectorStrategies || []).map((s) => ({
          ...s,
          value: String(interpolate(s.value, variables) ?? s.value),
        })),
      };
      const trace: TraceStep = {
        index,
        type: step.type,
        description: step.description,
        status: "running",
        startedAt: Date.now(),
      };
      traces.push(trace);
      currentRun.currentIndex = index;
      currentRun.traces = [...traces];
      emitRun();

      if (step.type === "navigation") {
        const url = normalizeWikipediaUrl(step.url);
        if (!url) throw new Error("navigation step is missing url");
        const out = await navigate(tabId, url, step.wait_time);
        const asserted = await sendAction(tabId, { op: "assert", expected: step.expected_state });
        if (!asserted.ok) throw new Error(asserted.error);
        trace.status = "ok";
        trace.output = out;
        trace.endedAt = Date.now();
        emitRun();
        continue;
      }
      if (step.type === "go_back" || step.type === "go_forward") {
        if (step.type === "go_back") await chrome.tabs.goBack(tabId).catch(() => undefined);
        else await chrome.tabs.goForward(tabId).catch(() => undefined);
        injected.delete(tabId);
        await waitForTabLoad(tabId).catch(() => undefined);
        const info = await sendAction(tabId, { op: "pageInfo" });
        trace.status = "ok";
        trace.output = info.ok ? info.data : undefined;
        trace.endedAt = Date.now();
        emitRun();
        continue;
      }
      if (step.type === "scroll") {
        const res = await sendAction(tabId, { op: "scroll", scrollX: step.scrollX || 0, scrollY: step.scrollY || 0 });
        if (!res.ok) throw new Error(res.error);
        trace.status = "ok";
        trace.output = res.data;
        trace.endedAt = Date.now();
        emitRun();
        continue;
      }
      if (step.type === "extract" || step.type === "extract_page_content") {
        const res = await sendAction(tabId, { op: "extract", step });
        if (!res.ok) throw new Error(res.error);
        const asserted = await sendAction(tabId, { op: "assert", expected: step.expected_state });
        if (!asserted.ok) throw new Error(asserted.error);
        trace.status = "ok";
        trace.output = res.data;
        trace.endedAt = Date.now();
        emitRun();
        continue;
      }
      if (step.type === "agent") throw new Error("agent steps are not executed in the extension (workflow-use only)");
      if (!["click", "double_click", "right_click", "input", "select_change", "key_press"].includes(step.type)) {
        throw new Error(`unsupported step type: ${step.type}`);
      }

      const plan = buildSelectorPlan(step);
      const det = await tryPlan(tabId, step.type, plan, step.value, step.selectedText, step.key);
      if (!det.ok && step.type === "click") {
        const info = await sendAction(tabId, { op: "pageInfo" });
        const url = String((info.ok && info.data && (info.data as { url?: string }).url) || "");
        const onWikiArticle = /\/wiki\/[^/]+/.test(url) && !/Special:Search|Special:Search|title=Special/i.test(url);
        const openingResult = /first result|search result/i.test(step.description || step.target_text || "");
        if (onWikiArticle && openingResult) {
          trace.status = "ok";
          trace.message = "already on an article page";
          trace.output = { url };
          trace.endedAt = Date.now();
          emitRun();
          continue;
        }
      }
      if (det.ok) {
        if (["click", "double_click", "right_click", "key_press", "input", "select_change"].includes(step.type)) await afterAction(tabId);
        const asserted = await sendAction(tabId, { op: "assert", expected: step.expected_state });
        if (!asserted.ok) throw new Error(asserted.error);
        trace.status = "ok";
        trace.attempt = det.attempt ? { type: det.attempt.strategy.type, value: det.attempt.strategy.value, kind: det.attempt.kind } : undefined;
        trace.endedAt = Date.now();
        emitRun();
        continue;
      }

      const fuzzy = mergeFuzzy(definition, step);
      if (fuzzyApplies(step, fuzzy.enabled)) {
        const query = queryForStep(step, variables);
        const aliases = (await getInstalled(installed.id))?.definition.aliases || definition.aliases;
        const alias = query ? findAlias(aliases, index, query) : null;
        let applied: Candidate | null = null;
        let reason = "";
        if (alias) {
          const strat = aliasToStrategy(alias);
          const res = await sendAction(tabId, { op: "act", typ: step.type, strategy: { type: strat.type, value: strat.value, metadata: strat.metadata }, value: step.value, selected: step.selectedText, key: step.key });
          if (res.ok) {
            await afterAction(tabId);
            applied = { id: alias.chosen_id || "alias", text: alias.chosen_text, selector: alias.cssSelector, role: alias.role };
            reason = "alias";
          }
        }
        if (!applied && query) {
          const candidates = await collect(tabId, step);
          const decision = scoreCandidates(query, candidates, { threshold: fuzzy.threshold, margin: fuzzy.margin });
          if (decision.accepted && decision.winner) {
            const res = await sendAction(tabId, {
              op: "apply",
              typ: step.type,
              selector: decision.winner.selector,
              text: decision.winner.text,
              value: decision.winner.value ?? step.value ?? null,
            });
            if (!res.ok) throw new Error(res.error);
            await afterAction(tabId);
            applied = decision.winner;
            reason = "fuzzy";
          } else {
            const mode = definition.hitl?.mode || "always_on_uncertain";
            if (mode !== "llm_only") {
              trace.status = "hitl";
              trace.message = decision.reason;
              currentRun.status = "hitl";
              currentRun.hitl = {
                query,
                reason: decision.reason,
                candidates: decision.ranked.slice(0, fuzzy.top_n),
              };
              emitRun();
              const timeout = definition.hitl?.timeout_s ?? 300;
              const chosen = await waitForHitl(timeout);
              if (!chosen) throw new Error("HITL cancelled");
              const res = await sendAction(tabId, {
                op: "apply",
                typ: step.type,
                selector: chosen.selector,
                text: chosen.text,
                value: chosen.value ?? step.value ?? null,
              });
              if (!res.ok) throw new Error(res.error);
              await afterAction(tabId);
              applied = chosen;
              reason = "hitl";
              await persistAlias(installed.id, makeAlias(index, query, chosen));
            } else {
              throw new Error(`fuzzy match failed (${decision.reason}); LLM is not used in the extension`);
            }
          }
        }
        if (applied) {
          const asserted = await sendAction(tabId, { op: "assert", expected: step.expected_state });
          if (!asserted.ok) throw new Error(asserted.error);
          trace.status = "ok";
          trace.message = reason;
          trace.output = { text: applied.text, selector: applied.selector };
          trace.endedAt = Date.now();
          currentRun.status = "running";
          currentRun.hitl = undefined;
          emitRun();
          continue;
        }
      }
      throw new Error(`all selector strategies failed for ${step.type}: ${det.error}`);
    }
    currentRun.status = "ok";
    currentRun.hitl = undefined;
    emitRun();
    return currentRun;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    const last = traces[traces.length - 1];
    if (last && (last.status === "running" || last.status === "hitl")) {
      last.status = "fail";
      last.message = message;
      last.endedAt = Date.now();
    }
    currentRun.status = currentRun.status === "cancel" ? "cancel" : "fail";
    currentRun.error = message;
    currentRun.hitl = undefined;
    emitRun();
    return currentRun;
  }
}

async function targetTab(): Promise<number> {
  const tabs = await chrome.tabs.query({});
  const usable = tabs.filter((t) => {
    if (t.id == null || !t.url) return false;
    if (t.url.startsWith("chrome-extension://") || t.url.startsWith("chrome://") || t.url.startsWith("chrome-search://") || t.url.startsWith("devtools://")) return false;
    return /^https?:|^file:|^about:blank/.test(t.url);
  });
  usable.sort((a, b) => {
    if (a.active !== b.active) return a.active ? -1 : 1;
    return (b.lastAccessed || 0) - (a.lastAccessed || 0);
  });
  // Prefer a real http(s) page over about:blank when both exist.
  const http = usable.find((t) => /^https?:/.test(t.url || ""));
  const page = http || usable[0];
  if (!page?.id) throw new Error("Open a regular web page tab first, then press Run. The extension plays back in your current tab.");
  return page.id;
}

async function installFromListing(listingId: string): Promise<InstalledWorkflow> {
  const listings = await listMarket();
  const listing = listings.find((l) => l.id === listingId);
  if (!listing) throw new Error("Listing not found.");
  if (listing.type === "pomerado") {
    throw new Error("Pomerado tool packages are not supported in this extension (workflow-use only).");
  }
  const { bytes, sha256Header } = await downloadPackage(listingId);
  const digest = await sha256Hex(bytes);
  if (listing.sha256 && listing.sha256 !== digest) {
    throw new Error("Package sha256 does not match the listing. Install aborted.");
  }
  if (sha256Header && sha256Header !== digest) {
    throw new Error("Package sha256 does not match the download header. Install aborted.");
  }
  const { definition } = parseWorkflowPackage(new Uint8Array(bytes));
  const existing = (await listInstalled()).find((w) => w.listingId === listing.id);
  const item: InstalledWorkflow = {
    id: existing?.id || crypto.randomUUID(),
    listingId: listing.id,
    name: listing.name || definition.name,
    description: listing.description || definition.description || "",
    type: listing.type || "workflow-use",
    version: listing.version,
    sha256: digest,
    installedAt: new Date().toISOString(),
    source: "market",
    definition: { ...definition, aliases: existing?.definition.aliases || definition.aliases || [] },
  };
  await putInstalled(item);
  return item;
}

async function importDefinition(definition: WorkflowDefinition, sha256 = "", source: "file" | "market" | "record" = "file"): Promise<InstalledWorkflow> {
  const item: InstalledWorkflow = {
    id: crypto.randomUUID(),
    name: definition.name,
    description: definition.description || "",
    type: "workflow-use",
    version: String(definition.version || "1"),
    sha256,
    installedAt: new Date().toISOString(),
    source,
    definition,
  };
  await putInstalled(item);
  return item;
}


type RecordedPageEvent = {
  type: string;
  ts?: number;
  url?: string;
  url_after?: string;
  value?: string | null;
  key?: string;
  text?: string | null;
  changed?: boolean;
  element?: Record<string, unknown> | null;
};

type RecordingState = {
  active: boolean;
  events: RecordedPageEvent[];
  startedAt: number;
};

const recording: RecordingState = { active: false, events: [], startedAt: 0 };
const recorderInjected = new Set<number>();

function emitRecord(): void {
  broadcast({ kind: "bp-record-state", recording: { active: recording.active, count: recording.events.length } });
}

async function backendRoot(): Promise<{ url: string; token: string }> {
  const settings = await loadSettings();
  const url = (settings.backendUrl || DEFAULT_BACKEND_URL).replace(/\/+$/, "");
  const token = settings.marketToken || "";
  if (!url) throw new Error("Set the backend URL in Settings.");
  if (!token) throw new Error("Set the site access token in Settings so the recording can be saved to your library.");
  return { url, token };
}

async function backendFetch(path: string, init: RequestInit = {}): Promise<any> {
  const { url, token } = await backendRoot();
  const headers = new Headers(init.headers || {});
  headers.set("X-Browser-Platform-Token", token);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(`${url}${path}`, { ...init, headers });
  const text = await response.text();
  let data: any = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { error: text.slice(0, 300) };
  }
  if (!response.ok) {
    throw new Error(data.detail || data.error || data.message || `Backend HTTP ${response.status}`);
  }
  return data;
}

function rememberEvent(ev: RecordedPageEvent): void {
  if (!recording.active || !ev || !ev.type) return;
  recording.events.push(ev);
  emitRecord();
}

function withTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${label} timed out`)), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}

let recordingTabId: number | null = null;

async function ensureRecorder(tabId: number): Promise<void> {
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  const url = tab?.url || "";
  if (!/^https?:/i.test(url)) return;
  if (recorderInjected.has(tabId)) {
    try {
      // Non-destructive liveness check — never drain here.
      const ping = await withTimeout(chrome.tabs.sendMessage(tabId, { kind: "bp-record-ping" }), 2000, "recorder ping");
      if (ping) return;
    } catch {
      recorderInjected.delete(tabId);
    }
  }
  await withTimeout(
    chrome.scripting.executeScript({ target: { tabId }, files: ["recorder.js"] }),
    5000,
    "inject recorder",
  );
  recorderInjected.add(tabId);
}

async function injectRecordingTab(): Promise<void> {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (tab?.id == null || !tab.url || !/^https?:/i.test(tab.url)) {
    recordingTabId = null;
    return;
  }
  recordingTabId = tab.id;
  await ensureRecorder(tab.id).catch(() => undefined);
}

async function drainRecordingTab(): Promise<RecordedPageEvent[]> {
  const extra: RecordedPageEvent[] = [];
  const tabId = recordingTabId;
  if (tabId == null) return extra;
  try {
    await ensureRecorder(tabId);
    const res = (await chrome.tabs.sendMessage(tabId, { kind: "bp-record-drain" })) as { events?: RecordedPageEvent[] };
    for (const ev of res?.events || []) {
      if (ev && typeof ev === "object") {
        const copy = { ...ev };
        if (typeof copy.value === "string" && /(password|passwd|secret)/i.test(String((copy.element as { name?: string } | undefined)?.name || ""))) {
          copy.value = null;
        }
        extra.push(copy);
      }
    }
  } catch {
    /* tab may not have the recorder */
  }
  return extra;
}

function sortEvents(events: RecordedPageEvent[]): RecordedPageEvent[] {
  return [...events].sort((a, b) => (a.ts || 0) - (b.ts || 0));
}

async function startRecording(): Promise<void> {
  if (currentRun) {
    throw new Error("A workflow run is already in progress.");
  }
  recording.active = true;
  recording.events = [];
  recording.startedAt = Date.now();
  recorderInjected.clear();
  await injectRecordingTab();
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (tab?.id != null && tab.url && /^https?:/i.test(tab.url)) {
    rememberEvent({ type: "navigation", ts: Date.now(), url: tab.url, changed: true });
  }
  void chrome.action.setBadgeText({ text: "REC" });
  void chrome.action.setBadgeBackgroundColor({ color: "#c62828" });
  emitRecord();
}

async function stopRecording(): Promise<RecordedPageEvent[]> {
  const drained = await drainRecordingTab();
  for (const ev of drained) rememberEvent(ev);
  recording.active = false;
  recordingTabId = null;
  void chrome.action.setBadgeText({ text: "" });
  emitRecord();
  return sortEvents(recording.events);
}

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (!recording.active) return;
  if (info.status === "loading" && info.url && /^https?:/i.test(info.url)) {
    rememberEvent({ type: "navigation", ts: Date.now(), url: info.url, changed: true });
  }
  if (info.status === "complete" && tab.url && /^https?:/i.test(tab.url)) {
    void ensureRecorder(tabId).catch(() => undefined);
  }
});

chrome.tabs.onActivated.addListener((active) => {
  if (!recording.active) return;
  void chrome.tabs.get(active.tabId).then((tab) => {
    if (tab.url && /^https?:/i.test(tab.url)) {
      rememberEvent({ type: "tab_change", ts: Date.now(), url: tab.url, changed: true });
      void ensureRecorder(active.tabId).catch(() => undefined);
    }
  });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  recorderInjected.delete(tabId);
});


chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || typeof message !== "object" || !("kind" in message)) return;
  const kind = (message as { kind: string }).kind;
  if (kind === "bp-run") return; // broadcast
  void (async () => {
    try {
      if (kind === "bp-get-state") {
        sendResponse({ ok: true, settings: await loadSettings(), workflows: await listInstalled(), run: currentRun, recording: { active: recording.active, count: recording.events.length } });
        return;
      }
      if (kind === "bp-save-settings") {
        await saveSettings((message as { settings: ExtensionSettings }).settings);
        sendResponse({ ok: true, settings: await loadSettings() });
        return;
      }
      if (kind === "bp-list-market") {
        const listings = await listMarket();
        const installed = await listInstalled();
        const byListing = new Map(installed.filter((w) => w.listingId).map((w) => [w.listingId!, w]));
        sendResponse({
          ok: true,
          listings: listings.map((l) => {
            const local = byListing.get(l.id);
            return {
              ...l,
              installed: Boolean(local),
              installedVersion: local?.version,
              updateAvailable: Boolean(local && local.version !== l.version),
            };
          }),
        });
        return;
      }
      if (kind === "bp-publish") {
        const { id, name, description, type, inputs } = message as {
          id: string;
          name?: string;
          description?: string;
          type?: string;
          inputs?: { name: string; label?: string; type?: string; required?: boolean }[];
        };
        const installed = await getInstalled(id);
        if (!installed) throw new Error("Workflow is not installed.");
        const definition = installed.definition;
        const title = (name || definition.name).trim();
        const listing = await publishWorkflow({
          id: listingIdForName(title),
          name: title,
          description: description ?? definition.description ?? "",
          type: type || installed.type || "workflow-use",
          inputs: inputs || deriveInputs(definition).map((item) => ({
            name: item.name,
            label: item.label,
            type: item.type,
            required: item.required !== false,
            description: item.description,
          })),
          workflow: definition as unknown as Record<string, unknown>,
        });
        await putInstalled({
          ...installed,
          listingId: listing.id,
          name: listing.name,
          description: listing.description || installed.description,
          type: listing.type,
          version: listing.version,
          sha256: listing.sha256,
          source: "market",
        });
        sendResponse({ ok: true, listing, workflow: await getInstalled(installed.id) });
        return;
      }
      if (kind === "bp-install") {
        const item = await installFromListing((message as { id: string }).id);
        sendResponse({ ok: true, workflow: item });
        return;
      }
      if (kind === "bp-import") {
        const definition = parseWorkflowJson((message as { json: string }).json);
        const item = await importDefinition(definition);
        sendResponse({ ok: true, workflow: item });
        return;
      }
      if (kind === "bp-remove") {
        const id = (message as { id: string }).id;
        const item = await getInstalled(id);
        await removeInstalled(id);
        if (item?.source === "record") {
          try {
            await backendFetch(`/api/workflows/${encodeURIComponent(id)}`, { method: "DELETE" });
          } catch {
            // Local uninstall still succeeds if the library copy is already gone or offline.
          }
        }
        sendResponse({ ok: true });
        return;
      }
      if (kind === "bp-last-inputs") {
        sendResponse({ ok: true, values: await loadLastInputs((message as { id: string }).id) });
        return;
      }
      if (kind === "bp-confirm-details") {
        const requestId = String((message as { id?: string }).id || "");
        const pending = pendingRunConfirms.get(requestId);
        if (!pending) {
          sendResponse({ ok: false, error: "לא נמצאה בקשת הרצה, או שפג תוקפה." });
          return;
        }
        sendResponse({
          ok: true,
          name: pending.payload.name || pending.payload.definition?.name || "זרימה",
          description: pending.payload.definition?.description || "",
          values: pending.payload.values || {},
        });
        return;
      }
      if (kind === "bp-confirm-decision") {
        const requestId = String((message as { id?: string }).id || "");
        const accepted = Boolean((message as { accepted?: boolean }).accepted);
        if (!settleRunConfirm(requestId, accepted)) {
          sendResponse({ ok: false, error: "לא נמצאה בקשת הרצה, או שכבר טופלה." });
          return;
        }
        sendResponse({ ok: true, accepted });
        return;
      }
      if (kind === "bp-run-request") {
        const payload = message as RunRequestPayload;
        if (!payload.definition) throw new Error("No workflow definition to run.");
        if (pendingRunConfirms.size) throw new Error("A run confirmation is already open.");
        if (currentRun && (currentRun.status === "running" || currentRun.status === "hitl")) {
          throw new Error("A workflow is already running.");
        }
        const accepted = await confirmRunRequest(payload);
        if (!accepted) {
          sendResponse({ ok: false, cancelled: true, error: "ההרצה בוטלה." });
          return;
        }
        const installed: InstalledWorkflow = {
          id: payload.id || "adhoc",
          name: payload.name || payload.definition.name || "workflow",
          description: payload.definition.description || "",
          type: "workflow-use",
          version: String(payload.definition.version || "1"),
          sha256: "",
          installedAt: new Date().toISOString(),
          source: "file",
          definition: payload.definition,
        };
        const tabId = payload.tabId || await targetTab();
        const run = await runWorkflow(tabId, installed, payload.values || {});
        sendResponse({ ok: run.status === "ok", confirmed: true, run });
        return;
      }
      if (kind === "bp-run-definition") {
        const payload = message as { definition: InstalledWorkflow["definition"]; values?: Record<string, string>; id?: string; name?: string };
        if (!payload.definition) throw new Error("No workflow definition to run.");
        const installed: InstalledWorkflow = {
          id: payload.id || "adhoc",
          name: payload.name || payload.definition.name || "workflow",
          description: payload.definition.description || "",
          type: "workflow-use",
          version: String(payload.definition.version || "1"),
          sha256: "",
          installedAt: new Date().toISOString(),
          source: "file",
          definition: payload.definition,
        };
        const tabId = (message as { tabId?: number }).tabId || await targetTab();
        const run = await runWorkflow(tabId, installed, payload.values || {});
        sendResponse({ ok: run.status === "ok", run });
        return;
      }
      if (kind === "bp-run-workflow") {
        const { id, values } = message as { id: string; values: Record<string, string> };
        const installed = await getInstalled(id);
        if (!installed) throw new Error("Workflow is not installed.");
        await saveLastInputs(id, values);
        const tabId = (message as { tabId?: number }).tabId || await targetTab();
        const run = await runWorkflow(tabId, installed, values);
        sendResponse({ ok: run.status === "ok", run });
        return;
      }
      if (kind === "bp-hitl-choice") {
        const cand = (message as { candidate?: Candidate | null }).candidate ?? null;
        if (hitlWaiter) hitlWaiter.resolve(cand);
        sendResponse({ ok: true });
        return;
      }
      if (kind === "bp-cancel") {
        if (currentRun) currentRun.status = "cancel";
        if (hitlWaiter) hitlWaiter.resolve(null);
        sendResponse({ ok: true });
        return;
      }
      if (kind === "bp-web-ping") {
        sendResponse({ ok: true, extension: true, recording: { active: recording.active, count: recording.events.length } });
        return;
      }
      if (kind === "bp-record-event") {
        rememberEvent((message as { event: RecordedPageEvent }).event);
        sendResponse({ ok: true, count: recording.events.length });
        return;
      }
      if (kind === "bp-record-start") {
        await startRecording();
        sendResponse({ ok: true, count: recording.events.length });
        return;
      }
      if (kind === "bp-record-stop") {
        const events = await stopRecording();
        sendResponse({ ok: true, events, count: events.length });
        return;
      }
      if (kind === "bp-record-state") {
        sendResponse({ ok: true, recording: { active: recording.active, count: recording.events.length } });
        return;
      }
      if (kind === "bp-record-clean") {
        const { events, goal, name, variables } = message as {
          events: RecordedPageEvent[];
          goal?: string;
          name?: string;
          variables?: Record<string, string>;
        };
        const result = await backendFetch("/api/workflows/from-recording", {
          method: "POST",
          body: JSON.stringify({ events, goal: goal || "", name: name || "", variables: variables || {} }),
        });
        sendResponse({ ok: true, result });
        return;
      }
      if (kind === "bp-record-save") {
        const payload = (message as { payload: Record<string, unknown> }).payload;
        const stored = await backendFetch("/api/workflows/from-recording/save", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        const definition = stored.definition as WorkflowDefinition;
        const item = await importDefinition(definition, "", "record");
        // Prefer the backend id when present so library + extension share one workflow.
        if (stored.id && stored.id !== item.id) {
          await removeInstalled(item.id);
          const linked = { ...item, id: String(stored.id), source: "record" as const };
          await putInstalled(linked);
          sendResponse({ ok: true, workflow: linked, stored });
          return;
        }
        sendResponse({ ok: true, workflow: item, stored });
        return;
      }
      sendResponse({ ok: false, error: "unknown message" });
    } catch (err) {
      sendResponse({ ok: false, error: err instanceof Error ? err.message : String(err) });
    }
  })();
  return true;
});


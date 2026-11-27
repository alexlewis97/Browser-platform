import { currentWorkspace } from "./workspaces";
import { request } from "./api";
import { listingIdForName, publishWorkflowJson, type MarketListing } from "./market";
import { loadSettings } from "./storage";

export type ReviewItem = { id: string; kept: boolean; reason?: string; stage?: string; step: Record<string, unknown> };
export type ParamDraft = { name: string; label?: string; example?: string; required?: boolean; description?: string };
export type RecordSource = "remote" | "extension";
export type RecordPhase = "idle" | "live" | "cleaning" | "review" | "saved";

export type RecordState = {
  source: RecordSource;
  phase: RecordPhase;
  sessionId: string;
  goal: string;
  name: string;
  url: string;
  eventCount: number;
  viewport: { width: number; height: number };
  frame: string;
  error: string;
  review: ReviewItem[];
  parameters: ParamDraft[];
  usageLabel: string;
  statsLine: string;
  workflowId: string;
  listing: MarketListing | null;
  extensionReady: boolean | null;
  definition: Record<string, unknown> | null;
  inputValidation: import("./validation").InputValidation | null;
};

const DEFAULT_VIEWPORT = { width: 1280, height: 800 };

export const recordState: RecordState = {
  source: "remote",
  phase: "idle",
  sessionId: "",
  goal: "",
  name: "",
  url: "",
  eventCount: 0,
  viewport: { ...DEFAULT_VIEWPORT },
  frame: "",
  error: "",
  review: [],
  parameters: [],
  usageLabel: "",
  statsLine: "",
  workflowId: "",
  listing: null,
  extensionReady: null,
  definition: null,
  inputValidation: null,
};

let ws: WebSocket | null = null;
let pollTimer: number | undefined;
let onChange: (() => void) | null = null;

export function setRecordRenderer(fn: (() => void) | null): void {
  onChange = fn;
}

function emit(): void {
  onChange?.();
}

function backendWsUrl(path: string): string {
  const settings = loadSettings();
  const base = settings.backendUrl.replace(/\/+$/, "");
  const url = new URL(path, base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  if (settings.accessToken) url.searchParams.set("access_token", settings.accessToken);
  return url.toString();
}

function applyCleanResult(result: {
  review?: ReviewItem[];
  parameters?: ParamDraft[];
  usage?: { label?: string; tokens_in?: number; tokens_out?: number; used?: boolean; extract_used?: boolean; prune_used?: boolean };
  stats?: { steps_before?: number; steps_after?: number };
  creation?: { steps_recorded?: number; steps_kept?: number };
  name?: string;
  definition?: Record<string, unknown> | null;
  input_validation?: import("./validation").InputValidation | null;
}): void {
  recordState.review = result.review || [];
  recordState.parameters = result.parameters || [];
  recordState.usageLabel =
    result.usage?.label ||
    (result.usage?.prune_used
      ? `LLM prune used ${result.usage.tokens_in || 0} in / ${result.usage.tokens_out || 0} out tokens.`
      : result.usage?.extract_used
        ? `Parameter extract used ${result.usage.tokens_in || 0} in / ${result.usage.tokens_out || 0} out tokens (deterministic prune only).`
        : result.usage?.used
          ? `LLM used ${result.usage.tokens_in || 0} in / ${result.usage.tokens_out || 0} out tokens.`
          : "Deterministic prune only (no LLM tokens).");
  const before = result.stats?.steps_before ?? result.creation?.steps_recorded ?? recordState.review.length;
  const after = result.stats?.steps_after ?? result.creation?.steps_kept ?? recordState.review.filter((i) => i.kept).length;
  recordState.statsLine = `${before} recorded steps → ${after} kept.`;
  if (!recordState.name && result.name) recordState.name = result.name;
  if (result.definition) recordState.definition = result.definition;
  recordState.inputValidation = result.input_validation || null;
  recordState.phase = "review";
}

export function stepSummary(step: Record<string, unknown>): string {
  const typ = String(step.type || "");
  if (typ === "navigation") return `Go to ${String(step.url || "")}`;
  if (typ === "input") return `Type ${String(step.value || "")} in ${String(step.cssSelector || step.target_text || "field")}`;
  if (typ === "select_change" || typ === "select") return `Choose ${String(step.value || step.target_text || "")}`;
  if (typ === "key_press" || typ === "key") return `Press ${String(step.value || "Enter")}`;
  if (typ === "click") return `Click ${String(step.target_text || step.cssSelector || "element")}`;
  if (typ === "extract") return `Extract ${String(step.extractionGoal || "result")}`;
  return typ || "step";
}

function toRemotePoint(event: PointerEvent, img: HTMLImageElement): { x: number; y: number } | null {
  const rect = img.getBoundingClientRect();
  if (rect.width < 4 || rect.height < 4) return null;
  const x = ((event.clientX - rect.left) / rect.width) * recordState.viewport.width;
  const y = ((event.clientY - rect.top) / rect.height) * recordState.viewport.height;
  return { x, y };
}

async function sendRemoteInput(payload: Record<string, unknown>): Promise<void> {
  if (!recordState.sessionId) return;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(payload));
    return;
  }
  const session = await request<{ session: { url?: string; event_count?: number; viewport?: { width: number; height: number } } }>(
    `/api/record/sessions/${recordState.sessionId}/input`,
    { method: "POST", body: JSON.stringify(payload) },
  );
  if (session.session?.url) recordState.url = session.session.url;
  if (session.session?.event_count != null) recordState.eventCount = session.session.event_count;
  if (session.session?.viewport) recordState.viewport = session.session.viewport;
}

function applyFrame(message: { data?: string; url?: string; event_count?: number; viewport?: { width: number; height: number } }): void {
  if (message.data) recordState.frame = `data:image/jpeg;base64,${message.data}`;
  if (message.url) recordState.url = message.url;
  if (message.event_count != null) recordState.eventCount = message.event_count;
  if (message.viewport) recordState.viewport = message.viewport;
  const img = document.getElementById("record-screen") as HTMLImageElement | null;
  const urlBar = document.getElementById("record-url") as HTMLInputElement | null;
  const count = document.getElementById("record-event-count");
  const placeholder = document.getElementById("record-screen-placeholder");
  if (img && recordState.frame) {
    img.src = recordState.frame;
    if (placeholder) placeholder.remove();
  }
  if (urlBar && message.url && document.activeElement !== urlBar) urlBar.value = message.url;
  if (count) count.textContent = `${recordState.eventCount} event${recordState.eventCount === 1 ? "" : "s"}`;
}

function stopPolling(): void {
  if (pollTimer) {
    window.clearInterval(pollTimer);
    pollTimer = undefined;
  }
}

function startPolling(): void {
  stopPolling();
  pollTimer = window.setInterval(() => {
    if (!recordState.sessionId || recordState.phase !== "live") return;
    void request<{ data?: string; url?: string; event_count?: number; viewport?: { width: number; height: number } }>(
      `/api/record/sessions/${recordState.sessionId}/frame`,
    )
      .then(applyFrame)
      .catch(() => undefined);
  }, 400);
}

function connectSocket(): void {
  if (!recordState.sessionId) return;
  try {
    ws?.close();
  } catch {
    /* ignore */
  }
  const socket = new WebSocket(backendWsUrl(`/ws/record/${recordState.sessionId}`));
  ws = socket;
  socket.addEventListener("open", () => {
    stopPolling();
  });
  socket.addEventListener("message", (event) => {
    try {
      const message = JSON.parse(String(event.data)) as {
        type?: string;
        data?: string;
        url?: string;
        event_count?: number;
        viewport?: { width: number; height: number };
        session?: { url?: string; event_count?: number };
        error?: string;
      };
      if (message.type === "frame") applyFrame(message);
      if (message.type === "url" && message.url) {
        recordState.url = message.url;
        applyFrame(message);
      }
      if (message.type === "ack" && message.session) {
        recordState.url = message.session.url || recordState.url;
        recordState.eventCount = message.session.event_count ?? recordState.eventCount;
        applyFrame({ url: recordState.url, event_count: recordState.eventCount });
      }
      if (message.type === "error" && message.error) {
        recordState.error = message.error;
        emit();
      }
    } catch {
      /* ignore malformed frames */
    }
  });
  socket.addEventListener("close", () => {
    if (ws === socket) ws = null;
    if (recordState.phase === "live" && recordState.sessionId) startPolling();
  });
  socket.addEventListener("error", () => {
    if (recordState.phase === "live") startPolling();
  });
}

export async function probeExtension(): Promise<boolean> {
  return await new Promise((resolve) => {
    const timer = window.setTimeout(() => {
      window.removeEventListener("message", onMsg);
      resolve(false);
    }, 500);
    const onMsg = (event: MessageEvent) => {
      const data = event.data;
      if (!data || data.source !== "bp-ext") return;
      window.clearTimeout(timer);
      window.removeEventListener("message", onMsg);
      resolve(true);
    };
    window.addEventListener("message", onMsg);
    window.postMessage({ source: "bp-web", direction: "request", kind: "bp-web-ping", id: "ping", payload: {} }, "*");
  });
}

export function extensionRequest<T>(kind: string, payload: Record<string, unknown> = {}): Promise<T & { ok: boolean; error?: string }> {
  return new Promise((resolve, reject) => {
    const id = `r-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const timer = window.setTimeout(() => {
      window.removeEventListener("message", onMsg);
      reject(new Error("The extension did not respond. Is the Browser Platform extension loaded and allowed on this site?"));
    }, 60000);
    const onMsg = (event: MessageEvent) => {
      const data = event.data;
      if (!data || data.source !== "bp-ext" || data.direction !== "response" || data.id !== id) return;
      window.clearTimeout(timer);
      window.removeEventListener("message", onMsg);
      if (data.error) reject(new Error(String(data.error)));
      else resolve(data.result as T & { ok: boolean; error?: string });
    };
    window.addEventListener("message", onMsg);
    window.postMessage({ source: "bp-web", direction: "request", kind, id, payload }, "*");
  });
}

export async function startRemote(): Promise<void> {
  recordState.error = "";
  recordState.phase = "live";
  recordState.review = [];
  recordState.parameters = [];
  recordState.definition = null;
  recordState.inputValidation = null;
  recordState.listing = null;
  recordState.workflowId = "";
  recordState.frame = "";
  emit();
  const created = await request<{ session: { id: string; url?: string; viewport?: { width: number; height: number } } }>(
    "/api/record/sessions",
    { method: "POST", body: JSON.stringify({ start_url: recordState.url || "", goal: recordState.goal, name: recordState.name }) },
  );
  recordState.sessionId = created.session.id;
  recordState.url = created.session.url || recordState.url;
  if (created.session.viewport) recordState.viewport = created.session.viewport;
  connectSocket();
  startPolling();
  emit();
}

export async function stopRemote(): Promise<void> {
  if (!recordState.sessionId) throw new Error("No live remote session.");
  recordState.phase = "cleaning";
  emit();
  const sessionId = recordState.sessionId;
  try {
    ws?.close();
  } catch {
    /* ignore */
  }
  ws = null;
  stopPolling();
  const result = await request<Parameters<typeof applyCleanResult>[0]>(`/api/record/sessions/${sessionId}/stop`, {
    method: "POST",
    body: JSON.stringify({ goal: recordState.goal, name: recordState.name }),
  });
  recordState.sessionId = "";
  applyCleanResult(result);
  emit();
}

export async function cancelRemote(): Promise<void> {
  const sessionId = recordState.sessionId;
  recordState.sessionId = "";
  recordState.phase = "idle";
  try {
    ws?.close();
  } catch {
    /* ignore */
  }
  ws = null;
  stopPolling();
  if (sessionId) {
    await request(`/api/record/sessions/${sessionId}`, { method: "DELETE" }).catch(() => undefined);
  }
  emit();
}

export async function startExtension(): Promise<void> {
  recordState.error = "";
  const res = await extensionRequest("bp-record-start");
  if (!res.ok) throw new Error(res.error || "Could not start the extension recorder.");
  recordState.phase = "live";
  recordState.source = "extension";
  emit();
}

export async function stopExtension(): Promise<void> {
  recordState.phase = "cleaning";
  emit();
  const stopped = await extensionRequest<{ events: Record<string, unknown>[] }>("bp-record-stop");
  if (!stopped.ok) throw new Error(stopped.error || "Could not stop the extension recorder.");
  const clean = await request<Parameters<typeof applyCleanResult>[0]>("/api/workflows/from-recording", {
    method: "POST",
    body: JSON.stringify({ events: stopped.events || [], goal: recordState.goal, name: recordState.name }),
  });
  applyCleanResult(clean);
  emit();
}

export async function saveRecording(): Promise<string> {
  const kept = recordState.review.filter((item) => item.kept);
  if (!kept.length) throw new Error("Keep at least one step before saving.");
  const stored = await request<{ id: string }>("/api/workflows/from-recording/save", {
    method: "POST",
    body: JSON.stringify({
      name: recordState.name || "Recorded workflow",
      goal: recordState.goal,
      review: recordState.review,
      parameters: recordState.parameters,
      workspace_ids: currentWorkspace() ? [currentWorkspace()] : undefined,
    }),
  });
  recordState.workflowId = stored.id;
  recordState.phase = "saved";
  emit();
  return stored.id;
}

export async function publishRecording(): Promise<MarketListing> {
  if (!recordState.workflowId) throw new Error("Save the workflow first.");
  const stored = await request<{ id: string; definition: Record<string, unknown> }>(`/api/workflows/${recordState.workflowId}`);
  const definition = stored.definition || {};
  const inputs = recordState.parameters.map((item) => ({
    name: item.name,
    label: item.label || item.name,
    type: "string",
    required: item.required !== false,
  }));
  const published = await publishWorkflowJson({
    id: listingIdForName(recordState.name || String(definition.name || "recorded-workflow")),
    name: recordState.name || String(definition.name || "Recorded workflow"),
    description: recordState.goal || String(definition.description || "Recorded in the admin area and cleaned into a reusable workflow."),
    type: "workflow-use",
    inputs,
    workflow: definition,
  });
  recordState.listing = published.listing;
  emit();
  return published.listing;
}

export function teardownRecordSockets(): void {
  // Leave the remote session running so a brief navigation can reconnect.
  try {
    ws?.close();
  } catch {
    /* ignore */
  }
  ws = null;
  stopPolling();
}

export function resumeLiveSession(): void {
  if (recordState.phase === "live" && recordState.source === "remote" && recordState.sessionId) {
    connectSocket();
    startPolling();
  }
}

export async function locateAndClick(query: { selector?: string; text?: string; role?: string }): Promise<boolean> {
  if (!recordState.sessionId) return false;
  const found = await request<{ match: { x: number; y: number } | null }>(`/api/record/sessions/${recordState.sessionId}/locate`, {
    method: "POST",
    body: JSON.stringify(query),
  });
  if (!found.match) return false;
  await sendRemoteInput({ type: "click", x: found.match.x, y: found.match.y });
  return true;
}

export function bindRecordScreen(): void {
  (window as unknown as { __bpRecord?: unknown }).__bpRecord = {
    locateAndClick,
    submitUrlBar,
    submitTypeBox,
    recordState,
  };

  const img = document.getElementById("record-screen") as HTMLImageElement | null;
  if (!img) return;
  const sendPoint = (type: string, event: PointerEvent) => {
    const point = toRemotePoint(event, img);
    if (!point) return;
    event.preventDefault();
    void sendRemoteInput({ type, ...point, deltaY: 0 }).catch((err) => {
      recordState.error = err instanceof Error ? err.message : String(err);
      emit();
    });
  };
  img.addEventListener("pointerdown", (event) => {
    img.setPointerCapture(event.pointerId);
    sendPoint("click", event);
  });
  img.addEventListener(
    "wheel",
    (event) => {
      const point = toRemotePoint(event as unknown as PointerEvent, img);
      if (!point) return;
      event.preventDefault();
      void sendRemoteInput({ type: "scroll", ...point, deltaX: event.deltaX, deltaY: event.deltaY }).catch(() => undefined);
    },
    { passive: false },
  );
}

export async function submitUrlBar(url: string): Promise<void> {
  recordState.url = url;
  await sendRemoteInput({ type: "navigate", url });
}

export async function submitTypeBox(text: string, pressEnter = false): Promise<void> {
  if (text) await sendRemoteInput({ type: "type", text });
  if (pressEnter) await sendRemoteInput({ type: "key", key: "Enter" });
}

export async function refreshExtensionProbe(): Promise<void> {
  recordState.extensionReady = await probeExtension();
}

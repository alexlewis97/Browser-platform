import { request as apiRequest } from "./api";
/**
 * Human-in-the-loop confirmation client.
 *
 * Contract: README.md "Human-in-the-loop confirmation".
 * Start a run with wait:false, then poll GET /api/runs/{id} or
 * subscribe to WS /ws/runs?run_id={id}. When status is
 * needs_confirmation, `confirmation` (or GET /api/runs/{id}/confirmation)
 * holds the prompt. Resume or abandon with POST /api/runs/{id}/confirmation.
 */
import { loadSettings, type Settings } from "./storage";

export const HITL_MODES = ["always_on_uncertain", "llm_then_human", "llm_only"] as const;
export type HitlMode = (typeof HITL_MODES)[number];

export const HITL_TIMEOUT_POLICIES = ["fail", "fall_to_llm"] as const;
export type HitlTimeoutPolicy = (typeof HITL_TIMEOUT_POLICIES)[number];

export const HITL_ROUTES = {
  confirmation: (runId: string) => `/api/runs/${encodeURIComponent(runId)}/confirmation`,
  runsWs: (runId?: string, token?: string) => {
    const params = new URLSearchParams();
    if (runId) params.set("run_id", runId);
    if (token) params.set("access_token", token);
    const q = params.toString();
    return q ? `/ws/runs?${q}` : "/ws/runs";
  },
} as const;

export const HITL_ENDPOINTS_DOCUMENTED = true;

export type ConfirmationCandidate = {
  id: string;
  label: string;
  score?: number;
  description?: string;
  selector?: string;
  role?: string;
  screenshot?: string;
  metadata?: Record<string, unknown>;
};

export type ConfirmationPrompt = {
  id: string;
  runId: string;
  type?: string;
  stepIndex?: number;
  stepType?: string;
  stepDescription?: string;
  query?: string;
  reason?: string;
  message?: string;
  screenshot?: string;
  threshold?: number;
  margin?: number;
  winnerScore?: number;
  runnerUpScore?: number;
  timeoutS?: number;
  timeoutAt?: string;
  timeoutPolicy?: string;
  hitlMode?: string;
  candidates: ConfirmationCandidate[];
};

function trimSlash(url: string): string {
  return url.replace(/\/+$/, "");
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function str(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function num(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function candidateFrom(raw: unknown, index: number): ConfirmationCandidate | null {
  if (typeof raw === "string") return { id: raw, label: raw };
  const rec = asRecord(raw);
  if (!rec) return null;
  const id = str(rec.id) || str(rec.candidate_id) || `c${index}`;
  const label = str(rec.text) || str(rec.label) || str(rec.name) || str(rec.selector) || `Option ${index + 1}`;
  return {
    id,
    label,
    score: num(rec.score),
    description: str(rec.description),
    selector: str(rec.selector),
    role: str(rec.role),
    screenshot: str(rec.screenshot) || undefined,
    metadata: rec,
  };
}

/** Normalize a README confirmation payload (`top`, reason, scores, …). */
export function promptFrom(runId: string, raw: unknown): ConfirmationPrompt | null {
  const rec = asRecord(raw);
  if (!rec) return null;
  const type = str(rec.type) || str(rec.event) || str(rec.kind);
  const list = rec.top ?? rec.candidates ?? rec.top_candidates ?? rec.options;
  const looksLike =
    type === "needs_confirmation" ||
    rec.top != null ||
    rec.candidates != null ||
    rec.query != null ||
    rec.reason != null;
  if (!looksLike) return null;
  const candidates = Array.isArray(list)
    ? list.map((item, i) => candidateFrom(item, i)).filter((item): item is ConfirmationCandidate => item !== null)
    : [];
  const reason = str(rec.reason);
  const query = str(rec.query);
  const message =
    str(rec.message) ||
    (reason === "ambiguous_margin"
      ? "The top matches are too close. Pick the right one."
      : reason === "below_threshold"
        ? "No candidate cleared the match threshold."
        : query
          ? `Confirm the match for “${query}”.`
          : undefined);
  const firstShot = candidates.find((c) => c.screenshot)?.screenshot;
  return {
    id: str(rec.id) || str(rec.confirmation_id) || runId,
    runId: str(rec.run_id) || runId,
    type: type || "needs_confirmation",
    stepIndex: num(rec.step_index) ?? num(rec.stepIndex),
    stepType: str(rec.step_type),
    stepDescription: str(rec.step_description),
    query,
    reason,
    message,
    screenshot: str(rec.screenshot) || firstShot,
    threshold: num(rec.threshold),
    margin: num(rec.margin),
    winnerScore: num(rec.winner_score),
    runnerUpScore: num(rec.runner_up_score),
    timeoutS: num(rec.timeout_s),
    timeoutAt: str(rec.timeout_at),
    timeoutPolicy: str(rec.timeout_policy),
    hitlMode: str(rec.hitl_mode),
    candidates,
  };
}

export function extractConfirmation(run: {
  id: string;
  status?: string;
  confirmation?: unknown;
  [key: string]: unknown;
}): ConfirmationPrompt | null {
  const nested = promptFrom(run.id, run.confirmation);
  if (nested) return nested;
  if (run.status === "needs_confirmation") {
    return promptFrom(run.id, run);
  }
  return null;
}

export function formatScore(score?: number): string {
  if (score == null || Number.isNaN(score)) return "";
  const value = score <= 1 && score >= 0 ? score * 100 : score;
  return `${value.toFixed(1)}`;
}

export function resolveScreenshotSrc(screenshot: string | undefined, settings: Settings = loadSettings()): string | undefined {
  if (!screenshot) return undefined;
  if (screenshot.startsWith("data:") || screenshot.startsWith("blob:") || /^https?:\/\//i.test(screenshot)) {
    return screenshot;
  }
  if (!settings.backendUrl) return undefined;
  if (screenshot.startsWith("/")) return `${trimSlash(settings.backendUrl)}${screenshot}`;
  return `${trimSlash(settings.backendUrl)}/${screenshot}`;
}

function backendOrigin(settings: Settings = loadSettings()): string {
  if (!settings.backendUrl) throw new Error("Set a backend URL in Settings first.");
  return trimSlash(settings.backendUrl);
}

async function hitlRequest<T>(path: string, init: RequestInit = {}, _settings: Settings = loadSettings()): Promise<T> {
  return apiRequest<T>(path, init);
}

export async function getConfirmation(runId: string): Promise<ConfirmationPrompt | null> {
  const data = await hitlRequest<{ confirmation?: unknown; status?: string }>(HITL_ROUTES.confirmation(runId), {
    method: "GET",
  });
  return promptFrom(runId, data.confirmation) ?? (data.status === "needs_confirmation" ? promptFrom(runId, data) : null);
}

/** Resume a paused run by choosing one of the top-N candidates. */
export function confirmRun(runId: string, _prompt: ConfirmationPrompt, candidateId: string, text?: string): Promise<unknown> {
  const body: Record<string, string> = { action: "confirm" };
  if (candidateId) body.candidate_id = candidateId;
  if (text) body.text = text;
  return hitlRequest(HITL_ROUTES.confirmation(runId), { method: "POST", body: JSON.stringify(body) });
}

/** Cancel a paused run. */
export function cancelRun(runId: string, _prompt?: ConfirmationPrompt): Promise<unknown> {
  return hitlRequest(HITL_ROUTES.confirmation(runId), {
    method: "POST",
    body: JSON.stringify({ action: "cancel" }),
  });
}

/** Open WS /ws/runs for live events. Caller should also poll REST as a fallback. */
export function openRunSocket(
  runId: string,
  onEvent: (event: Record<string, unknown>) => void,
  settings: Settings = loadSettings(),
): WebSocket | null {
  if (!settings.backendUrl || typeof WebSocket === "undefined") return null;
  try {
    const base = new URL(backendOrigin(settings));
    base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${base.origin}${HITL_ROUTES.runsWs(runId, settings.accessToken)}`);
    ws.addEventListener("message", (ev) => {
      try {
        const data = JSON.parse(String(ev.data)) as Record<string, unknown>;
        onEvent(data);
      } catch {
        /* ignore non-JSON */
      }
    });
    return ws;
  } catch {
    return null;
  }
}

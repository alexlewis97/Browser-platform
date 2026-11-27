import { refreshDiscovery, isNetworkError } from "./discover";
import { isManualBackendOverride, loadSettings, type Settings } from "./storage";

export type WorkflowInput = { name: string; type?: string; required?: boolean; default?: string | number | boolean; label?: string; description?: string };
export type WorkflowSummary = { id: string; name: string; version: string; path?: string; description?: string; published?: boolean; needs_repair?: boolean; repair_from?: { run_id?: string; error?: string; variables?: Record<string, unknown>; step?: unknown; status?: string } | null; input_schema?: WorkflowInput[] };
export type WorkflowStep = {
  type: string;
  description?: string;
  url?: string;
  value?: string;
  extractionGoal?: string;
  output?: string;
  [key: string]: unknown;
};
export type WorkflowDefinition = {
  name: string;
  description?: string;
  version?: string;
  steps: WorkflowStep[];
  input_schema?: WorkflowInput[];
  published?: boolean;
  [key: string]: unknown;
};
export type StoredWorkflow = { id: string; path?: string; definition: WorkflowDefinition };
export type StepTelemetry = {
  step_index: number;
  step_type: string;
  description?: string | null;
  tier?: string | null;
  success: boolean;
  latency_ms: number;
  tokens_in?: number;
  tokens_out?: number;
  error?: string | null;
  output?: unknown;
  resolution?: string | null;
};

export type MintTraceStep = {
  ts?: string;
  phase?: string;
  step_type?: string;
  title?: string;
  description?: string;
  detail?: string;
  success?: boolean;
  error?: string | null;
  source?: string;
  tool?: string;
  tier?: string;
  latency_ms?: number;
};
export type MintJob = {
  job_id?: string;
  id?: string;
  name?: string;
  url?: string;
  intent?: string;
  status?: string;
  ok?: boolean;
  partial?: boolean;
  trace_source?: string;
  started_at?: string;
  finished_at?: string;
  directory?: string;
  error?: string | null;
  workflow?: { id?: string; name?: string; path?: string };
  step_count?: number;
  workflow_id?: string;
  workflow_name?: string;
  steps?: MintTraceStep[];
  trace?: { steps?: MintTraceStep[]; partial?: boolean; source?: string };
  [key: string]: unknown;
};

export type RunRecord = {
  id: string;
  workflow_id: string;
  workflow_name: string;
  workflow_version?: string;
  status: "running" | "needs_confirmation" | "succeeded" | "succeeded_with_fallback" | "failed" | "cancelled";
  fallbacks?: { step_index: number; step: string; step_type?: string; recorded?: string | null; matched_by?: string; matched?: string }[];
  needs_repair?: boolean;
  strict?: boolean;
  failure_kind?: string | null;
  started_at?: string;
  finished_at?: string | null;
  model?: string | null;
  steps: StepTelemetry[];
  outputs?: Record<string, unknown>;
  error?: string | null;
  confirmation?: unknown;
  pending_confirmation?: unknown;
  needs_confirmation?: unknown;
  confirmation_event?: unknown;
  event?: unknown;
  workflow_deleted?: boolean;
  [key: string]: unknown;
};

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function trimSlash(url: string): string {
  return url.replace(/\/+$/, "");
}

async function parseBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function describeNetworkError(err: unknown, target: string): Error {
  const original = err instanceof Error ? err.message : String(err);
  if (!isNetworkError(err)) return err instanceof Error ? err : new Error(original);
  return new ApiError(
    0,
    `Can't reach the backend at ${target}. The tunnel may be down or the saved URL may be stale — refresh the page to pick up a new one. (${original})`,
  );
}

async function fetchBackend(path: string, init: RequestInit, settings: Settings): Promise<Response> {
  if (!settings.backendUrl) throw new ApiError(0, "Set a backend URL in Settings first.");
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  if (settings.accessToken) headers.set("X-Browser-Platform-Token", settings.accessToken);
  const target = `${trimSlash(settings.backendUrl)}${path}`;
  try {
    return await fetch(target, { ...init, headers });
  } catch (err) {
    throw describeNetworkError(err, trimSlash(settings.backendUrl));
  }
}

export async function backendFetch(path: string, init: RequestInit = {}, settings: Settings = loadSettings()): Promise<Response> {
  return withDiscoveryRetry(path, init, settings);
}

async function withDiscoveryRetry(path: string, init: RequestInit, settings: Settings): Promise<Response> {
  try {
    return await fetchBackend(path, init, settings);
  } catch (err) {
    if (!isNetworkError(err) || isManualBackendOverride(settings.backendUrl)) throw err;
    const previous = settings.backendUrl;
    const discovered = await refreshDiscovery();
    if (!discovered || discovered === previous) throw err;
    return fetchBackend(path, init, loadSettings());
  }
}

export async function request<T>(
  path: string,
  init: RequestInit = {},
  settings: Settings = loadSettings(),
): Promise<T> {
  const response = await withDiscoveryRetry(path, init, settings);
  const data = await parseBody(response);
  if (!response.ok) {
    const detail =
      data && typeof data === "object" && "detail" in data
        ? String((data as { detail: unknown }).detail)
        : typeof data === "string"
          ? data
          : response.statusText;
    throw new ApiError(response.status, detail || `Request failed (${response.status})`);
  }
  return data as T;
}

export async function health(settings: Settings = loadSettings()): Promise<{ ok: boolean; service?: string }> {
  const response = await withDiscoveryRetry("/health", {}, settings);
  if (!response.ok) throw new ApiError(response.status, "Health check failed");
  return response.json();
}

export const api = {
  health,
  listWorkflows: () => request<{ workflows: WorkflowSummary[] }>("/api/workflows"),
  getWorkflow: (id: string) => request<StoredWorkflow>(`/api/workflows/${id}`),
  createWorkflow: (payload: Record<string, unknown>) =>
    request<StoredWorkflow>("/api/workflows", { method: "POST", body: JSON.stringify(payload) }),
  updateWorkflow: (id: string, definition: WorkflowDefinition) =>
    request<StoredWorkflow>(`/api/workflows/${id}`, { method: "PUT", body: JSON.stringify({ definition }) }),
  deleteWorkflow: (id: string) =>
    request<{ ok: boolean; id: string; runs_kept?: number }>(`/api/workflows/${id}`, { method: "DELETE" }),
  setPublished: (id: string, published: boolean) =>
    request<{ id: string; published: boolean }>(`/api/workflows/${id}/published`, {
      method: "PUT",
      body: JSON.stringify({ published }),
    }),
  startRun: (id: string, body: Record<string, unknown>) =>
    request<RunRecord>(`/api/workflows/${id}/run`, { method: "POST", body: JSON.stringify({ ...body, wait: false }) }),
  repairWorkflow: (id: string, body: Record<string, unknown> = {}) =>
    request<RunRecord>(`/api/workflows/${id}/repair`, { method: "POST", body: JSON.stringify({ wait: false, ...body }) }),
  listRuns: () => request<{ runs: RunRecord[] }>("/api/runs"),
  getRun: (id: string) => request<RunRecord>(`/api/runs/${id}`),
  mint: (payload: Record<string, unknown>) =>
    request<{ result: MintJob }>("/api/pomerado/mint", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  listMintJobs: () => request<{ jobs: MintJob[] }>("/api/pomerado/jobs"),
  getMintJob: (id: string) => request<MintJob>(`/api/pomerado/jobs/${id}`),
  getKeyStatus: () =>
    request<{ openai?: { configured?: boolean; source?: string | null }; openrouter?: { stored_on_server?: boolean } }>(
      "/api/settings/keys",
    ),
  setOpenAIKey: (apiKey: string) =>
    request<{ openai?: { configured?: boolean; source?: string | null } }>("/api/settings/openai", {
      method: "PUT",
      body: JSON.stringify({ api_key: apiKey }),
    }),
};


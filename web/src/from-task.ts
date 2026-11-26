/**
 * Create-a-workflow-from-task client.
 * Contract: README.md "Create a workflow from a task" and GET /api/recorder.
 *
 * POST /api/workflows/from-task (wait: false) → poll GET /api/workflows/from-task/{id}
 * or WS /ws/runs. If status is needs_confirmation and kind is "variables",
 * resume with POST /api/workflows/from-task/{id}/confirmation.
 */
import { request as apiRequest, type WorkflowDefinition } from "./api";

export type FromTaskUsage = {
  tokens_in?: number;
  tokens_out?: number;
  latency_ms?: number;
  steps?: number;
  cost_usd?: number | null;
};

export type FromTaskParameter = {
  name: string;
  label?: string;
  example?: string;
  required?: boolean;
  description?: string;
};

export type FromTaskSuggestion = {
  value?: string;
  suggested_name?: string;
  type?: string;
  url?: string;
  element?: Record<string, unknown>;
};

export type FromTaskConfirmation = {
  event?: string;
  kind?: string;
  job_id?: string;
  task?: string;
  suggestions?: FromTaskSuggestion[];
  placeholders?: string[];
  variables?: Record<string, unknown>;
  message?: string;
  prompt?: string;
  [key: string]: unknown;
};

export type FromTaskReplay = {
  status?: string;
  tokens_in?: number;
  tokens_out?: number;
  latency_ms?: number;
  outputs?: Record<string, unknown>;
  steps?: unknown[];
  error?: string | null;
  [key: string]: unknown;
};

export type FromTaskJob = {
  id: string;
  status: string;
  task: string;
  name: string;
  variables?: Record<string, unknown>;
  workflow_id?: string | null;
  definition?: WorkflowDefinition | null;
  creation?: FromTaskUsage;
  replay?: FromTaskReplay | null;
  suggestions?: FromTaskSuggestion[];
  parameters?: FromTaskParameter[];
  confirmation?: FromTaskConfirmation | null;
  input_validation?: import("./validation").InputValidation | null;
  explore?: FromTaskExplore | null;
  error?: string | null;
};

export type FromTaskExploreProposal = {
  name: string;
  label?: string;
  reason?: string;
  description?: string;
  verified?: boolean;
  verify_with?: string;
  runtime_default?: string;
  default_url?: string;
  alt_url?: string;
  dropped_reason?: string;
};

export type FromTaskExplore = {
  enabled?: boolean;
  proposals?: FromTaskExploreProposal[];
  verified?: FromTaskExploreProposal[];
  dropped?: FromTaskExploreProposal[];
  accepted?: string[];
  rejected?: string[];
  usage?: FromTaskUsage;
  applied?: boolean;
  error?: string;
};

export type FromTaskStart = {
  task: string;
  variables?: Record<string, string>;
  start_url?: string;
  name?: string;
  confirm_variables?: boolean;
  explore?: boolean;
  llm?: Record<string, string>;
};

export function startFromTask(payload: FromTaskStart): Promise<FromTaskJob> {
  return apiRequest<FromTaskJob>("/api/workflows/from-task", {
    method: "POST",
    body: JSON.stringify({ ...payload, wait: false }),
  });
}

export function getFromTaskJob(jobId: string): Promise<FromTaskJob> {
  return apiRequest<FromTaskJob>(`/api/workflows/from-task/${encodeURIComponent(jobId)}`);
}

export function confirmFromTaskVariables(
  jobId: string,
  variables: Record<string, string>,
  parameters?: FromTaskParameter[],
): Promise<{ ok: boolean; job_id: string; action: string }> {
  return apiRequest(`/api/workflows/from-task/${encodeURIComponent(jobId)}/confirmation`, {
    method: "POST",
    body: JSON.stringify({ action: "confirm", variables, parameters }),
  });
}

export function applyFromTaskExplore(
  jobId: string,
  accepted: string[],
  rejected: string[] = [],
): Promise<{ ok: boolean; job_id: string; accepted: string[]; rejected: string[] }> {
  return apiRequest(`/api/workflows/from-task/${encodeURIComponent(jobId)}/explore`, {
    method: "POST",
    body: JSON.stringify({ accepted, rejected }),
  });
}

export function cancelFromTask(jobId: string): Promise<{ ok: boolean; job_id: string; action: string }> {
  return apiRequest(`/api/workflows/from-task/${encodeURIComponent(jobId)}/confirmation`, {
    method: "POST",
    body: JSON.stringify({ action: "cancel" }),
  });
}

export function parseVariableLines(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const cut = line.indexOf("=");
    if (cut <= 0) continue;
    out[line.slice(0, cut).trim()] = line.slice(cut + 1).trim();
  }
  return out;
}

export function isLiveJob(status: string): boolean {
  return status === "running" || status === "needs_confirmation";
}

export function formatTokens(usage?: { tokens_in?: number; tokens_out?: number } | null): string {
  const inn = usage?.tokens_in ?? 0;
  const out = usage?.tokens_out ?? 0;
  if (!inn && !out) return "0 tokens";
  return `${inn}↓ ${out}↑`;
}

export function formatLatency(ms?: number | null): string {
  if (ms == null || Number.isNaN(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

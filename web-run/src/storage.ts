import type { ExtRun } from "./ext";

const INPUTS_KEY = "bp-run-site-inputs-v1";
const HISTORY_KEY = "bp-run-site-history-v1";
const WORKSPACE_KEY = "bp-run-site-workspace";

/** Remembered workspace slug ("" = all published workflows). */
export function loadWorkspace(): string {
  try {
    return localStorage.getItem(WORKSPACE_KEY) || "";
  } catch {
    return "";
  }
}

export function saveWorkspace(slug: string): void {
  try {
    if (slug) localStorage.setItem(WORKSPACE_KEY, slug);
    else localStorage.removeItem(WORKSPACE_KEY);
  } catch {
    /* private mode */
  }
}

export type LocalRun = {
  id: string;
  workflowId: string;
  workflowName: string;
  status: string;
  startedAt: string;
  finishedAt?: string | null;
  values: Record<string, string>;
  traces: NonNullable<ExtRun["traces"]>;
  outputs?: Record<string, unknown>;
  error?: string | null;
};

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

export function loadInputs(workflowId: string): Record<string, string> {
  const all = readJson<Record<string, Record<string, string>>>(INPUTS_KEY, {});
  return all[workflowId] || {};
}

export function saveInputs(workflowId: string, values: Record<string, string>): void {
  const all = readJson<Record<string, Record<string, string>>>(INPUTS_KEY, {});
  all[workflowId] = values;
  localStorage.setItem(INPUTS_KEY, JSON.stringify(all));
}

export function listRuns(): LocalRun[] {
  const items = readJson<LocalRun[]>(HISTORY_KEY, []);
  return [...items].sort((a, b) => String(b.startedAt).localeCompare(String(a.startedAt)));
}

export function getRun(id: string): LocalRun | undefined {
  return listRuns().find((item) => item.id === id);
}

export function upsertRun(run: LocalRun): void {
  const items = readJson<LocalRun[]>(HISTORY_KEY, []).filter((item) => item.id !== run.id);
  items.unshift(run);
  localStorage.setItem(HISTORY_KEY, JSON.stringify(items.slice(0, 100)));
}

export function newRunId(): string {
  return `local-${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 8)}`;
}

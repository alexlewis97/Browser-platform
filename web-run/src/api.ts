export const DEFAULT_BACKEND_URL = "https://browser.129-159-156-75.sslip.io";

export type WorkflowInput = {
  name: string;
  type?: string;
  required?: boolean;
  default?: string | number | boolean | null;
  label?: string;
  description?: string;
};

export type CatalogWorkflow = {
  id: string;
  name: string;
  description?: string;
  version?: string;
  published?: boolean;
  input_schema?: WorkflowInput[];
  definition?: {
    name: string;
    description?: string;
    version?: string;
    steps?: unknown[];
    input_schema?: WorkflowInput[];
    published?: boolean;
    [key: string]: unknown;
  };
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

function isAllowedBackend(url: string): boolean {
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase();
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
    if (host === "localhost" || host === "127.0.0.1" || host === "[::1]") return true;
    if (host.endsWith(".sslip.io") || host.endsWith(".trycloudflare.com")) return true;
    return false;
  } catch {
    return false;
  }
}

async function discoverBackendUrl(): Promise<string> {
  const sources = [`${trimSlash(location.origin)}/backend.json?t=${Date.now()}`];
  for (const source of sources) {
    try {
      const response = await fetch(source, { cache: "no-store" });
      if (!response.ok) continue;
      const data = (await response.json()) as { backendUrl?: string; url?: string };
      const url = trimSlash(String(data.backendUrl || data.url || ""));
      if (url && isAllowedBackend(url)) return url;
    } catch {
      /* try the default */
    }
  }
  return DEFAULT_BACKEND_URL;
}

let cachedBackend: string | null = null;

export async function backendUrl(): Promise<string> {
  if (cachedBackend) return cachedBackend;
  cachedBackend = await discoverBackendUrl();
  return cachedBackend;
}

async function request<T>(path: string): Promise<T> {
  const base = await backendUrl();
  const response = await fetch(`${trimSlash(base)}${path}`, { cache: "no-store" });
  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
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

export type CatalogWorkspace = { id: string; slug: string; name: string; description?: string; workflow_count: number };

export const api = {
  listCatalog: (workspace = "") =>
    request<{ workflows: CatalogWorkflow[] }>(workspace ? `/api/catalog?workspace=${encodeURIComponent(workspace)}` : "/api/catalog"),
  listWorkspaces: () => request<{ workspaces: CatalogWorkspace[] }>("/api/catalog/workspaces"),
  getCatalog: (id: string) => request<CatalogWorkflow>(`/api/catalog/${encodeURIComponent(id)}`),
};

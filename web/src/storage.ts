export const DEFAULT_MODEL = "qwen/qwen3.7-flash";
export const ALT_MODEL = "openai/gpt-oss-20b";

const KEYS = {
  backendUrl: "bp.backendUrl",
  accessToken: "bp.accessToken",
  openrouterKey: "bp.openrouterKey",
  model: "bp.model",
  manualUrl: "bp.backendUrlManual",
} as const;

function read(key: string): string {
  try {
    return localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}

function write(key: string, value: string): void {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch {
    /* private mode / blocked storage */
  }
}

export type Settings = {
  backendUrl: string;
  accessToken: string;
  openrouterKey: string;
  model: string;
};

export function isLocalOrigin(url = typeof window !== "undefined" ? window.location.origin : ""): boolean {
  try {
    const hostname = new URL(url || "http://invalid.invalid").hostname;
    return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]";
  } catch {
    return false;
  }
}

/** When the UI is served by the local backend, talk to the same origin (no CORS, no tunnel). */
export function defaultBackendUrl(): string {
  if (typeof window === "undefined") return "";
  const origin = window.location.origin;
  return isLocalOrigin(origin) ? origin : "";
}

/** Quick-tunnel URLs are published automatically; do not treat them as a manual override. */
export function isManagedBackendUrl(url: string): boolean {
  try {
    const hostname = new URL(url).hostname.toLowerCase();
    return hostname.endsWith(".trycloudflare.com") || hostname === "trycloudflare.com";
  } catch {
    return false;
  }
}

export function isManualBackendOverride(url = ""): boolean {
  if (!url || isLocalOrigin(url) || isManagedBackendUrl(url)) return false;
  try {
    return read(KEYS.manualUrl) === "1";
  } catch {
    return false;
  }
}

export function applyDiscoveredBackendUrl(url: string): Settings {
  const trimmed = url.replace(/\/+$/, "");
  const current = loadSettings();
  if (!trimmed || isManualBackendOverride(current.backendUrl)) return current;
  write(KEYS.backendUrl, trimmed);
  write(KEYS.manualUrl, "");
  return loadSettings();
}

export function loadSettings(): Settings {
  const model = read(KEYS.model) || DEFAULT_MODEL;
  const stored = read(KEYS.backendUrl).replace(/\/+$/, "");
  return {
    backendUrl: stored || defaultBackendUrl(),
    accessToken: read(KEYS.accessToken),
    openrouterKey: read(KEYS.openrouterKey),
    model: model === ALT_MODEL || model === DEFAULT_MODEL ? model : DEFAULT_MODEL,
  };
}

export function saveSettings(next: Settings): void {
  const url = next.backendUrl.replace(/\/+$/, "");
  write(KEYS.backendUrl, url);
  write(KEYS.accessToken, next.accessToken.trim());
  write(KEYS.openrouterKey, next.openrouterKey.trim());
  write(KEYS.model, next.model || DEFAULT_MODEL);
  const manual = Boolean(url) && !isLocalOrigin(url) && !isManagedBackendUrl(url);
  write(KEYS.manualUrl, manual ? "1" : "");
}

export function settingsComplete(s: Settings): boolean {
  if (!s.backendUrl) return false;
  const servedLocally = typeof window !== "undefined" && isLocalOrigin(window.location.origin);
  if (servedLocally || isLocalOrigin(s.backendUrl)) return true;
  return Boolean(s.accessToken);
}

const RUN_INPUTS_PREFIX = "bp.runInputs.";

export function loadRunInputs(workflowId: string): Record<string, string> {
  if (!workflowId) return {};
  try {
    const raw = localStorage.getItem(RUN_INPUTS_PREFIX + workflowId);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const out: Record<string, string> = {};
    for (const [key, value] of Object.entries(parsed)) {
      if (typeof value === "string") out[key] = value;
    }
    return out;
  } catch {
    return {};
  }
}

export function saveRunInputs(workflowId: string, values: Record<string, string>): void {
  if (!workflowId) return;
  try {
    const clean: Record<string, string> = {};
    for (const [key, value] of Object.entries(values)) {
      if (key && String(value).trim()) clean[key] = String(value);
    }
    if (Object.keys(clean).length) localStorage.setItem(RUN_INPUTS_PREFIX + workflowId, JSON.stringify(clean));
    else localStorage.removeItem(RUN_INPUTS_PREFIX + workflowId);
  } catch {
    /* private mode / blocked storage */
  }
}


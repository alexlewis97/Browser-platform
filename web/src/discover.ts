import { applyDiscoveredBackendUrl, isLocalOrigin, isManualBackendOverride, loadSettings } from "./storage";

export type BackendDiscovery = {
  backendUrl?: string;
  url?: string;
  updatedAt?: string;
};

function trimSlash(url: string): string {
  return url.replace(/\/+$/, "");
}

export function discoveryFileUrl(): string | null {
  if (typeof window === "undefined") return null;
  if (isLocalOrigin(window.location.origin)) return null;
  return `${trimSlash(window.location.origin)}/backend.json`;
}

export function discoveryApiUrl(): string | null {
  if (typeof window === "undefined") return null;
  if (isLocalOrigin(window.location.origin)) return null;
  return `${trimSlash(window.location.origin)}/api/backend`;
}

export function isNetworkError(err: unknown): boolean {
  if (!err) return false;
  const message = err instanceof Error ? err.message : String(err);
  const name = err instanceof Error ? err.name : "";
  return (
    name === "TypeError" ||
    /failed to fetch|networkerror|load failed|network request failed|err_connection|econnrefused|econnreset|enotfound/i.test(
      message,
    )
  );
}

async function parseDiscovery(response: Response): Promise<string | null> {
  if (!response.ok) return null;
  const data = (await response.json()) as BackendDiscovery | string;
  const raw = typeof data === "string" ? data : data.backendUrl || data.url || "";
  const url = trimSlash(String(raw));
  if (!url || !/^https?:\/\//i.test(url)) return null;
  return url;
}

export async function fetchDiscoveredBackendUrl(): Promise<string | null> {
  const sources = [discoveryApiUrl(), discoveryFileUrl()].filter((item): item is string => Boolean(item));
  if (!sources.length) return null;
  for (const file of sources) {
    try {
      const response = await fetch(`${file}?t=${Date.now()}`, { cache: "no-store" });
      const url = await parseDiscovery(response);
      if (url) return url;
    } catch {
      /* try the next source */
    }
  }
  return null;
}

/** Refresh the published backend URL unless the user set a custom (non-tunnel) override. */
export async function refreshDiscovery(): Promise<string | null> {
  const current = loadSettings();
  if (isLocalOrigin(window.location.origin)) return current.backendUrl || null;
  if (isManualBackendOverride(current.backendUrl)) return current.backendUrl || null;
  try {
    const discovered = await fetchDiscoveredBackendUrl();
    if (!discovered) return current.backendUrl || null;
    applyDiscoveredBackendUrl(discovered);
    return discovered;
  } catch {
    return current.backendUrl || null;
  }
}

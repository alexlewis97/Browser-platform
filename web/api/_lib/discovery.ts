import { get, put } from "@vercel/blob";

export const DISCOVERY_PATH = "discovery/backend.json";

export type BackendDiscovery = {
  backendUrl: string;
  updatedAt: string;
};

const URL_RE = /^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/i;
const LOCAL_RE = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$/i;

function extraAllowedHosts(): Set<string> {
  const raw = (process.env.BROWSER_PLATFORM_ALLOWED_BACKEND_HOSTS || "").trim();
  return new Set(
    raw
      .split(",")
      .map((item) => item.trim().toLowerCase())
      .filter(Boolean),
  );
}

export function normalizeBackendUrl(raw: unknown): string | null {
  const url = String(raw || "").trim().replace(/\/+$/, "");
  if (!url || !/^https?:\/\//i.test(url)) return null;
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase();
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    if (parsed.pathname && parsed.pathname !== "/") return null;
    if (URL_RE.test(url) || LOCAL_RE.test(url)) return url;
    if (host.endsWith(".trycloudflare.com")) return url;
    if (extraAllowedHosts().has(host)) return url;
    return null;
  } catch {
    return null;
  }
}

export async function readDiscovery(): Promise<BackendDiscovery | null> {
  const result = await get(DISCOVERY_PATH, { access: "private", useCache: false });
  if (!result || result.statusCode !== 200 || !result.stream) return null;
  const res = new Response(result.stream);
  const raw = Buffer.from(await res.arrayBuffer()).toString("utf8");
  try {
    const data = JSON.parse(raw) as BackendDiscovery;
    const backendUrl = normalizeBackendUrl(data.backendUrl);
    if (!backendUrl) return null;
    return { backendUrl, updatedAt: String(data.updatedAt || "") };
  } catch {
    return null;
  }
}

export async function writeDiscovery(backendUrl: string, updatedAt?: string): Promise<BackendDiscovery> {
  const url = normalizeBackendUrl(backendUrl);
  if (!url) throw new Error("backendUrl must be a trycloudflare, localhost, or allowlisted host URL.");
  const payload: BackendDiscovery = { backendUrl: url, updatedAt: updatedAt || new Date().toISOString() };
  await put(DISCOVERY_PATH, JSON.stringify(payload, null, 2) + "\n", {
    access: "private",
    addRandomSuffix: false,
    allowOverwrite: true,
    contentType: "application/json",
  });
  return payload;
}

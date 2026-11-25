import { loadSettings } from "./storage";

export const DEFAULT_MARKET_ORIGIN = "https://browser-platform-web.vercel.app";

export type MarketInput = {
  name: string;
  label?: string;
  type?: string;
  required?: boolean;
  description?: string;
};

export type MarketListing = {
  id: string;
  name: string;
  description: string;
  type: "pomerado" | "workflow-use" | "from-task";
  tier: string;
  inputs: MarketInput[];
  version: string;
  created_at: string;
  updated_at: string;
  sha256: string;
  contains_code: boolean;
  size: number;
};

export function marketOrigin(): string {
  if (typeof window !== "undefined") {
    try {
      const origin = window.location.origin;
      const host = new URL(origin).hostname.toLowerCase();
      if (host === "browser-platform-web.vercel.app" || host.endsWith(".vercel.app")) return origin;
    } catch {
      /* use default */
    }
  }
  return DEFAULT_MARKET_ORIGIN;
}

class MarketError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function marketRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const settings = loadSettings();
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  if (settings.accessToken) headers.set("X-Browser-Platform-Token", settings.accessToken);
  const target = `${marketOrigin()}${path}`;
  let response: Response;
  try {
    response = await fetch(target, { ...init, headers });
  } catch (err) {
    const original = err instanceof Error ? err.message : String(err);
    throw new MarketError(0, `Can't reach the marketplace at ${marketOrigin()}. (${original})`);
  }
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail =
      data && typeof data === "object" && data !== null && "error" in data
        ? String((data as { error: unknown }).error)
        : typeof data === "string" && data
          ? data
          : response.status === 401
            ? "Marketplace is private. Add the shared access token in Settings."
            : `Marketplace request failed (${response.status})`;
    throw new MarketError(response.status, detail);
  }
  return data as T;
}

export function listMarket(): Promise<{ listings: MarketListing[] }> {
  return marketRequest("/api/market");
}

export function getMarketListing(id: string): Promise<{ listing: MarketListing }> {
  return marketRequest(`/api/market/${encodeURIComponent(id)}`);
}

export function deleteMarketListing(id: string): Promise<{ ok: boolean }> {
  return marketRequest(`/api/market/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function publishMarketListing(form: FormData): Promise<{ listing: MarketListing }> {
  return marketRequest("/api/market", { method: "POST", body: form });
}

export function packageUrl(id: string): string {
  return `${marketOrigin()}/api/market/${encodeURIComponent(id)}/package`;
}

export async function sha256File(file: File): Promise<string> {
  const buf = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export function listingIdForName(name: string): string {
  const slug = name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
  return slug || "listing";
}

export type WorkflowPublishPayload = {
  id?: string;
  name: string;
  description?: string;
  type?: string;
  inputs?: MarketInput[];
  version?: string;
  workflow: Record<string, unknown>;
};

export function publishWorkflowJson(payload: WorkflowPublishPayload): Promise<{ listing: MarketListing }> {
  return marketRequest("/api/market", { method: "POST", body: JSON.stringify(payload) });
}

export function inferWorkflowType(workflow: Record<string, unknown> | null | undefined, explicit?: string): MarketListing["type"] {
  const raw = String(explicit || "").trim();
  if (raw === "pomerado" || raw === "workflow-use" || raw === "from-task") return raw;
  if (!workflow) return "workflow-use";
  if (workflow.pomerado_artifact || workflow.pomerado_tool) return "pomerado";
  const via = String(workflow.created_via || workflow.source || "");
  const name = String(workflow.name || "");
  if (/from[-_]?task/i.test(via) || /from[-_]?task/i.test(name)) return "from-task";
  return "workflow-use";
}

export function matchListing(listings: MarketListing[], name: string, id?: string): MarketListing | undefined {
  const slug = listingIdForName(name);
  return listings.find((item) => item.id === id || item.id === slug || item.name === name);
}

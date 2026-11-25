import type { MarketListing } from "./types.ts";
import { loadSettings } from "./storage.ts";

class MarketError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request(path: string, init: RequestInit = {}): Promise<Response> {
  const settings = await loadSettings();
  const headers = new Headers(init.headers);
  if (settings.marketToken) headers.set("X-Browser-Platform-Token", settings.marketToken);
  const res = await fetch(`${settings.marketOrigin}${path}`, { ...init, headers });
  if (!res.ok) {
    let detail = `Marketplace request failed (${res.status})`;
    try {
      const data = await res.clone().json();
      if (data && typeof data === "object" && "error" in data) detail = String((data as { error: unknown }).error);
    } catch {
      /* ignore */
    }
    if (res.status === 401) detail = "Marketplace is private. Add the site access token in Settings.";
    throw new MarketError(res.status, detail);
  }
  return res;
}

export async function listMarket(): Promise<MarketListing[]> {
  const res = await request("/api/market");
  const data = (await res.json()) as { listings?: MarketListing[] };
  return data.listings || [];
}

export async function downloadPackage(id: string): Promise<{ bytes: ArrayBuffer; sha256Header: string | null }> {
  const res = await request(`/api/market/${encodeURIComponent(id)}/package`);
  return { bytes: await res.arrayBuffer(), sha256Header: res.headers.get("x-package-sha256") };
}

export async function sha256Hex(data: ArrayBuffer | Uint8Array): Promise<string> {
  const buf = data instanceof Uint8Array ? data : new Uint8Array(data);
  const copy = new Uint8Array(buf.byteLength);
  copy.set(buf);
  const digest = await crypto.subtle.digest("SHA-256", copy);
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

export async function publishWorkflow(payload: {
  id?: string;
  name: string;
  description?: string;
  type?: string;
  inputs?: MarketListing["inputs"];
  version?: string;
  workflow: Record<string, unknown>;
}): Promise<MarketListing> {
  const res = await request("/api/market", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) });
  const data = (await res.json()) as { listing: MarketListing };
  return data.listing;
}

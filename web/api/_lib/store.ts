import { createHash } from "node:crypto";
import { del, get, list, put } from "@vercel/blob";
import { LISTING_TYPES, type MarketInput, type MarketListing } from "./types.js";
import { bumpVersion, versionGreater } from "./version.js";

const INDEX_PATH = "market/index.json";

function listingPath(id: string): string {
  return `market/listings/${id}.json`;
}

function packagePath(id: string, version: string): string {
  return `market/packages/${id}/${safeVersion(version)}.zip`;
}

function privatePut(pathname: string, body: string | Buffer | Blob, contentType: string) {
  return put(pathname, body, {
    access: "private",
    addRandomSuffix: false,
    allowOverwrite: true,
    contentType,
  });
}

async function readPrivate(pathname: string): Promise<Buffer | null> {
  const result = await get(pathname, { access: "private", useCache: false });
  if (!result || result.statusCode !== 200 || !result.stream) return null;
  const res = new Response(result.stream);
  return Buffer.from(await res.arrayBuffer());
}

export function sha256Hex(buf: Buffer): string {
  return createHash("sha256").update(buf).digest("hex");
}

export function slugify(name: string): string {
  const slug = name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
  return slug || "listing";
}

export function safeId(raw: string): string | null {
  const id = raw.trim().toLowerCase();
  if (!/^[a-z0-9][a-z0-9-]{0,79}$/.test(id)) return null;
  return id;
}

function safeVersion(version: string): string {
  const v = version.trim().replace(/[^A-Za-z0-9._-]+/g, "-").slice(0, 40);
  return v || "1.0.0";
}

function asInputs(value: unknown): MarketInput[] {
  if (!Array.isArray(value)) return [];
  const out: MarketInput[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") continue;
    const rec = item as Record<string, unknown>;
    const name = String(rec.name ?? "").trim();
    if (!name) continue;
    const input: MarketInput = { name };
    if (typeof rec.label === "string" && rec.label.trim()) input.label = rec.label.trim();
    if (typeof rec.type === "string" && rec.type.trim()) input.type = rec.type.trim();
    if (typeof rec.required === "boolean") input.required = rec.required;
    if (typeof rec.description === "string" && rec.description.trim()) input.description = rec.description.trim();
    out.push(input);
  }
  return out;
}

export function normalizeListing(raw: unknown, fallback: Partial<MarketListing> = {}): MarketListing | null {
  if (!raw || typeof raw !== "object") return null;
  const rec = raw as Record<string, unknown>;
  const id = safeId(String(rec.id ?? fallback.id ?? ""));
  const name = String(rec.name ?? fallback.name ?? "").trim();
  const typeRaw = String(rec.type ?? fallback.type ?? "workflow-use").trim();
  const type = LISTING_TYPES.has(typeRaw) ? (typeRaw as MarketListing["type"]) : "workflow-use";
  if (!id || !name) return null;
  const now = new Date().toISOString();
  const containsCode =
    typeof rec.contains_code === "boolean" ? rec.contains_code : type === "pomerado";
  return {
    id,
    name,
    description: String(rec.description ?? fallback.description ?? "").trim(),
    type,
    tier: String(rec.tier ?? fallback.tier ?? type).trim() || type,
    inputs: asInputs(rec.inputs ?? fallback.inputs),
    version: safeVersion(String(rec.version ?? fallback.version ?? "1.0.0")),
    created_at: String(rec.created_at ?? fallback.created_at ?? now),
    updated_at: String(rec.updated_at ?? now),
    sha256: String(rec.sha256 ?? fallback.sha256 ?? "").trim().toLowerCase(),
    contains_code: containsCode,
    size: Number(rec.size ?? fallback.size ?? 0) || 0,
  };
}

export async function readIndex(): Promise<MarketListing[]> {
  const buf = await readPrivate(INDEX_PATH);
  if (buf) {
    try {
      const parsed = JSON.parse(buf.toString("utf8")) as unknown;
      const items = Array.isArray(parsed) ? parsed : (parsed as { listings?: unknown }).listings;
      if (Array.isArray(items)) {
        return items.map((item) => normalizeListing(item)).filter((x): x is MarketListing => Boolean(x));
      }
    } catch {
      /* rebuild below */
    }
  }
  return rebuildIndex();
}

async function writeIndex(listings: MarketListing[]): Promise<void> {
  const body = JSON.stringify({ listings, updated_at: new Date().toISOString() });
  await privatePut(INDEX_PATH, body, "application/json");
}

async function rebuildIndex(): Promise<MarketListing[]> {
  const { blobs } = await list({ prefix: "market/listings/" });
  const listings: MarketListing[] = [];
  for (const blob of blobs) {
    const pathname = (blob as { pathname?: string }).pathname || "";
    if (!pathname.endsWith(".json")) continue;
    const buf = await readPrivate(pathname);
    if (!buf) continue;
    try {
      const listing = normalizeListing(JSON.parse(buf.toString("utf8")));
      if (listing) listings.push(listing);
    } catch {
      continue;
    }
  }
  listings.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  await writeIndex(listings);
  return listings;
}

export async function getListing(id: string): Promise<MarketListing | null> {
  const safe = safeId(id);
  if (!safe) return null;
  const buf = await readPrivate(listingPath(safe));
  if (!buf) {
    const all = await readIndex();
    return all.find((item) => item.id === safe) ?? null;
  }
  try {
    return normalizeListing(JSON.parse(buf.toString("utf8")));
  } catch {
    return null;
  }
}

export async function saveListing(listing: MarketListing, packageBytes: Buffer, opts: { bumpIfExists?: boolean } = {}): Promise<MarketListing> {
  const sha = sha256Hex(packageBytes);
  if (listing.sha256 && listing.sha256 !== sha) {
    throw new Error("Package sha256 does not match the uploaded file.");
  }
  const existing = await getListing(listing.id);
  const now = new Date().toISOString();
  let version = listing.version;
  const shouldBump = opts.bumpIfExists !== false;
  if (existing && shouldBump) {
    version = versionGreater(listing.version, existing.version) ? listing.version : bumpVersion(existing.version);
  }
  const stored: MarketListing = {
    ...listing,
    version,
    sha256: sha,
    size: packageBytes.length,
    created_at: existing?.created_at || listing.created_at || now,
    updated_at: now,
  };
  await privatePut(packagePath(stored.id, stored.version), packageBytes, "application/zip");
  await privatePut(listingPath(stored.id), JSON.stringify(stored, null, 2), "application/json");
  const listings = (await readIndex()).filter((item) => item.id !== stored.id);
  listings.unshift(stored);
  await writeIndex(listings);
  return stored;
}

export async function removeListing(id: string): Promise<boolean> {
  const safe = safeId(id);
  if (!safe) return false;
  const existing = await getListing(safe);
  if (!existing) return false;
  const paths = [listingPath(safe), packagePath(safe, existing.version)];
  try {
    const { blobs } = await list({ prefix: `market/packages/${safe}/` });
    for (const blob of blobs) {
      const pathname = (blob as { pathname?: string }).pathname;
      if (pathname) paths.push(pathname);
    }
  } catch {
    /* still delete known paths */
  }
  await del(paths);
  const listings = (await readIndex()).filter((item) => item.id !== safe);
  await writeIndex(listings);
  return true;
}

export async function readPackage(id: string, version?: string): Promise<{ listing: MarketListing; bytes: Buffer } | null> {
  const listing = await getListing(id);
  if (!listing) return null;
  const bytes = await readPrivate(packagePath(listing.id, version || listing.version));
  if (!bytes) return null;
  return { listing, bytes };
}

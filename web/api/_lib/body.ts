import type { MarketInput, MarketListing } from "./types.js";
import { LISTING_TYPES } from "./types.js";
import { normalizeListing, sha256Hex, slugify } from "./store.js";
import { zipStore } from "./zip.js";

export type PublishPayload = {
  listing: MarketListing;
  packageBytes: Buffer;
  republish: boolean;
};

const MAX_PACKAGE_BYTES = 8 * 1024 * 1024; // 8 MiB
const BRACE = /\{\{?\s*([a-zA-Z_][\w]*)\s*\}?\}/g;
const LABELS: Record<string, string> = {
  term: "Search term",
  query: "Search query",
  q: "Search query",
  search: "Search term",
  name: "Name",
  status: "Status",
  url: "URL",
  email: "Email",
  title: "Title",
};

function prettyLabel(name: string, label?: string): string {
  if (label && label.trim()) return label.trim();
  const key = (name || "").trim();
  if (LABELS[key.toLowerCase()]) return LABELS[key.toLowerCase()];
  const text = key.replaceAll("_", " ").trim();
  return text ? text[0].toUpperCase() + text.slice(1) : "Input";
}

function placeholdersIn(value: unknown, found: string[] = []): string[] {
  if (typeof value === "string") {
    const re = new RegExp(BRACE.source, "g");
    let match: RegExpExecArray | null;
    while ((match = re.exec(value))) {
      if (!found.includes(match[1])) found.push(match[1]);
    }
  } else if (Array.isArray(value)) {
    for (const item of value) placeholdersIn(item, found);
  } else if (value && typeof value === "object") {
    for (const item of Object.values(value as Record<string, unknown>)) placeholdersIn(item, found);
  }
  return found;
}

function asInputs(value: unknown): MarketInput[] {
  if (!Array.isArray(value)) return [];
  const out: MarketInput[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") continue;
    const rec = item as Record<string, unknown>;
    const name = String(rec.name ?? "").trim();
    if (!name) continue;
    const input: MarketInput = { name, label: prettyLabel(name, typeof rec.label === "string" ? rec.label : undefined) };
    if (typeof rec.type === "string" && rec.type.trim()) input.type = rec.type.trim();
    if (typeof rec.required === "boolean") input.required = rec.required;
    if (typeof rec.description === "string" && rec.description.trim()) input.description = rec.description.trim();
    out.push(input);
  }
  return out;
}

export function deriveInputs(workflow: unknown, explicit?: unknown): MarketInput[] {
  const given = asInputs(explicit);
  const byName = new Map(given.map((item) => [item.name, item]));
  for (const name of placeholdersIn(workflow)) {
    if (!byName.has(name)) byName.set(name, { name, type: "string", required: true, label: prettyLabel(name) });
  }
  if (workflow && typeof workflow === "object") {
    const schema = (workflow as { input_schema?: unknown }).input_schema;
    for (const item of asInputs(schema)) {
      if (!byName.has(item.name)) byName.set(item.name, item);
      else byName.set(item.name, { ...item, ...byName.get(item.name) });
    }
  }
  return [...byName.values()];
}

export function inferType(workflow: Record<string, unknown> | null, explicit?: string): MarketListing["type"] {
  const raw = String(explicit || "").trim();
  if (LISTING_TYPES.has(raw)) return raw as MarketListing["type"];
  if (!workflow) return "workflow-use";
  if (workflow.pomerado_artifact || workflow.pomerado_tool) return "pomerado";
  const via = String(workflow.created_via || workflow.source || "");
  const name = String(workflow.name || "");
  if (/from[-_]?task/i.test(via) || /from[-_]?task/i.test(name)) return "from-task";
  return "workflow-use";
}

export function extractWorkflow(raw: Record<string, unknown>): Record<string, unknown> | null {
  const candidate = (raw.workflow || raw.definition || raw) as Record<string, unknown>;
  if (candidate && typeof candidate === "object" && Array.isArray(candidate.steps) && candidate.steps.length && String(candidate.name || raw.name || "").trim()) {
    const name = String(candidate.name || raw.name || "").trim();
    return { ...candidate, name, description: String(candidate.description ?? raw.description ?? "") };
  }
  return null;
}

export function buildWorkflowPackage(workflow: Record<string, unknown>, listing: { name: string; type: string; description?: string; inputs?: MarketInput[]; version?: string; id?: string }): Buffer {
  const manifest = {
    schema: 1,
    id: listing.id,
    name: listing.name,
    description: listing.description || "",
    type: listing.type,
    version: listing.version || "1",
    inputs: listing.inputs || [],
    workflow: "workflow.json",
  };
  return zipStore({
    "manifest.json": JSON.stringify(manifest, null, 2) + "\n",
    "workflow.json": JSON.stringify(workflow, null, 2) + "\n",
  });
}

function fromBase64(raw: string): Buffer {
  const cleaned = raw.replace(/^data:[^;]+;base64,/, "").replace(/\s+/g, "");
  return Buffer.from(cleaned, "base64");
}

async function fromMultipart(request: Request): Promise<PublishPayload> {
  const form = await request.formData();
  const file = form.get("package") ?? form.get("file");
  const fields: Record<string, unknown> = {};
  for (const [key, value] of form.entries()) {
    if (key === "package" || key === "file") continue;
    if (typeof value === "string") fields[key] = value;
  }
  if (typeof fields.inputs === "string") {
    try {
      fields.inputs = JSON.parse(fields.inputs);
    } catch {
      throw new Error("inputs must be a JSON array.");
    }
  }
  if (typeof fields.workflow === "string") {
    try {
      fields.workflow = JSON.parse(fields.workflow);
    } catch {
      throw new Error("workflow must be a JSON object.");
    }
  }
  if (typeof fields.contains_code === "string") {
    fields.contains_code = fields.contains_code === "true" || fields.contains_code === "1" || fields.contains_code === "on";
  }
  const workflow = fields.workflow && typeof fields.workflow === "object" ? extractWorkflow(fields as Record<string, unknown>) : null;
  const typ = inferType(workflow, String(fields.type || ""));
  if (typ === "pomerado" && !(file instanceof File)) {
    throw new Error("Pomerado packages must be uploaded as a zip (they include integration code).");
  }
  if (file instanceof File) {
    if (typeof file.size === "number" && file.size > MAX_PACKAGE_BYTES) {
      throw new Error(`Package exceeds ${MAX_PACKAGE_BYTES} byte limit.`);
    }
    const packageBytes = Buffer.from(await file.arrayBuffer());
    if (packageBytes.length > MAX_PACKAGE_BYTES) {
      throw new Error(`Package exceeds ${MAX_PACKAGE_BYTES} byte limit.`);
    }
    if (packageBytes.length >= 4) {
      const isZip = packageBytes[0] === 0x50 && packageBytes[1] === 0x4b;
      if (!isZip) throw new Error("Package must be a zip archive.");
    }
    const claimed = String(fields.sha256 || "").toLowerCase();
    const sha = sha256Hex(packageBytes);
    if (claimed && claimed !== sha) throw new Error("Package sha256 does not match the uploaded file.");
    const listing = normalizeListing({
      ...fields,
      type: typ,
      sha256: sha,
      size: packageBytes.length,
      contains_code: typ === "pomerado",
    });
    if (!listing) throw new Error("A name is required.");
    return { listing, packageBytes, republish: false };
  }
  if (!workflow) throw new Error("Send a workflow JSON object, or a zip for Pomerado packages.");
  return fromWorkflowFields(fields, workflow);
}

function fromWorkflowFields(raw: Record<string, unknown>, workflow: Record<string, unknown>): PublishPayload {
  const typ = inferType(workflow, String(raw.type || ""));
  if (typ === "pomerado") {
    throw new Error("Pomerado packages must be uploaded as a zip (they include integration code).");
  }
  const name = String(raw.name || workflow.name || "").trim();
  if (!name) throw new Error("A name is required.");
  const id = String(raw.id || slugify(name));
  const description = String(raw.description ?? workflow.description ?? "");
  const inputs = deriveInputs(workflow, raw.inputs);
  const version = String(raw.version || workflow.version || "1");
  const listing = normalizeListing({
    id,
    name,
    description,
    type: typ,
    tier: raw.tier || typ,
    inputs,
    version,
    contains_code: false,
  });
  if (!listing) throw new Error("A name is required.");
  const cleanedWorkflow = { ...workflow, name, description, input_schema: inputs, version: listing.version };
  const packageBytes = buildWorkflowPackage(cleanedWorkflow, listing);
  listing.sha256 = sha256Hex(packageBytes);
  listing.size = packageBytes.length;
  return { listing, packageBytes, republish: true };
}

export async function parsePublishBody(request: Request): Promise<PublishPayload> {
  const contentType = request.headers.get("content-type") || "";
  if (contentType.includes("multipart/form-data")) return fromMultipart(request);

  const raw = (await request.json()) as Record<string, unknown>;
  const encoded = raw.package_base64 ?? raw.packageBase64;
  const typHint = inferType(extractWorkflow(raw), String(raw.type || ""));
  if (typeof encoded === "string" && encoded) {
    const packageBytes = fromBase64(encoded);
    const sha = sha256Hex(packageBytes);
    const claimed = String(raw.sha256 || "").toLowerCase();
    if (claimed && claimed !== sha) throw new Error("Package sha256 does not match the uploaded file.");
    const listing = normalizeListing({ ...raw, sha256: sha, size: packageBytes.length, contains_code: typHint === "pomerado" || raw.contains_code });
    if (!listing) throw new Error("A name is required.");
    return { listing, packageBytes, republish: false };
  }
  const workflow = extractWorkflow(raw);
  if (!workflow) throw new Error("Send a workflow JSON object (workflow / definition / steps), or a zip for Pomerado packages.");
  return fromWorkflowFields(raw, workflow);
}

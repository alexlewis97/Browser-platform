import { placeholdersIn } from "./interpolate.ts";
import type { WorkflowDefinition, WorkflowInput } from "./types.ts";

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

export function prettyLabel(name: string, label?: string | null): string {
  if (label && label.trim()) return label.trim();
  const key = (name || "").trim();
  if (LABELS[key.toLowerCase()]) return LABELS[key.toLowerCase()];
  const text = key.replaceAll("_", " ").trim();
  return text ? text[0].toUpperCase() + text.slice(1) : "Input";
}

export function deriveInputs(definition: WorkflowDefinition): WorkflowInput[] {
  const byName = new Map<string, WorkflowInput>();
  for (const item of definition.input_schema || []) {
    if (!item?.name) continue;
    byName.set(item.name, { ...item, label: prettyLabel(item.name, item.label) });
  }
  for (const name of placeholdersIn(definition)) {
    if (byName.has(name)) continue;
    byName.set(name, { name, type: "string", required: true, label: prettyLabel(name) });
  }
  return [...byName.values()];
}

export function missingRequired(inputs: WorkflowInput[], values: Record<string, string>): string[] {
  const missing: string[] = [];
  for (const input of inputs) {
    if (input.required === false) continue;
    const raw = values[input.name];
    if (raw == null || String(raw).trim() === "") missing.push(input.name);
  }
  return missing;
}

import { unzipSync, strFromU8 } from "fflate";
import type { WorkflowDefinition } from "./types.ts";

export function parseWorkflowPackage(bytes: Uint8Array): { definition: WorkflowDefinition; path: string } {
  const files = unzipSync(bytes);
  const names = Object.keys(files).filter((n) => !n.endsWith("/") && !n.includes("__MACOSX"));
  const preferred = names.find((n) => n.endsWith("workflow.json") || n.endsWith(".workflow.json"))
    || names.find((n) => n.endsWith("manifest.json") && names.some((x) => x.includes("workflow")))
    || names.find((n) => n.toLowerCase().endsWith(".json") && !n.endsWith("manifest.json"));
  // If a manifest points at the workflow, honour it.
  const manifestName = names.find((n) => n.split("/").pop() === "manifest.json");
  let path = preferred || "";
  if (manifestName) {
    try {
      const manifest = JSON.parse(strFromU8(files[manifestName])) as { workflow?: string; definition?: string };
      const rel = manifest.workflow || manifest.definition;
      if (rel) {
        const hit = names.find((n) => n === rel || n.endsWith("/" + rel) || n.endsWith(rel));
        if (hit) path = hit;
      }
    } catch {
      /* use preferred */
    }
  }
  if (!path) {
    // Maybe the "zip" is actually a raw JSON file published as a zip of one json, or not a zip.
    throw new Error("Package does not contain a workflow JSON file.");
  }
  const raw = JSON.parse(strFromU8(files[path])) as Record<string, unknown>;
  const definition = (raw.definition && typeof raw.definition === "object" ? raw.definition : raw) as WorkflowDefinition;
  if (!definition?.name || !Array.isArray(definition.steps)) {
    throw new Error("workflow JSON is missing name or steps.");
  }
  return { definition, path };
}

/** Also accept a bare workflow JSON (import file). */
export function parseWorkflowJson(text: string): WorkflowDefinition {
  const raw = JSON.parse(text) as Record<string, unknown>;
  const definition = (raw.definition && typeof raw.definition === "object" ? raw.definition : raw) as WorkflowDefinition;
  if (!definition?.name || !Array.isArray(definition.steps)) {
    throw new Error("workflow JSON is missing name or steps.");
  }
  return definition;
}

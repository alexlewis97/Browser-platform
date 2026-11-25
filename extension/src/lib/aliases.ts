import { normalizeText, nameVariants } from "./normalize.ts";
import type { AliasMapping, Candidate } from "./types.ts";

export function findAlias(aliases: AliasMapping[] | undefined, stepIndex: number, query: string): AliasMapping | null {
  const qVars = nameVariants(query);
  const qNorm = normalizeText(query);
  for (const alias of aliases || []) {
    if (alias.step_index !== stepIndex) continue;
    const stored = new Set<string>([alias.query_normalized, normalizeText(alias.query), ...nameVariants(alias.query)]);
    if (stored.has(qNorm)) return alias;
    for (const v of qVars) {
      if (stored.has(v)) return alias;
    }
  }
  return null;
}

export function makeAlias(stepIndex: number, query: string, candidate: Candidate): AliasMapping {
  return {
    step_index: stepIndex,
    query,
    query_normalized: normalizeText(query),
    chosen_text: candidate.text,
    chosen_id: candidate.id,
    cssSelector: !candidate.selector_type || candidate.selector_type === "css" ? candidate.selector : null,
    xpath: candidate.selector_type === "xpath" ? candidate.selector : null,
    role: candidate.role,
  };
}

export function aliasToStrategy(alias: AliasMapping): { type: string; value: string; metadata?: Record<string, unknown> } {
  if (alias.cssSelector) return { type: "css", value: alias.cssSelector };
  if (alias.xpath) return { type: "xpath", value: alias.xpath };
  return { type: "text_exact", value: alias.chosen_text, metadata: alias.role ? { role: alias.role } : {} };
}

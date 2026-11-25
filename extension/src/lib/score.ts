import * as fuzz from "fuzzball";
import { interpolate } from "./interpolate.ts";
import { nameVariants, normalizeText } from "./normalize.ts";
import type { Candidate, FuzzyDecision } from "./types.ts";

export function scoreText(query: string, candidate: string): number {
  let best = 0;
  const qVars = nameVariants(query);
  const cVars = nameVariants(candidate);
  for (const q of qVars) {
    if (!q) continue;
    for (const c of cVars) {
      if (!c) continue;
      best = Math.max(
        best,
        Number(fuzz.WRatio(q, c)),
        Number(fuzz.token_set_ratio(q, c)),
        Number(fuzz.token_sort_ratio(q, c)),
        Number(fuzz.ratio(q, c)),
      );
      if (best >= 99.9) return 100;
    }
  }
  return best;
}

export function scoreCandidates(
  query: string,
  candidates: Iterable<Candidate>,
  opts: { threshold?: number; margin?: number } = {},
): FuzzyDecision {
  const threshold = opts.threshold ?? 90;
  const margin = opts.margin ?? 8;
  const ranked: Candidate[] = [];
  const seen = new Set<string>();
  for (const cand of candidates) {
    const text = cand.text || "";
    const key = `${normalizeText(text)}|${cand.selector || ""}`;
    if (!normalizeText(text) || seen.has(key)) continue;
    seen.add(key);
    ranked.push({ ...cand, score: Math.round(scoreText(query, text) * 100) / 100 });
  }
  ranked.sort((a, b) => (b.score || 0) - (a.score || 0) || a.text.localeCompare(b.text));
  const winner = ranked[0] ?? null;
  const runnerUp = ranked[1] ?? null;
  if (!winner) {
    return { accepted: false, reason: "no_candidates", query, winner: null, runner_up: null, ranked: [], threshold, margin };
  }
  const winScore = Number(winner.score || 0);
  const second = runnerUp ? Number(runnerUp.score || 0) : 0;
  const gap = winScore - second;
  if (winScore < threshold) {
    return { accepted: false, reason: "below_threshold", query, winner, runner_up: runnerUp, ranked, threshold, margin };
  }
  if (runnerUp && gap < margin) {
    return { accepted: false, reason: "ambiguous_margin", query, winner, runner_up: runnerUp, ranked, threshold, margin };
  }
  return { accepted: true, reason: "accepted", query, winner, runner_up: runnerUp, ranked, threshold, margin };
}

export function queryForStep(step: { fuzzy_query?: string; target_text?: string; selectedText?: string; value?: string; description?: string }, variables?: Record<string, unknown> | null): string {
  const raw = step.fuzzy_query || step.target_text || step.selectedText || step.value || step.description || "";
  return String(interpolate(raw, variables) || "").trim();
}


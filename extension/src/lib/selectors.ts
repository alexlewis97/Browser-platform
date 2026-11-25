import type { SelectorAttempt, SelectorStrategy, WorkflowStep } from "./types.ts";

function strategy(type: string, value: string, priority: number, source?: string, extra: Record<string, unknown> = {}): SelectorStrategy {
  const metadata: Record<string, unknown> = { ...extra };
  if (source) metadata.source = source;
  return { type, value, priority, metadata };
}

function quotedPhrases(text: string): string[] {
  const out: string[] = [];
  const re = /['"]([^'"]{1,80})['"]/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) out.push(m[1]);
  return out;
}

export function buildSelectorPlan(step: WorkflowStep, cached?: SelectorStrategy | null): SelectorAttempt[] {
  const attempts: SelectorAttempt[] = [];
  const seen = new Set<string>();

  const add = (kind: SelectorAttempt["kind"], strat: SelectorStrategy) => {
    if (!strat.type) return;
    const key = `${strat.type}|${strat.value}|${String(strat.metadata?.role || "")}`;
    if (seen.has(key)) return;
    seen.add(key);
    attempts.push({ kind, strategy: strat });
  };

  const winner = cached || step.cachedSelector;
  if (winner) add("cached", winner);

  const strategies = [...(step.selectorStrategies || [])];
  strategies.sort((a, b) => (a.priority ?? 100) - (b.priority ?? 100));
  for (const s of strategies) add("selector", s);

  if (step.cssSelector) add("selector", strategy("css", step.cssSelector, 500, "legacy_css"));
  if (step.xpath) add("selector", strategy("xpath", step.xpath, 510, "legacy_xpath"));
  if (step.target_text) {
    add("selector", strategy("text_exact", step.target_text, 520, "target_text"));
    add("heuristic", strategy("text_fuzzy", step.target_text, 800, "target_text_fuzzy"));
    add("heuristic", strategy("role_text", step.target_text, 810, "target_role", { role: "button" }));
    add("heuristic", strategy("placeholder", step.target_text, 820, "target_placeholder"));
    add("heuristic", strategy("aria_label", step.target_text, 830, "target_aria"));
  }
  if (step.description) {
    for (const phrase of quotedPhrases(step.description)) {
      add("heuristic", strategy("text_exact", phrase, 850, "description"));
      add("heuristic", strategy("text_fuzzy", phrase, 860, "description_fuzzy"));
    }
  }
  return attempts;
}

export function fuzzyApplies(step: WorkflowStep, enabled = true): boolean {
  if (!enabled) return false;
  return ["click", "input", "select_change", "key_press"].includes(step.type);
}

export function normalizeWikipediaUrl(url: string | null | undefined): string | undefined {
  if (!url) return url ?? undefined;
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase();
    if (!host.includes("wikipedia.org") && !host.includes("wikimedia.org")) return url;
    if (parsed.pathname.startsWith("/wiki/") && parsed.pathname.includes(" ")) {
      parsed.pathname = parsed.pathname.split("/").map((p) => p.replaceAll(" ", "_")).join("/");
      return parsed.toString();
    }
  } catch {
    return url;
  }
  return url;
}

export type SelectorStrategy = {
  type: string;
  value: string;
  priority?: number;
  metadata?: Record<string, unknown>;
};

export type WorkflowInput = {
  name: string;
  type?: string;
  required?: boolean;
  default?: string | number | boolean | null;
  label?: string;
  description?: string;
};

export type ExpectedState = {
  url_includes?: string;
  title_contains?: string;
  text_contains?: string;
  heading?: string;
};

export type WorkflowStep = {
  type: string;
  description?: string;
  output?: string;
  url?: string;
  value?: string;
  default_value?: string;
  selectedText?: string;
  key?: string;
  scrollX?: number;
  scrollY?: number;
  goal?: string;
  extractionGoal?: string;
  target_text?: string;
  cssSelector?: string;
  xpath?: string;
  selectorStrategies?: SelectorStrategy[];
  cachedSelector?: SelectorStrategy;
  container_hint?: string;
  wait_time?: number;
  expected_state?: ExpectedState;
  fuzzy_query?: string;
  fuzzy?: FuzzyConfig;
  hitl?: HitlConfig;
};

export type FuzzyConfig = {
  enabled?: boolean;
  threshold?: number;
  margin?: number;
  top_n?: number;
};

export type HitlConfig = {
  mode?: "always_on_uncertain" | "llm_then_human" | "llm_only";
  timeout_s?: number;
  timeout_policy?: "fail" | "fall_to_llm";
};

export type AliasMapping = {
  step_index: number;
  query: string;
  query_normalized: string;
  chosen_text: string;
  chosen_id?: string | null;
  cssSelector?: string | null;
  xpath?: string | null;
  role?: string | null;
};

export type WorkflowDefinition = {
  name: string;
  description?: string;
  version?: string;
  steps: WorkflowStep[];
  input_schema?: WorkflowInput[];
  fuzzy?: FuzzyConfig;
  hitl?: HitlConfig;
  aliases?: AliasMapping[];
  [key: string]: unknown;
};

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
  type: string;
  tier: string;
  inputs: MarketInput[];
  version: string;
  created_at: string;
  updated_at: string;
  sha256: string;
  contains_code: boolean;
  size: number;
};

export type InstalledWorkflow = {
  id: string;
  listingId?: string;
  name: string;
  description: string;
  type: string;
  version: string;
  sha256: string;
  installedAt: string;
  source: "market" | "file" | "record";
  definition: WorkflowDefinition;
};

export type Candidate = {
  id: string;
  text: string;
  role?: string | null;
  selector?: string | null;
  selector_type?: string;
  value?: string | null;
  score?: number | null;
  disabled?: boolean;
};

export type FuzzyDecision = {
  accepted: boolean;
  reason: "accepted" | "no_candidates" | "below_threshold" | "ambiguous_margin";
  query: string;
  winner: Candidate | null;
  runner_up: Candidate | null;
  ranked: Candidate[];
  threshold: number;
  margin: number;
};

export type SelectorAttempt = {
  kind: "cached" | "selector" | "heuristic";
  strategy: SelectorStrategy;
};

export type TraceStep = {
  index: number;
  type: string;
  description?: string;
  status: "running" | "ok" | "fail" | "hitl";
  message?: string;
  output?: unknown;
  attempt?: { type: string; value: string; kind?: string };
  startedAt: number;
  endedAt?: number;
};

export type RunState = {
  runId: string;
  workflowId: string;
  status: "running" | "hitl" | "ok" | "fail" | "cancel";
  currentIndex: number;
  traces: TraceStep[];
  error?: string;
  hitl?: { query: string; reason: string; candidates: Candidate[] };
};

export type ExtensionSettings = {
  marketToken: string;
  marketOrigin: string;
  backendUrl?: string;
};

export const DEFAULT_MARKET_ORIGIN = "https://browser-platform-web.vercel.app";
export const DEFAULT_BACKEND_URL = "https://browser.129-159-156-75.sslip.io";
export const DEFAULT_FUZZY: Required<Pick<FuzzyConfig, "enabled" | "threshold" | "margin" | "top_n">> = {
  enabled: true,
  threshold: 90,
  margin: 8,
  top_n: 5,
};

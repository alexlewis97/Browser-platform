import { request, type WorkflowDefinition } from "./api";

export type ValidationFinding = {
  id?: string;
  severity?: string;
  code?: string;
  message: string;
  suggestion_label?: string | null;
  patch?: Record<string, unknown> | null;
  source?: string;
};

export type InputValidation = {
  findings?: ValidationFinding[];
  error_count?: number;
  warning_count?: number;
  blocks_publish?: boolean;
  usage?: { tokens_in?: number; tokens_out?: number; cost_usd?: number; used?: boolean };
  definition?: WorkflowDefinition;
  error?: string;
};

export type OutputValidation = {
  verdict?: "ok" | "suspect" | "wrong" | string;
  reason?: string;
  source?: string;
  usage?: { tokens_in?: number; tokens_out?: number; cost_usd?: number; used?: boolean };
};

export function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function formatUsage(usage?: { tokens_in?: number; tokens_out?: number; cost_usd?: number | null; used?: boolean } | null): string {
  if (!usage) return "0 tokens";
  const inn = usage.tokens_in || 0;
  const out = usage.tokens_out || 0;
  const cost = usage.cost_usd;
  const costLabel = cost != null && cost > 0 ? ` · $${Number(cost).toFixed(6)}` : "";
  return `${inn} in / ${out} out${costLabel}`;
}

export function findingsCard(validation?: InputValidation | null, opts?: { applyAction?: string }): string {
  const findings = validation?.findings || [];
  if (!findings.length && !validation?.error) return "";
  const blocks = Boolean(validation?.blocks_publish);
  const rows = findings
    .map((item) => {
      const sev = item.severity === "error" ? "bad" : "warn";
      const fix = item.suggestion_label && item.patch && opts?.applyAction
        ? `<button type="button" class="btn" data-action="${escapeHtml(opts.applyAction)}" data-finding="${escapeHtml(item.id || "")}">${escapeHtml(item.suggestion_label)}</button>`
        : item.suggestion_label
          ? `<div class="muted">Suggested: ${escapeHtml(item.suggestion_label)}</div>`
          : "";
      return `<div class="finding">
        <span class="pill ${sev}">${escapeHtml(item.severity || "warning")}</span>
        <div><div>${escapeHtml(item.message)}</div>${fix}</div>
      </div>`;
    })
    .join("");
  const banner = blocks
    ? `<div class="banner err">Publishing is blocked until the errors below are fixed. You can still save.</div>`
    : `<div class="banner info">Input checks found something to review. Saving is still allowed.</div>`;
  const usage = validation?.usage?.used ? `<p class="muted">Input check tokens: ${escapeHtml(formatUsage(validation.usage))}</p>` : "";
  return `<div class="card" id="input-findings">${banner}${rows}${usage}</div>`;
}

export async function validateInputs(definition: WorkflowDefinition, task = "", patch?: Record<string, unknown>): Promise<InputValidation> {
  return request<InputValidation>("/api/workflows/validate-inputs", {
    method: "POST",
    body: JSON.stringify({ definition, task, patch, use_llm: true }),
  });
}

export async function validateOutput(payload: Record<string, unknown>): Promise<OutputValidation> {
  return request<OutputValidation>("/api/runs/validate-output", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

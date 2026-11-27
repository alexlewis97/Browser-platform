export type ExtRun = {
  runId?: string;
  workflowId?: string;
  status?: "running" | "hitl" | "ok" | "fail" | "cancel" | string;
  currentIndex?: number;
  traces?: Array<{
    index?: number;
    type?: string;
    description?: string;
    status?: string;
    message?: string;
    output?: unknown;
  }>;
  error?: string;
  outputs?: Record<string, unknown>;
};

const SITE = "bp-web";
const EXT = "bp-ext";

export function probeExtension(timeoutMs = 700): Promise<boolean> {
  return new Promise((resolve) => {
    const timer = window.setTimeout(() => {
      window.removeEventListener("message", onMsg);
      resolve(false);
    }, timeoutMs);
    const onMsg = (event: MessageEvent) => {
      const data = event.data;
      if (!data || data.source !== EXT) return;
      window.clearTimeout(timer);
      window.removeEventListener("message", onMsg);
      resolve(true);
    };
    window.addEventListener("message", onMsg);
    window.postMessage({ source: SITE, direction: "request", kind: "bp-web-ping", id: "ping", payload: {} }, "*");
  });
}

export function extensionRequest<T>(
  kind: string,
  payload: Record<string, unknown> = {},
  timeoutMs = 300000,
): Promise<T & { ok: boolean; error?: string; cancelled?: boolean }> {
  return new Promise((resolve, reject) => {
    const id = `r-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    let settled = false;
    const timer = window.setTimeout(() => {
      if (settled) return;
      settled = true;
      window.removeEventListener("message", onMsg);
      reject(new Error("The extension did not respond. Is the Browser Platform extension loaded and allowed on this site?"));
    }, timeoutMs);
    const onMsg = (event: MessageEvent) => {
      const data = event.data;
      if (!data || data.source !== EXT || data.direction !== "response" || data.id !== id) return;
      window.clearTimeout(timer);
      window.removeEventListener("message", onMsg);
      settled = true;
      if (data.error && !data.result) reject(new Error(String(data.error)));
      else resolve((data.result || { ok: false, error: data.error }) as T & { ok: boolean; error?: string; cancelled?: boolean });
    };
    window.addEventListener("message", onMsg);
    window.postMessage({ source: SITE, direction: "request", kind, id, payload }, "*");
  });
}

export function watchRunProgress(onRun: (run: ExtRun) => void): () => void {
  const onMsg = (event: MessageEvent) => {
    const data = event.data;
    if (!data || data.source !== EXT || data.direction !== "progress" || data.kind !== "bp-run") return;
    if (data.run) onRun(data.run as ExtRun);
  };
  window.addEventListener("message", onMsg);
  return () => window.removeEventListener("message", onMsg);
}

export async function requestRun(payload: {
  id: string;
  name: string;
  definition: Record<string, unknown>;
  values: Record<string, string>;
}): Promise<{ ok: boolean; cancelled?: boolean; error?: string; run?: ExtRun }> {
  return extensionRequest("bp-run-request", payload);
}

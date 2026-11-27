/**
 * Relays a small allowlisted set of official-site postMessage calls into the
 * extension background. Never returns tokens or arbitrary settings to the page.
 */
const SITE = "bp-web";
const EXT = "bp-ext";

const ALLOWED_ORIGINS = new Set([
  "https://browser-platform-web.vercel.app",
  "https://browser-platform-run.vercel.app",
  "http://localhost:8745",
  "http://127.0.0.1:8745",
  "http://localhost:5173",
  "http://127.0.0.1:5173",
  "http://localhost:5174",
  "http://127.0.0.1:5174",
]);

/** Kinds the page may invoke. Keep this tight — no settings/token reads. */
const ALLOWED_KINDS = new Set([
  "bp-web-ping",
  "bp-record-start",
  "bp-record-stop",
  "bp-record-status",
  "bp-run-definition",
  "bp-run-request",
]);

function originAllowed(origin: string): boolean {
  if (ALLOWED_ORIGINS.has(origin)) return true;
  try {
    const url = new URL(origin);
    if (url.hostname === "localhost" || url.hostname === "127.0.0.1" || url.hostname === "[::1]") {
      return url.protocol === "http:" || url.protocol === "https:";
    }
  } catch {
    /* ignore */
  }
  return false;
}

function scrubResult(kind: string, result: unknown): unknown {
  if (!result || typeof result !== "object") return result;
  const copy = { ...(result as Record<string, unknown>) };
  // Never leak tokens / keys back into page JS.
  delete copy.accessToken;
  delete copy.marketToken;
  delete copy.openrouterKey;
  delete copy.apiKey;
  delete copy.token;
  if (copy.settings && typeof copy.settings === "object") {
    const settings = { ...(copy.settings as Record<string, unknown>) };
    delete settings.accessToken;
    delete settings.marketToken;
    delete settings.openrouterKey;
    delete settings.apiKey;
    delete settings.token;
    copy.settings = settings;
  }
  if (kind === "bp-get-state") {
    return { ok: false, error: "bp-get-state is not available to web pages" };
  }
  return copy;
}

window.postMessage({ source: EXT, direction: "hello", ok: true }, "*");

chrome.runtime.onMessage.addListener((message) => {
  if (!message || typeof message !== "object" || (message as { kind?: string }).kind !== "bp-run") return;
  window.postMessage(
    { source: EXT, direction: "progress", kind: "bp-run", run: (message as { run?: unknown }).run },
    window.location.origin,
  );
});

window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  if (!originAllowed(String(event.origin || ""))) return;
  const data = event.data as
    | { source?: string; direction?: string; kind?: string; id?: string; payload?: Record<string, unknown> }
    | null;
  if (!data || data.source !== SITE || data.direction !== "request" || !data.kind) return;
  if (!ALLOWED_KINDS.has(data.kind)) {
    window.postMessage(
      { source: EXT, direction: "response", id: data.id, error: `kind '${data.kind}' is not allowed` },
      event.origin,
    );
    return;
  }
  const id = data.id;
  const kind = data.kind;
  void chrome.runtime
    .sendMessage({ kind, ...(data.payload || {}) })
    .then((result) => {
      window.postMessage({ source: EXT, direction: "response", id, result: scrubResult(kind, result) }, event.origin);
    })
    .catch((err: unknown) => {
      window.postMessage(
        { source: EXT, direction: "response", id, error: err instanceof Error ? err.message : String(err) },
        event.origin,
      );
    });
});

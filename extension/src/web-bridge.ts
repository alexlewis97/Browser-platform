/**
 * Relays the official website's postMessage calls into the extension background
 * so the admin Record tab can start/stop a recording in this Chrome.
 */
const SITE = "bp-web";
const EXT = "bp-ext";

window.postMessage({ source: EXT, direction: "hello", ok: true }, "*");

window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  const data = event.data as { source?: string; direction?: string; kind?: string; id?: string; payload?: Record<string, unknown> } | null;
  if (!data || data.source !== SITE || data.direction !== "request" || !data.kind) return;
  const id = data.id;
  void chrome.runtime
    .sendMessage({ kind: data.kind, ...(data.payload || {}) })
    .then((result) => {
      window.postMessage({ source: EXT, direction: "response", id, result }, "*");
    })
    .catch((err: unknown) => {
      window.postMessage({ source: EXT, direction: "response", id, error: err instanceof Error ? err.message : String(err) }, "*");
    });
});

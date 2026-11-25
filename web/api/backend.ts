import { accessTokenOk } from "./_lib/auth.js";
import { json, options, unauthorized } from "./_lib/cors.js";
import { normalizeBackendUrl, readDiscovery, writeDiscovery } from "./_lib/discovery.js";

export function OPTIONS(request: Request): Response {
  return options(request);
}

export async function GET(request: Request): Promise<Response> {
  try {
    const discovery = await readDiscovery();
    if (!discovery) return json(request, { error: "No backend URL published yet." }, 404);
    return json(request, discovery, 200, { "cache-control": "no-store" });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Failed to read backend discovery.";
    return json(request, { error: message }, 500);
  }
}

export async function POST(request: Request): Promise<Response> {
  if (!accessTokenOk(request)) return unauthorized(request);
  try {
    const body = (await request.json()) as { backendUrl?: string; url?: string; updatedAt?: string };
    const url = normalizeBackendUrl(body.backendUrl || body.url);
    if (!url) return json(request, { error: "backendUrl must be a trycloudflare, sslip, or localhost URL." }, 400);
    const discovery = await writeDiscovery(url, typeof body.updatedAt === "string" ? body.updatedAt : undefined);
    return json(request, discovery, 200, { "cache-control": "no-store" });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Failed to publish backend URL.";
    return json(request, { error: message }, 500);
  }
}

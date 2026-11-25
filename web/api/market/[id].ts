import { accessTokenOk } from "../_lib/auth.js";
import { json, options, unauthorized } from "../_lib/cors.js";
import { getListing, removeListing, safeId } from "../_lib/store.js";

function idFromUrl(request: Request): string | null {
  const parts = new URL(request.url).pathname.split("/").filter(Boolean);
  const idx = parts.indexOf("market");
  const raw = idx >= 0 ? parts[idx + 1] : parts.at(-1);
  return raw ? safeId(decodeURIComponent(raw)) : null;
}

export function OPTIONS(request: Request): Response {
  return options(request);
}

export async function GET(request: Request): Promise<Response> {
  if (!accessTokenOk(request)) return unauthorized(request);
  const id = idFromUrl(request);
  if (!id) return json(request, { error: "Not found" }, 404);
  try {
    const listing = await getListing(id);
    if (!listing) return json(request, { error: "Not found" }, 404);
    return json(request, { listing });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Failed to load listing.";
    return json(request, { error: message }, 500);
  }
}

export async function DELETE(request: Request): Promise<Response> {
  if (!accessTokenOk(request)) return unauthorized(request);
  const id = idFromUrl(request);
  if (!id) return json(request, { error: "Not found" }, 404);
  try {
    const removed = await removeListing(id);
    if (!removed) return json(request, { error: "Not found" }, 404);
    return json(request, { ok: true, id });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Failed to delete listing.";
    return json(request, { error: message }, 500);
  }
}

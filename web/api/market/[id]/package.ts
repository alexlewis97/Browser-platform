import { accessTokenOk } from "../../_lib/auth.js";
import { corsHeaders, json, options, unauthorized } from "../../_lib/cors.js";
import { readPackage, safeId, sha256Hex } from "../../_lib/store.js";

function idFromUrl(request: Request): string | null {
  const parts = new URL(request.url).pathname.split("/").filter(Boolean);
  const idx = parts.indexOf("market");
  const raw = idx >= 0 ? parts[idx + 1] : "";
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
    const found = await readPackage(id);
    if (!found) return json(request, { error: "Not found" }, 404);
    const digest = sha256Hex(found.bytes);
    if (found.listing.sha256 && found.listing.sha256 !== digest) {
      return json(request, { error: "Stored package hash mismatch." }, 409);
    }
    const filename = `${found.listing.id}-${found.listing.version}.zip`;
    const headers = corsHeaders(request, {
      "content-type": "application/zip",
      "content-disposition": `attachment; filename="${filename}"`,
      "x-package-sha256": digest,
      "cache-control": "no-store",
    });
    return new Response(new Uint8Array(found.bytes), { status: 200, headers });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Failed to download package.";
    return json(request, { error: message }, 500);
  }
}

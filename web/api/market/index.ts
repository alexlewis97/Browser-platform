import { accessTokenOk } from "../_lib/auth.js";
import { parsePublishBody } from "../_lib/body.js";
import { json, options, unauthorized } from "../_lib/cors.js";
import { readIndex, saveListing } from "../_lib/store.js";

export function OPTIONS(request: Request): Response {
  return options(request);
}

export async function GET(request: Request): Promise<Response> {
  if (!accessTokenOk(request)) return unauthorized(request);
  try {
    const listings = await readIndex();
    return json(request, { listings });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Failed to list marketplace items.";
    return json(request, { error: message }, 500);
  }
}

export async function POST(request: Request): Promise<Response> {
  if (!accessTokenOk(request)) return unauthorized(request);
  try {
    const { listing, packageBytes } = await parsePublishBody(request);
    if (!packageBytes.length) return json(request, { error: "Package file is empty." }, 400);
    const stored = await saveListing(listing, packageBytes);
    return json(request, { listing: stored }, 201);
  } catch (err) {
    const message = err instanceof Error ? err.message : "Publish failed.";
    const status = /sha256|required|name|inputs|workflow|Pomerado|zip/i.test(message) ? 400 : 500;
    return json(request, { error: message }, status);
  }
}

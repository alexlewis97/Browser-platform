import { timingSafeEqual } from "node:crypto";

const TOKEN_HEADERS = ["x-browser-platform-token", "authorization"];

export function expectedAccessToken(): string {
  return (
    (process.env.BROWSER_PLATFORM_USER_TOKEN || process.env.BROWSER_PLATFORM_ACCESS_TOKEN || "").trim()
  );
}

export function providedAccessToken(request: Request): string {
  for (const name of TOKEN_HEADERS) {
    const header = request.headers.get(name) || "";
    if (!header) continue;
    if (header.length >= 7 && header.slice(0, 7).toLowerCase() === "bearer ") {
      return header.slice(7).trim();
    }
    return header.trim();
  }
  const url = new URL(request.url);
  return (url.searchParams.get("access_token") || url.searchParams.get("token") || "").trim();
}

function tokensEqual(provided: string, expected: string): boolean {
  const a = Buffer.from(provided);
  const b = Buffer.from(expected);
  if (a.length !== b.length || a.length === 0) {
    // Keep the compare constant-time-ish even on length mismatch.
    const dummy = Buffer.alloc(32, 0);
    timingSafeEqual(dummy, dummy);
    return false;
  }
  return timingSafeEqual(a, b);
}

/** True when the request carries Alex's access token. Never logs the token. */
export function accessTokenOk(request: Request): boolean {
  const expected = expectedAccessToken();
  if (!expected) return false;
  const provided = providedAccessToken(request);
  if (!provided) return false;
  return tokensEqual(provided, expected);
}

const ALLOWED = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$|^https:\/\/([a-z0-9-]+\.)*vercel\.app$|^chrome-extension:\/\/[a-p]{32}$/i;

export function allowOrigin(request: Request): string | null {
  const origin = request.headers.get("origin");
  if (origin && ALLOWED.test(origin)) return origin;
  return null;
}

export function corsHeaders(request: Request, extra: Record<string, string> = {}): Headers {
  const headers = new Headers(extra);
  const origin = allowOrigin(request);
  if (origin) {
    headers.set("Access-Control-Allow-Origin", origin);
    headers.set("Vary", "Origin");
  }
  headers.set("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Browser-Platform-Token");
  headers.set("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
  return headers;
}

export function json(request: Request, body: unknown, status = 200, extra: Record<string, string> = {}): Response {
  const headers = corsHeaders(request, { "content-type": "application/json; charset=utf-8", ...extra });
  return new Response(JSON.stringify(body), { status, headers });
}

export function empty(request: Request, status = 204): Response {
  return new Response(null, { status, headers: corsHeaders(request) });
}

export function unauthorized(request: Request): Response {
  return json(request, { error: "Unauthorized" }, 401, { "WWW-Authenticate": "Bearer" });
}

export function options(request: Request): Response {
  return empty(request, 204);
}

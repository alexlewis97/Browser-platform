"""Local-mode request checks (loopback vs tunneled / public)."""

from __future__ import annotations

import hmac
import ipaddress
import logging
import re
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)
_warned_identical_tokens = False

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "[::1]"})

# Exact localhost / loopback origins only. Production origins come from settings.cors_origins.
CORS_ORIGIN_REGEX = (
    r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
)
DEFAULT_CORS_ORIGINS = ("https://browser-platform-web.vercel.app", "https://browser-platform-run.vercel.app")

_FORWARD_HEADERS = (
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-forwarded-host",
    "x-real-ip",
    "cf-connecting-ip",
    "forwarded",
)


def hostname_without_port(host: str | None) -> str:
    value = (host or "").strip().lower()
    if not value:
        return ""
    if value.startswith("[") and "]" in value:
        return value[1 : value.index("]")]
    # IPv4 or hostname with an optional :port — but not raw IPv6 (which has multiple colons).
    if value.count(":") == 1 and not value.startswith(":"):
        return value.split(":", 1)[0]
    return value


def is_loopback_host(host: str | None) -> bool:
    value = hostname_without_port(host)
    if not value:
        return False
    if value in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def peer_host(connection: Any) -> str:
    client = getattr(connection, "client", None)
    if client is None:
        return ""
    return getattr(client, "host", None) or (client[0] if isinstance(client, (tuple, list)) else "") or ""


def request_hostname(connection: Any) -> str:
    headers = getattr(connection, "headers", None) or {}
    host = ""
    try:
        host = headers.get("host") or ""
    except Exception:
        host = ""
    if not host:
        url = getattr(connection, "url", None)
        host = getattr(url, "hostname", None) or ""
    return hostname_without_port(host)


def has_proxy_headers(connection: Any) -> bool:
    headers = getattr(connection, "headers", None) or {}
    try:
        return any(headers.get(name) for name in _FORWARD_HEADERS)
    except Exception:
        return False


def is_local_request(connection: Any, settings: Any | None = None) -> bool:
    """True when the caller is on loopback and not arriving via a reverse proxy/tunnel."""

    if has_proxy_headers(connection):
        return False
    if is_loopback_host(peer_host(connection)):
        return True
    bound = is_loopback_host(getattr(settings, "host", None) if settings is not None else None)
    if bound and is_loopback_host(request_hostname(connection)) and not has_proxy_headers(connection):
        return True
    return False


def provided_access_token(connection: Any, *, allow_query: bool = False) -> str:
    """Read the access token from headers (and optionally query — WebSocket only)."""

    headers = getattr(connection, "headers", None) or {}
    header = ""
    try:
        header = headers.get("x-browser-platform-token") or headers.get("authorization") or ""
    except Exception:
        header = ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    if header:
        return header.strip()
    if allow_query:
        query = getattr(connection, "query_params", None)
        if query is not None:
            return (query.get("access_token") or query.get("token") or "").strip()
    return ""


def access_token_ok(connection: Any, settings: Any | None, *, allow_query: bool = False) -> bool:
    """Authorize an API/WS call.

    - No token configured: allow only when the process is bound to loopback
      (local-only laptop mode). Public binds without a token are rejected at startup.
    - Token configured: required for every call by default, including localhost.
      Proxied/tunneled peers (X-Forwarded-*) always need the token. Set
      ``require_token_on_localhost=false`` only for trusted same-machine clients.
    """

    expected = _configured_tokens(settings)
    local = is_local_request(connection, settings)
    if not expected["admin"] and not expected["user"]:
        bound_loopback = is_loopback_host(getattr(settings, "host", None) if settings is not None else "127.0.0.1")
        # Bound to loopback ⇒ unreachable from the network; allow (incl. TestClient).
        if bound_loopback and not has_proxy_headers(connection):
            return True
        return local
    require_local = True
    if settings is not None:
        require_local = bool(getattr(settings, "require_token_on_localhost", True))
    if not require_local and local:
        return True
    provided = provided_access_token(connection, allow_query=allow_query)
    if not provided:
        return False
    return any(hmac.compare_digest(provided, token) for token in expected.values() if token)


def _configured_tokens(settings: Any | None) -> dict[str, str]:
    global _warned_identical_tokens
    admin = ((getattr(settings, "access_token", "") if settings is not None else "") or "").strip()
    user = ((getattr(settings, "user_token", "") if settings is not None else "") or "").strip()
    if admin and user and hmac.compare_digest(admin, user) and not _warned_identical_tokens:
        _warned_identical_tokens = True
        log.warning(
            "access_token and user_token are identical; /run stays user mode, "
            "admin only on /repair and /admin-run"
        )
    return {"admin": admin, "user": user}


def require_admin(connection: Any, settings: Any | None, *, allow_query: bool = False) -> bool:
    """True only for the explicit admin endpoints (/repair, /admin-run).

    When an access token is configured, the request must present that token.
    When no tokens are configured, only a *direct* loopback request (peer on
    loopback and no X-Forwarded-* / X-Real-IP headers) is admin. Traffic that
    arrives via Caddy cannot spoof that: forwarded headers, or a non-loopback
    peer, are not local.
    """

    tokens = _configured_tokens(settings)
    provided = provided_access_token(connection, allow_query=allow_query)
    if tokens["admin"]:
        return bool(provided and hmac.compare_digest(provided, tokens["admin"]))
    # No tokens: only a direct (unproxied) loopback process — same bar as
    # access_token_ok. Forwarded headers, including those Caddy adds, are not local.
    if is_local_request(connection, settings):
        return True
    bound = is_loopback_host(getattr(settings, "host", None) if settings is not None else "127.0.0.1")
    return bool(bound and not has_proxy_headers(connection))


def caller_role(connection: Any, settings: Any | None, *, allow_query: bool = False) -> str:
    """'admin' only when require_admin is true. Used by the admin endpoints.

    ``POST /run`` does not consult this: it is always user mode, even if the
    access token was presented (including when access and user tokens match).
    """

    return "admin" if require_admin(connection, settings, allow_query=allow_query) else "user"


def origin_allowed(origin: str | None, settings: Any | None) -> bool:
    """True when Origin is missing (non-browser) or matches localhost / configured list."""

    value = (origin or "").strip()
    if not value:
        return True
    if re.fullmatch(CORS_ORIGIN_REGEX, value):
        return True
    allowed = []
    if settings is not None:
        allowed = list(getattr(settings, "cors_origin_list", []) or [])
    if "*" in allowed:
        return True
    if value in allowed:
        return True
    # Optional explicit chrome-extension:// ids listed in cors_origins.
    if value.startswith("chrome-extension://") and value in allowed:
        return True
    return False


def validate_public_bind(settings: Any) -> None:
    """Raise if the process would listen publicly without an access token."""

    host = getattr(settings, "host", "127.0.0.1") or "127.0.0.1"
    token = (getattr(settings, "access_token", "") or "").strip()
    if not is_loopback_host(host) and not token:
        raise RuntimeError(
            f"BROWSER_PLATFORM_HOST={host} requires BROWSER_PLATFORM_ACCESS_TOKEN "
            "(refusing to bind a public interface without auth)"
        )


def normalize_backend_discovery_url(url: str | None, *, allowed_hosts: list[str] | None = None) -> str | None:
    """Accept only localhost, trycloudflare, or explicitly allowed hostnames."""

    raw = (url or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    if host in LOOPBACK_HOSTS or host.endswith(".trycloudflare.com") or host == "trycloudflare.com":
        return f"{parsed.scheme}://{parsed.netloc}"
    allowed = {h.lower() for h in (allowed_hosts or []) if h}
    if host in allowed:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


__all__ = [
    "CORS_ORIGIN_REGEX",
    "DEFAULT_CORS_ORIGINS",
    "access_token_ok",
    "caller_role",
    "require_admin",
    "has_proxy_headers",
    "hostname_without_port",
    "is_local_request",
    "is_loopback_host",
    "normalize_backend_discovery_url",
    "origin_allowed",
    "peer_host",
    "provided_access_token",
    "request_hostname",
    "validate_public_bind",
]

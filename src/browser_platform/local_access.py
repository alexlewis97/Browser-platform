"""Local-mode request checks (loopback vs tunneled / public)."""

from __future__ import annotations

import hmac
import ipaddress
from typing import Any

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "[::1]"}

# Exact origins plus this regex: any localhost/127.0.0.1 port, and Vercel HTTPS apps.
CORS_ORIGIN_REGEX = (
    r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?"
    r"|https://([a-z0-9-]+\.)*vercel\.app"
    r"|chrome-extension://[a-p]{32}"
)
DEFAULT_CORS_ORIGINS = ("https://browser-platform-web.vercel.app",)


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


def is_local_request(connection: Any, settings: Any | None = None) -> bool:
    """True when the caller is on loopback (the laptop talking to itself)."""

    if is_loopback_host(peer_host(connection)):
        return True
    bound = is_loopback_host(getattr(settings, "host", None) if settings is not None else None)
    if bound and is_loopback_host(request_hostname(connection)):
        return True
    return False


def provided_access_token(connection: Any) -> str:
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
    query = getattr(connection, "query_params", None)
    if query is not None:
        return (query.get("access_token") or query.get("token") or "").strip()
    return ""


def access_token_ok(connection: Any, settings: Any | None) -> bool:
    """Empty token always allows. Localhost is optional unless require_token_on_localhost."""

    expected = (getattr(settings, "access_token", "") if settings is not None else "") or ""
    if not expected:
        return True
    require_local = bool(getattr(settings, "require_token_on_localhost", False)) if settings is not None else False
    if not require_local and is_local_request(connection, settings):
        return True
    provided = provided_access_token(connection)
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)

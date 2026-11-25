"""Safe navigation URL policy (block file:// and private targets for remote browsers)."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _host_is_blocked(hostname: str) -> bool:
    host = (hostname or "").strip().lower().rstrip(".")
    if not host:
        return True
    if host in {"localhost", "metadata.google.internal"}:
        return True
    if host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        # Unresolvable — reject rather than letting Playwright try odd schemes/hosts.
        return True
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_private
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
    return False


def assert_safe_navigation_url(url: str | None, *, allow_private: bool = False) -> str:
    """Return a cleaned http(s) URL or raise ValueError.

    When ``allow_private`` is True (local laptop Chrome), private/loopback hosts
    are permitted so users can hit intranet apps. Remote-record / cloud browsers
    should keep the default False.
    """

    value = (url or "").strip()
    if not value:
        raise ValueError("navigation URL is empty")
    parsed = urlparse(value)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"navigation scheme '{scheme or 'none'}' is not allowed (http/https only)")
    host = parsed.hostname or ""
    if not allow_private and _host_is_blocked(host):
        raise ValueError(f"navigation host '{host}' is blocked (private/loopback/link-local)")
    return value


__all__ = ["assert_safe_navigation_url"]

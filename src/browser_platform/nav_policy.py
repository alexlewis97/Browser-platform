"""Safe navigation URL policy (block file:// and private targets for remote browsers)."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_ALLOWED_SCHEMES = frozenset({"http", "https"})
# RFC 2544 benchmarking range. Some sandboxes resolve public hostnames here
# through a transparent proxy; that is not an SSRF target.
_INTERCEPTOR_NETS = (ipaddress.ip_network("198.18.0.0/15"),)


def _is_literal_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _ip_is_dangerous(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, hostname_is_ip: bool) -> bool:
    # Python 3.13 treats 198.18.0.0/15 as private; sandboxes often resolve
    # public names there through a proxy, which is not an SSRF target.
    if not hostname_is_ip and any(ip in net for net in _INTERCEPTOR_NETS):
        return False
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


def _host_is_blocked(hostname: str) -> bool:
    host = (hostname or "").strip().lower().rstrip(".")
    if not host:
        return True
    if host in {"localhost", "metadata.google.internal"}:
        return True
    if host.endswith(".localhost") or host.endswith(".local"):
        return True
    hostname_is_ip = _is_literal_ip(host)
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
        if _ip_is_dangerous(ip, hostname_is_ip=hostname_is_ip):
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

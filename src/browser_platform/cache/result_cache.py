"""Short-lived cache for identical read-only extract results on the same URL."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from browser_platform.cache.action_cache import url_pattern


@dataclass
class _Entry:
    value: Any
    expires_at: float


def _variables_key(variables: dict[str, Any] | None) -> str:
    if not variables:
        return ""
    try:
        payload = json.dumps(variables, sort_keys=True, default=str, separators=(",", ":"))
    except Exception:
        payload = str(sorted((str(k), str(v)) for k, v in variables.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _url_with_query(url: str | None) -> str:
    """Origin+path plus a stable query fingerprint (order-independent)."""

    base = url_pattern(url)
    if not url:
        return base
    try:
        parts = urlsplit(url)
        query = "&".join(sorted(q for q in parts.query.split("&") if q))
        return urlunsplit(("", "", base, query, ""))
    except Exception:
        return base


@dataclass
class ResultCache:
    ttl_seconds: int = 300
    _items: dict[tuple[str, int, str, str, str], _Entry] = field(default_factory=dict)

    def _key(
        self,
        workflow_id: str,
        step_index: int,
        url: str | None,
        goal: str,
        variables: dict[str, Any] | None = None,
        *,
        namespace: str = "",
    ) -> tuple[str, int, str, str, str]:
        return (workflow_id, step_index, _url_with_query(url), goal, f"{namespace}:{_variables_key(variables)}")

    def get(
        self,
        workflow_id: str,
        step_index: int,
        url: str | None,
        goal: str,
        variables: dict[str, Any] | None = None,
        *,
        namespace: str = "",
    ) -> Any | None:
        key = self._key(workflow_id, step_index, url, goal, variables, namespace=namespace)
        entry = self._items.get(key)
        if not entry:
            return None
        if entry.expires_at < time.time():
            self._items.pop(key, None)
            return None
        return entry.value

    def put(
        self,
        workflow_id: str,
        step_index: int,
        url: str | None,
        goal: str,
        value: Any,
        variables: dict[str, Any] | None = None,
        *,
        namespace: str = "",
    ) -> None:
        self._items[self._key(workflow_id, step_index, url, goal, variables, namespace=namespace)] = _Entry(
            value=value,
            expires_at=time.time() + self.ttl_seconds,
        )

    def clear(self) -> None:
        self._items.clear()

"""Short-lived cache for identical read-only extract results on the same URL."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from browser_platform.cache.action_cache import url_pattern


@dataclass
class _Entry:
    value: Any
    expires_at: float


@dataclass
class ResultCache:
    ttl_seconds: int = 300
    _items: dict[tuple[str, int, str, str, str], _Entry] = field(default_factory=dict)

    def _vars_part(self, variables: dict[str, Any] | None) -> str:
        items = {str(k): str(v) for k, v in sorted((variables or {}).items()) if v is not None}
        return json.dumps(items, sort_keys=True, ensure_ascii=False)

    def _key(
        self,
        workflow_id: str,
        step_index: int,
        url: str | None,
        goal: str,
        variables: dict[str, Any] | None = None,
    ) -> tuple[str, int, str, str, str]:
        return (workflow_id, step_index, url_pattern(url), goal, self._vars_part(variables))

    def get(
        self,
        workflow_id: str,
        step_index: int,
        url: str | None,
        goal: str,
        variables: dict[str, Any] | None = None,
    ) -> Any | None:
        key = self._key(workflow_id, step_index, url, goal, variables)
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
    ) -> None:
        self._items[self._key(workflow_id, step_index, url, goal, variables)] = _Entry(
            value=value,
            expires_at=time.time() + self.ttl_seconds,
        )

    def clear(self) -> None:
        self._items.clear()

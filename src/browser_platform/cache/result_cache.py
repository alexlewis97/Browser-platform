"""Short-lived cache for identical read-only extract results on the same URL."""

from __future__ import annotations

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
    _items: dict[tuple[str, int, str, str], _Entry] = field(default_factory=dict)

    def _key(self, workflow_id: str, step_index: int, url: str | None, goal: str) -> tuple[str, int, str, str]:
        return (workflow_id, step_index, url_pattern(url), goal)

    def get(self, workflow_id: str, step_index: int, url: str | None, goal: str) -> Any | None:
        key = self._key(workflow_id, step_index, url, goal)
        entry = self._items.get(key)
        if not entry:
            return None
        if entry.expires_at < time.time():
            self._items.pop(key, None)
            return None
        return entry.value

    def put(self, workflow_id: str, step_index: int, url: str | None, goal: str, value: Any) -> None:
        self._items[self._key(workflow_id, step_index, url, goal)] = _Entry(
            value=value,
            expires_at=time.time() + self.ttl_seconds,
        )

    def clear(self) -> None:
        self._items.clear()

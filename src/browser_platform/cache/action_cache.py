"""Stagehand-style action cache: reuse the last selector that worked.

Keyed by workflow + step index + a stable URL pattern (origin + path, no query).
This avoids re-running the full selector chain on repeated runs of the same page.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from browser_platform.models.workflow import SelectorStrategy


def url_pattern(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def cache_key(workflow_id: str, step_index: int, url: str | None) -> tuple[str, int, str]:
    return (workflow_id, step_index, url_pattern(url))


@dataclass
class ActionCache:
    _items: dict[tuple[str, int, str], SelectorStrategy] | None = None

    def __post_init__(self) -> None:
        self._items = {} if self._items is None else self._items

    def get(self, workflow_id: str, step_index: int, url: str | None) -> SelectorStrategy | None:
        return self._items.get(cache_key(workflow_id, step_index, url))

    def put(self, workflow_id: str, step_index: int, url: str | None, strategy: SelectorStrategy) -> None:
        self._items[cache_key(workflow_id, step_index, url)] = strategy

    def clear(self) -> None:
        self._items.clear()

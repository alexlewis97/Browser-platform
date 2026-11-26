"""Stagehand-style action cache: reuse the last selector that worked.

Keyed by workflow + step index + a stable URL pattern (origin + path, no query).
This avoids re-running the full selector chain on repeated runs of the same page.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from browser_platform.models.workflow import SelectorStrategy


def url_pattern(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _vars_part(variables: dict[str, Any] | None) -> str:
    items = {str(k): str(v) for k, v in sorted((variables or {}).items()) if v is not None}
    return json.dumps(items, sort_keys=True, ensure_ascii=False)


def cache_key(
    workflow_id: str,
    step_index: int,
    url: str | None,
    variables: dict[str, Any] | None = None,
) -> tuple[str, int, str, str]:
    return (workflow_id, step_index, url_pattern(url), _vars_part(variables))


@dataclass
class ActionCache:
    _items: dict[tuple[str, int, str, str], SelectorStrategy] | None = None

    def __post_init__(self) -> None:
        self._items = {} if self._items is None else self._items

    def get(
        self,
        workflow_id: str,
        step_index: int,
        url: str | None,
        variables: dict[str, Any] | None = None,
    ) -> SelectorStrategy | None:
        return self._items.get(cache_key(workflow_id, step_index, url, variables))

    def put(
        self,
        workflow_id: str,
        step_index: int,
        url: str | None,
        strategy: SelectorStrategy,
        variables: dict[str, Any] | None = None,
    ) -> None:
        self._items[cache_key(workflow_id, step_index, url, variables)] = strategy

    def clear(self) -> None:
        self._items.clear()

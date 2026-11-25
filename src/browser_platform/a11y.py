"""Compress Playwright accessibility snapshots so LLM repair uses fewer tokens."""

from __future__ import annotations

from typing import Any

_SKIP_ROLES = frozenset({"none", "presentation", "ignored", "generic"})
_MAX_NAME = 120


def _iter_nodes(node: dict[str, Any] | None, depth: int = 0):
    if not node:
        return
    yield depth, node
    for child in node.get("children") or []:
        if isinstance(child, dict):
            yield from _iter_nodes(child, depth + 1)


def compress_ax_tree(snapshot: dict[str, Any] | None, *, max_chars: int = 4000) -> str:
    """Flatten an a11y tree into compact role/name lines, dropping noisy nodes."""

    if not snapshot:
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    for depth, node in _iter_nodes(snapshot):
        role = str(node.get("role") or "").strip()
        name = " ".join(str(node.get("name") or "").split())
        if not role or role.lower() in _SKIP_ROLES and not name:
            continue
        if name:
            name = name[:_MAX_NAME]
        line = f"{'  ' * min(depth, 6)}[{role}] {name}".rstrip()
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 16].rstrip() + "\n…[truncated]"
    return text

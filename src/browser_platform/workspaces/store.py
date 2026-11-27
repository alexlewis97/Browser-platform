"""Workspace model and JSON-file store.

A workspace is a named set of workflows (many-to-many: a workflow can sit in several).
It scopes everything that offers tools: skill retrieval and the chat planner, /api/skills,
MCP tools/list and tools/call, and the public run-site catalogue. ``instructions`` is an
optional per-workspace system prompt, so a workspace can behave as one assistant for a whole
site or app. Replay stays deterministic; workspaces only change what the planner may choose.

Kept deliberately small. ``kind`` and ``access`` are reserved for a future marketplace
(installed packs) and per-workspace permissions; today every workspace is "custom"/"private".
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Any

from pydantic import BaseModel, Field

ALL = "all"
_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    slug = _SLUG.sub("-", (name or "").strip().lower()).strip("-")
    return slug[:48] or "workspace"


class Workspace(BaseModel):
    id: str
    slug: str
    name: str
    description: str = ""
    instructions: str = ""
    workflow_ids: list[str] = Field(default_factory=list)
    kind: str = "custom"  # reserved: "marketplace" for installed packs
    access: str = "private"  # reserved: per-workspace permissions
    created_at: str = ""
    updated_at: str = ""

    def summary(self) -> dict[str, Any]:
        data = self.model_dump()
        data["workflow_count"] = len(self.workflow_ids)
        return data


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class WorkspaceStore:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "workspaces.json"
        self._lock = Lock()

    # ---- persistence -------------------------------------------------
    def _load(self) -> list[Workspace]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return []
        return [Workspace.model_validate(item) for item in raw.get("workspaces") or []]

    def _save(self, items: list[Workspace]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"workspaces": [w.model_dump() for w in items]}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    # ---- queries -----------------------------------------------------
    def list(self) -> list[Workspace]:
        return self._load()

    def get(self, key: str) -> Workspace | None:
        key = (key or "").strip()
        for ws in self._load():
            if key in {ws.id, ws.slug}:
                return ws
        return None

    def for_workflow(self, workflow_id: str) -> list[Workspace]:
        return [ws for ws in self._load() if workflow_id in ws.workflow_ids]

    # ---- mutations ---------------------------------------------------
    def create(self, name: str, *, description: str = "", instructions: str = "", workflow_ids: list[str] | None = None, slug: str | None = None) -> Workspace:
        name = (name or "").strip()
        if not name:
            raise ValueError("name is required")
        with self._lock:
            items = self._load()
            base = slugify(slug or name)
            if base == ALL:
                base = "all-workspace"
            taken = {w.slug for w in items}
            final, n = base, 2
            while final in taken:
                final, n = f"{base}-{n}", n + 1
            ws = Workspace(
                id=uuid.uuid4().hex[:12],
                slug=final,
                name=name[:80],
                description=(description or "")[:600],
                instructions=(instructions or "")[:4000],
                workflow_ids=list(dict.fromkeys(workflow_ids or [])),
                created_at=_now(),
                updated_at=_now(),
            )
            items.append(ws)
            self._save(items)
            return ws

    def update(self, key: str, **changes: Any) -> Workspace:
        with self._lock:
            items = self._load()
            for index, ws in enumerate(items):
                if key in {ws.id, ws.slug}:
                    data = ws.model_dump()
                    for field in ("name", "description", "instructions"):
                        if changes.get(field) is not None:
                            data[field] = str(changes[field]).strip()
                    if not data["name"]:
                        raise ValueError("name is required")
                    if changes.get("workflow_ids") is not None:
                        data["workflow_ids"] = list(dict.fromkeys(str(i) for i in changes["workflow_ids"]))
                    data["updated_at"] = _now()
                    items[index] = Workspace.model_validate(data)
                    self._save(items)
                    return items[index]
        raise KeyError(key)

    def add(self, key: str, workflow_ids: list[str]) -> Workspace:
        ws = self.get(key)
        if ws is None:
            raise KeyError(key)
        return self.update(ws.id, workflow_ids=[*ws.workflow_ids, *workflow_ids])

    def remove(self, key: str, workflow_ids: list[str]) -> Workspace:
        ws = self.get(key)
        if ws is None:
            raise KeyError(key)
        drop = set(workflow_ids)
        return self.update(ws.id, workflow_ids=[i for i in ws.workflow_ids if i not in drop])

    def delete(self, key: str) -> None:
        with self._lock:
            items = self._load()
            kept = [w for w in items if key not in {w.id, w.slug}]
            if len(kept) == len(items):
                raise KeyError(key)
            self._save(kept)

    def forget_workflow(self, workflow_id: str) -> None:
        """Drop a deleted workflow from every workspace."""

        with self._lock:
            items = self._load()
            changed = False
            for ws in items:
                if workflow_id in ws.workflow_ids:
                    ws.workflow_ids = [i for i in ws.workflow_ids if i != workflow_id]
                    ws.updated_at = _now()
                    changed = True
            if changed:
                self._save(items)

    def on_store_change(self, event: str, workflow_id: str) -> None:
        if event == "deleted":
            self.forget_workflow(workflow_id)


def resolve_scope(store: WorkspaceStore | None, key: str | None) -> Workspace | None:
    """None/''/'all' means the whole library; an unknown key raises KeyError."""

    key = (key or "").strip()
    if not key or key.lower() == ALL:
        return None
    ws = store.get(key) if store is not None else None
    if ws is None:
        raise KeyError(key)
    return ws

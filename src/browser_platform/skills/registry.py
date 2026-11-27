"""Skill registry: one manifest per saved workflow, stored beside the workflows."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from threading import Lock
from typing import Any

from browser_platform.authoring.tools import tools_from_workflows
from browser_platform.llm.openrouter import chat_complete, env_api_key, merge_usage, message_text
from browser_platform.models.workflow import StoredWorkflow
from browser_platform.skills.manifest import (
    ENRICH_SYSTEM,
    apply_enrichment,
    base_manifest,
    card,
    enrich_prompt,
    fingerprint,
    parse_json_object,
)
from browser_platform.skills.retrieve import Embedder, rank_skills
from browser_platform.store.file_store import safe_workflow_id


class SkillRegistry:
    def __init__(self, store: Any, *, embedder: Embedder | None = None):
        self.store = store
        data_dir = Path(getattr(store, "data_dir", "data"))
        self.dir = data_dir / "skills"
        self.embedder = embedder if embedder is not None else Embedder(self.dir / "embeddings.json")
        self._lock = Lock()
        self._pending: set[str] = set()
        self._stats: tuple[float, dict[str, list[int]]] = (0.0, {})

    # ---- storage -------------------------------------------------------
    def _path(self, wf_id: str) -> Path:
        return self.dir / f"{safe_workflow_id(wf_id)}.skill.json"

    def _read(self, wf_id: str) -> dict[str, Any] | None:
        path = self._path(wf_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write(self, manifest: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(manifest["id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        tmp.replace(path)

    def forget(self, wf_id: str) -> None:
        try:
            self._path(wf_id).unlink()
        except OSError:
            pass

    # ---- catalog -------------------------------------------------------
    def tools(self) -> list[dict[str, Any]]:
        return tools_from_workflows(self.store.list_workflows())

    def _merge(self, stored: StoredWorkflow, tool: dict[str, Any]) -> dict[str, Any]:
        fn = tool["function"]
        fresh = base_manifest(
            stored,
            slug=fn["name"],
            action_ending=bool(tool.get("action_ending")),
            input_schema=fn.get("parameters") or {"type": "object", "properties": {}},
        )
        saved = self._read(stored.id)
        if saved and saved.get("fingerprint") == fresh["fingerprint"] and saved.get("manifest_version") == fresh["manifest_version"]:
            # Slug can shift when duplicates appear or vanish; schema comes from the live definition.
            saved.update({"slug": fresh["slug"], "input_schema": fresh["input_schema"], "action_ending": fresh["action_ending"], "read_only": fresh["read_only"]})
            if not saved.get("enriched"):
                saved["inputs"] = fresh["inputs"]
            return saved
        if saved and (saved.get("enriched") or saved.get("stale")):
            # Definition changed (e.g. explore added month): keep prose, refresh structure, re-enrich later.
            keep = {k: saved[k] for k in ("summary", "description", "tags", "example_request") if saved.get(k)}
            fresh.update(keep)
            fresh["stale"] = True
        with self._lock:
            self._write(fresh)
        return fresh

    def catalog(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return (tools, manifests) in the same order; manifests[i] describes tools[i]."""

        tools = self.tools()
        manifests = []
        for tool in tools:
            try:
                stored = self.store.get_workflow(str(tool.get("workflow_id")))
            except Exception:
                continue
            manifest = self._merge(stored, tool)
            manifest["primary"] = bool(tool.get("primary", True))
            manifests.append(manifest)
        return tools, manifests

    def manifests(self) -> list[dict[str, Any]]:
        return self.catalog()[1]

    def get(self, key: str) -> dict[str, Any] | None:
        for manifest in self.manifests():
            if key in {manifest.get("id"), manifest.get("slug")}:
                return manifest
        return None

    def run_stats(self, ttl: float = 60.0) -> dict[str, list[int]]:
        """{workflow_id: [succeeded, total]} from run history, cached briefly."""

        stamp, stats = self._stats
        if time.monotonic() - stamp < ttl:
            return stats
        stats = {}
        runs_dir = Path(getattr(self.store, "runs_dir", self.dir.parent / "runs"))
        for path in runs_dir.glob("*.json") if runs_dir.is_dir() else []:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            wf = str(data.get("workflow_id") or "")
            if not wf:
                continue
            row = stats.setdefault(wf, [0, 0])
            row[1] += 1
            if data.get("status") == "succeeded":
                row[0] += 1
        self._stats = (time.monotonic(), stats)
        return stats

    def reliability(self) -> dict[str, float]:
        return {wf: (ok + 1) / (total + 2) for wf, (ok, total) in self.run_stats().items()}

    def selection_pool(self, manifests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """One skill per capability: same site + same input names collapse to the most reliable.

        Saved libraries collect near-duplicates (several recordings of the same BBC
        fixtures flow). Showing all of them wastes planner tokens and invites the wrong pick,
        so keep the one with the best run history; the others stay callable by name.
        """

        stats = self.run_stats()
        groups: dict[tuple, list[dict[str, Any]]] = {}
        order: list[tuple] = []
        for m in manifests:
            if not m.get("primary", True):
                continue
            names = tuple(sorted((i.get("name") or "").lower() for i in m.get("inputs") or []))
            sig = (m.get("entry") or "", names, bool(m.get("action_ending")))
            if not m.get("entry") or not names:
                sig = (m.get("slug"),)
            if sig not in groups:
                order.append(sig)
            groups.setdefault(sig, []).append(m)
        pool = []
        for sig in order:
            rows = groups[sig]

            def score(m: dict[str, Any]) -> tuple:
                ok, total = stats.get(m["id"], [0, 0])
                return ((ok + 1) / (total + 2), ok, bool(m.get("enriched")))

            rows.sort(key=score, reverse=True)
            best = dict(rows[0])
            if len(rows) > 1:
                best["aliases"] = [r["slug"] for r in rows[1:]]
            pool.append(best)
        return pool

    async def search(self, query: str, *, k: int = 6, include_duplicates: bool = False) -> dict[str, Any]:
        _tools, manifests = self.catalog()
        pool = manifests if include_duplicates else self.selection_pool(manifests)
        return await rank_skills(query, pool, k=k, embedder=self.embedder, prior=self.reliability())

    # ---- enrichment ----------------------------------------------------
    async def enrich(self, wf_id: str, *, api_key: str | None = None, model: str | None = None) -> dict[str, Any]:
        manifest = self.get(wf_id)
        if manifest is None:
            raise KeyError(wf_id)
        key = api_key or env_api_key()
        if not key:
            return {"manifest": manifest, "usage": None}
        stored = self.store.get_workflow(manifest["id"])
        message, usage = await chat_complete(
            [{"role": "system", "content": ENRICH_SYSTEM}, {"role": "user", "content": enrich_prompt(manifest, stored.definition)}],
            max_tokens=350,
            response_format={"type": "json_object"},
            api_key=key,
            model=model,
        )
        data = parse_json_object(message_text(message))
        if data:
            manifest = apply_enrichment(manifest, data)
            manifest.pop("stale", None)
            manifest["fingerprint"] = fingerprint(stored.definition)
            manifest.pop("primary", None)
            with self._lock:
                self._write(manifest)
        return {"manifest": manifest, "usage": usage}

    async def backfill(self, *, force: bool = False, concurrency: int = 4, api_key: str | None = None) -> dict[str, Any]:
        manifests = self.manifests()
        todo = [m for m in manifests if force or not m.get("enriched") or m.get("stale")]
        sem = asyncio.Semaphore(concurrency)
        usages: list[dict[str, Any] | None] = []
        failed: list[str] = []

        async def one(m: dict[str, Any]) -> None:
            async with sem:
                try:
                    result = await self.enrich(m["id"], api_key=api_key)
                    usages.append(result.get("usage"))
                except Exception as exc:
                    failed.append(f"{m.get('slug')}: {type(exc).__name__}")

        await asyncio.gather(*[one(m) for m in todo])
        # Warm the embedding cache so the first chat request does not pay for it.
        warmed = False
        try:
            await self.search("warm up", k=1)
            warmed = self.embedder.enabled
        except Exception:
            pass
        return {"total": len(manifests), "enriched": len(todo) - len(failed), "failed": failed, "usage": merge_usage(*usages), "embeddings": warmed}

    def on_store_change(self, event: str, wf_id: str) -> None:
        """FileStore hook: drop manifests for deleted workflows, (re)enrich new or changed ones."""

        if event == "deleted":
            self.forget(wf_id)
            return
        saved = self._read(wf_id) or {}
        try:
            current = fingerprint(self.store.get_workflow(wf_id).definition)
        except Exception:
            return
        if saved.get("enriched") and saved.get("fingerprint") == current:
            return
        self.schedule_enrich(wf_id)

    def schedule_enrich(self, wf_id: str) -> None:
        """Fire-and-forget LLM manifest pass after a workflow is created or changed."""

        if not env_api_key() or wf_id in self._pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._pending.add(wf_id)

        async def run() -> None:
            try:
                await self.enrich(wf_id)
            except Exception:
                pass
            finally:
                self._pending.discard(wf_id)

        loop.create_task(run())


def compact_cards(manifests: list[dict[str, Any]]) -> str:
    return "\n".join(card(m) for m in manifests)


def mcp_tool(manifest: dict[str, Any]) -> dict[str, Any]:
    """MCP tools/list entry with annotations and the output shape."""

    desc = manifest.get("description") or manifest.get("summary") or ""
    if manifest.get("action_ending"):
        desc += " Ends in a click or submit; call with confirm=true."
    return {
        "name": manifest["slug"],
        "title": manifest.get("title"),
        "description": desc[:600],
        "inputSchema": manifest.get("input_schema") or {"type": "object", "properties": {}},
        "annotations": {
            "readOnlyHint": bool(manifest.get("read_only")),
            "destructiveHint": bool(manifest.get("action_ending")),
            "openWorldHint": True,
        },
        "_meta": {
            "workflow_id": manifest.get("id"),
            "summary": manifest.get("summary"),
            "site": manifest.get("site"),
            "tags": manifest.get("tags") or [],
            "example_request": manifest.get("example_request"),
            "output": manifest.get("output"),
        },
    }

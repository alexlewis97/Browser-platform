"""File-backed workflow and run store. API keys are never written."""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Any

from browser_platform.models.run import RunRecord
from browser_platform.models.workflow import StoredWorkflow, WorkflowDefinition
from browser_platform.redaction import redact

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def safe_workflow_id(raw: str | None) -> str:
    value = (raw or "").strip()
    if not value or not _SAFE_ID.fullmatch(value):
        raise ValueError("workflow id must match [A-Za-z0-9._-]{1,80}")
    return value


class FileStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.workflows_dir = data_dir / "workflows"
        self.runs_dir = data_dir / "runs"
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._index_path = self.workflows_dir / "index.json"
        # Callbacks (event, workflow_id) for created / updated / deleted, e.g. skill manifests.
        self.on_change: list = []
        if not self._index_path.exists():
            self._write_json(self._index_path, {})

    def _notify(self, event: str, wf_id: str) -> None:
        for callback in list(getattr(self, "on_change", None) or []):
            try:
                callback(event, wf_id)
            except Exception:
                continue

    def _read_json(self, path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(redact(payload), indent=2, default=str) + "\n", encoding="utf-8")
        tmp.replace(path)

    def _index(self) -> dict[str, Any]:
        if not self._index_path.exists():
            return {}
        return self._read_json(self._index_path)

    def _save_index(self, index: dict[str, Any]) -> None:
        self._write_json(self._index_path, index)

    def _workflow_file(self, workflow_id: str) -> Path:
        wf_id = safe_workflow_id(workflow_id)
        path = (self.workflows_dir / f"{wf_id}.workflow.json").resolve()
        root = self.workflows_dir.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("workflow path escapes data dir") from exc
        return path

    def create_workflow(self, definition: WorkflowDefinition, workflow_id: str | None = None) -> StoredWorkflow:
        with self._lock:
            raw = workflow_id or definition.id or uuid.uuid4().hex
            wf_id = safe_workflow_id(raw)
            path = self._workflow_file(wf_id)
            definition.id = wf_id
            self._write_json(path, definition.model_dump(exclude_none=True))
            index = self._index()
            index[wf_id] = {"path": str(path), "name": definition.name, "version": definition.version}
            self._save_index(index)
            stored = StoredWorkflow(id=wf_id, path=str(path), definition=definition)
        self._notify("created", wf_id)
        return stored

    def update_workflow(self, workflow_id: str, definition: WorkflowDefinition) -> StoredWorkflow:
        with self._lock:
            wf_id = safe_workflow_id(workflow_id)
            index = self._index()
            if wf_id not in index:
                raise KeyError(wf_id)
            path = self._workflow_file(wf_id)
            definition.id = wf_id
            self._write_json(path, definition.model_dump(exclude_none=True))
            index[wf_id] = {"path": str(path), "name": definition.name, "version": definition.version}
            self._save_index(index)
            stored = StoredWorkflow(id=wf_id, path=str(path), definition=definition)
        self._notify("updated", wf_id)
        return stored

    def snapshot_workflow_version(self, workflow_id: str, definition: WorkflowDefinition) -> Path:
        """Keep a copy of a workflow version before write-back overwrites it."""

        wf_id = safe_workflow_id(workflow_id)
        folder = self.data_dir / "workflow_versions"
        folder.mkdir(parents=True, exist_ok=True)
        version = re.sub(r"[^A-Za-z0-9._-]+", "_", str(definition.version or "1"))[:80] or "1"
        path = folder / f"{wf_id}.{version}.json"
        if path.exists():
            path = folder / f"{wf_id}.{version}.{int(time.time())}.json"
        payload = definition.model_copy(deep=True)
        payload.id = wf_id
        self._write_json(path, payload.model_dump(exclude_none=True))
        return path

    def _related_version_files(self, wf_id: str) -> list[Path]:
        """Version snapshots and backups for a workflow id, never another id's files."""
        # Only exact id or dotted suffixes (foo.workflow.json.bak, foo.v1.json).
        # A hyphen/underscore prefix would also match a different id such as foo-bar.
        folders = [
            self.workflows_dir,
            self.data_dir / "versions",
            self.data_dir / "workflow_versions",
            self.workflows_dir / "versions",
        ]
        found: list[Path] = []
        for folder in folders:
            if not folder.is_dir():
                continue
            for path in folder.iterdir():
                if not path.is_file():
                    continue
                name = path.name
                if name == wf_id or name.startswith(f"{wf_id}."):
                    found.append(path)
        return found

    def _flag_runs_deleted(self, wf_id: str) -> int:
        """Keep run history, but mark that the source workflow is gone."""
        flagged = 0
        for path in self.runs_dir.glob("*.json"):
            try:
                data = self._read_json(path)
            except Exception:
                continue
            if not isinstance(data, dict) or str(data.get("workflow_id") or "") != wf_id:
                continue
            if data.get("workflow_deleted") is True:
                flagged += 1
                continue
            data["workflow_deleted"] = True
            self._write_json(path, redact(data))
            flagged += 1
        return flagged

    def delete_workflow(self, workflow_id: str) -> int:
        """Remove the workflow document and related versions. Returns flagged run count."""
        with self._lock:
            wf_id = safe_workflow_id(workflow_id)
            index = self._index()
            if wf_id not in index:
                raise KeyError(wf_id)
            path = self._workflow_file(wf_id)
            if path.is_file():
                path.unlink()
            for extra in self._related_version_files(wf_id):
                try:
                    extra.unlink()
                except OSError:
                    continue
            index.pop(wf_id, None)
            self._save_index(index)
            flagged = self._flag_runs_deleted(wf_id)
        self._notify("deleted", wf_id)
        return flagged

    def _workflow_path(self, workflow_id: str, recorded: str | None = None) -> Path:
        """Prefer a recorded path under workflows_dir; never follow escapes."""
        wf_id = safe_workflow_id(workflow_id)
        preferred = self._workflow_file(wf_id)
        if preferred.is_file():
            return preferred
        if recorded:
            candidate = Path(recorded).resolve()
            try:
                candidate.relative_to(self.workflows_dir.resolve())
            except ValueError:
                return preferred
            if candidate.is_file():
                return candidate
        return preferred

    def get_workflow(self, workflow_id: str) -> StoredWorkflow:
        wf_id = safe_workflow_id(workflow_id)
        index = self._index()
        if wf_id not in index:
            raise KeyError(wf_id)
        path = self._workflow_path(wf_id, index[wf_id].get("path"))
        data = self._read_json(path)
        definition = WorkflowDefinition.model_validate(data)
        definition.id = wf_id
        return StoredWorkflow(id=wf_id, path=str(path), definition=definition)

    def list_workflows(self) -> list[StoredWorkflow]:
        index = self._index()
        items = []
        for wf_id in index:
            try:
                items.append(self.get_workflow(wf_id))
            except Exception:
                continue
        return items

    def save_run(self, record: RunRecord) -> RunRecord:
        with self._lock:
            path = self.runs_dir / f"{record.id}.json"
            payload = record.model_dump(mode="json")
            # Defense in depth: never write an api_key even if one is smuggled in extras.
            self._write_json(path, redact(payload))
            return record

    def get_run(self, run_id: str) -> RunRecord:
        path = self.runs_dir / f"{run_id}.json"
        if not path.exists():
            raise KeyError(run_id)
        return RunRecord.model_validate(self._read_json(path))

    def list_runs(self) -> list[RunRecord]:
        records = []
        for path in sorted(self.runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                records.append(RunRecord.model_validate(self._read_json(path)))
            except Exception:
                continue
        return records

"""File-backed workflow and run store. API keys are never written."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from threading import Lock
from typing import Any

from browser_platform.models.run import RunRecord
from browser_platform.models.workflow import StoredWorkflow, WorkflowDefinition
from browser_platform.redaction import redact


class FileStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.workflows_dir = data_dir / "workflows"
        self.runs_dir = data_dir / "runs"
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._index_path = self.workflows_dir / "index.json"
        if not self._index_path.exists():
            self._write_json(self._index_path, {})

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

    def create_workflow(self, definition: WorkflowDefinition, workflow_id: str | None = None) -> StoredWorkflow:
        with self._lock:
            wf_id = workflow_id or definition.id or uuid.uuid4().hex
            path = self.workflows_dir / f"{wf_id}.workflow.json"
            definition.id = wf_id
            self._write_json(path, definition.model_dump(exclude_none=True))
            index = self._index()
            index[wf_id] = {"path": str(path), "name": definition.name, "version": definition.version}
            self._save_index(index)
            return StoredWorkflow(id=wf_id, path=str(path), definition=definition)

    def update_workflow(self, workflow_id: str, definition: WorkflowDefinition) -> StoredWorkflow:
        with self._lock:
            index = self._index()
            if workflow_id not in index:
                raise KeyError(workflow_id)
            path = Path(index[workflow_id]["path"])
            definition.id = workflow_id
            self._write_json(path, definition.model_dump(exclude_none=True))
            index[workflow_id] = {"path": str(path), "name": definition.name, "version": definition.version}
            self._save_index(index)
            return StoredWorkflow(id=workflow_id, path=str(path), definition=definition)

    def _workflow_path(self, workflow_id: str, recorded: str | None = None) -> Path:
        """Prefer a recorded path, but fall back to this data dir after a host move."""
        candidates = []
        if recorded:
            candidates.append(Path(recorded))
        candidates.append(self.workflows_dir / f"{workflow_id}.workflow.json")
        for path in candidates:
            if path.is_file():
                return path
        return candidates[0]

    def get_workflow(self, workflow_id: str) -> StoredWorkflow:
        index = self._index()
        if workflow_id not in index:
            raise KeyError(workflow_id)
        path = self._workflow_path(workflow_id, index[workflow_id].get("path"))
        data = self._read_json(path)
        definition = WorkflowDefinition.model_validate(data)
        definition.id = workflow_id
        return StoredWorkflow(id=workflow_id, path=str(path), definition=definition)

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

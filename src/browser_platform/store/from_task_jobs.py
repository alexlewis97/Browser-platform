"""Durable from-task job status, so wait=false clients can poll after a process restart."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class FromTaskJobStore:
    """In-memory map with a JSON file per job under data_dir/from_task_jobs."""

    def __init__(self, directory: Path | None = None, cap: int = 200):
        self.dir = Path(directory) if directory else None
        self.cap = cap
        self._jobs: dict[str, Any] = {}

    def put(self, job: Any) -> None:
        job_id = getattr(job, "id", None) or (job.get("id") if isinstance(job, dict) else None)
        if not job_id:
            raise ValueError("from-task job is missing an id")
        self._jobs[str(job_id)] = job
        self._save(job, str(job_id))
        self._trim()

    def get(self, job_id: str) -> Any | None:
        job = self._jobs.get(job_id)
        if job is not None:
            return job
        if not self.dir:
            return None
        path = self.dir / f"{job_id}.json"
        if not path.is_file():
            return None
        from browser_platform.authoring.from_task import FromTaskResult

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        job = FromTaskResult.model_validate(payload)
        self._jobs[job_id] = job
        return job

    def _save(self, job: Any, job_id: str) -> None:
        if not self.dir:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            payload = job.model_dump(mode="json") if hasattr(job, "model_dump") else dict(job)
            path = self.dir / f"{job_id}.json"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    def _trim(self) -> None:
        if not self.dir or self.cap <= 0:
            return
        files = sorted(
            (p for p in self.dir.glob("*.json") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        extra = len(files) - self.cap
        if extra <= 0:
            return
        for path in files[:extra]:
            job_id = path.stem
            job = self._jobs.get(job_id)
            status = getattr(job, "status", None) if job is not None else None
            if status in {"running", "needs_confirmation", "needs_input"}:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            self._jobs.pop(job_id, None)

"""Run records and per-step telemetry. Never includes API keys."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Tier = Literal[
    "pomerado",
    "action_cache",
    "workflow_use",
    "selector",
    "heuristic",
    "result_cache",
    "llm",
    "browser_use",
]


class StepTelemetry(BaseModel):
    model_config = ConfigDict(extra="allow")

    step_index: int
    step_type: str
    description: str | None = None
    tier: Tier | None = None
    success: bool
    latency_ms: float
    tokens_in: int = 0
    tokens_out: int = 0
    selector_type: str | None = None
    selector_value: str | None = None
    error: str | None = None
    output: Any = None
    # How workflow-use resolved the step target (selector / fuzzy / alias / hitl).
    resolution: str | None = None


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    workflow_id: str
    workflow_name: str
    workflow_version: str
    status: Literal["running", "succeeded", "failed", "cancelled", "needs_confirmation"] = "running"
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    cdp_url: str | None = None
    model: str | None = None
    steps: list[StepTelemetry] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    write_back: bool = True
    confirmation: dict[str, Any] | None = None

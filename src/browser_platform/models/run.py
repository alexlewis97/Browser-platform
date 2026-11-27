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
    # succeeded_with_fallback: finished, but at least one step only worked through a
    # non-primary strategy (see ``fallbacks``) - the workflow needs repair.
    status: Literal[
        "running", "succeeded", "succeeded_with_fallback", "failed", "cancelled", "needs_confirmation", "needs_input"
    ] = "running"
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    cdp_url: str | None = None
    model: str | None = None
    steps: list[StepTelemetry] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    write_back: bool = True
    confirmation: dict[str, Any] | None = None
    workflow_deleted: bool = False
    # Every step resolved by something other than its recorded primary selector.
    fallbacks: list[dict[str, Any]] = Field(default_factory=list)
    needs_repair: bool = False
    strict: bool = False
    # Named failure kind when the run stopped on a guard (step_timeout, run_timeout, strict_selector, extract_missing).
    failure_kind: str | None = None


OK_STATUSES = frozenset({"succeeded", "succeeded_with_fallback"})


def run_ok(status: str | None) -> bool:
    """Finished with a result (possibly through a fallback that needs repair)."""

    return status in OK_STATUSES

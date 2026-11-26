"""Request / response models for the REST API. Keys are SecretStr and never returned."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from browser_platform.models.llm import LLMConfig
from browser_platform.models.workflow import WorkflowDefinition


class WorkflowCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    definition: WorkflowDefinition | None = None
    # Allow posting a raw workflow-use document at the top level.
    name: str | None = None
    description: str | None = None
    version: str | None = None
    steps: list[dict[str, Any]] | None = None
    input_schema: list[dict[str, Any]] | None = None


class WorkflowReplace(BaseModel):
    definition: WorkflowDefinition


class RunRequest(BaseModel):
    cdp_url: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    llm: LLMConfig | None = None
    write_back: bool = True
    wait: bool = True


class MintRequest(BaseModel):
    name: str
    url: str
    intent: str
    effect: str = "read"
    input: dict[str, Any] = Field(default_factory=dict)
    llm: LLMConfig | None = None
    cdp_url: str | None = None
    wait_s: float = 0


class ConfirmationRequest(BaseModel):
    """Resume a run that is waiting on a needs_confirmation event."""

    model_config = ConfigDict(extra="allow")

    action: str = "confirm"
    candidate_id: str | None = None
    text: str | None = None
    variables: dict[str, Any] | None = None


class FromTaskRequest(BaseModel):
    """Create a workflow-use document from a natural-language task (browser-use demonstration)."""

    model_config = ConfigDict(extra="allow")

    task: str
    variables: dict[str, Any] = Field(default_factory=dict)
    name: str | None = None
    start_url: str | None = None
    cdp_url: str | None = None
    llm: LLMConfig | None = None
    write_back: bool = True
    replay: bool = True
    wait: bool = True
    confirm_variables: bool | None = None
    explore: bool = True
    replay_variables: dict[str, Any] | None = None
    max_steps: int = 25
    timeout: float = 300


class FromTaskConfirmation(BaseModel):
    model_config = ConfigDict(extra="allow")

    action: str = "confirm"
    text: str | None = None
    variables: dict[str, Any] | None = None
    parameters: list[dict[str, Any]] | None = None
    promote: dict[str, str] | None = None


class FromRecordingRequest(BaseModel):
    """Clean a side-panel Chrome recording into a reviewable workflow."""

    model_config = ConfigDict(extra="allow")

    events: list[dict[str, Any]] = Field(default_factory=list)
    goal: str = ""
    name: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    llm: LLMConfig | None = None


class FromRecordingSaveRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = "Recorded workflow"
    goal: str = ""
    review: list[dict[str, Any]] = Field(default_factory=list)
    parameters: list[dict[str, Any]] | None = None
    variables: dict[str, Any] | None = None

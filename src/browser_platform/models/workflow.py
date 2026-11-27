"""workflow-use compatible workflow documents, plus platform metadata."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HitlMode = Literal["always_on_uncertain", "llm_then_human", "llm_only"]
HitlTimeoutPolicy = Literal["fail", "fall_to_llm"]


class FuzzyMatchConfig(BaseModel):
    """Thresholds are rapidfuzz scores on a 0–100 scale."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    threshold: float = 90.0
    margin: float = 8.0
    top_n: int = 5
    include_screenshot: bool = False


class HitlConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    mode: HitlMode = "always_on_uncertain"
    timeout_s: float = 300.0
    timeout_policy: HitlTimeoutPolicy = "fail"


class AliasMapping(BaseModel):
    """A human-confirmed query → element mapping reused on later runs."""

    model_config = ConfigDict(extra="allow")

    step_index: int
    query: str
    query_normalized: str
    chosen_text: str
    chosen_id: str | None = None
    cssSelector: str | None = None
    xpath: str | None = None
    role: str | None = None


WORKFLOW_USE_STEP_TYPES = (
    "navigation",
    "click",
    "input",
    "select_change",
    "key_press",
    "scroll",
    "go_back",
    "go_forward",
    "extract_page_content",
    "extract",
    "agent",
)

EXTRACT_TYPES = frozenset({"extract", "extract_page_content"})
ACTION_TYPES = frozenset({"click", "input", "select_change", "key_press"})
TERMINAL_TYPES = EXTRACT_TYPES | ACTION_TYPES


class WorkflowInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    type: Literal["string", "number", "bool"] = "string"
    format: str | None = None
    required: bool | None = None
    default: str | int | float | bool | None = None
    label: str | None = None
    description: str | None = None
    sensitive: bool | None = None


class ExpectedState(BaseModel):
    """Optional deterministic assertions (platform extension, ignored by stock workflow-use)."""

    model_config = ConfigDict(extra="allow")

    url_includes: str | None = None
    title_contains: str | None = None
    text_contains: str | None = None
    heading: str | None = None
    parameter_values: list[str] | None = None
    echo_values: list[str] | None = None
    field_selector: str | None = None
    field_value: str | None = None
    network_url_includes: str | None = None
    network_status: int | None = None
    description: str | None = None


class SelectorStrategy(BaseModel):
    """One entry in workflow-use selectorStrategies."""

    model_config = ConfigDict(extra="allow")

    type: str
    value: str = ""
    priority: int = 100
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkflowStep(BaseModel):
    """A single workflow-use step. Unknown fields are preserved (extra=allow)."""

    model_config = ConfigDict(extra="allow")

    type: str
    description: str | None = None
    output: str | None = None
    url: str | None = None
    value: str | None = None
    default_value: str | None = None
    selectedText: str | None = None
    key: str | None = None
    scrollX: int | None = None
    scrollY: int | None = None
    goal: str | None = None
    extractionGoal: str | None = None
    task: str | None = None
    max_steps: int | None = None
    target_text: str | None = None
    cssSelector: str | None = None
    xpath: str | None = None
    selectorStrategies: list[SelectorStrategy] | None = None
    cachedSelector: SelectorStrategy | None = None
    container_hint: str | None = None
    position_hint: str | None = None
    wait_time: float | None = None
    expected_state: ExpectedState | None = None
    pomerado_artifact: str | None = None
    pomerado_tool: str | None = None
    pomerado_input: dict[str, Any] | None = None
    fuzzy_query: str | None = None
    fuzzy: FuzzyMatchConfig | None = None
    hitl: HitlConfig | None = None

    def strategies(self) -> list[SelectorStrategy]:
        return list(self.selectorStrategies or [])


class RepairRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    step_index: int
    reason: str
    selector: SelectorStrategy
    previous_version: str
    new_version: str


class WorkflowDefinition(BaseModel):
    """Canonical on-disk document: workflow-use schema + platform extensions."""

    model_config = ConfigDict(extra="allow")

    name: str
    description: str = ""
    version: str = "1"
    default_wait_time: float | None = 0.1
    steps: list[WorkflowStep] = Field(..., min_length=1)
    input_schema: list[WorkflowInput] = Field(default_factory=list)
    workflow_analysis: str | None = None
    # Platform extensions (Pydantic v2 ignores unknown fields on workflow-use load)
    id: str | None = None
    pomerado_artifact: str | None = None
    pomerado_tool: str | None = None
    pomerado_input: dict[str, Any] | None = None
    mint_job_id: str | None = None
    cache_results: bool = True
    repair_history: list[RepairRecord] = Field(default_factory=list)
    fuzzy: FuzzyMatchConfig | None = None
    hitl: HitlConfig | None = None
    aliases: list[AliasMapping] = Field(default_factory=list)
    # None = platform default (settings.strict_selectors); True/False overrides it for this workflow.
    strict_selectors: bool | None = None
    # Visible on the end-user run site. Missing on existing documents => published.
    published: bool = True

    @field_validator("version", mode="before")
    @classmethod
    def _version_as_str(cls, value: Any) -> str:
        return str(value)

    @model_validator(mode="after")
    def _ends_with_terminal(self) -> "WorkflowDefinition":
        if not self.steps:
            raise ValueError("Workflow must have at least one step")
        last = self.steps[-1].type
        if last not in TERMINAL_TYPES:
            raise ValueError(
                "Workflow must end with an extract step or a final action "
                "(click, fill, select, or key press)."
            )
        return self

    def to_workflow_use_dict(self) -> dict[str, Any]:
        """Fields workflow-use's WorkflowDefinitionSchema understands."""
        payload = self.model_dump(
            exclude={
                "id",
                "pomerado_artifact",
                "pomerado_tool",
                "pomerado_input",
                "cache_results",
                "repair_history",
                "fuzzy",
                "hitl",
                "aliases",
                "strict_selectors",
                "published",
            },
            exclude_none=True,
        )
        return payload


class StoredWorkflow(BaseModel):
    id: str
    path: str
    definition: WorkflowDefinition

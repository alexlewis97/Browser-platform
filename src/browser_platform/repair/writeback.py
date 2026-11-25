"""Write a repaired selector back into a workflow-use workflow and bump version."""

from __future__ import annotations

from browser_platform.models.workflow import RepairRecord, SelectorStrategy, WorkflowDefinition
from browser_platform.versioning import bump_version


def write_back(
    definition: WorkflowDefinition,
    *,
    step_index: int,
    strategy: SelectorStrategy,
    reason: str,
) -> WorkflowDefinition:
    if step_index < 0 or step_index >= len(definition.steps):
        raise IndexError(f"step_index {step_index} is out of range")
    previous = definition.version
    new_version = bump_version(previous)
    step = definition.steps[step_index].model_copy(deep=True)
    # Prepend the winner so the next deterministic run hits it first.
    existing = list(step.selectorStrategies or [])
    winner = strategy.model_copy(update={"priority": 1, "metadata": {**strategy.metadata, "source": "llm_repair"}})
    # Avoid duplicating the exact same leading strategy.
    filtered = [s for s in existing if not (s.type == winner.type and s.value == winner.value)]
    step.selectorStrategies = [winner, *filtered]
    step.cachedSelector = winner
    definition.steps[step_index] = step
    definition.version = new_version
    history = list(definition.repair_history)
    history.append(
        RepairRecord(
            step_index=step_index,
            reason=reason,
            selector=winner,
            previous_version=previous,
            new_version=new_version,
        )
    )
    definition.repair_history = history
    return definition

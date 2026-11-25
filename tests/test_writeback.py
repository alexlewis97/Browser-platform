import json
from pathlib import Path

from browser_platform.models.workflow import SelectorStrategy, WorkflowDefinition, WorkflowStep
from browser_platform.repair.writeback import write_back
from browser_platform.store.file_store import FileStore


def _workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        name="demo",
        description="demo",
        version="1.0",
        steps=[
            WorkflowStep(type="navigation", url="https://example.com"),
            WorkflowStep(
                type="click",
                target_text="Old",
                selectorStrategies=[SelectorStrategy(type="text_exact", value="Old", priority=10)],
            ),
            WorkflowStep(type="extract", extractionGoal="heading"),
        ],
    )


def test_write_back_prepends_selector_and_bumps_version(data_dir: Path):
    definition = _workflow()
    winner = SelectorStrategy(type="role_text", value="Submit", priority=50, metadata={"role": "button"})
    updated = write_back(definition, step_index=1, strategy=winner, reason="text_exact missed")
    assert updated.version == "1.1"
    step = updated.steps[1]
    assert step.cachedSelector is not None
    assert step.cachedSelector.type == "role_text"
    assert step.cachedSelector.value == "Submit"
    assert step.selectorStrategies[0].type == "role_text"
    assert step.selectorStrategies[0].priority == 1
    assert any(s.type == "text_exact" and s.value == "Old" for s in step.selectorStrategies)
    assert updated.repair_history[-1].previous_version == "1.0"
    assert updated.repair_history[-1].new_version == "1.1"


def test_write_back_persists_into_workflow_use_file(data_dir: Path):
    store = FileStore(data_dir)
    stored = store.create_workflow(_workflow())
    updated = write_back(
        stored.definition,
        step_index=1,
        strategy=SelectorStrategy(type="css", value="button.submit"),
        reason="broken text",
    )
    stored = store.update_workflow(stored.id, updated)
    on_disk = json.loads(Path(stored.path).read_text())
    assert on_disk["version"] == "1.1"
    assert on_disk["steps"][1]["cachedSelector"]["type"] == "css"
    assert on_disk["steps"][1]["selectorStrategies"][0]["value"] == "button.submit"
    # workflow-use required fields still present
    assert on_disk["steps"][-1]["type"] == "extract"
    assert "api_key" not in json.dumps(on_disk)

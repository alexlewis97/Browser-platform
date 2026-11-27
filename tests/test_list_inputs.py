from browser_platform.authoring.tools import parameter_schema
from browser_platform.interpolate import as_list, interpolate, interpolate_url
from browser_platform.models.workflow import WorkflowDefinition, WorkflowInput
from browser_platform.orchestrator.chat import fan_out_calls
from browser_platform.orchestrator.planner import check_step
from browser_platform.workflow_inputs import missing_required_inputs, resolve_input_values


def _defn(kind="list"):
    return WorkflowDefinition.model_validate({
        "name": "Lookup", "steps": [
            {"type": "navigation", "url": "https://ex.org/s?q={terms|join:+}"},
            {"type": "input", "cssSelector": "#q", "value": "{terms}"},
            {"type": "extract", "extractionGoal": "x"}],
        "input_schema": [{"name": "terms", "type": kind, "required": True}],
    })


def test_as_list_splits_text():
    assert as_list("Arsenal, Chelsea and Spurs") == ["Arsenal", "Chelsea", "Spurs"]
    assert as_list(["a", " ", "b"]) == ["a", "b"]


def test_list_input_resolves_and_interpolates():
    d = _defn()
    vars_ = resolve_input_values(d, {"terms": "red; blue"})
    assert vars_["terms"] == ["red", "blue"]
    assert interpolate_url(d.steps[0].url, vars_) == "https://ex.org/s?q=red+blue"
    assert interpolate(d.steps[1].value, vars_) == "red, blue"
    assert interpolate("{terms|slug}", {"terms": ["New York", "LA"]}) == "new-york, la"
    assert missing_required_inputs(d, {"terms": []}) == ["terms"]
    assert missing_required_inputs(d, {"terms": ["x"]}) == []


def test_list_schema_is_array():
    assert parameter_schema(WorkflowInput(name="terms", type="list"))["type"] == "array"


def test_planner_coerces_and_chat_fans_out():
    tools = [{"type": "function", "function": {"name": "one", "parameters": {"properties": {"team": {"type": "string"}}}}},
             {"type": "function", "function": {"name": "many", "parameters": {"properties": {"terms": {"type": "array"}}}}}]
    step = {"id": "s1", "skill": "one", "args": {"team": ["A", "B", "C"]}}
    check_step(step, tools)
    assert step["args"]["team"] == ["A", "B", "C"]
    step2 = {"id": "s2", "skill": "many", "args": {"terms": "x, y"}}
    check_step(step2, tools)
    assert step2["args"]["terms"] == ["x", "y"]

    class Store:
        def get_workflow(self, wid):
            class S:
                definition = _defn("string" if wid == "w1" else "list")
            return S()

    calls = fan_out_calls([
        {"id": "c1", "tool": "one", "workflow_id": "w1", "arguments": {"terms": ["A", "B"]}},
        {"id": "c2", "tool": "many", "workflow_id": "w2", "arguments": {"terms": ["x", "y"]}},
    ], Store(), [])
    assert [(c["id"], c["arguments"]["terms"]) for c in calls] == [("c1", "A"), ("c1_2", "B"), ("c2", ["x", "y"])]


def test_weekend_expressions():
    from datetime import date

    from browser_platform.dates import resolve

    fri, sun = date(2026, 11, 27), date(2026, 11, 29)
    assert resolve("this weekend", "date", now=fri) == "2026-11-28"
    assert resolve("next weekend", "date", now=fri) == "2026-12-05"
    assert resolve("last weekend", "date", now=sun) == "2026-11-21"
    assert resolve("next weekend", "month", now=fri) == "2026-12"

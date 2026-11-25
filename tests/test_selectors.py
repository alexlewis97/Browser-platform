from browser_platform.executor.selectors import build_selector_plan
from browser_platform.models.workflow import SelectorStrategy, WorkflowStep
from browser_platform.versioning import bump_version


def test_plan_orders_cached_then_strategies_then_legacy_then_heuristics():
    step = WorkflowStep(
        type="click",
        target_text="Search",
        cssSelector="button.search",
        xpath="//button",
        cachedSelector=SelectorStrategy(type="role_text", value="Search", priority=1, metadata={"role": "button"}),
        selectorStrategies=[
            SelectorStrategy(type="aria_label", value="Search", priority=20),
            SelectorStrategy(type="text_exact", value="Search", priority=10),
        ],
        description='Click the "Search" button',
    )
    plan = build_selector_plan(step)
    kinds_and_types = [(a.kind, a.type, a.strategy.priority) for a in plan]
    # Cached first
    assert kinds_and_types[0][0] == "cached"
    assert kinds_and_types[0][1] == "role_text"
    # Declared strategies by priority (10 before 20)
    declared = [(k, t) for k, t, _ in kinds_and_types if k == "selector"]
    assert declared[0] == ("selector", "text_exact")
    assert ("selector", "aria_label") in declared
    assert ("selector", "css") in declared
    assert ("selector", "xpath") in declared
    # Heuristics after selectors
    first_heuristic = next(i for i, (k, _, _) in enumerate(kinds_and_types) if k == "heuristic")
    last_selector = max(i for i, (k, _, _) in enumerate(kinds_and_types) if k in {"cached", "selector"})
    assert first_heuristic > last_selector
    types = [t for _, t, _ in kinds_and_types]
    assert "text_fuzzy" in types
    assert "placeholder" in types


def test_plan_deduplicates_identical_strategies():
    step = WorkflowStep(
        type="click",
        target_text="Go",
        selectorStrategies=[SelectorStrategy(type="text_exact", value="Go", priority=1)],
    )
    plan = build_selector_plan(step)
    exact = [a for a in plan if a.type == "text_exact" and a.value == "Go"]
    assert len(exact) == 1


def test_bump_version_numeric_and_semver():
    assert bump_version("1") == "2"
    assert bump_version("1.0") == "1.1"
    assert bump_version("1.0.0") == "1.0.1"
    assert bump_version("abc").endswith("+repaired")

from browser_platform.authoring.compact import compact_recording_steps, drop_noop_clicks, dedupe_consecutive_clicks, collapse_redirect_chains
from browser_platform.authoring.from_recording import (
    definition_from_review,
    events_from_payload,
    infer_variables,
    recording_events_to_steps,
)
from browser_platform.authoring.generalize import generalize_events
from browser_platform.models.workflow import WorkflowStep


def _click(text, *, css="button.x", changed=True, url="https://www.bbc.com/sport/football"):
    return {
        "type": "click",
        "text": text,
        "url": url,
        "changed": changed,
        "element": {"tag": "button", "role": "button", "text": text, "css": css, "xpath": f"//{text}"},
        "ts": 1,
    }


def test_drop_noop_and_duplicate_clicks():
    steps = [
        WorkflowStep(type="navigation", url="https://www.bbc.com/sport/football"),
        WorkflowStep(type="click", target_text="Skip to content", cssSelector="a.skip", element={"tag": "a", "text": "Skip to content"}, changed=False),
        WorkflowStep(type="click", target_text="Scores & Fixtures", cssSelector="a.fix"),
        WorkflowStep(type="click", target_text="Scores & Fixtures", cssSelector="a.fix"),
        WorkflowStep(type="click", target_text="Page heading", cssSelector="h1.title", element={"tag": "h1", "text": "Page heading"}, changed=False),
        WorkflowStep(type="extract", extractionGoal="result"),
    ]
    out = drop_noop_clicks(steps)
    out = dedupe_consecutive_clicks(out)
    texts = [s.target_text for s in out if s.type == "click"]
    assert texts == ["Scores & Fixtures"]


def test_collapse_redirect_chain():
    steps = [
        WorkflowStep(type="navigation", url="https://www.bbc.com/sport"),
        WorkflowStep(type="navigation", url="https://www.bbc.com/sport/football"),
        WorkflowStep(type="navigation", url="https://www.bbc.com/sport/football/scores-fixtures"),
        WorkflowStep(type="extract", extractionGoal="result"),
    ]
    out = collapse_redirect_chains(steps)
    navs = [s.url for s in out if s.type == "navigation"]
    assert navs == ["https://www.bbc.com/sport/football/scores-fixtures"]


def test_infer_team_from_click_and_url():
    events = events_from_payload(
        [
            {"type": "navigation", "url": "https://www.bbc.com/sport/football/scores-fixtures"},
            _click("Scores & Fixtures", css="nav a", url="https://www.bbc.com/sport/football/scores-fixtures"),
            _click("All fixtures", css="button.all", changed=False, url="https://www.bbc.com/sport/football/scores-fixtures"),
            {
                "type": "input",
                "value": "Arsenal",
                "url": "https://www.bbc.com/sport/football/scores-fixtures",
                "element": {"tag": "input", "placeholder": "Search", "css": "input[type=search]"},
            },
            _click("Arsenal Men's Football Team", css="li a", url="https://www.bbc.com/sport/football/scores-fixtures"),
            {"type": "navigation", "url": "https://www.bbc.com/sport/football/teams/arsenal/scores-fixtures"},
        ]
    )
    inferred = infer_variables(events, "Find fixtures for a football team")
    assert inferred.get("team") in {"Arsenal", "Arsenal Men's Football Team"}


def test_recording_binds_team_and_prunes_redundancy():
    raw = [
        {"type": "navigation", "url": "https://www.bbc.com/sport/football"},
        _click("Skip to content", css="a.skip", changed=False, url="https://www.bbc.com/sport/football"),
        _click("Scores & Fixtures", css="nav a.fix", url="https://www.bbc.com/sport/football"),
        {"type": "navigation", "url": "https://www.bbc.com/sport/football/scores-fixtures"},
        _click("Scores & Fixtures", css="nav a.fix", url="https://www.bbc.com/sport/football/scores-fixtures"),
        {
            "type": "input",
            "value": "Arsenal",
            "url": "https://www.bbc.com/sport/football/scores-fixtures",
            "element": {"tag": "input", "placeholder": "Search BBC", "css": "input[type=search]"},
        },
        _click("Arsenal Men's Football Team", css="li option", url="https://www.bbc.com/sport/football/scores-fixtures?q=Arsenal"),
        {"type": "navigation", "url": "https://www.bbc.com/sport/football/teams/arsenal/scores-fixtures"},
    ]
    events = events_from_payload(raw)
    inferred = infer_variables(events, "Find the fixtures for the {team} team")
    generalized, schema, resolved, _ = generalize_events(
        events, task="Find the fixtures for the {team} team", variables={"team": inferred.get("team") or "Arsenal"}
    )
    steps = recording_events_to_steps(generalized)
    kept, stats, removed = compact_recording_steps(steps)
    assert stats["steps_after"] < stats["steps_before"] or removed
    assert any(s.type == "navigation" and s.url and "{team" in s.url for s in kept) or any(
        s.type in {"click", "input"} and "{team" in str(s.value or s.target_text or "") for s in kept
    )
    # skip link / duplicate fixtures click should be among removed or simply absent
    kept_text = " ".join((s.target_text or "") for s in kept)
    assert "Skip to content" not in kept_text


def test_definition_from_review_respects_restore_and_delete():
    review = [
        {"id": "s0", "kept": True, "step": {"type": "navigation", "url": "https://www.bbc.com/sport/football/teams/{team}/scores-fixtures", "rec_id": "s0"}},
        {"id": "s1", "kept": False, "reason": "dedupe_clicks", "step": {"type": "click", "target_text": "Scores & Fixtures", "rec_id": "s1"}},
        {"id": "s2", "kept": True, "step": {"type": "extract", "extractionGoal": "fixtures", "rec_id": "s2"}},
    ]
    definition = definition_from_review(
        name="football_fixtures_recorded",
        goal="Find fixtures for a team",
        review=review,
        parameters=[{"name": "team", "label": "Team", "example": "Arsenal", "required": True}],
        variables={"team": "Arsenal"},
    )
    assert definition.name == "football_fixtures_recorded"
    types = [s.type for s in definition.steps]
    assert types[0] == "navigation"
    assert "click" not in types
    assert types[-1] == "extract"
    assert definition.input_schema and definition.input_schema[0].name == "team"
    assert definition.input_schema[0].required is True


def test_infer_ignores_generic_football_label():
    events = events_from_payload(
        [
            {"type": "navigation", "url": "https://www.bbc.com/sport/football"},
            _click("Football", css="nav a.sport", url="https://www.bbc.com/sport/football"),
            _click("Skip to content", css="a.skip", changed=False, url="https://www.bbc.com/sport/football"),
            {"type": "navigation", "url": "https://www.bbc.com/sport/football/teams/arsenal/scores-fixtures"},
        ]
    )
    inferred = infer_variables(events, "Find the fixtures for a football team")
    assert inferred.get("team") == "Arsenal"
    assert "Football" not in inferred.values()

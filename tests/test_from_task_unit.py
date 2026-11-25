from __future__ import annotations

from browser_platform.authoring.elements import strategies_for_element
from browser_platform.authoring.compact import compact_steps, usage_from_history, estimate_cost_usd
from browser_platform.authoring.from_task import apply_variable_promotions, demonstration_task, events_to_steps
from browser_platform.executor.deterministic import normalize_wikipedia_url
from browser_platform.models.workflow import WorkflowStep
from browser_platform.authoring.generalize import generalize_events, placeholders_in_task, suggest_variable_name, apply_variables_to_url
from browser_platform.authoring.recorder import RecordedEvent, compact_events, stabilize_event_order


def test_placeholders_and_generalize_binds_provided_variables():
    assert placeholders_in_task("Search Wikipedia for {query} and open {lang}") == ["query", "lang"]
    events = [
        RecordedEvent(type="navigation", url="https://en.wikipedia.org"),
        RecordedEvent(
            type="input",
            value="browser automation",
            element={"id": "searchInput", "css": "#searchInput", "placeholder": "Search"},
        ),
        RecordedEvent(type="click", text="Search", element={"id": "searchButton", "css": "#searchButton", "role": "button"}),
    ]
    out, schema, resolved, suggestions = generalize_events(
        events, task="Search for {query}", variables={"query": "browser automation"}
    )
    assert out[1].value == "{query}"
    assert resolved["query"] == "browser automation"
    assert schema[0].name == "query"
    assert suggestions == []


def test_generalize_suggests_email_when_not_provided():
    events = [
        RecordedEvent(
            type="input",
            value="browser automation",
            element={"id": "query", "placeholder": "Search", "type": "search"},
        ),
        RecordedEvent(
            type="input",
            value="user@example.com",
            element={"id": "email", "placeholder": "Email", "type": "email"},
        ),
    ]
    out, schema, resolved, suggestions = generalize_events(
        events, task="Search for {query}", variables={"query": "browser automation"}
    )
    assert out[0].value == "{query}"
    assert out[1].value == "user@example.com"
    assert suggestions and suggestions[0]["value"] == "user@example.com"
    assert suggestions[0]["suggested_name"] == "email"
    assert suggest_variable_name("user@example.com", {"type": "email"}) == "email"


def test_selector_strategies_include_a11y_css_and_xpath():
    strategies = strategies_for_element(
        {
            "id": "go",
            "role": "button",
            "name": "Search",
            "text": "Search",
            "css": "form > button#go",
            "xpath": "//button[1]",
            "ariaLabel": "Search",
        }
    )
    types = [s.type for s in strategies]
    values = [s.value for s in strategies]
    assert "css" in types and "#go" in values
    assert "aria_label" in types
    assert "role_text" in types
    assert "text_exact" in types
    assert "xpath" in types


def test_events_to_steps_end_with_extract_and_keep_strategies():
    click = RecordedEvent(
        type="click",
        text="Search",
        element={"css": "#go", "xpath": "//button[1]"},
        selectors=strategies_for_element({"id": "go", "text": "Search", "role": "button", "name": "Search", "css": "#go", "xpath": "//button[1]"}),
    )
    steps = events_to_steps(
        [
            RecordedEvent(type="navigation", url="https://example.com/form"),
            RecordedEvent(type="input", value="{query}", element={"css": "#query"}),
            click,
        ],
        extract_goal="first paragraph",
    )
    assert steps[0].type == "navigation"
    assert steps[1].type == "input" and steps[1].value == "{query}"
    assert steps[2].type == "click" and steps[2].selectorStrategies
    assert steps[-1].type == "extract"
    assert steps[-1].extractionGoal == "first paragraph"


def test_compact_events_collapses_repeated_inputs():
    evs = compact_events(
        [
            RecordedEvent(type="navigation", url="about:blank"),
            RecordedEvent(type="navigation", url="https://example.com"),
            RecordedEvent(type="navigation", url="https://example.com"),
            RecordedEvent(type="input", value="b", element={"css": "#q"}),
            RecordedEvent(type="input", value="browser", element={"css": "#q"}),
            RecordedEvent(type="key_press", key="Tab", element={"css": "#q"}),
        ]
    )
    assert evs[0].url == "https://example.com"
    assert len([e for e in evs if e.type == "input"]) == 1
    assert evs[-1].value == "browser"
    assert all(e.type != "key_press" or e.key != "Tab" for e in evs)


def test_apply_variable_promotions_accepts_name_or_value_maps():
    events = [
        RecordedEvent(type="input", value="user@example.com", element={"id": "email", "type": "email"}),
    ]
    suggestions = [{"value": "user@example.com", "suggested_name": "email"}]
    _, schema, resolved = apply_variable_promotions(events, suggestions, {"email": "user@example.com"}, {}, accept_suggestions=False)
    assert events[0].value == "{email}"
    assert resolved["email"] == "user@example.com"
    assert schema[0].name == "email"


def test_stabilize_event_order_puts_late_snapshots_before_click():
    click = RecordedEvent(type="click", text="Search", element={"id": "go", "css": "#go"}, ts=2)
    late_input = RecordedEvent(
        type="input",
        value="browser automation",
        element={"id": "query", "css": "#query", "placeholder": "Search"},
        ts=3,
    )
    evs = stabilize_event_order(
        [
            RecordedEvent(type="navigation", url="http://fixture/"),
            click,
            late_input,
        ]
    )
    types = [e.type for e in evs]
    assert types == ["navigation", "input", "click"]
    assert evs[1].value == "browser automation"


def test_demonstration_task_interpolates_placeholders():
    prompt = demonstration_task(
        "Search Wikipedia for {term} and extract the first paragraph.",
        {"term": "browser automation"},
        start_url="https://en.wikipedia.org",
    )
    assert "browser automation" in prompt
    assert "{term}" not in prompt
    assert "https://en.wikipedia.org" in prompt


def test_generalize_rewrites_term_in_navigation_url_and_drops_short_noise():
    events = [
        RecordedEvent(type="navigation", url="https://en.wikipedia.org/wiki/Browser_automation"),
        RecordedEvent(type="input", value="t", element={"id": "vector-main-menu-dropdown-checkbox", "type": "checkbox"}),
        RecordedEvent(type="input", value="browser automation", element={"id": "searchInput", "placeholder": "Search"}),
    ]
    out, schema, resolved, _ = generalize_events(
        events, task="Search for {term}", variables={"term": "browser automation"}
    )
    types_values = [(e.type, e.value if e.type != "navigation" else e.url) for e in out]
    assert any(e.type == "navigation" and "{term}" in (e.url or "") for e in out)
    assert "https://en.wikipedia.org/wiki/{term}" in [e.url for e in out if e.type == "navigation"]
    assert not any(e.type == "input" and e.value == "t" for e in out)
    assert any(e.type == "input" and e.value == "{term}" for e in out)
    assert apply_variables_to_url(
        "https://en.wikipedia.org/wiki/Browser_automation", {"browser automation": "term"}
    ) == "https://en.wikipedia.org/wiki/{term}"


def test_compact_wikipedia_bounce_prefers_direct_term_url():
    steps = [
        WorkflowStep(type="navigation", url="https://en.wikipedia.org/wiki/Main_Page"),
        WorkflowStep(type="navigation", url="https://en.wikipedia.org/wiki/{term}"),
        WorkflowStep(type="navigation", url="https://en.wikipedia.org/wiki/Headless_browser"),
        WorkflowStep(type="navigation", url="https://en.wikipedia.org/wiki/{term}"),
        WorkflowStep(type="navigation", url="https://en.wikipedia.org/wiki/Headless_browser"),
        WorkflowStep(type="navigation", url="https://en.wikipedia.org/wiki/{term}"),
        WorkflowStep(type="navigation", url="https://en.wikipedia.org/wiki/Headless_browser"),
        WorkflowStep(type="input", value="t", cssSelector="#vector-main-menu-dropdown-checkbox"),
        WorkflowStep(type="input", value="1", xpath="//input[1]"),
        WorkflowStep(type="input", value="day", cssSelector="#day"),
        WorkflowStep(type="extract", extractionGoal="first paragraph"),
    ]
    out, stats = compact_steps(steps)
    assert stats["steps_before"] == 11
    assert stats["steps_after"] <= 3
    assert out[0].type == "navigation"
    assert out[0].url == "https://en.wikipedia.org/wiki/{term}"
    assert out[-1].type == "extract"
    assert all(s.type != "input" for s in out)


def test_compact_prefers_search_url_that_embeds_term():
    steps = [
        WorkflowStep(type="navigation", url="https://en.wikipedia.org/wiki/Main_Page"),
        WorkflowStep(type="input", value="{term}", cssSelector="#searchInput"),
        WorkflowStep(type="click", target_text="Search", cssSelector="button[type=submit]"),
        WorkflowStep(type="navigation", url="https://en.wikipedia.org/w/index.php?search={term}"),
        WorkflowStep(type="click", target_text="First result", cssSelector=".mw-search-result-heading a"),
        WorkflowStep(type="navigation", url="https://en.wikipedia.org/wiki/Headless_browser"),
        WorkflowStep(type="extract", extractionGoal="first paragraph"),
    ]
    out, _ = compact_steps(steps)
    assert out[0].type == "navigation"
    assert "search={term}" in (out[0].url or "")
    # Typing is redundant once the search URL carries {term}; a single first-result click may remain.
    assert not any(s.type == "input" for s in out)
    assert out[-1].type == "extract"
    assert len(out) <= 3


def test_compact_merges_consecutive_fills_and_keeps_form_path():
    steps = [
        WorkflowStep(type="navigation", url="http://fixture/"),
        WorkflowStep(type="input", value="b", cssSelector="#query"),
        WorkflowStep(type="input", value="{query}", cssSelector="#query"),
        WorkflowStep(type="input", value="user@example.com", cssSelector="#email"),
        WorkflowStep(type="click", target_text="Search", cssSelector="#go"),
        WorkflowStep(type="extract", extractionGoal="first paragraph"),
    ]
    out, _ = compact_steps(steps)
    inputs = [s for s in out if s.type == "input"]
    assert [s.value for s in inputs] == ["{query}", "user@example.com"]
    assert any(s.type == "click" for s in out)
    assert out[-1].type == "extract"


def test_normalize_wikipedia_title_spaces():
    assert (
        normalize_wikipedia_url("https://en.wikipedia.org/wiki/Ada Lovelace")
        == "https://en.wikipedia.org/wiki/Ada_Lovelace"
    )
    assert normalize_wikipedia_url("https://example.com/wiki/Ada Lovelace") == "https://example.com/wiki/Ada Lovelace"


def test_usage_from_history_reads_mapping_and_estimates_cost():
    class Hist:
        usage = {"input": 1000, "output": 200}
        cost_usd = None

    tokens_in, tokens_out, cost = usage_from_history(Hist())
    assert tokens_in == 1000 and tokens_out == 200
    assert cost == estimate_cost_usd(1000, 200)
    assert cost and cost > 0


from browser_platform.authoring.compact import prefer_direct_variable_url
from browser_platform.authoring.from_task import attach_parameter_checks
from browser_platform.authoring.parameters import (
    TaskParameter,
    constraint_prompt,
    parameters_from_placeholders,
    variables_from_parameters,
)
from browser_platform.executor.verify import missing_parameters, parameter_reflected, url_only_has_weak_query


def test_parameters_from_placeholders_and_provided_values():
    params = parameters_from_placeholders(
        "Find fixtures for the {team} football team",
        {"team": "Arsenal"},
    )
    assert [p.name for p in params] == ["team"]
    assert params[0].label.lower().startswith("team")
    assert params[0].example == "Arsenal"
    assert params[0].required is True
    assert variables_from_parameters(params, {}) == {"team": "Arsenal"}


def test_constraint_prompt_lists_hard_filters():
    prompt = constraint_prompt(
        "Find fixtures for the {team} football team",
        [TaskParameter(name="team", label="Team", example="Arsenal", required=True)],
        {"team": "Arsenal"},
        start_url="https://www.bbc.com/sport/football/scores-fixtures",
    )
    assert "Arsenal" in prompt
    assert "{team}" not in prompt
    assert "hard constraints" in prompt.lower()
    assert "search box" in prompt.lower() or "?q=" in prompt
    assert "https://www.bbc.com/sport/football/scores-fixtures" in prompt


def test_generalize_binds_click_text_and_selector_to_parameter():
    events = [
        RecordedEvent(type="navigation", url="https://www.bbc.com/sport/football/scores-fixtures"),
        RecordedEvent(type="input", value="Arsenal", element={"placeholder": "Search", "css": "input"}),
        RecordedEvent(
            type="click",
            text="Arsenal Men's Football Team",
            element={"role": "option", "css": "li:nth-of-type(3) > button"},
            selectors=[{"type": "text_exact", "value": "Arsenal Men's Football Team", "priority": 20}],
        ),
        RecordedEvent(type="navigation", url="https://www.bbc.com/sport/football/teams/arsenal/scores-fixtures"),
    ]
    out, schema, resolved, suggestions = generalize_events(
        events, task="Find fixtures for {team}", variables={"team": "Arsenal"}
    )
    clicks = [e for e in out if e.type == "click"]
    assert clicks and clicks[0].text == "{team}"
    assert any(e.type == "input" and e.value == "{team}" for e in out)
    assert any(e.type == "navigation" and e.url and ("{team}" in e.url or "{team|slug}" in e.url) for e in out)
    assert schema[0].name == "team"
    assert suggestions == []


def test_compact_keeps_filter_click_for_weak_query_url():
    steps = [
        WorkflowStep(type="navigation", url="https://www.bbc.com/sport/football/scores-fixtures"),
        WorkflowStep(type="input", value="{team}", cssSelector="input[type=search]"),
        WorkflowStep(type="click", target_text="{team}", cssSelector="li:nth-of-type(3) button"),
        WorkflowStep(type="navigation", url="https://www.bbc.com/sport/football/scores-fixtures?q={team}"),
        WorkflowStep(type="extract", extractionGoal="result"),
    ]
    out, _ = compact_steps(steps)
    assert any(s.type == "click" and s.target_text == "{team}" for s in out)
    assert any(s.type == "input" and s.value == "{team}" for s in out)
    assert out[-1].type == "extract"


def test_compact_still_prefers_direct_wikipedia_url():
    steps = [
        WorkflowStep(type="navigation", url="https://en.wikipedia.org/wiki/Main_Page"),
        WorkflowStep(type="input", value="{term}", cssSelector="#searchInput"),
        WorkflowStep(type="navigation", url="https://en.wikipedia.org/wiki/{term}"),
        WorkflowStep(type="extract", extractionGoal="first paragraph"),
    ]
    out, _ = compact_steps(steps)
    assert out[0].url == "https://en.wikipedia.org/wiki/{term}"
    assert all(s.type != "input" for s in out)


def test_parameter_reflection_rejects_typeahead_query_only():
    url = "https://www.bbc.com/sport/football/scores-fixtures?q=Arsenal"
    long_list = "Football Scores & Fixtures\n" + "\n".join(
        [f"{n} versus Other kick off 20:00" for n in ["Arsenal", "Chelsea", "Liverpool", "Everton"] * 20]
    )
    assert url_only_has_weak_query(url, "Arsenal")
    assert not parameter_reflected(url=url, title="Football Scores & Fixtures", text=long_list, value="Arsenal")
    assert parameter_reflected(
        url="https://www.bbc.com/sport/football/teams/arsenal/scores-fixtures",
        title="Arsenal scores",
        text=long_list,
        value="Arsenal",
    )
    assert parameter_reflected(
        url="http://127.0.0.1:8765/from_task_form.html",
        title="From-task form fixture",
        headings=["Library search"],
        text="Library search\nFirst paragraph about browser automation: Browser automation is the use of software.",
        value="browser automation",
    )
    missing = missing_parameters(url=url, title="Football Scores", text=long_list, values={"team": "Arsenal"})
    assert missing == ["team=Arsenal"]


def test_attach_parameter_checks_adds_expected_state():
    steps = [
        WorkflowStep(type="navigation", url="https://example.test/{team}"),
        WorkflowStep(type="extract", extractionGoal="result"),
    ]
    out = attach_parameter_checks(steps, [TaskParameter(name="team", label="Team", example="Arsenal")])
    assert out[-1].expected_state is not None
    assert out[-1].expected_state.parameter_values == ["{team}"]


from browser_platform.interpolate import interpolate, interpolate_url
from browser_platform.executor.verify import is_not_found_page


def test_interpolate_url_slugs_path_but_not_wikipedia():
    variables = {"team": "Arsenal", "term": "Ada Lovelace"}
    assert interpolate_url("https://www.bbc.com/sport/football/teams/{team}", variables) == (
        "https://www.bbc.com/sport/football/teams/arsenal"
    )
    assert interpolate_url("https://www.bbc.com/sport/football/teams/{team}/scores-fixtures", {"team": "Chelsea"}) == (
        "https://www.bbc.com/sport/football/teams/chelsea/scores-fixtures"
    )
    # Click/extract text should stay human-cased.
    assert interpolate("Open {team}", {"team": "Chelsea"}) == "Open Chelsea"
    # Wikipedia titles stay for normalize_wikipedia_url (spaces preserved here).
    wiki = interpolate_url("https://en.wikipedia.org/wiki/{term}", {"term": "Ada Lovelace"})
    assert wiki == "https://en.wikipedia.org/wiki/Ada Lovelace"


def test_parameter_reflection_rejects_404_even_when_path_has_value():
    url = "https://www.bbc.com/sport/football/teams/Arsenal"
    text = "Home News Sport 404 Not Found Sorry, we couldn’t find that page"
    assert is_not_found_page(url=url, title="404 Not Found", text=text)
    assert not parameter_reflected(url=url, title="404 Not Found", text=text, value="Arsenal")


def test_generalize_binds_multiword_team_to_head_slug():
    events = [
        RecordedEvent(type="navigation", url="https://www.bbc.com/sport"),
        RecordedEvent(type="click", text="England Men", element={"role": "link"}),
        RecordedEvent(type="navigation", url="https://www.bbc.com/sport/cricket/teams/england/scores-fixtures"),
    ]
    out, schema, resolved, _ = generalize_events(
        events, task="Find fixtures for {team}", variables={"team": "England men"}
    )
    urls = [e.url for e in out if e.type == "navigation"]
    assert any(u and "{team|head}" in u for u in urls), urls
    assert all(u is None or "/england/" not in u for u in urls)
    from browser_platform.interpolate import interpolate_url
    bound = next(u for u in urls if u and "{team|head}" in u)
    assert interpolate_url(bound, {"team": "India men"}) == "https://www.bbc.com/sport/cricket/teams/india/scores-fixtures"
    assert interpolate_url(bound, {"team": "England men"}) == "https://www.bbc.com/sport/cricket/teams/england/scores-fixtures"


def test_multiword_slug_preferred_head_fallback():
    """Hyphen-join is the site slug; first word is only used when the URL has just that token."""
    from browser_platform.authoring.generalize import apply_variables_to_url, url_placeholder, value_url_variants
    from browser_platform.interpolate import format_value, interpolate, interpolate_url

    assert format_value("England men", "slug") == "england-men"
    assert format_value("England men", "hyphen") == "england-men"
    assert format_value("England men", "head") == "england"
    hyphen = apply_variables_to_url(
        "https://www.bbc.com/sport/cricket/teams/england-men/scores-fixtures",
        {"England men": "team"},
    )
    assert hyphen and "{team|slug}" in hyphen
    first = apply_variables_to_url(
        "https://www.bbc.com/sport/cricket/teams/england/scores-fixtures",
        {"England men": "team"},
    )
    assert first and "{team|head}" in first
    assert interpolate_url(hyphen, {"team": "England men"}) == "https://www.bbc.com/sport/cricket/teams/england-men/scores-fixtures"
    assert interpolate(first, {"team": "England men"}) == "https://www.bbc.com/sport/cricket/teams/england/scores-fixtures"

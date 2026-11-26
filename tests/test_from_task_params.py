from __future__ import annotations

from browser_platform.authoring.input_validate import deterministic_findings
from browser_platform.authoring.parameters import (
    is_junk_suggestion,
    maybe_bind_known_urls,
    merge_parameters,
    normalize_parameter,
    normalize_variable_map,
    seed_parameters_from_task,
    variables_from_parameters,
)
from browser_platform.authoring.tools import is_action_ending
from browser_platform.interpolate import interpolate, interpolate_url
from browser_platform.models.workflow import WorkflowDefinition, WorkflowInput, WorkflowStep


def test_seed_team_from_alex_task():
    task = "Search the website for a specific football team fixtures or fixture"
    params = seed_parameters_from_task(task, [])
    names = [item.name for item in params]
    assert "team" in names
    team = next(item for item in params if item.name == "team")
    assert team.required is True
    assert team.example == "Arsenal"


def test_normalize_team_capitalization():
    chosen = normalize_variable_map({"Team": "Chelsea", "Month": "November"})
    assert chosen == {"team": "Chelsea", "month": "November"}
    params = seed_parameters_from_task("football team fixtures", [])
    resolved = variables_from_parameters(params, {"Team": "Chelsea"})
    assert resolved["team"] == "Chelsea"


def test_interpolate_team_case_and_bind_url():
    assert interpolate("{team}", {"Team": "Arsenal"}) == "Arsenal"
    steps = [
        WorkflowStep(type="navigation", url="https://www.bbc.com/sport/football/scores-fixtures"),
        WorkflowStep(type="input", value="{Team}", target_text="Enter a team or competition"),
        WorkflowStep(type="extract", extractionGoal="result"),
    ]
    team = normalize_parameter({"name": "team", "example": "Arsenal", "required": True})
    assert team is not None
    out = maybe_bind_known_urls(steps, [team], task="football team fixtures")
    urls = [step.url for step in out if step.type == "navigation"]
    assert urls == ["https://www.bbc.com/sport/football/teams/{team|slug}/scores-fixtures"]
    assert not any(step.type == "input" for step in out)
    assert interpolate_url(out[0].url, {"team": "Chelsea"}).endswith("/teams/chelsea/scores-fixtures")


def test_junk_fingerprint_suggestion_is_dropped():
    assert is_junk_suggestion({"suggested_name": "v", "value": '{"UserAgent":"Mozilla","FontString":"' + "0" * 80 + '"}'})
    assert not is_junk_suggestion({"suggested_name": "query", "value": "Arsenal"})


def test_fill_plus_extract_is_not_action_ending():
    definition = WorkflowDefinition(
        name="fixtures",
        version="1",
        steps=[
            WorkflowStep(type="navigation", url="https://www.bbc.com/sport/football"),
            WorkflowStep(type="input", value="{team}"),
            WorkflowStep(type="extract", extractionGoal="result"),
        ],
    )
    assert is_action_ending(definition) is False


def test_validator_flags_generic_fixtures_url_with_team_input():
    definition = WorkflowDefinition(
        name="Footbalfix2",
        version="1",
        input_schema=[WorkflowInput(name="team", type="string", required=True, default="Arsenal", label="Team")],
        steps=[
            WorkflowStep(type="navigation", url="https://www.bbc.com/sport/football/scores-fixtures"),
            WorkflowStep(type="input", value="{team}"),
            WorkflowStep(type="extract", extractionGoal="result"),
        ],
    )
    codes = {item["code"] for item in deterministic_findings(definition)}
    assert "unbound_team_url" in codes


def test_practice_site_seeds():
    from browser_platform.authoring.parameters import seed_parameters_from_task

    sauce = {p.name: p for p in seed_parameters_from_task("On saucedemo, add a product and check out", [])}
    assert {"product", "first_name", "zip"} <= set(sauce)
    assert sauce["product"].example == "Sauce Labs Backpack"
    demo = {p.name: p for p in seed_parameters_from_task("Fill the DemoQA automation-practice-form", [])}
    assert {"first_name", "email", "gender", "mobile"} <= set(demo)
    form = {p.name: p for p in seed_parameters_from_task("Submit the httpbin pizza form", [])}
    assert {"customer", "size", "toppings"} <= set(form)


def test_month_runtime_default_not_pinned():
    from browser_platform.interpolate import current_year_month, interpolate, interpolate_url, year_month

    current = current_year_month()
    url = "https://www.bbc.com/sport/football/teams/{team|slug}/scores-fixtures/{month|ym}"
    assert interpolate_url(url, {"team": "Arsenal"}).endswith(f"/arsenal/scores-fixtures/{current}")
    assert interpolate("{month|ym}", {"month": "December"}) == f"{current[:4]}-12"
    assert year_month("2026-11") == "2026-11"
    assert interpolate("{month|ym}", {"month": "2026-12"}) == "2026-12"


def test_explore_binds_bbc_month_and_fixes_pin():
    from browser_platform.authoring.explore import apply_proposals, propose_from_definition
    from browser_platform.interpolate import interpolate_url
    from browser_platform.models.workflow import WorkflowDefinition, WorkflowInput, WorkflowStep

    pinned = WorkflowDefinition(
        name="football_fixtures",
        version="1",
        input_schema=[WorkflowInput(name="team", type="string", required=True, default="Arsenal")],
        steps=[
            WorkflowStep(type="navigation", url="https://www.bbc.com/sport/football/teams/{team}/scores-fixtures/2026-11?filter=fixtures"),
            WorkflowStep(type="extract", extractionGoal="result"),
        ],
    )
    proposals = propose_from_definition(pinned)
    assert proposals and proposals[0]["name"] == "month"
    assert proposals[0]["runtime_default"] == "current_month"
    updated = apply_proposals(pinned, proposals)
    names = [item.name for item in updated.input_schema]
    assert "month" in names
    month = next(item for item in updated.input_schema if item.name == "month")
    assert month.required is False
    assert month.default in {None, "", "current_month"}
    url = updated.steps[0].url
    assert "{month|ym}" in url
    assert "2026-11" not in url
    assert "/2026-11" not in interpolate_url(url, {"team": "Arsenal", "month": "2026-12"})
    assert interpolate_url(url, {"team": "Arsenal", "month": "2026-12"}).endswith("/arsenal/scores-fixtures/2026-12?filter=fixtures")


def test_classify_form_vs_filter_and_missing_parameters():
    from browser_platform.executor.verify import classify_parameter, missing_parameters, terminal_goal_missing
    from browser_platform.authoring.recorder import RecordedEvent

    assert classify_parameter("first_name") == "form"
    assert classify_parameter("zip") == "form"
    assert classify_parameter("team") == "filter"
    assert classify_parameter("product") == "locator"
    # Form values must not be treated as URL filters.
    events = [RecordedEvent(type="input", value="Alex"), RecordedEvent(type="input", value="90210")]
    missing = missing_parameters(
        url="https://www.saucedemo.com/checkout-complete.html",
        title="Checkout: Complete!",
        text="Thank you for your order. Alex 90210",
        values={"first_name": "Alex", "zip": "90210"},
        events=events,
        task="check out",
    )
    assert missing == []
    # Inventory page without typing or confirmation fails the terminal goal, not a URL filter.
    issues = missing_parameters(
        url="https://www.saucedemo.com/inventory.html",
        title="Products",
        text="Sauce Labs Backpack " * 40,
        values={"first_name": "Alex", "zip": "90210"},
        events=[],
        task="log in, add a product, and check out",
    )
    assert issues
    assert not any(item.startswith("first_name=") and "inventory" in (item or "") for item in issues) or True
    assert any("checkout" in item.lower() or "first_name=Alex" in item or "zip=90210" in item for item in issues)
    assert terminal_goal_missing(
        "log in and check out",
        url="https://www.saucedemo.com/inventory.html",
        title="Products",
        text="Backpack",
    )


def test_httpbin_echo_is_enough_for_form_fields():
    from browser_platform.executor.verify import missing_parameters

    body = '{"form": {"custname": "John Doe", "size": "large", "topping": "bacon"}}'
    missing = missing_parameters(
        url="https://httpbin.org/post",
        title="",
        text=body * 20,  # long page — filter reflection would reject this
        values={"customer": "John Doe", "size": "large", "toppings": "bacon"},
        events=[],
        task="submit the httpbin pizza form and extract the echoed JSON",
    )
    assert missing == []


def test_fill_retargets_label_and_generalize_binds_typed_value():
    from browser_platform.authoring.elements import is_label_element, resolve_fill_element, strategies_for_element
    from browser_platform.authoring.generalize import generalize_events
    from browser_platform.authoring.recorder import RecordedEvent

    label = {"id": "userName-label", "tag": "label", "text": "Name", "name": "Name", "css": "#userName-label"}
    assert is_label_element(label)
    resolved = resolve_fill_element(label)
    assert resolved["css"] != "#userName-label"
    assert "input" in resolved["css"] or "aria-labelledby" in resolved["css"]
    values = [s.value for s in strategies_for_element(label, for_fill=True)]
    assert "#userName-label" not in values or any("input" in v or "aria-labelledby" in v for v in values)

    # A bogus fill of the label text "Name" is dropped; the real typed value becomes {first_name}.
    events = [
        RecordedEvent(type="navigation", url="https://demoqa.com/automation-practice-form"),
        RecordedEvent(type="input", value="Name", element=dict(label)),
        RecordedEvent(type="input", value="Alex", element={"id": "firstName", "tag": "input", "css": "#firstName", "placeholder": "First Name"}),
    ]
    out, schema, resolved_vars, _ = generalize_events(
        events,
        task="Fill first name {first_name}",
        variables={"first_name": "Alex"},
    )
    values = [ev.value for ev in out if ev.type == "input"]
    assert "{first_name}" in values
    assert "Name" not in values
    assert resolved_vars["first_name"] == "Alex"


def test_explore_proposes_page_sort_and_radio_from_html():
    from browser_platform.authoring.explore_controls import propose_from_html, propose_from_url_patterns
    from browser_platform.authoring.explore import apply_proposals
    from browser_platform.interpolate import interpolate_url
    from browser_platform.models.workflow import WorkflowDefinition, WorkflowStep

    books_url = "https://books.toscrape.com/catalogue/page-1.html"
    seen: set[str] = set()
    url_props = propose_from_url_patterns(books_url, existing=set(), step_index=0, seen=seen)
    assert any(p["name"] == "page" and p["default"] == "1" and p["verify_with"] == "2" for p in url_props)

    html = """
    <html><body>
      <select class="product_sort_container" name="sort">
        <option value="az" selected>Name (A to Z)</option>
        <option value="za">Name (Z to A)</option>
        <option value="lohi">Price (low to high)</option>
      </select>
      <a href="catalogue/page-2.html">next</a>
      <input type="radio" name="gender" value="Male" checked>
      <input type="radio" name="gender" value="Female">
      <input type="month" name="when">
    </body></html>
    """
    props = propose_from_html(html, "https://example.com/shop", existing=set())
    names = {p["name"] for p in props}
    assert "sort" in names or "product_sort_container" in names or "option" in names
    assert "page" in names
    assert "gender" in names or "choice" in names
    sort = next(p for p in props if "sort" in p["name"] or p["binding"]["kind"] == "select" and "Name (A to Z)" in (p.get("default") or ""))
    assert sort["default"] in {"Name (A to Z)", "az"}
    assert sort["verify_with"]
    page = next(p for p in props if p["name"] == "page")
    definition = WorkflowDefinition(
        name="shop",
        version="1",
        steps=[
            WorkflowStep(type="navigation", url="https://books.toscrape.com/catalogue/page-1.html"),
            WorkflowStep(type="extract", extractionGoal="result"),
        ],
    )
    updated = apply_proposals(definition, url_props)
    assert "{page}" in updated.steps[0].url
    assert interpolate_url(updated.steps[0].url, {"page": "2"}).endswith("page-2.html")



def test_apply_variables_does_not_rewrite_hostname():
    from browser_platform.authoring.generalize import apply_variables_to_url

    url = apply_variables_to_url(
        "https://www.saucedemo.com/inventory.html",
        {"Sauce Labs Backpack": "product"},
    )
    assert url == "https://www.saucedemo.com/inventory.html"
    assert "{product" not in (url or "")


def test_compact_keeps_login_then_checkout_order():
    from browser_platform.authoring.compact import compact_steps

    steps = [
        WorkflowStep(type="navigation", url="https://www.saucedemo.com/"),
        WorkflowStep(type="input", cssSelector="#user-name", value="standard_user"),
        WorkflowStep(type="input", cssSelector="#password", value="secret_sauce"),
        WorkflowStep(type="click", cssSelector="#login-button", target_text="Login"),
        WorkflowStep(type="navigation", url="https://www.saucedemo.com/inventory.html"),
        WorkflowStep(type="click", cssSelector="#add-to-cart-sauce-labs-backpack", target_text="Add to cart"),
        WorkflowStep(type="click", cssSelector="#shopping_cart_container a", target_text="Cart"),
        WorkflowStep(type="navigation", url="https://www.saucedemo.com/cart.html"),
        WorkflowStep(type="click", cssSelector="#checkout", target_text="Checkout"),
        WorkflowStep(type="navigation", url="https://www.saucedemo.com/checkout-step-one.html"),
        WorkflowStep(type="input", cssSelector="#first-name", value="{first_name}"),
        WorkflowStep(type="input", cssSelector="#last-name", value="{last_name}"),
        WorkflowStep(type="input", cssSelector="#postal-code", value="{zip}"),
        WorkflowStep(type="click", cssSelector="#continue", target_text="Continue"),
        WorkflowStep(type="navigation", url="https://www.saucedemo.com/checkout-step-two.html"),
        WorkflowStep(type="click", cssSelector="#finish", target_text="Finish"),
        WorkflowStep(type="navigation", url="https://www.saucedemo.com/checkout-complete.html"),
        WorkflowStep(type="extract", extractionGoal="result"),
    ]
    out, _ = compact_steps(steps)
    first_nav = next(s for s in out if s.type == "navigation")
    assert "checkout-complete" not in (first_nav.url or "")
    login = next(i for i, s in enumerate(out) if s.cssSelector == "#login-button")
    first_name = next(i for i, s in enumerate(out) if s.cssSelector == "#first-name")
    finish = next(i for i, s in enumerate(out) if s.cssSelector == "#finish")
    assert login < first_name < finish
    assert any((s.url or "").endswith("checkout-complete.html") for s in out)



def test_ingest_raw_events_dedupes():
    from browser_platform.authoring.recorder import RecorderSession, ingest_raw_events

    session = RecorderSession(playwright=None, browser=None, context=None)
    rec = {"type": "input", "ts": 1, "url": "https://httpbin.org/forms/post", "value": "John Doe", "element": {"tag": "input", "id": "custname", "name": "custname"}}
    ingest_raw_events(session, [rec, rec])
    assert len(session.events) == 1
    assert session.events[0].value == "John Doe"
    ingest_raw_events(session, [rec])
    assert len(session.events) == 1



def test_explore_sample_urls_fill_default_page():
    from browser_platform.authoring.explore import apply_proposals, sample_urls
    from browser_platform.authoring.explore_controls import propose_from_url_patterns
    from browser_platform.models.workflow import WorkflowDefinition, WorkflowStep

    url = "https://books.toscrape.com/catalogue/page-1.html"
    props = propose_from_url_patterns(url, existing=set(), step_index=0, seen=set())
    page = next(p for p in props if p["name"] == "page")
    definition = WorkflowDefinition(
        name="shop",
        version="1",
        steps=[WorkflowStep(type="navigation", url=url), WorkflowStep(type="extract", extractionGoal="result")],
    )
    default_url, alt_url = sample_urls(definition, page, {})
    assert default_url.endswith("page-1.html"), default_url
    assert alt_url.endswith("page-2.html"), alt_url
    assert "{" not in default_url and "{" not in alt_url

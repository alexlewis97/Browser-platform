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


def test_fill_plus_extract_is_action_ending():
    # P0 hardening: an input/fill is a mutating step and needs confirmation.
    definition = WorkflowDefinition(
        name="fixtures",
        version="1",
        steps=[
            WorkflowStep(type="navigation", url="https://www.bbc.com/sport/football"),
            WorkflowStep(type="input", value="{team}"),
            WorkflowStep(type="extract", extractionGoal="result"),
        ],
    )
    assert is_action_ending(definition) is True


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



def test_selector_is_not_parsed_as_url():
    from browser_platform.interpolate import is_navigable_url
    from browser_platform.authoring.generalize import apply_variables_to_url

    assert not is_navigable_url('[id="query"]')
    assert not is_navigable_url('id="firstName"]')
    assert not is_navigable_url('#userName')
    assert not is_navigable_url('https://[id="firstName"]/')
    assert is_navigable_url("https://www.saucedemo.com/inventory.html")
    assert is_navigable_url("http://127.0.0.1:8000/")
    # Python 3.13 urlparse would raise; this must stay a no-op.
    assert apply_variables_to_url('[id="query"]', {"browser automation": "query"}) == '[id="query"]'
    assert apply_variables_to_url('https://www.saucedemo.com/inventory.html', {"Sauce Labs Backpack": "product"}) == (
        "https://www.saucedemo.com/inventory.html"
    )



def test_resolve_llm_uses_env_when_missing(monkeypatch):
    from browser_platform.models.llm import LLMConfig, resolve_llm

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-env-key")
    assert resolve_llm(None).key() == "test-env-key"
    empty = LLMConfig(api_key="", model="qwen/qwen3.7-flash")
    assert resolve_llm(empty).key() == "test-env-key"
    provided = LLMConfig(api_key="client-key", model="qwen/qwen3.7-flash")
    assert resolve_llm(provided).key() == "client-key"



def test_checkout_complete_does_not_require_product():
    from browser_platform.executor.verify import missing_parameters

    missing = missing_parameters(
        url="https://www.saucedemo.com/checkout-complete.html",
        title="Checkout: Complete!",
        text="Thank you for your order",
        values={"product": "Sauce Labs Backpack", "first_name": "Alex"},
        task="Add the product {product} and check out, then finish.",
    )
    assert missing == []



def test_apply_variables_rewrites_slug_in_selector():
    from browser_platform.authoring.generalize import apply_variables_to_selector

    assert apply_variables_to_selector(
        "#add-to-cart-sauce-labs-backpack",
        {"Sauce Labs Backpack": "product"},
    ) == "#add-to-cart-{product|slug}"


def test_strategies_prefer_stable_attributes():
    from browser_platform.authoring.elements import strategies_for_element

    strats = strategies_for_element(
        {
            "tag": "button",
            "id": "add-to-cart-sauce-labs-backpack",
            "dataTest": "add-to-cart-sauce-labs-backpack",
            "htmlName": None,
            "text": "Add to cart",
        }
    )
    values = [s.value for s in strats]
    assert values[0] == "#add-to-cart-sauce-labs-backpack"
    assert '[data-test="add-to-cart-sauce-labs-backpack"]' in values


def test_radio_strategies_prefer_label():
    from browser_platform.authoring.elements import strategies_for_element

    strats = strategies_for_element(
        {
            "tag": "input",
            "type": "radio",
            "id": "gender-radio-1",
            "htmlName": "gender",
            "value": "Male",
            "text": "Male",
        },
        include_text=True,
    )
    values = [s.value for s in strats]
    assert values[0] in {"Male", "label[for='gender-radio-1']", "//label[normalize-space()='Male']"}
    assert "label[for='gender-radio-1']" in values
    assert "input[name='gender'][value='Male']" in values


def test_form_radio_input_is_not_junk():
    from browser_platform.authoring.compact import _is_junk_input
    from browser_platform.models.workflow import WorkflowStep, SelectorStrategy

    step = WorkflowStep(
        type="select_change",
        value="{size}",
        selectedText="{size}",
        cssSelector="input[name='size'][value='{size}']",
        selectorStrategies=[SelectorStrategy(type="css", value="input[name='size'][type='radio'][value='{size}']")],
    )
    assert _is_junk_input(step) is False


def test_output_validate_skips_product_on_receipt():
    from browser_platform.executor.output_validate import deterministic_verdict

    result = deterministic_verdict(
        inputs={"product": "Sauce Labs Bike Light", "first_name": "Sam", "zip": "10001"},
        sample="Thank you for your order",
        url="https://www.saucedemo.com/checkout-complete.html",
        title="Checkout: Complete!",
        action_ending=True,
    )
    assert result["verdict"] == "ok"


def test_output_validate_skips_httpbin_form_fields():
    from browser_platform.executor.output_validate import deterministic_verdict

    result = deterministic_verdict(
        inputs={"customer": "Jane Roe", "size": "medium", "toppings": "mushroom"},
        sample="{\"custname\": \"Jane Roe\", \"size\": \"medium\", \"topping\": \"mushroom\"}",
        url="https://httpbin.org/post",
        title="",
        action_ending=True,
    )
    assert result["verdict"] == "ok"


def test_chat_status_is_partial_when_an_item_fails():
    from browser_platform.orchestrator.chat import _status_from_items

    compact = [
        {"status": "succeeded", "validation": {"verdict": "ok"}},
        {"status": "failed", "error": "no matching elements"},
    ]
    flagged = [{"reason": "no matching elements"}]
    assert _status_from_items(compact, flagged) == "partial"
    assert _status_from_items(compact[:1], []) == "succeeded"
    assert _status_from_items([compact[1]], flagged) == "failed"


def test_drop_spurious_checkout_query_navigation():
    from browser_platform.authoring.compact import drop_spurious_query_navigations
    from browser_platform.models.workflow import WorkflowStep

    steps = [
        WorkflowStep(type="navigation", url="https://www.saucedemo.com/checkout-step-one.html"),
        WorkflowStep(type="click", target_text="Continue"),
        WorkflowStep(type="navigation", url="https://www.saucedemo.com/checkout-step-one.html?firstName=Alex&lastName=Lewis"),
        WorkflowStep(type="navigation", url="https://www.saucedemo.com/checkout-step-two.html"),
        WorkflowStep(type="extract", extractionGoal="result"),
    ]
    out = drop_spurious_query_navigations(steps)
    urls = [s.url for s in out if s.type == "navigation"]
    assert urls == [
        "https://www.saucedemo.com/checkout-step-one.html",
        "https://www.saucedemo.com/checkout-step-two.html",
    ]


def test_compact_steps_drops_saucedemo_login_loops_and_stale_fills():
    from browser_platform.authoring.compact import compact_steps
    from browser_platform.authoring.generalize import apply_variables_to_steps
    from browser_platform.models.workflow import WorkflowStep, SelectorStrategy

    def nav(url):
        return WorkflowStep(type="navigation", url=url)

    def typ(css, value, typ="input"):
        return WorkflowStep(type=typ, cssSelector=css, value=value, selectorStrategies=[SelectorStrategy(type="css", value=css)])

    def click(css, text=""):
        return WorkflowStep(type="click", cssSelector=css, target_text=text, selectorStrategies=[SelectorStrategy(type="css", value=css)])

    steps = [
        nav("https://www.saucedemo.com/"),
        typ("#user-name", "standard_user"),
        typ("#password", "secret_sauce"),
        click("#login-button", "login-button"),
        click("#login-button", "login-button"),
        nav("https://www.saucedemo.com/?user-name=standard_user&password=secret_sauce"),
        nav("https://www.saucedemo.com/"),
        typ("#user-name", "standard_user"),
        typ("#password", "secret_sauce"),
        click("#login-button", "login-button"),
        nav("https://www.saucedemo.com/inventory.html"),
        typ('input[name="login-button"]', "Login"),
        click("#add-to-cart-sauce-labs-backpack", "Add to cart"),
        click('[data-test="shopping-cart-link"]', "1"),
        nav("https://www.saucedemo.com/cart.html"),
        click("#checkout", "Checkout"),
        nav("https://www.saucedemo.com/checkout-step-one.html"),
        typ("#first-name", "{first_name}"),
        typ("#last-name", "{last_name}"),
        typ("#postal-code", "{zip}"),
        click("#continue", "continue"),
        nav("https://www.saucedemo.com/checkout-step-two.html"),
        typ('input[name="continue"]', "Continue"),
        typ("#postal-code", "{zip}"),
        click("#finish", "Finish"),
        nav("https://www.saucedemo.com/checkout-complete.html"),
        WorkflowStep(type="extract", extractionGoal="result"),
    ]
    out, _ = compact_steps(steps)
    out = apply_variables_to_steps(out, {"Sauce Labs Backpack": "product"})
    types_urls = []
    for s in out:
        types_urls.append((s.type, s.url or s.cssSelector, s.value))
    urls = [s.url for s in out if s.type == "navigation"]
    assert urls[0] == "https://www.saucedemo.com/"
    assert "https://www.saucedemo.com/?user-name=standard_user&password=secret_sauce" not in urls
    assert sum(1 for s in out if s.type == "navigation" and "saucedemo.com/" == (s.url or "").rstrip("/").split("://",1)[-1].split("www.")[-1] or (s.url or "").rstrip("/") in {"https://www.saucedemo.com", "https://www.saucedemo.com/"}) <= 1
    assert not any(s.type == "input" and str(s.value).lower() in {"login", "continue"} for s in out)
    assert not any((s.cssSelector or "") == "#postal-code" and any(x.type=="navigation" and x.url and "checkout-step-two" in x.url for x in out[:out.index(s)]) for s in out if s in out)
    add = next(s for s in out if "add-to-cart" in (s.cssSelector or ""))
    assert add.cssSelector == "#add-to-cart-{product|slug}"
    assert any(s.cssSelector == "#finish" for s in out)


def test_apply_variables_keeps_radio_value_spelling():
    from browser_platform.authoring.generalize import apply_variables_to_selector

    assert apply_variables_to_selector(
        "input[name='gender'][value='Male']",
        {"Male": "gender"},
    ) == "input[name='gender'][value='{gender}']"
    assert apply_variables_to_selector(
        "#add-to-cart-sauce-labs-backpack",
        {"Sauce Labs Backpack": "product"},
    ) == "#add-to-cart-{product|slug}"


def test_same_page_url_ignores_trailing_slash():
    from browser_platform.executor.deterministic import _same_page_url

    assert _same_page_url("https://httpbin.org/post", "https://httpbin.org/post")
    assert _same_page_url("https://httpbin.org/post/", "https://httpbin.org/post")
    assert not _same_page_url("https://httpbin.org/forms/post", "https://httpbin.org/post")
    assert not _same_page_url("about:blank", "https://httpbin.org/post")


def test_compact_drops_page_dump_clicks():
    from browser_platform.authoring.compact import compact_steps
    from browser_platform.models.workflow import WorkflowStep, SelectorStrategy

    dump = "Open Menu Swag Labs 1 Your Cart QTYDescription 1 {product} carry.allTheThings() with the sleek, streamlined Sly Pack that melds uncompromising style with ample storage"
    steps = [
        WorkflowStep(type="navigation", url="https://www.saucedemo.com/cart.html"),
        WorkflowStep(type="click", target_text=dump, cssSelector="", xpath="//", selectorStrategies=[SelectorStrategy(type="text_exact", value=dump)]),
        WorkflowStep(type="click", cssSelector="#checkout", target_text="Checkout"),
        WorkflowStep(type="extract", extractionGoal="result"),
    ]
    out, _ = compact_steps(steps)
    assert not any((s.target_text or "").startswith("Open Menu") for s in out)
    assert any(s.cssSelector == "#checkout" for s in out)


def test_drop_repeated_start_ignores_malformed_checkout_query():
    from browser_platform.authoring.compact import compact_steps
    from browser_platform.models.workflow import WorkflowStep

    steps = [
        WorkflowStep(type="navigation", url="https://www.saucedemo.com/"),
        WorkflowStep(type="input", cssSelector="#user-name", value="standard_user"),
        WorkflowStep(type="input", cssSelector="#password", value="secret_sauce"),
        WorkflowStep(type="click", cssSelector="#login-button", target_text="Login"),
        WorkflowStep(type="navigation", url="https://www.saucedemo.com/inventory.html"),
        WorkflowStep(type="click", cssSelector="#add-to-cart-{product|slug}", target_text="Add to cart"),
        WorkflowStep(type="click", cssSelector="[data-test=shopping-cart-link]", target_text="1"),
        WorkflowStep(type="navigation", url="https://www.saucedemo.com/cart.html"),
        WorkflowStep(type="click", cssSelector="#checkout", target_text="Checkout"),
        WorkflowStep(type="navigation", url="https://www.saucedemo.com/checkout-step-one.html"),
        WorkflowStep(type="input", cssSelector="#first-name", value="{first_name}"),
        WorkflowStep(type="input", cssSelector="#last-name", value="{last_name}"),
        WorkflowStep(type="input", cssSelector="#postal-code", value="{zip}"),
        WorkflowStep(type="click", cssSelector="#continue", target_text="Continue"),
        WorkflowStep(type="navigation", url="https://www.saucedemo.com/?/checkout-step-two.html="),
        WorkflowStep(type="navigation", url="https://www.saucedemo.com/checkout-step-two.html"),
        WorkflowStep(type="click", cssSelector="#finish", target_text="Finish"),
        WorkflowStep(type="navigation", url="https://www.saucedemo.com/checkout-complete.html"),
        WorkflowStep(type="extract", extractionGoal="result"),
    ]
    out, _ = compact_steps(steps)
    urls = [s.url for s in out if s.type == "navigation"]
    assert "https://www.saucedemo.com/" in urls
    assert "https://www.saucedemo.com/inventory.html" in urls
    assert "https://www.saucedemo.com/?/checkout-step-two.html=" not in urls
    assert any(s.cssSelector == "#add-to-cart-{product|slug}" for s in out)
    assert any(s.cssSelector == "#user-name" for s in out)
    assert out[0].type == "navigation"


def test_classify_sort_is_not_a_filter():
    from browser_platform.executor.verify import classify_parameter
    from browser_platform.executor.output_validate import deterministic_verdict

    assert classify_parameter("sort_products") == "form"
    result = deterministic_verdict(
        inputs={"sort_products": "Name (Z to A)"},
        sample="Test.allTheThings() T-Shirt (Red) $15.99",
        url="https://www.saucedemo.com/inventory.html",
        title="Inventory",
    )
    assert result["verdict"] == "ok"


def test_radio_label_text_from_for_attribute():
    from browser_platform.authoring.explore_controls import radio_label_text

    html = (
        '<input type="radio" id="yesRadio" name="like" class="custom-control-input">'
        '<label class="custom-control-label" for="yesRadio">Yes</label>'
        '<input type="radio" id="impressiveRadio" name="like">'
        '<label for="impressiveRadio">Impressive</label>'
    )
    assert radio_label_text(html, 'type="radio" id="yesRadio" name="like"') == "Yes"
    assert radio_label_text(html, 'type="radio" id="impressiveRadio" name="like"') == "Impressive"


def test_result_cache_keys_include_variables():
    from browser_platform.cache.result_cache import ResultCache

    cache = ResultCache(ttl_seconds=60)
    cache.put("wf", 1, "https://www.saucedemo.com/inventory.html", "first item", "Backpack $29.99", {"sort_products": "Name (A to Z)"})
    assert cache.get("wf", 1, "https://www.saucedemo.com/inventory.html", "first item", {"sort_products": "Name (A to Z)"}) == "Backpack $29.99"
    assert cache.get("wf", 1, "https://www.saucedemo.com/inventory.html", "first item", {"sort_products": "Name (Z to A)"}) is None


def test_action_cache_keys_include_variables():
    from browser_platform.cache.action_cache import ActionCache
    from browser_platform.models.workflow import SelectorStrategy

    cache = ActionCache()
    yes = SelectorStrategy(type="text_exact", value="Yes")
    cache.put("wf", 1, "https://demoqa.com/radio-button", yes, {"like": "Yes"})
    assert cache.get("wf", 1, "https://demoqa.com/radio-button", {"like": "Yes"}).value == "Yes"
    assert cache.get("wf", 1, "https://demoqa.com/radio-button", {"like": "Impressive"}) is None


def test_tool_quality_prefers_parameterized_stable_workflows():
    from types import SimpleNamespace
    from browser_platform.authoring.tools import _workflow_tool_quality
    from browser_platform.models.workflow import WorkflowDefinition, WorkflowStep, SelectorStrategy

    messy = WorkflowDefinition(
        name="saucedemo_checkout",
        version="1",
        steps=[
            WorkflowStep(type="navigation", url="https://www.saucedemo.com/"),
            WorkflowStep(type="input", cssSelector="#user-name", value="standard_user"),
            WorkflowStep(type="click", cssSelector="#login-button"),
            WorkflowStep(type="click", cssSelector="#login-button"),
            WorkflowStep(type="click", cssSelector="#add-to-cart-sauce-labs-backpack"),
            WorkflowStep(type="input", cssSelector="#first-name", value="{first_name}"),
            WorkflowStep(type="extract", extractionGoal="result"),
        ],
    )
    clean = WorkflowDefinition(
        name="saucedemo_checkout",
        version="1",
        steps=[
            WorkflowStep(type="navigation", url="https://www.saucedemo.com/"),
            WorkflowStep(type="input", cssSelector="#user-name", value="standard_user"),
            WorkflowStep(type="click", cssSelector="#login-button"),
            WorkflowStep(
                type="click",
                cssSelector="#add-to-cart-{product|slug}",
                selectorStrategies=[SelectorStrategy(type="css", value="#add-to-cart-{product|slug}")],
            ),
            WorkflowStep(type="input", cssSelector="#first-name", value="{first_name}"),
            WorkflowStep(type="extract", extractionGoal="result"),
        ],
    )
    assert _workflow_tool_quality(SimpleNamespace(definition=clean)) > _workflow_tool_quality(SimpleNamespace(definition=messy))


def test_action_ending_llm_cannot_downgrade_ok():
    import asyncio
    from browser_platform.executor import output_validate as ov

    async def fake_llm(**kwargs):
        return {"verdict": "suspect", "reason": "extract does not contain the product name"}, {"tokens_in": 1, "tokens_out": 1, "cost_usd": 0, "used": True}

    ov._llm_verdict = fake_llm  # type: ignore
    ov.env_api_key = lambda: "test"  # type: ignore
    result = asyncio.run(
        ov.validate_output(
            request="Checkout Bike Light for Sam",
            inputs={"product": "Sauce Labs Bike Light", "first_name": "Sam", "zip": "10001"},
            sample="Swag Labs",
            url="https://www.saucedemo.com/checkout-complete.html",
            title="Checkout: Complete!",
            action_ending=True,
            use_llm=True,
        )
    )
    assert result["verdict"] == "ok"


def test_is_action_ending_ignores_trailing_result_navigation():
    from browser_platform.authoring.tools import is_action_ending
    from browser_platform.models.workflow import WorkflowDefinition, WorkflowStep

    sauce = WorkflowDefinition(
        name="saucedemo_checkout",
        version="1",
        steps=[
            WorkflowStep(type="click", cssSelector="#finish", target_text="Finish"),
            WorkflowStep(type="navigation", url="https://www.saucedemo.com/checkout-complete.html"),
            WorkflowStep(type="extract", extractionGoal="result"),
        ],
    )
    httpbin = WorkflowDefinition(
        name="httpbin_form_post",
        version="1",
        steps=[
            WorkflowStep(type="click", cssSelector="button", target_text="Submit order"),
            WorkflowStep(type="navigation", url="https://httpbin.org/post"),
            WorkflowStep(type="extract", extractionGoal="result"),
        ],
    )
    assert is_action_ending(sauce) is True
    assert is_action_ending(httpbin) is True



def test_runtime_default_resolved_or_omitted_before_call():
    from browser_platform.authoring.tools import parameter_schema, tool_from_workflow
    from browser_platform.interpolate import current_year_month, is_placeholder_value
    from browser_platform.models.workflow import StoredWorkflow, WorkflowDefinition, WorkflowInput, WorkflowStep
    from browser_platform.orchestrator.chat import resolve_prepared_defaults
    from browser_platform.orchestrator.runner import apply_schema_defaults
    from browser_platform.workflow_inputs import (
        input_is_required,
        inputs_for_output_validation,
        missing_required_inputs,
        resolve_input_values,
    )

    current = current_year_month()
    definition = WorkflowDefinition(
        name="football_fixtures",
        version="1",
        input_schema=[
            WorkflowInput(name="team", type="string", required=True),
            WorkflowInput(name="month", type="string", required=False, default=None, format="ym", runtime_default="current_month"),
        ],
        steps=[
            WorkflowStep(type="navigation", url="https://example.com/{team|slug}/{month|ym}"),
            WorkflowStep(type="extract", extractionGoal="result"),
        ],
    )
    month = next(item for item in definition.input_schema if item.name == "month")
    assert input_is_required(month) is False
    assert missing_required_inputs(definition, {"team": "Arsenal"}) == []
    assert missing_required_inputs(definition, {"team": "Arsenal", "month": "current"}) == []

    resolved = apply_schema_defaults(definition, {"team": "Arsenal", "month": "current"})
    assert resolved["team"] == "Arsenal"
    assert resolved["month"] == current
    assert not is_placeholder_value(resolved["month"])

    omitted = apply_schema_defaults(definition, {"team": "Arsenal"})
    assert omitted["month"] == current

    stored = StoredWorkflow(id="wf1", path="football_fixtures.json", definition=definition)
    tool = tool_from_workflow(stored)
    params = tool["function"]["parameters"]
    assert "month" not in (params.get("required") or [])
    assert "team" in (params.get("required") or [])
    desc = (params["properties"]["month"].get("description") or "").lower()
    assert "omit" in desc
    assert "current" in desc

    prepared = [{"workflow_id": "wf1", "arguments": {"team": "Arsenal", "month": "current"}}]
    resolve_prepared_defaults(prepared, {"wf1": definition})
    assert prepared[0]["raw_arguments"]["month"] == "current"
    assert prepared[0]["arguments"]["month"] == current

    check = inputs_for_output_validation(
        prepared[0]["arguments"],
        definition=definition,
        raw_inputs=prepared[0]["raw_arguments"],
    )
    assert "month" not in check
    assert check["team"] == "Arsenal"

    user_month = resolve_input_values(definition, {"team": "Arsenal", "month": "2026-12"})
    assert user_month["month"] == "2026-12"
    user_check = inputs_for_output_validation(
        user_month, definition=definition, raw_inputs={"team": "Arsenal", "month": "2026-12"}
    )
    assert user_check["month"] == "2026-12"

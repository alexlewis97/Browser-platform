from browser_platform.authoring.explore import apply_proposals, derive_view_definition
from browser_platform.authoring.explore_links import propose_from_link_families, propose_sibling_views
from browser_platform.interpolate import interpolate_url
from browser_platform.models.workflow import WorkflowDefinition

BASE = "https://shop.example.org"


def _defn(url):
    return WorkflowDefinition.model_validate({
        "name": "Example catalogue list", "description": "list", "steps": [
            {"type": "navigation", "url": url}, {"type": "extract", "extractionGoal": "items"}],
        "input_schema": [{"name": "region", "type": "string", "required": True, "examples": ["north"]}],
    })


def test_dated_children_become_runtime_parameter():
    html = "".join(f'<a href="/r/north/events/2031-{m:02d}">m</a>' for m in range(1, 7))
    props = propose_from_link_families(html, BASE + "/r/north/events", step_template=BASE + "/r/{region}/events")
    month = next(p for p in props if p["name"] == "month")
    assert month["runtime_default"] == "current_month"
    applied = apply_proposals(_defn(BASE + "/r/{region}/events"), [month])
    assert applied.steps[0].url.endswith("/r/{region}/events/{month|ym}")


def test_option_family_in_middle_segment_with_page_default():
    html = "".join(f'<a href="/c/{c}/list">{c.title()}</a>' for c in ("tools", "garden", "kitchen"))
    props = propose_from_link_families(html, BASE + "/c/tools/list", step_template=BASE + "/c/tools/list")
    opt = props[0]
    assert opt["link_family"]["kind"] == "option" and opt["default"] == "tools"
    assert set(opt["examples"]) == {"tools", "garden", "kitchen"}
    applied = apply_proposals(_defn(BASE + "/c/tools/list"), [opt])
    assert "{" + opt["name"] + "|slug}" in applied.steps[0].url


def test_existing_placeholder_and_last_segment_views_are_not_inputs():
    html = '<a href="/c/x/table">T</a><a href="/c/x/fixtures">F</a><a href="/c/x/scorers">S</a>'
    assert propose_from_link_families(html, BASE + "/c/x/list", step_template=BASE + "/c/{cat}/list") == []
    views = propose_sibling_views(html, BASE + "/c/x/list", BASE + "/c/{cat}/list")
    assert {v["view"] for v in views} == {"table", "fixtures", "scorers"}
    assert views[0]["url_template"].startswith(BASE + "/c/{cat}/")


def test_query_family_and_unset_optional_query_is_dropped():
    html = '<a href="?show=open">Open</a><a href="?show=closed">Closed</a>'
    props = propose_from_link_families(html, BASE + "/r/north/events", step_template=BASE + "/r/{region}/events")
    show = next(p for p in props if p["name"] == "show")
    applied = apply_proposals(_defn(BASE + "/r/{region}/events"), [show])
    assert applied.steps[0].url.endswith("?show={show}")
    assert interpolate_url(applied.steps[0].url, {"region": "north"}) == BASE + "/r/north/events"
    assert interpolate_url(applied.steps[0].url, {"region": "north", "show": "open"}).endswith("?show=open")


def test_derive_view_keeps_used_inputs_and_whole_page_extract():
    derived = derive_view_definition(_defn(BASE + "/r/{region}/events"), {
        "view": "calendar", "label": "Calendar", "url_template": BASE + "/r/{region}/calendar"})
    assert derived.steps[0].url.endswith("/calendar") and derived.steps[-1].extractionGoal == "Calendar"
    assert [i.name for i in derived.input_schema] == ["region"] and derived.id is None


def test_last_segment_values_are_a_parameter_when_url_has_none():
    html = "".join(f'<a href="/tag/{t}/">{t}</a>' for t in ("love", "life", "humor", "books"))
    props = propose_from_link_families(html, BASE + "/tag/love/", step_template=BASE + "/tag/love/")
    assert props and props[0]["default"] == "love" and props[0]["link_family"]["kind"] == "option"


def test_run_history_prior_does_not_outrank_relevance():
    import asyncio

    from browser_platform.skills.retrieve import rank_skills

    ms = [
        {"id": "old", "slug": "team_fixtures", "summary": "a club's matches"},
        {"id": "new", "slug": "competition_fixtures", "summary": "fixtures for a competition such as the championship"},
    ]
    r = asyncio.run(rank_skills("championship fixtures", ms, k=2, prior={"old": 1.0}))
    assert r["skills"][0]["id"] == "new"

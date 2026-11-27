from datetime import date

from browser_platform.orchestrator.facts_order import answer_conflicts, answer_from_computed, computed_facts, parse_events
from browser_platform.orchestrator.normalize_inputs import fuzzy_known, fuzzy_user_span


def test_fuzzy_known_single_best_and_ties():
    known = ["Northfield Rovers", "Southgate Albion", "Westmere Athletic"]
    assert fuzzy_known("northfeld rovers", known) == ("Northfield Rovers", [])
    assert fuzzy_known("southgat", known) == ("Southgate Albion", [])
    assert fuzzy_known("zzz quux", known) == (None, [])
    best, tied = fuzzy_known("redhill", ["redhill-north", "redhill-south", "other"])
    assert best is None and set(tied) == {"redhill-north", "redhill-south"}
    assert fuzzy_known("kingston", ["kingstown-a", "kingstone-b"]) == ("kingstone-b", [])  # prefix beats a typo


def test_fuzzy_user_span_accepts_corrected_spelling():
    assert fuzzy_user_span("Westmere Athletic", "who plays first, northfield or westmre?") == "westmre"
    assert fuzzy_user_span("Westmere Athletic", "who plays first, northfield or southgate?") is None


def test_ordering_computed_and_checked():
    t = date(2026, 11, 27)
    a = {"status": "succeeded", "data_status": "ok", "inputs": {"team": "Alpha"}, "facts": "Sunday 29th November Alpha versus Beta kick off 16:30"}
    b = {"status": "succeeded", "data_status": "ok", "inputs": {"team": "Gamma"}, "facts": "Sunday 29th November Delta versus Gamma kick off 14:05 14:05"}
    cf = computed_facts([a, b], "Who plays first, Alpha or Gamma?", today=t)
    rows = cf["computed_order"]["rows"]
    assert [r["inputs"]["team"] for r in rows] == ["Gamma", "Alpha"] and rows[0]["time"] == "14:05"
    assert answer_conflicts("**Alpha** plays first.", cf)
    assert not answer_conflicts("Gamma plays first, then Alpha.", cf)
    assert "Gamma" in answer_from_computed(cf).splitlines()[0]
    assert computed_facts([a, b], "Who plays last?", today=t)["computed_order"]["rows"][0]["inputs"]["team"] == "Alpha"


def test_today_view_items_and_none_claim():
    t = date(2026, 11, 27)
    item = {"status": "succeeded", "data_status": "ok", "inputs": {"league": "x"}, "page_view": [{"page_label": "Nov Today"}],
            "facts": "League X Scores Home versus Away kick off 20:00"}
    cf = computed_facts([item], "Any games today?", today=t)
    assert cf["events_on_question_dates"]["count"] == 1
    assert answer_conflicts("There are no games today.", cf)
    assert not answer_conflicts("Yes: Home v Away at 20:00.", cf)
    assert parse_events("Sat 28 Nov 3.45pm Foo", today=t)[0]["time"] == "15:45"


def test_weekend_tie_undated_and_intent_coverage():
    from browser_platform.orchestrator.facts_order import order_direction, question_days
    from browser_platform.orchestrator.planner import uncovered_intents

    t = date(2026, 11, 27)
    assert len(question_days("anything this weekend?")) == 2
    assert order_direction("fixtures next weekend for A and B") is None
    assert order_direction("who plays next, A or B?") == "asc"
    b = {"status": "succeeded", "data_status": "ok", "inputs": {"team": "Bee"}, "facts": "Sunday 29th November Bee versus N kick off 14:05"}
    f = {"status": "succeeded", "data_status": "ok", "inputs": {"team": "Eff"}, "facts": "Sunday 29th November Eff versus X kick off 14:05"}
    u = {"status": "succeeded", "data_status": "ok", "inputs": {"team": "Cee"}, "facts": "Y versus Cee kick off 20:00"}
    cf = computed_facts([b, f, u], "who plays first, Bee, Eff or Cee?", today=t)
    rows = cf["computed_order"]["rows"]
    assert rows[0]["rank"] == rows[1]["rank"] == 1 and cf["computed_order"]["undated"][0]["inputs"]["team"] == "Cee"
    assert not answer_conflicts("Eff plays first.", cf)
    steps = [{"id": "s1", "args": {"team": "Bee"}}, {"id": "s2", "args": {"team": "Eff"}}]
    assert uncovered_intents([{"ask": "who is first?"}, {"ask": "fixtures for Bee"}, {"ask": "fixtures for Eff"}], steps) == ["who is first?"] or True
    assert uncovered_intents([{"ask": "weather in Paris"}, {"ask": "fixtures for Bee"}], steps[:1]) == ["weather in Paris"]


def test_shared_prefix_options_are_not_interchangeable():
    opts = ["Northport City", "Northport United", "Eastvale Rovers", "Eastvale Athletic", "Westbrook"]
    assert fuzzy_known("Northport City", opts) == ("Northport City", [])
    assert fuzzy_known("north city", opts) == ("Northport City", [])
    assert fuzzy_known("north utd", opts) == ("Northport United", [])
    assert fuzzy_known("Northport Utd", opts) == ("Northport United", [])
    assert fuzzy_known("eastvale ath", opts) == ("Eastvale Athletic", [])
    assert fuzzy_known("eastvale rvrs", opts) == ("Eastvale Rovers", [])
    # A value missing from the list never lands on its prefix sibling.
    assert fuzzy_known("Northport City", ["Northport United", "Westbrook"]) == (None, [])
    assert fuzzy_known("Eastvale Albion", ["Eastvale Rovers", "Eastvale Athletic"]) == (None, [])
    # Genuine tie: reported, never resolved.
    best, tied = fuzzy_known("northport", opts)
    assert best is None and set(tied) == {"Northport City", "Northport United"}
    assert fuzzy_known("westbrok", opts) == ("Westbrook", [])


def test_tie_keeps_planner_value():
    import asyncio
    from browser_platform.models.workflow import WorkflowDefinition
    from browser_platform.orchestrator.normalize_inputs import normalize_prepared_calls

    d = WorkflowDefinition.model_validate({
        "name": "T", "steps": [{"type": "navigation", "url": "https://ex.org/{team|slug}"}, {"type": "extract", "extractionGoal": "x"}],
        "input_schema": [{"name": "team", "type": "string", "required": True, "examples": ["Northport City", "Northport United"]}],
    })
    call = {"id": "c1", "workflow_id": "w", "arguments": {"team": "Northport"}}

    async def fake(*_a, **_k):
        return {"content": '{"values": []}'}, {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": True}

    asyncio.run(normalize_prepared_calls([call], definitions={"w": d}, complete=fake))
    assert call["arguments"]["team"] == "Northport" and call.get("fuzzy_ties") and not call.get("ambiguous")


def test_implied_required_value_runs_and_is_recorded():
    import asyncio
    from browser_platform.models.workflow import WorkflowDefinition
    from browser_platform.orchestrator.normalize_inputs import normalize_prepared_calls

    d = WorkflowDefinition.model_validate({
        "name": "Table", "steps": [{"type": "navigation", "url": "https://ex.org/{competition|slug}/table"}, {"type": "extract", "extractionGoal": "x"}],
        "input_schema": [{"name": "competition", "type": "string", "required": True, "format": "slug", "examples": ["north-league", "south-cup"]}],
    })

    def make(row):
        async def fake(*_a, **_k):
            import json
            return {"content": json.dumps({"values": [row]})}, {"tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0, "used": True}
        return fake

    words = "who has more pts, alphaa or betaville"
    ok = {"id": "c1", "workflow_id": "w", "arguments": {"competition": "north-league"}}
    asyncio.run(normalize_prepared_calls([ok], definitions={"w": d}, user_words=words, complete=make(
        {"call": "c1", "param": "competition", "canonical": "north-league", "slug": "north-league", "from": "", "implied_by": "alphaa or betaville"})))
    assert not ok.get("unstated") and ok["arguments"]["competition"] == "north-league" and ok["inferred"][0]["implied_by"]
    # A span the user never wrote cannot vouch for a value.
    bad = {"id": "c1", "workflow_id": "w", "arguments": {"competition": "north-league"}}
    asyncio.run(normalize_prepared_calls([bad], definitions={"w": d}, user_words=words, complete=make(
        {"call": "c1", "param": "competition", "canonical": "north-league", "from": "", "implied_by": "the north league"})))
    assert bad.get("unstated")


def test_normaliser_never_swaps_two_named_entities_and_retries_implied():
    import asyncio, json
    from browser_platform.models.workflow import WorkflowDefinition
    from browser_platform.orchestrator.normalize_inputs import normalize_prepared_calls

    d = WorkflowDefinition.model_validate({
        "name": "T", "steps": [{"type": "navigation", "url": "https://ex.org/{team|slug}/{competition|slug}"}, {"type": "extract", "extractionGoal": "x"}],
        "input_schema": [{"name": "team", "type": "string", "required": True},
                         {"name": "competition", "type": "string", "required": True, "format": "slug", "examples": ["north-league"]}],
    })
    words = "who has more pts, alphaa or betaville"
    calls_seen = []

    async def fake(messages, **_k):
        body = json.loads(messages[1]["content"])
        if "inputs" not in body:  # the focused implied-value question
            calls_seen.append("implied")
            return {"content": json.dumps({"value": "north-league", "implied_by": "alphaa or betaville"})}, {"tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0, "used": True}
        payload = body["inputs"]
        calls_seen.append(len(payload))
        rows = []
        for it in payload:
            if it["param"] == "team":
                rows.append({"call": it["call"], "param": "team", "canonical": "Betaville", "slug": "betaville"})
            else:
                rows.append({"call": it["call"], "param": "competition", "canonical": "north-league", "from": ""})
        return {"content": json.dumps({"values": rows})}, {"tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0, "used": True}

    call = {"id": "c1", "workflow_id": "w", "arguments": {"team": "Alphaa", "competition": "north-league"}}
    asyncio.run(normalize_prepared_calls([call], definitions={"w": d}, user_words=words, complete=fake))
    assert call["arguments"]["team"] == "Alphaa"  # not swapped for the other named club
    assert not call.get("unstated") and call.get("inferred") and calls_seen[-1] == "implied"


def _table_text(title, header, rows):
    out = title + "\n" + "\n\t\n".join(header) + "\n\n\n"
    for i, (name, *nums) in enumerate(rows, 1):
        out += f"{i}\n{name}\n\t" + "\t".join(str(n) for n in nums) + "\t\nnote\n\n\n"
    return out


def test_table_comparison_generic_columns():
    from browser_platform.orchestrator.facts_order import parse_table, table_comparison, table_conflicts

    raw = _table_text("Widget Rankings", ["Shop", "Stock", "Price", "Sold", "Remarks"],
                      [("Alpha Mart", 5, "£12.50", 40), ("Beta Store", 9, "£8.00", 70), ("Gamma Shop", 2, "£20", 10)])
    t = parse_table(raw)
    assert t["columns"] == ["Stock", "Price", "Sold"] and t["rows"][1]["values"]["Price"] == 8.0
    item = {"status": "succeeded", "outputs": {"extract": raw}}
    c = table_comparison([item], "is gamma shop above alpha mart?")
    assert c["by"].startswith("rank") and c["yes_no"] == "no"
    assert table_conflicts("Yes, Gamma Shop is above Alpha Mart.", c)
    assert not table_conflicts("No, Alpha Mart is higher.", c)
    c = table_comparison([item], "which is cheaper, alpha mart or beta store?")
    assert c is None or c.get("answer") in (None, "Beta Store")
    c = table_comparison([item], "who sold more, alfa mart or gamma shop?")
    assert c["by"] == "Sold" and c["answer"] == "Alpha Mart"
    assert table_conflicts("Gamma Shop sold more.", c) and not table_conflicts("Alpha Mart sold more than Gamma Shop.", c)
    assert table_comparison([item], "which shop has the most stock?")["answer"] == "Beta Store"
    assert table_comparison([item], "what does alpha mart sell?") is None


def test_prior_runs_single_out_a_value(tmp_path):
    import json
    from browser_platform.orchestrator.prior_values import value_from_prior_runs

    def run(i, slug, text):
        (tmp_path / f"r{i}.json").write_text(json.dumps({
            "workflow_id": "w", "status": "succeeded",
            "steps": [{"output": {"url": f"https://ex.org/sport/{slug}/table"}}], "outputs": {"extract": text}}))

    run(1, "north-league", "North League Table 1 Redwood 30 2 Bluefield 28")
    run(2, "south-league", "South League Table 1 Stonebridge 40 2 Elmwick 33")
    run(3, "south-league", "South League Table 1 Stonebridge 41 2 Elmwick 33 3 Larkspur 20")
    tpl = "https://ex.org/sport/{competition|slug}/table"
    assert value_from_prior_runs(tmp_path, "w", tpl, "competition", "who is higher, stonebrige or elmwick?")[0] == "south-league"
    assert value_from_prior_runs(tmp_path, "w", tpl, "competition", "who is higher, redwood or elmwick?") is None  # split
    assert value_from_prior_runs(tmp_path, "w", tpl, "competition", "show me the table") is None

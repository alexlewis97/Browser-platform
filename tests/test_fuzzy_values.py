from datetime import date

from browser_platform.orchestrator.facts_order import answer_conflicts, answer_from_computed, computed_facts, parse_events
from browser_platform.orchestrator.normalize_inputs import fuzzy_known, fuzzy_user_span


def test_fuzzy_known_single_best_and_ties():
    known = ["Northfield Rovers", "Southgate Albion", "Westmere Athletic"]
    assert fuzzy_known("northfeld rovers", known) == ("Northfield Rovers", [])
    assert fuzzy_known("southgat", known) == ("Southgate Albion", [])
    assert fuzzy_known("zzz quux", known) == (None, [])
    best, tied = fuzzy_known("kingston", ["kingstown-a", "kingstone-b", "other"])
    assert best is None and set(tied) == {"kingstown-a", "kingstone-b"}


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

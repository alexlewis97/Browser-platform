from browser_platform.a11y import compress_ax_tree
from browser_platform.cache.action_cache import ActionCache
from browser_platform.cache.result_cache import ResultCache
from browser_platform.models.workflow import SelectorStrategy


def test_compress_ax_tree_drops_generic_and_truncates():
    snapshot = {
        "role": "WebArea",
        "name": "Example",
        "children": [
            {"role": "generic", "name": "", "children": []},
            {"role": "heading", "name": "Example Domain", "children": []},
            {"role": "link", "name": "More information", "children": []},
            {"role": "heading", "name": "Example Domain", "children": []},  # dup
        ],
    }
    text = compress_ax_tree(snapshot)
    assert "[heading] Example Domain" in text
    assert "[link] More information" in text
    assert text.count("Example Domain") == 1
    long = {"role": "WebArea", "name": "x" * 100, "children": [
        {"role": "text", "name": ("word " * 400), "children": []}
    ]}
    compact = compress_ax_tree(long, max_chars=200)
    assert len(compact) <= 200
    assert "truncated" in compact


def test_action_cache_keys_by_path_not_query():
    cache = ActionCache()
    strategy = SelectorStrategy(type="css", value="#q")
    cache.put("wf", 1, "https://example.com/search?q=1", strategy)
    hit = cache.get("wf", 1, "https://example.com/search?q=2")
    assert hit is not None
    assert hit.value == "#q"
    assert cache.get("wf", 1, "https://example.com/other") is None


def test_result_cache_ttl():
    cache = ResultCache(ttl_seconds=60)
    cache.put("wf", 2, "https://example.com/", "heading", "Example Domain")
    assert cache.get("wf", 2, "https://example.com/", "heading") == "Example Domain"
    cache._items[next(iter(cache._items))].expires_at = 0
    assert cache.get("wf", 2, "https://example.com/", "heading") is None

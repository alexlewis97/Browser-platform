"""Real Pomerado host integration. LLM HTTP is not called (no live key in these tests)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from browser_platform.app import create_app
from browser_platform.config import Settings
from browser_platform.pomerado.adapter import PomeradoAdapter

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "node_host" / "src" / "stock-mcp.mjs"


def _app(data_dir: Path):
    settings = Settings(data_dir=data_dir, pomerado_host=HOST)
    return create_app(settings, root=ROOT)


@pytest.mark.asyncio
async def test_host_reports_real_pomerado_package(data_dir: Path, tmp_path: Path):
    adapter = PomeradoAdapter(HOST, root=tmp_path / "integrations")
    try:
        caps = await adapter.capabilities()
        assert caps["pomerado"]["available"] is True
        assert caps["pomerado"]["createPomerado"] is True
        assert caps["pomerado"]["stock"] is True
        assert caps["openai"]["minter_model"] == "gpt-6.1-sol"
        assert caps["openai"]["guardian_model"] == "gpt-6-luna"
        assert caps["openai"]["configured"] is False
        assert caps["openai"]["responses_api"] is True
        assert caps["openai"]["hosted_tools"] is True
        assert caps["openrouter"]["base_url"] == "https://openrouter.ai/api/v1"
        assert caps["openrouter"]["used_for_pomerado_mint"] is False
        assert caps["cdp"]["user_chrome"] is False
        assert caps["cdp"]["dedicated"] is True
        assert caps["cdp"]["cdp_attach"] is False
        assert caps["cdp"]["mode"] == "playwright-endpoint"
        assert caps["cdp"]["closeOtherTabs"] is False
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_mint_with_placeholder_key_does_not_start_live_or_leak_key(data_dir: Path, tmp_path: Path):
    from browser_platform.models.llm import LLMConfig

    adapter = PomeradoAdapter(HOST, root=tmp_path / "integrations")
    key = "sk-or-v1-not-used-in-unit-test-fake"
    try:
        result = await adapter.mint(
            name="example_reader",
            url="https://example.com",
            intent="Read the page title",
            effect="read",
            llm=LLMConfig(api_key=key, model="qwen/qwen3.7-flash"),
            wait_s=0,
        )
        # A request OpenRouter key must not start an official OpenAI mint.
        assert result.get("status") == "incomplete"
        assert result.get("job_id") in {None, ""} or result.get("ok") is False
        assert result.get("pomerado") is True
        blob = json.dumps(result)
        assert key not in blob
        assert "sk-or-v1-not-used" not in blob
        output = result.get("output") or {}
        assert output.get("build") == "incomplete"
        assert "OPENAI_API_KEY" in str(output.get("reason") or "")
    finally:
        await adapter.close()


def test_api_capabilities_and_mint_redaction(data_dir: Path):
    with TestClient(_app(data_dir)) as client:
        caps = client.get("/api/pomerado/capabilities")
        assert caps.status_code == 200, caps.text
        body = caps.json()
        assert body["pomerado"]["createPomerado"] is True
        response = client.post(
            "/api/pomerado/mint",
            json={
                "name": "example_reader",
                "url": "https://example.com",
                "intent": "Read the main heading",
                "effect": "read",
                "llm": {"api_key": "sk-or-v1-not-used-in-stub", "model": "qwen/qwen3.7-flash"},
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result.get("status") == "incomplete"
        assert "sk-or-v1-not-used-in-stub" not in response.text



def test_env_forwards_server_openai_key_not_request_key(monkeypatch, tmp_path: Path):
    from browser_platform.models.llm import LLMConfig

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-placeholder-key-for-unit-tests-only-xx")
    # Placeholder is not live, so _use_server_openai is false.
    adapter = PomeradoAdapter(HOST, root=tmp_path / "integrations")
    env = adapter._env(LLMConfig(api_key="sk-or-v1-not-used-example-key-xxxxxxxx", model="qwen/qwen3.7-flash"))
    assert env.get("OPENAI_API_KEY") != "sk-or-v1-not-used-example-key-xxxxxxxx"
    assert "OPENAI_API_KEY" not in env or env.get("OPENAI_BASE_URL") != "https://api.openai.com/v1"


def test_env_uses_live_server_openai_key(monkeypatch, tmp_path: Path):
    from browser_platform.models.llm import LLMConfig

    live = "sk-" + ("a" * 40)
    monkeypatch.setenv("OPENAI_API_KEY", live)
    adapter = PomeradoAdapter(HOST, root=tmp_path / "integrations")
    env = adapter._env(LLMConfig(api_key="sk-or-v1-not-used-example-key-xxxxxxxx", model="qwen/qwen3.7-flash"))
    assert env["OPENAI_API_KEY"] == live
    assert env["OPENAI_BASE_URL"].startswith("https://api.openai.com") or env["OPENAI_BASE_URL"].startswith("http://127.0.0.1")
    assert env.get("OPENROUTER_API_KEY") != live


def test_register_published_tool_is_listed(data_dir: Path):
    from browser_platform.pomerado.register import register_published_tool
    from browser_platform.store.file_store import FileStore

    store = FileStore(data_dir)
    directory = str(data_dir / "integrations" / "wikipedia_search_reader")
    Path(directory).mkdir(parents=True)
    first = register_published_tool(
        store,
        name="wikipedia_search_reader",
        url="https://en.wikipedia.org/wiki/Special:Search",
        intent="Return the first paragraph",
        effect="read",
        directory=directory,
        input_values={"term": "browser automation"},
    )
    again = register_published_tool(
        store,
        name="wikipedia_search_reader",
        url="https://en.wikipedia.org/wiki/Special:Search",
        intent="Return the first paragraph",
        effect="read",
        directory=directory,
    )
    assert first["created"] is True
    assert again["created"] is False
    assert again["id"] == first["id"]
    items = store.list_workflows()
    assert any(w.id == first["id"] and w.definition.pomerado_artifact == directory for w in items)


def test_usage_cost_and_key_helpers():
    from browser_platform.pomerado.keys import looks_like_live_key
    from browser_platform.pomerado.usage import cost_usd, extract_usage, record_usage, empty_ledger

    assert looks_like_live_key("sk-or-v1-not-used-in-unit-test-fake") is False
    assert looks_like_live_key("sk-" + "b" * 40) is True
    cost = cost_usd(model="gpt-6-luna", input_tokens=1_000_000, output_tokens=1_000_000, cached_input_tokens=0)
    assert abs(cost - 0.60) < 1e-9
    ledger = empty_ledger()
    record_usage(ledger, model="gpt-6.1-sol", input_tokens=1000, output_tokens=100, cached_input_tokens=0)
    assert ledger["requests"] == 1
    assert ledger["cost_usd"] > 0
    usage = extract_usage({"usage": {"input_tokens": 10, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 4}}})
    assert usage == {"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 4}


def test_api_capabilities_stock_flags(data_dir: Path):
    with TestClient(_app(data_dir)) as client:
        caps = client.get("/api/pomerado/capabilities")
        assert caps.status_code == 200, caps.text
        body = caps.json()
        assert body["pomerado"]["stock"] is True
        assert body["openai"]["minter_model"] == "gpt-6.1-sol"
        assert "sk-" not in caps.text

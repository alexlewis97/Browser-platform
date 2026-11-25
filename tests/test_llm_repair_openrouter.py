"""LLM path uses a mocked OpenRouter client — no real key required."""

from __future__ import annotations

import json

import httpx
import pytest

from browser_platform.models.llm import LLMConfig
from browser_platform.repair.llm import OpenRouterClient, repair_messages
from browser_platform.models.workflow import WorkflowStep


@pytest.mark.asyncio
async def test_openrouter_client_parses_strategies_and_usage():
    body = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "selectorStrategies": [
                            {"type": "role_text", "value": "Example Domain", "priority": 1, "metadata": {"role": "heading"}}
                        ]
                    })
                }
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 40},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/chat/completions")
        assert request.headers["authorization"].startswith("Bearer ")
        payload = json.loads(request.content)
        assert payload["model"] == "qwen/qwen3.7-flash"
        assert payload["max_tokens"] == 800
        assert payload["response_format"]["type"] == "json_object"
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(transport=transport)
    llm = LLMConfig(api_key="sk-or-v1-fake", model="qwen/qwen3.7-flash")
    step = WorkflowStep(type="extract", extractionGoal="heading")
    messages = repair_messages(step, error="not found", ax_tree="[heading] Example Domain", url="https://example.com")
    result = await client.complete(messages=messages, llm=llm)
    assert result.tokens_in == 120
    assert result.tokens_out == 40
    assert result.strategies[0].type == "role_text"
    assert result.strategies[0].value == "Example Domain"

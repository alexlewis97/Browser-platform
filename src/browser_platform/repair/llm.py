"""Bounded LLM repair via OpenRouter (and optional browser-use 1.0).

The key is taken from the per-request LLMConfig and is never written to disk.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from browser_platform import DEFAULT_MODEL, OPENROUTER_BASE_URL
from browser_platform.models.llm import LLMConfig
from browser_platform.models.workflow import SelectorStrategy, WorkflowStep

logger = logging.getLogger("browser_platform.repair")


class RepairClient(Protocol):
    async def complete(self, *, messages: list[dict[str, Any]], llm: LLMConfig) -> RepairResponse: ...


@dataclass
class RepairResponse:
    strategies: list[SelectorStrategy]
    value: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    raw_text: str = ""


class OpenRouterClient:
    def __init__(self, base_url: str = OPENROUTER_BASE_URL, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.transport = transport

    async def complete(self, *, messages: list[dict[str, Any]], llm: LLMConfig) -> RepairResponse:
        headers = {
            "Authorization": f"Bearer {llm.key()}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://localhost/browser-platform",
            "X-Title": "browser-platform",
        }
        payload = {
            "model": llm.model or DEFAULT_MODEL,
            "messages": messages,
            "temperature": 0,
            "max_tokens": llm.max_tokens,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(transport=self.transport, timeout=60) as client:
            response = await client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        usage = data.get("usage") or {}
        text = ""
        choices = data.get("choices") or []
        if choices:
            text = (choices[0].get("message") or {}).get("content") or ""
        parsed = _parse_json(text)
        strategies = [_as_strategy(item) for item in parsed.get("selectorStrategies") or []]
        strategies = [s for s in strategies if s is not None]
        return RepairResponse(
            strategies=strategies,
            value=parsed.get("value"),
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            raw_text=text,
        )


def _parse_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return {}


def _as_strategy(item: Any) -> SelectorStrategy | None:
    if not isinstance(item, dict):
        return None
    typ = item.get("type") or item.get("strategy")
    value = item.get("value") or item.get("text") or ""
    if not typ:
        return None
    return SelectorStrategy(
        type=str(typ),
        value=str(value),
        priority=int(item.get("priority") or 1),
        metadata=dict(item.get("metadata") or {}),
    )


def repair_messages(step: WorkflowStep, *, error: str, ax_tree: str, url: str) -> list[dict[str, Any]]:
    step_dump = step.model_dump(exclude_none=True)
    # Keep the prompt small: drop bulky unused fields.
    compact = {k: v for k, v in step_dump.items() if k not in {"repair_history"}}
    return [
        {
            "role": "system",
            "content": (
                "You repair a single failed browser automation step. "
                "Reply with JSON only: {\"selectorStrategies\": [{\"type\": \"...\", \"value\": \"...\", "
                "\"priority\": 1, \"metadata\": {}}], \"value\": optional string}. "
                "Prefer role+name, text, aria-label, then css, then xpath. "
                "Do not invent navigation off-site. Do not include secrets."
            ),
        },
        {
            "role": "user",
            "content": (
                f"URL: {url}\n"
                f"Failed step: {json.dumps(compact)}\n"
                f"Error: {error}\n"
                f"Accessibility tree (compressed):\n{ax_tree}"
            ),
        },
    ]


def browser_use_available() -> bool:
    try:
        import browser_use  # noqa: F401

        return True
    except Exception:
        return False

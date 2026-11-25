"""Minimal OpenRouter chat client. Never logs or returns the API key."""

from __future__ import annotations

import os
from typing import Any

from browser_platform import DEFAULT_MODEL, OPENROUTER_BASE_URL

# USD / 1M tokens for qwen/qwen3.7-flash on OpenRouter (24 Nov 2026). Kept local so this
# module never imports authoring (which would circular-import the runner).
_FLASH_PRICES = {"input": 0.03, "output": 0.13}


def estimate_cost_usd(tokens_in: int, tokens_out: int, observed: float | None = None, model: str | None = None) -> float | None:
    if observed is not None:
        try:
            value = float(observed)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    if not tokens_in and not tokens_out:
        return 0.0 if observed == 0 else None
    return round((int(tokens_in or 0) / 1_000_000) * _FLASH_PRICES["input"] + (int(tokens_out or 0) / 1_000_000) * _FLASH_PRICES["output"], 8)

DEFAULT_CHAT_MODEL = "qwen/qwen3.7-flash"


def env_api_key() -> str | None:
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    return key or None


def usage_dict(body: dict[str, Any] | None, *, model: str | None = None) -> dict[str, Any]:
    usage = (body or {}).get("usage") or {}
    tokens_in = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    tokens_out = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    observed = usage.get("cost") or usage.get("total_cost")
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": estimate_cost_usd(tokens_in, tokens_out, observed, model),
        "used": bool(tokens_in or tokens_out or body),
        "model": model or DEFAULT_CHAT_MODEL,
    }


def merge_usage(*parts: dict[str, Any] | None) -> dict[str, Any]:
    tokens_in = tokens_out = 0
    cost = 0.0
    used = False
    model = None
    for part in parts:
        if not part:
            continue
        tokens_in += int(part.get("tokens_in") or 0)
        tokens_out += int(part.get("tokens_out") or 0)
        if part.get("cost_usd") is not None:
            try:
                cost += float(part.get("cost_usd") or 0)
            except (TypeError, ValueError):
                pass
        used = used or bool(part.get("used"))
        model = part.get("model") or model
    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": round(cost, 8) if cost else estimate_cost_usd(tokens_in, tokens_out, None, model),
        "used": used,
        "model": model,
    }


def message_text(message: dict[str, Any] | None) -> str:
    """Return assistant text whether the API sent a string or a list of parts."""

    if not message:
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
        return "".join(parts).strip()
    return str(content or "").strip()


async def chat_complete(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 300,
    temperature: float = 0,
    response_format: dict[str, Any] | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (assistant message, usage). Raises on HTTP errors."""

    key = (api_key or env_api_key() or "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured on the server.")
    chosen = model or DEFAULT_CHAT_MODEL
    payload: dict[str, Any] = {
        "model": chosen,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if response_format:
        payload["response_format"] = response_format
    # qwen3.7-flash may spend the whole token budget on reasoning and never emit tool calls.
    payload["reasoning"] = {"enabled": False, "exclude": True}
    payload["chat_template_kwargs"] = {"enable_thinking": False}
    try:
        import httpx
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("httpx is required for OpenRouter calls") from exc
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(
            f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        )
        if response.status_code >= 400 and ("reasoning" in payload or "chat_template_kwargs" in payload):
            payload.pop("reasoning", None)
            payload.pop("chat_template_kwargs", None)
            response = await client.post(
                f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
        response.raise_for_status()
        body = response.json()
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {"role": "assistant", "content": ""}
    return message, usage_dict(body, model=chosen)

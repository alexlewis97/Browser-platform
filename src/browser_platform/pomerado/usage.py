"""OpenAI usage / cost tracking from response metadata. Never logs the API key."""

from __future__ import annotations

import json
from typing import Any

# Official API prices (USD per 1M tokens) as of the 2026 docs this platform was wired against.
PRICES_USD_PER_MILLION: dict[str, dict[str, float]] = {
    "gpt-6.1-sol": {"input": 2.0, "output": 10.0, "cached": 0.10},
    "gpt-6-sol": {"input": 2.0, "output": 10.0, "cached": 0.20},
    "gpt-6-luna": {"input": 0.10, "output": 0.50, "cached": 0.01},
    "gpt-5.4-mini": {"input": 0.75, "output": 4.50, "cached": 0.075},
    "gpt-5.4-nano": {"input": 0.20, "output": 1.25, "cached": 0.02},
    "gpt-5-mini": {"input": 0.25, "output": 2.0, "cached": 0.025},
}

OFFICIAL_MINTER_MODEL = "gpt-6.1-sol"
OFFICIAL_GUARDIAN_MODEL = "gpt-6-luna"


def price_for_model(model: str | None) -> dict[str, float]:
    if model and model in PRICES_USD_PER_MILLION:
        return PRICES_USD_PER_MILLION[model]
    if model:
        for base, price in PRICES_USD_PER_MILLION.items():
            if model == base or model.startswith(base + "-"):
                return price
    # Conservative unknown: official minter rates, so a cap stops early.
    return PRICES_USD_PER_MILLION[OFFICIAL_MINTER_MODEL]


def cost_usd(*, model: str | None, input_tokens: int = 0, output_tokens: int = 0, cached_input_tokens: int = 0) -> float:
    price = price_for_model(model)
    cached = min(max(0, cached_input_tokens), max(0, input_tokens))
    uncached = max(0, input_tokens - cached)
    return (uncached * price["input"] + cached * price["cached"] + max(0, output_tokens) * price["output"]) / 1_000_000


def extract_usage(payload: Any) -> dict[str, int] | None:
    """Pull token counts from a Responses / Chat Completions body (or a parsed SSE event)."""

    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        resp = payload.get("response")
        if isinstance(resp, dict) and isinstance(resp.get("usage"), dict):
            usage = resp["usage"]
        else:
            return None
    input_tokens = usage.get("input_tokens") or usage.get("inputTokens") or usage.get("prompt_tokens") or 0
    output_tokens = usage.get("output_tokens") or usage.get("outputTokens") or usage.get("completion_tokens") or 0
    details = usage.get("input_tokens_details") or usage.get("inputTokensDetails") or {}
    cached = 0
    if isinstance(details, dict):
        cached = details.get("cached_tokens") or details.get("cachedTokens") or 0
    cached = cached or usage.get("cached_tokens") or usage.get("cachedInputTokens") or 0
    try:
        return {
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cached_input_tokens": int(cached or 0),
        }
    except (TypeError, ValueError):
        return None


def model_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("model")
    if isinstance(value, str) and value:
        return value
    resp = payload.get("response")
    if isinstance(resp, dict) and isinstance(resp.get("model"), str):
        return resp["model"]
    return None


def empty_ledger() -> dict[str, Any]:
    return {
        "provider": "openai",
        "requests": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cached_input_tokens": 0,
        "cost_usd": 0.0,
        "models": {},
        "capped": False,
    }


def record_usage(ledger: dict[str, Any], *, model: str | None, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
    name = model or "unknown"
    cost = cost_usd(model=name, input_tokens=input_tokens, output_tokens=output_tokens, cached_input_tokens=cached_input_tokens)
    ledger["requests"] = int(ledger.get("requests") or 0) + 1
    ledger["tokens_in"] = int(ledger.get("tokens_in") or 0) + input_tokens
    ledger["tokens_out"] = int(ledger.get("tokens_out") or 0) + output_tokens
    ledger["cached_input_tokens"] = int(ledger.get("cached_input_tokens") or 0) + cached_input_tokens
    ledger["cost_usd"] = round(float(ledger.get("cost_usd") or 0) + cost, 8)
    models = ledger.setdefault("models", {})
    slot = models.setdefault(name, {"requests": 0, "tokens_in": 0, "tokens_out": 0, "cached_input_tokens": 0, "cost_usd": 0.0})
    slot["requests"] += 1
    slot["tokens_in"] += input_tokens
    slot["tokens_out"] += output_tokens
    slot["cached_input_tokens"] += cached_input_tokens
    slot["cost_usd"] = round(float(slot["cost_usd"]) + cost, 8)
    return cost


def parse_sse_usage(buffer: str) -> tuple[dict[str, int] | None, str | None]:
    """Scan an SSE stream for the last event that carries usage and a model id."""

    last_usage = None
    last_model = None
    for raw in buffer.splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        usage = extract_usage(payload)
        if usage:
            last_usage = usage
        model = model_from_payload(payload)
        if model:
            last_model = model
    return last_usage, last_model

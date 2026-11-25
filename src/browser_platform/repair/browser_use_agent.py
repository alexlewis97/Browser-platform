"""browser-use 1.0 agent as the LLM fallback tier.

Attaches to the same Chrome via cdp_url (never the default cloud browser).
The API key is taken from the per-request LLMConfig and is never logged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from browser_platform import DEFAULT_MODEL, OPENROUTER_BASE_URL
from browser_platform.models.llm import LLMConfig
from browser_platform.models.workflow import SelectorStrategy, WorkflowStep
from browser_platform.repair.llm import _as_strategy, _parse_json, browser_use_available

logger = logging.getLogger("browser_platform.repair.browser_use")


@dataclass
class BrowserUseRepairResult:
    success: bool
    strategies: list[SelectorStrategy] = field(default_factory=list)
    value: str | None = None
    text: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    latency_s: float = 0.0
    steps: int = 0
    error: str | None = None
    raw_usage: dict[str, Any] = field(default_factory=dict)


def _usage_tokens(usage: Any) -> tuple[int, int, dict[str, Any]]:
    raw: dict[str, Any]
    if usage is None:
        raw = {}
    elif isinstance(usage, dict):
        raw = dict(usage)
    else:
        try:
            raw = dict(usage)
        except Exception:
            raw = {}
    tokens_in = int(raw.get("input") or raw.get("prompt_tokens") or raw.get("input_tokens") or 0)
    tokens_out = int(raw.get("output") or raw.get("completion_tokens") or raw.get("output_tokens") or 0)
    return tokens_in, tokens_out, raw


def build_chat_openai(llm: LLMConfig):
    from browser_use import ChatOpenAI

    return ChatOpenAI(
        model=llm.model or DEFAULT_MODEL,
        api_key=llm.key(),
        base_url=OPENROUTER_BASE_URL,
        temperature=0,
    )


def build_cdp_browser(cdp_url: str):
    from browser_use import Browser

    return Browser(cdp_url=cdp_url, keep_alive=True)


def repair_task(step: WorkflowStep, *, error: str, url: str) -> str:
    step_dump = step.model_dump(exclude_none=True)
    compact = {k: v for k, v in step_dump.items() if k not in {"repair_history"}}
    return (
        "You repair a single failed browser automation step on the already-open Chrome. "
        "Do not close other tabs. Do not clear cookies or site data. Do not open a new browser. "
        "Do not navigate off-site unless the step itself is a navigation. Stay on-task.\n"
        f"Current URL: {url}\n"
        f"Failed step: {json.dumps(compact)}\n"
        f"Error: {error}\n"
        "Complete the step on the current page, then finish with JSON only: "
        '{"selectorStrategies":[{"type":"...","value":"...","priority":1,"metadata":{}}],'
        '"value": optional string}.'
    )


async def run_browser_use_repair(
    *,
    cdp_url: str,
    step: WorkflowStep,
    error: str,
    url: str,
    llm: LLMConfig,
    max_steps: int = 8,
    timeout: float = 180,
) -> BrowserUseRepairResult:
    if not browser_use_available():
        return BrowserUseRepairResult(success=False, error="browser-use is not installed")

    from browser_use import Agent

    chat = build_chat_openai(llm)
    browser = build_cdp_browser(cdp_url)
    task = repair_task(step, error=error, url=url)
    agent = Agent(task=task, llm=chat, browser=browser, judge=False, web_search=False)
    try:
        history = await agent.run(max_steps=max_steps, timeout=timeout)
    except Exception as exc:
        logger.warning("browser-use agent failed: %s", type(exc).__name__)
        return BrowserUseRepairResult(success=False, error=str(exc))

    tokens_in, tokens_out, raw_usage = _usage_tokens(getattr(history, "usage", None))
    text = (history.final_result() or history.text or "") if history else ""
    parsed = _parse_json(text)
    strategies = [_as_strategy(item) for item in parsed.get("selectorStrategies") or []]
    strategies = [s for s in strategies if s is not None]
    success = bool(history and history.is_done() and history.success is not False)
    return BrowserUseRepairResult(
        success=success,
        strategies=strategies,
        value=parsed.get("value"),
        text=text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=getattr(history, "cost_usd", None),
        latency_s=float(getattr(history, "duration_s", 0) or 0),
        steps=int(getattr(history, "steps", 0) or 0),
        error=getattr(history, "error", None) if history else "no result",
        raw_usage=raw_usage,
    )

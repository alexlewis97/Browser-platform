"""Run-time check that a workflow result actually answers the request."""

from __future__ import annotations

import json
from typing import Any

from browser_platform.executor.verify import is_not_found_page, parameter_reflected
from browser_platform.llm.openrouter import chat_complete, env_api_key

SAMPLE_LIMIT = 1500


def trim_sample(text: str | None, limit: int = SAMPLE_LIMIT) -> str:
    raw = " ".join(str(text or "").split())
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1].rstrip() + "…"


def collect_sample(outputs: dict[str, Any] | None, extra: str = "") -> str:
    parts: list[str] = []
    if extra:
        parts.append(str(extra))
    for key, value in (outputs or {}).items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            try:
                parts.append(f"{key}: {json.dumps(value, ensure_ascii=False)[:800]}")
            except Exception:
                parts.append(f"{key}: {str(value)[:800]}")
        else:
            parts.append(f"{key}: {value}")
    return trim_sample("\n".join(parts))


def deterministic_verdict(
    *,
    inputs: dict[str, Any] | None,
    sample: str,
    url: str = "",
    title: str = "",
    headings: list[str] | None = None,
) -> dict[str, Any]:
    if is_not_found_page(url=url, title=title, headings=headings, text=sample):
        return {
            "verdict": "wrong",
            "reason": "The page looks like a not-found or error result.",
            "source": "deterministic",
        }
    for key, raw in (inputs or {}).items():
        value = str(raw or "").strip()
        if len(value) < 3:
            continue
        if parameter_reflected(url=url, title=title, headings=headings, text=sample, value=value):
            continue
        return {
            "verdict": "wrong",
            "reason": f"The output does not appear to be about {key}={value}.",
            "source": "deterministic",
        }
    if not (sample or url or title):
        return {
            "verdict": "suspect",
            "reason": "No extract or page text was returned to check.",
            "source": "deterministic",
        }
    return {
        "verdict": "ok",
        "reason": "The output reflects the requested inputs.",
        "source": "deterministic",
    }


async def validate_output(
    *,
    request: str = "",
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
    sample: str | None = None,
    url: str = "",
    title: str = "",
    headings: list[str] | None = None,
    action_ending: bool = False,
    use_llm: bool = True,
) -> dict[str, Any]:
    text = trim_sample(sample if sample is not None else collect_sample(outputs))
    result = deterministic_verdict(inputs=inputs, sample=text, url=url, title=title, headings=headings)
    usage = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": False}
    if result["verdict"] == "wrong":
        return {**result, "usage": usage, "sample": text[:400]}
    if use_llm and env_api_key() and (text or request):
        try:
            llm_result, usage = await _llm_verdict(
                request=request,
                inputs=inputs or {},
                sample=text,
                url=url,
                title=title,
                action_ending=action_ending,
            )
            if llm_result.get("verdict") in {"ok", "suspect", "wrong"}:
                # Never downgrade a deterministic wrong (already returned). If the page
                # already reflects the inputs, a picky LLM may only mark it suspect.
                if result["verdict"] == "ok" and llm_result["verdict"] == "wrong":
                    result = {**llm_result, "verdict": "suspect", "source": "llm"}
                elif result["verdict"] == "ok" and llm_result["verdict"] == "ok":
                    result = {**result, "reason": llm_result.get("reason") or result["reason"], "source": "llm"}
                else:
                    result = {**llm_result, "source": "llm"}
        except Exception:
            pass
    return {**result, "usage": usage, "sample": text[:400]}


async def _llm_verdict(
    *,
    request: str,
    inputs: dict[str, Any],
    sample: str,
    url: str,
    title: str,
    action_ending: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = {
        "request": (request or "")[:400],
        "inputs": inputs,
        "url": url[:300],
        "title": (title or "")[:200],
        "action_ending": action_ending,
        "sample": sample,
    }
    hint = (
        "Confirm the extract answers the user's request and matches the inputs "
        "(for example fixtures are for the requested team and month). "
        "For action workflows, confirm the page shows the action succeeded."
        if not action_ending
        else "Confirm the page shows the action completed as requested."
    )
    message, usage = await chat_complete(
        [
            {
                "role": "system",
                "content": (
                    f"{hint} Reply JSON only: {{\"verdict\":\"ok|suspect|wrong\",\"reason\":\"one line\"}}. "
                    "Use wrong when the result is clearly about something else or empty/error. "
                    "Use suspect when you cannot be sure."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)[:2200]},
        ],
        max_tokens=80,
        response_format={"type": "json_object"},
    )
    verdict, reason = "ok", "Looks consistent with the request."
    try:
        data = json.loads(message.get("content") or "{}")
        if data.get("verdict") in {"ok", "suspect", "wrong"}:
            verdict = data["verdict"]
        if data.get("reason"):
            reason = str(data["reason"])[:240]
    except Exception:
        pass
    return {"verdict": verdict, "reason": reason}, usage

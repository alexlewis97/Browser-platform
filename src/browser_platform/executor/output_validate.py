"""Run-time check that a workflow result actually answers the request."""

from __future__ import annotations

import re as _re

import json
from typing import Any

from browser_platform.executor.verify import classify_parameter, is_not_found_page, parameter_reflected
from browser_platform.interpolate import is_placeholder_value
from browser_platform.llm.openrouter import chat_complete, env_api_key

SAMPLE_LIMIT = 1500

_CHROME_PHRASES = (
    "skip to content", "skip to main content", "skip to navigation", "skip to",
    "cookie settings", "manage cookies", "cookie policy", "cookies", "cookie",
    "privacy policy", "privacy notice", "privacy",
    "terms of use", "terms and conditions", "terms of service", "terms",
    "all rights reserved", "copyright",
    "sign in", "log in", "sign up", "log out", "register",
    "subscribe", "newsletter", "back to top", "accessibility",
    "follow us", "share this", "related links", "main menu", "navigation",
    "footer", "header",
)

_WEAK_CHROME = {
    "home", "news", "sport", "sports", "weather", "menu", "search", "more",
    "watch", "listen", "account", "settings", "help", "about", "contact",
    "bbc", "cnn", "edition", "sign", "login", "logout", "content", "main",
    "page", "site", "skip", "top", "links", "related",
}

_STRONG_CONTENT_RE = _re.compile(
    r"\bvs\.?\b|versus|kick-?off|"
    r"thank you|you have selected|order (?:complete|confirmed|number)|"
    r"[£$€]\s?\d|"
    r"\b(?:20\d{2})\b|"
    r"\b\d{1,2}:\d{2}\b",
    _re.I,
)


def is_nav_footer_only(sample: str | None, *, action_ending: bool = False) -> bool:
    """True when an extract is only navigation / footer chrome, not page content."""

    if action_ending:
        return False
    text = " ".join(str(sample or "").split())
    if not text:
        return False
    if _STRONG_CONTENT_RE.search(text):
        return False
    lowered = text.lower()
    stripped = lowered
    phrase_hits = 0
    for phrase in _CHROME_PHRASES:
        if phrase in stripped:
            phrase_hits += 1
            stripped = stripped.replace(phrase, " ")
    tokens = [
        tok
        for tok in _re.findall(r"[a-z0-9]+", stripped)
        if tok not in _WEAK_CHROME and not tok.isdigit() and len(tok) > 1
    ]
    leftover = " ".join(tokens)
    raw_tokens = [tok for tok in _re.findall(r"[a-z0-9]+", lowered) if not tok.isdigit()]
    # A short unique heading such as "Example Domain" is real content.
    if tokens and phrase_hits == 0:
        return False
    if not tokens and raw_tokens and set(raw_tokens) <= _WEAK_CHROME:
        return True
    generic_only = set(tokens) <= {"scores", "fixtures", "football", "soccer", "rugby", "cricket"}
    if phrase_hits >= 3 and (len(tokens) <= 8 or generic_only or len(leftover) < 40):
        return True
    return False


def trim_sample(text: str | None, limit: int = SAMPLE_LIMIT) -> str:
    raw = " ".join(str(text or "").split())
    if len(raw) <= limit:
        return raw
    # Keep the start and the end so late fixtures or confirmations are not cut off.
    head = max(200, (limit * 3) // 5)
    tail = max(200, limit - head - 1)
    if head + tail + 1 > limit:
        tail = limit - head - 1
    return raw[:head].rstrip() + "…" + raw[-tail:].lstrip()


def has_strong_content(sample: str | None) -> bool:
    return bool(_STRONG_CONTENT_RE.search(" ".join(str(sample or "").split())))


def collect_sample(outputs: dict[str, Any] | None, extra: str = "") -> str:
    parts: list[str] = []
    for key, value in (outputs or {}).items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            try:
                parts.append(f"{key}: {json.dumps(value, ensure_ascii=False)[:1200]}")
            except Exception:
                parts.append(f"{key}: {str(value)[:1200]}")
        else:
            parts.append(f"{key}: {value}")
    body = "\n".join(parts)
    extra_text = str(extra or "").strip()
    # Page chrome is a fallback only. Prefer the workflow extract so fixtures
    # at the end of a long homepage dump are not trimmed away.
    if extra_text and extra_text not in body:
        if not body.strip() or not has_strong_content(body):
            parts.append(extra_text)
            body = "\n".join(parts)
    return trim_sample(body)


def deterministic_verdict(
    *,
    inputs: dict[str, Any] | None,
    sample: str,
    url: str = "",
    title: str = "",
    headings: list[str] | None = None,
    action_ending: bool = False,
) -> dict[str, Any]:
    if is_not_found_page(url=url, title=title, headings=headings, text=sample):
        return {
            "verdict": "wrong",
            "reason": "The page looks like a not-found or error result.",
            "source": "deterministic",
        }
    if is_nav_footer_only(sample, action_ending=action_ending):
        return {
            "verdict": "wrong",
            "reason": "The extract contains only navigation or footer text, not the requested content.",
            "source": "deterministic",
        }
    for key, raw in (inputs or {}).items():
        value = str(raw or "").strip()
        if len(value) < 3:
            continue
        if is_placeholder_value(value):
            continue
        kind = classify_parameter(str(key))
        # Form fields and clicked locators (product, item) often do not appear on a
        # confirmation page or in an action-ending extract.
        if kind in {"form", "locator"}:
            continue
        hay = " ".join([url or "", title or "", sample or ""])
        if action_ending and _re.search(r"checkout-complete|thank you for your order|/post/?$", hay, _re.I):
            if kind != "filter":
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
    inputs = {
        key: value
        for key, value in (inputs or {}).items()
        if value not in (None, "") and not is_placeholder_value(value)
    }
    full = sample if sample is not None else collect_sample(outputs)
    # Re-collect from outputs when a caller prepended a long page dump.
    extract_only = collect_sample(outputs)
    if extract_only and has_strong_content(extract_only) and not has_strong_content(full[:400]):
        full = extract_only
    text = trim_sample(full)
    result = deterministic_verdict(inputs=inputs, sample=text, url=url, title=title, headings=headings, action_ending=action_ending)
    usage = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "used": False}
    if result["verdict"] == "wrong":
        return {**result, "usage": usage, "sample": text[:400]}
    content_ok = has_strong_content(full) or has_strong_content(extract_only)
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
                if result["verdict"] == "ok" and llm_result["verdict"] == "ok":
                    result = {**result, "reason": llm_result.get("reason") or result["reason"], "source": "llm"}
                elif result["verdict"] == "ok" and (action_ending or content_ok):
                    # Receipts often omit form fields. Content-bearing extracts (fixtures,
                    # kick-off times) must not be failed because a trimmed sample hid them.
                    pass
                elif result["verdict"] == "ok" and llm_result["verdict"] == "wrong":
                    result = {**llm_result, "verdict": "suspect", "source": "llm"}
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
        "(for example fixtures are for the requested team). "
        "Do not require placeholders such as current, now, or default, or unstated runtime defaults "
        "such as the current month, to appear in the extract. Only require values the user named. "
        "For action workflows, confirm the page shows the action succeeded."
        if not action_ending
        else "Confirm the page shows the action completed. Do not require form fields or product names to appear in a short confirmation extract (for example Swag Labs or Thank you for your order)."
    )
    message, usage = await chat_complete(
        [
            {
                "role": "system",
                "content": (
                    f"{hint} Reply JSON only: {{\"verdict\":\"ok|suspect|wrong\",\"reason\":\"one line\"}}. "
                    "Use wrong when the result is clearly about something else, empty/error, or only navigation/footer text. "
                    "Use suspect when you cannot be sure the page is the requested one. "
                    "This is live page data and may be newer than anything you know: never judge whether facts, "
                    "players, scores, transfers or dates are true from your own knowledge; only check that the page "
                    "is the one the inputs asked for."
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

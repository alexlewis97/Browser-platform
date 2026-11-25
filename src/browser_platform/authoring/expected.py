"""Capture and describe the expected end state of a final action."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from browser_platform.models.workflow import ExpectedState

SUCCESS_RE = re.compile(
    r"(successfully|success|thank you|thanks for|submitted|logged in|signed in|welcome|saved|completed|form received|your (order|request|form))",
    re.I,
)
SETTLE_MS = 4000


def distinctive_url_token(before: str | None, after: str | None) -> str | None:
    before = before or ""
    after = after or ""
    if not after or after == before:
        return None
    after_path = urlparse(after).path or ""
    before_path = urlparse(before).path or ""
    netloc_path = f"{urlparse(after).netloc}{after_path}"
    candidates: list[str] = []
    if after_path and after_path != before_path:
        candidates.append(netloc_path)
        if after_path not in before:
            candidates.append(after_path)
    stripped = after.split("?", 1)[0]
    if stripped and stripped not in before:
        candidates.append(stripped)
    query = urlparse(after).query
    if query and query not in before:
        candidates.append(query.split("&")[0])
    for item in candidates:
        item = item.strip()
        if item and len(item) >= 2 and item not in before:
            return item
    return None


def pick_text_contains(
    before_text: str | None,
    after_text: str | None,
    values: list[str] | None = None,
) -> str | None:
    after = after_text or ""
    before = before_text or ""
    for value in values or []:
        text = str(value or "").strip()
        if len(text) >= 2 and text in after and not _looks_secret(text):
            return text
    match = SUCCESS_RE.search(after)
    if match:
        snippet = match.group(0)
        if snippet.lower() not in (before or "").lower() or snippet.lower() in after.lower():
            return snippet
    return None


def expected_from_snapshots(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    values: dict[str, str] | None = None,
    sensitive_names: set[str] | None = None,
) -> ExpectedState | None:
    after = after or {}
    before = before or {}
    if not after.get("url") and not after.get("text") and not after.get("title"):
        return None
    safe_values = []
    for name, value in (values or {}).items():
        if sensitive_names and name in sensitive_names:
            continue
        if _looks_secret(value) or _looks_secret(name):
            continue
        if value:
            safe_values.append(str(value))
    url_includes = distinctive_url_token(before.get("url"), after.get("url"))
    text_contains = pick_text_contains(before.get("text") or before.get("body"), after.get("text") or after.get("body"), safe_values)
    heading = None
    after_headings = [h for h in (after.get("headings") or []) if str(h).strip()]
    before_headings = set(str(h).strip() for h in (before.get("headings") or []))
    for item in after_headings:
        if item not in before_headings and len(item) <= 80:
            heading = item
            break
    title_contains = None
    after_title = str(after.get("title") or "").strip()
    before_title = str(before.get("title") or "").strip()
    if after_title and after_title != before_title and len(after_title) <= 80:
        title_contains = after_title
    field_selector = after.get("field_selector")
    field_value = after.get("field_value")
    network_url_includes = after.get("network_url_includes")
    network_status = after.get("network_status")
    if not any([url_includes, text_contains, heading, title_contains, field_selector, network_url_includes]):
        # Still record the landing URL path so replay has something to check when the page changed slowly.
        path = urlparse(str(after.get("url") or "")).path
        if path and path not in {"", "/"}:
            token = distinctive_url_token(before.get("url") or "about:blank", after.get("url"))
            url_includes = token
    if not any([url_includes, text_contains, heading, title_contains, field_selector, network_url_includes]):
        return None
    return ExpectedState(
        url_includes=url_includes,
        title_contains=title_contains,
        text_contains=text_contains,
        heading=heading,
        field_selector=str(field_selector) if field_selector else None,
        field_value=str(field_value) if field_value is not None and field_value != "" else None,
        network_url_includes=str(network_url_includes) if network_url_includes else None,
        network_status=int(network_status) if network_status else None,
    )


def describe_expected(expected: ExpectedState | None) -> list[str]:
    if expected is None:
        return []
    checks: list[str] = []
    if expected.url_includes:
        checks.append(f"url includes {expected.url_includes}")
    if expected.title_contains:
        checks.append(f"title contains {expected.title_contains}")
    if expected.text_contains:
        checks.append(f"page contains {expected.text_contains}")
    if expected.heading:
        checks.append(f"heading {expected.heading}")
    if expected.field_selector:
        checks.append(f"field {expected.field_selector} is set")
    if expected.network_url_includes:
        checks.append(f"network {expected.network_url_includes}")
    return checks


def expected_result_text(expected: ExpectedState | None) -> str:
    if expected is None:
        return ""
    if expected.description:
        return expected.description
    if expected.text_contains:
        return expected.text_contains
    if expected.url_includes:
        return f"URL includes {expected.url_includes}"
    if expected.heading:
        return expected.heading
    if expected.title_contains:
        return expected.title_contains
    return ""


def apply_expected_result(expected: ExpectedState | None, text: str | None) -> ExpectedState:
    text = (text or "").strip()
    state = expected or ExpectedState()
    if not text:
        return state
    state.description = text
    # If the builder typed a URL-ish check, prefer url_includes; otherwise treat as visible text.
    lower = text.lower()
    if lower.startswith("url includes "):
        state.url_includes = text[13:].strip() or state.url_includes
    elif lower.startswith("http://") or lower.startswith("https://") or lower.startswith("/"):
        state.url_includes = text
    else:
        state.text_contains = text
    return state


def _looks_secret(value: str | None) -> bool:
    text = (value or "").strip().lower()
    if not text:
        return False
    return bool(re.search(r"password|passwd|secret|api[_-]?key|token", text))


async def settle_page(page, timeout_ms: int = SETTLE_MS) -> None:
    """Wait for a slow navigation or confirmation to appear after the last action."""
    if page is None:
        return
    remaining = max(int(timeout_ms), 500)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=min(remaining, 2500))
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=min(remaining, 2000))
    except Exception:
        try:
            await page.wait_for_timeout(min(800, remaining))
        except Exception:
            pass


async def snapshot_effect(page) -> dict[str, Any]:
    if page is None:
        return {}
    try:
        url = page.url or ""
        if url.startswith(("chrome://", "chrome-extension://", "about:")):
            return {"url": url, "title": "", "text": "", "headings": []}
        title = await page.title()
        text = await page.locator("body").inner_text()
        headings = await page.locator("h1, h2, [role=heading]").all_inner_texts()
        return {"url": url, "title": title, "text": text, "headings": headings}
    except Exception:
        return {}

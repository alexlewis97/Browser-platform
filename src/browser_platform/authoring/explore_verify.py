"""Verify explore proposals that need a real browser (select, tab, radio)."""

from __future__ import annotations

from typing import Any

from browser_platform.interpolate import is_navigable_url
from browser_platform.models.workflow import WorkflowDefinition
from browser_platform.orchestrator.runner import apply_schema_defaults

BROWSER_TIMEOUT_MS = 20000


def _looks_like_js_shell(html: str | None) -> bool:
    text = html or ""
    if len(text) < 400:
        return True
    if "<select" in text.lower() or "type=\"radio\"" in text.lower() or "type='radio'" in text.lower() or "role=\"tab\"" in text.lower():
        return False
    return bool(
        "<div id=\"root\"" in text.lower()
        or "<div id=\"app\"" in text.lower()
        or "__NEXT_DATA__" in text
        or "id=\"app\"" in text
    )


async def snapshot_workflow(definition: WorkflowDefinition, variables: dict[str, Any] | None) -> dict[str, Any]:
    """Run non-extract steps in a short-lived Chromium and return url/html/text."""

    variables = apply_schema_defaults(definition, variables)
    page = None
    browser = None
    playwright = None
    isolated = None
    try:
        from playwright.async_api import async_playwright

        from browser_platform.executor.deterministic import run_step
        from browser_platform.executor.selectors import build_selector_plan

        playwright = await async_playwright().start()
        try:
            from browser_platform.executor.isolated import close_isolated, launch_isolated

            isolated = await launch_isolated(wait=8.0)
            browser = await playwright.chromium.connect_over_cdp(isolated.cdp_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
        except Exception:
            isolated = None
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
        for step in definition.steps or []:
            if step.type in {"extract", "extract_page_content"}:
                break
            await run_step(page, step, variables=variables or {}, plan=build_selector_plan(step), timeout_ms=8000)
        html = await page.content()
        text = await page.locator("body").inner_text()
        return {"ok": True, "url": page.url, "html": html, "text": text}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "url": "", "html": "", "text": ""}
    finally:
        try:
            if page and isolated is None and page.context:
                await page.context.close()
        except Exception:
            pass
        try:
            if browser and isolated is None:
                await browser.close()
        except Exception:
            pass
        try:
            if playwright:
                await playwright.stop()
        except Exception:
            pass
        if isolated is not None:
            try:
                from browser_platform.executor.isolated import close_isolated

                await close_isolated(isolated)
            except Exception:
                pass


async def fetch_rendered_html(url: str) -> str:
    if not is_navigable_url(url):
        return ""
    from browser_platform.models.workflow import WorkflowStep

    dummy = WorkflowDefinition(
        name="explore_preview",
        version="1",
        steps=[
            WorkflowStep(type="navigation", url=url),
            WorkflowStep(type="extract", extractionGoal="results"),
        ],
    )
    shot = await snapshot_workflow(dummy, {})
    return str(shot.get("html") or "") if shot.get("ok") else ""


async def verify_proposal_browser(
    definition: WorkflowDefinition,
    proposal: dict[str, Any],
    *,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay the workflow with the default and a non-default value; keep only if the page changes."""

    name = proposal.get("name")
    result = {**proposal, "verified": False, "dropped_reason": None, "verify_mode": "browser"}
    if not name:
        result["dropped_reason"] = "missing name"
        return result
    from browser_platform.authoring.explore import apply_proposals, _fingerprint
    applied = apply_proposals(definition, [proposal])
    default = proposal.get("default")
    alt = proposal.get("verify_with")
    if not alt or alt == default:
        result["dropped_reason"] = "no alternate value to verify"
        return result
    base = dict(variables or {})
    first = await snapshot_workflow(applied, {**base, name: default} if default not in (None, "") else base)
    second = await snapshot_workflow(applied, {**base, name: alt})
    result["default_url"] = first.get("url")
    result["alt_url"] = second.get("url")
    if not first.get("ok"):
        result["dropped_reason"] = f"default replay failed: {first.get('error')}"
        return result
    if not second.get("ok"):
        result["dropped_reason"] = f"alternate replay failed: {second.get('error')}"
        return result
    if first.get("url") == second.get("url") and _fingerprint(first.get("text") or first.get("html") or "") == _fingerprint(
        second.get("text") or second.get("html") or ""
    ):
        result["dropped_reason"] = "page content did not change for the alternate value"
        return result
    result["verified"] = True
    return result

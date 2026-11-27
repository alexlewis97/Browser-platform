"""Run one workflow-use step on an attached Chrome tab (no LLM)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeout

from browser_platform.executor.locators import locator_for
from browser_platform.executor.selectors import SelectorAttempt
from browser_platform.executor.verify import classify_parameter, missing_parameters, value_echoed
from browser_platform.interpolate import interpolate, interpolate_url, is_navigable_url
from browser_platform.models.workflow import ExpectedState, WorkflowStep


@dataclass
class StepSuccess:
    output: Any = None
    attempt: SelectorAttempt | None = None


class StepFailed(Exception):
    def __init__(self, message: str, last_error: str | None = None):
        super().__init__(message)
        self.last_error = last_error or message


def _same_page_url(left: str | None, right: str | None) -> bool:
    """True when a navigation would only reload the current page (e.g. GET after POST)."""
    if not left or not right:
        return False
    try:
        from urllib.parse import urlparse, urlunparse
        def _norm(url: str) -> str:
            parsed = urlparse(url)
            path = parsed.path.rstrip("/") or "/"
            return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", parsed.query, ""))
        return _norm(left) == _norm(right)
    except Exception:
        return left.rstrip("/") == right.rstrip("/")


async def _first_visible(locator: Locator, timeout_ms: int) -> Locator:
    count = await locator.count()
    if count == 0:
        raise StepFailed("no matching elements")
    # Prefer a visible match; fall back to the first.
    for i in range(count):
        item = locator.nth(i)
        try:
            if await item.is_visible():
                await item.wait_for(state="visible", timeout=min(timeout_ms, 2000))
                return item
        except Exception:
            continue
    item = locator.first
    await item.wait_for(state="attached", timeout=timeout_ms)
    return item


async def resolve_locator(page: Page, attempt: SelectorAttempt, timeout_ms: int) -> Locator:
    loc = locator_for(page, attempt.strategy)
    return await _first_visible(loc, timeout_ms)


def _interpolate_expected(expected: ExpectedState, variables: dict[str, Any] | None) -> ExpectedState:
    if not expected:
        return expected
    values = expected.parameter_values
    if values:
        values = [str(interpolate(item, variables) or item) for item in values]
    echoes = expected.echo_values
    if echoes:
        echoes = [str(interpolate(item, variables) or item) for item in echoes]
    return expected.model_copy(
        update={
            "url_includes": interpolate(expected.url_includes, variables),
            "title_contains": interpolate(expected.title_contains, variables),
            "text_contains": interpolate(expected.text_contains, variables),
            "heading": interpolate(expected.heading, variables),
            "parameter_values": values,
            "echo_values": echoes,
        }
    )


async def assert_expected(
    page: Page,
    expected: ExpectedState | None,
    element: Locator | None = None,
    variables: dict[str, Any] | None = None,
) -> None:
    if not expected:
        return
    raw_params = list(expected.parameter_values or [])
    expected = _interpolate_expected(expected, variables)
    if expected.url_includes and expected.url_includes not in page.url:
        raise StepFailed(f"url did not include {expected.url_includes!r} (was {page.url})")
    title = await page.title()
    if expected.title_contains and expected.title_contains not in title:
        raise StepFailed(f"title did not contain {expected.title_contains!r} (was {title!r})")
    body = await page.locator("body").inner_text()
    if expected.text_contains and expected.text_contains not in body:
        raise StepFailed(f"page text did not contain {expected.text_contains!r}")
    headings = await page.locator("h1, h2, [role=heading]").all_inner_texts()
    if expected.heading:
        if not any(expected.heading in h for h in headings):
            raise StepFailed(f"heading {expected.heading!r} not found in {headings!r}")
    if expected.parameter_values:
        try:
            selected = await page.locator("[aria-selected=true], [aria-current=page], option:checked").all_inner_texts()
        except Exception:
            selected = []
        raw_items = raw_params
        # Names must stay (product, team, …). Interpolating first and calling them param_0
        # made locators look like filters that have to appear in the confirmation URL.
        values: dict[str, str] = {}
        kinds: dict[str, str] = {}
        for raw in raw_items:
            if not raw:
                continue
            text = str(raw)
            name = text
            if text.startswith("{") and "}" in text:
                name = text[1:text.index("}")].split("|")[0]
            interpolated = str(interpolate(text, variables) or text)
            values[name] = interpolated
            kinds[name] = classify_parameter(name)
        # Replay has no recorder events. Locators and form fields do not have to appear
        # in a confirmation URL; only filters do.
        check = {name: value for name, value in values.items() if kinds.get(name) == "filter"}
        missing = missing_parameters(url=page.url, title=title, headings=headings, text=body, selected=selected, values=check, kinds=kinds)
        if missing:
            names = ", ".join(item.split("=", 1)[-1] for item in missing)
            raise StepFailed(
                f"The page is not filtered for {names}. URL was {page.url}. "
                "Apply the filter in the UI (dropdown, option, or team link), not only a search box."
            )
    if expected.echo_values:
        hay = " ".join([page.url or "", title or "", body or ""])
        if not re.search(r"checkout-complete|thank you for your order", hay, re.I):
            missing_echo = [value for value in expected.echo_values if value and not value_echoed(title=title, text=body, value=str(value))]
            if missing_echo:
                raise StepFailed(
                    f"The confirmation did not echo {', '.join(missing_echo)}. URL was {page.url}."
                )
    if element is not None:
        # Visibility is implied if we already acted on it; still check if attached.
        try:
            if not await element.count():
                raise StepFailed("element disappeared after the action")
        except StepFailed:
            raise
        except Exception:
            pass



def normalize_wikipedia_url(url: str | None) -> str | None:
    """MediaWiki titles use underscores; a {term} with spaces would 404 in /wiki/{term}."""

    if not url:
        return url
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if "wikipedia.org" not in host and "wikimedia.org" not in host:
        return url
    path = parsed.path or ""
    if path.startswith("/wiki/") and " " in path:
        parts = path.split("/")
        parts = [part.replace(" ", "_") for part in parts]
        parsed = parsed._replace(path="/".join(parts))
        return urlunparse(parsed)
    return url




async def _first_item_text(page: Page) -> str:
    try:
        loc = page.locator(".inventory_item_name, [data-test='inventory-item-name'], .product-title")
        if await loc.count():
            return (await loc.first.inner_text()).strip()
    except Exception:
        return ""
    return ""


async def _wait_for_list_update(page: Page, *, before: str = "", timeout_ms: int = 4000) -> None:
    """Poll until a sort/filter re-render updates the first visible item, if any."""
    if not before:
        await page.wait_for_timeout(800)
        return
    import time as _time
    deadline = _time.monotonic() + (timeout_ms / 1000.0)
    while _time.monotonic() < deadline:
        text = await _first_item_text(page)
        if text and text != before:
            return
        await page.wait_for_timeout(150)




async def _extract_first_item(page: Page) -> str:
    """Name and price of the first visible catalogue/inventory card, if present."""
    cards = page.locator(
        ".inventory_item, [data-test='inventory-item'], .product-card, .product, li.product, .card"
    )
    try:
        count = await cards.count()
    except Exception:
        count = 0
    if count:
        card = cards.first
        name = ""
        price = ""
        try:
            name_loc = card.locator(
                ".inventory_item_name, [data-test='inventory-item-name'], .product-title, .product_title, h2, h3"
            )
            if await name_loc.count():
                name = (await name_loc.first.inner_text()).strip()
        except Exception:
            pass
        try:
            price_loc = card.locator(
                ".inventory_item_price, [data-test='inventory-item-price'], .price, .product-price, .product_price"
            )
            if await price_loc.count():
                price = (await price_loc.first.inner_text()).strip()
        except Exception:
            pass
        text = " ".join(part for part in (name, price) if part).strip()
        if text:
            return text
        try:
            blob = (await card.inner_text()).strip()
            if blob:
                return " ".join(blob.split())[:200]
        except Exception:
            pass
    return ""


# Main text without page chrome: semantic nav/header/footer, in-page jump links, short control
# buttons and link strips (many short links, little other text: menus, date or tab scrollers).
# Elements are hidden only while innerText is read, so layout-aware text is kept.
_MAIN_TEXT_JS = r"""
(root) => {
  const words = (s) => (s || "").trim().split(/\s+/).filter(Boolean).length;
  const len = (s) => (s || "").replace(/\s+/g, " ").trim().length;
  const drop = new Set();
  root.querySelectorAll('nav, header, footer, aside, [role="navigation"], [role="banner"], [role="contentinfo"], [role="tablist"]').forEach(el => drop.add(el));
  root.querySelectorAll('a[href^="#"]').forEach(a => { if (words(a.innerText) <= 4) drop.add(a); });
  root.querySelectorAll('button').forEach(b => { if (words(b.innerText) <= 3) drop.add(b); });
  // Link strips: many short links and almost no other text (menus, date or tab scrollers).
  // Only the innermost such block is dropped, so a page whose whole body is link-heavy keeps
  // its content and loses only the menu itself.
  const strips = [];
  for (const el of root.querySelectorAll('ul, ol, div, section')) {
    const links = Array.from(el.querySelectorAll('a'));
    if (links.length < 5) continue;
    const short = links.filter(a => words(a.innerText) <= 3).length;
    const total = len(el.innerText);
    const linkText = links.reduce((n, a) => n + len(a.innerText), 0);
    if (total && short / links.length >= 0.8 && linkText / total >= 0.8) strips.push(el);
  }
  for (const el of strips) { if (!strips.some(o => o !== el && el.contains(o))) drop.add(el); }
  const hidden = [];
  for (const el of drop) { hidden.push([el, el.style.getPropertyValue("display"), el.style.getPropertyPriority("display")]); el.style.setProperty("display", "none", "important"); }
  let text = "";
  try { text = (root.innerText || "").trim(); } finally { for (const [el, v, p] of hidden) { if (v) el.style.setProperty("display", v, p); else el.style.removeProperty("display"); } }
  return text;
}
"""


async def _content_text(loc) -> str:
    """Inner text of ``loc`` with navigation chrome left out; falls back to plain inner text."""

    raw = (await loc.inner_text()).strip()
    try:
        clean = str(await loc.evaluate(_MAIN_TEXT_JS) or "").strip()
    except Exception:
        return raw
    # Keep the raw text if cleaning removed nearly everything (the page may be mostly links).
    if not clean or (len(clean) < 40 and len(raw) >= 40):
        return raw
    return clean


class ExtractMissing(StepFailed):
    """A mandatory extract found nothing: never rescued by fallbacks, never an empty success."""


def _is_optional(step: WorkflowStep) -> bool:
    return bool(getattr(step, "optional", False))


async def extract_content(page: Page, step: WorkflowStep, *, variables: dict[str, Any] | None = None) -> str:
    """Read the extract target. A step's ``cssSelector`` is mandatory: if it matches nothing or
    only empty text the step fails (naming the selector and what the page showed) unless the
    step is explicitly ``optional: true``, in which case it falls back to the page's main text."""

    goal = (step.extractionGoal or step.goal or step.description or "").lower()
    if step.cssSelector:
        selector = interpolate(step.cssSelector, variables or {})
        loc = page.locator(selector)
        count = await loc.count()
        if count:
            text = (await loc.first.inner_text()).strip()
            if not text:
                # Inputs/progress bars hold their value in an attribute, not inner text.
                try:
                    text = str(await loc.first.evaluate("el => el.value ?? el.getAttribute('aria-valuenow') ?? el.getAttribute('value') ?? ''") or "").strip()
                except Exception:
                    text = ""
            if text:
                return text
            if not _is_optional(step):
                raise ExtractMissing(
                    f"extract selector {selector!r} matched {count} element(s) but they are empty",
                    last_error=f"expected text in {selector!r}; found an empty element",
                )
        elif not _is_optional(step):
            title = ""
            try:
                title = (await page.title()).strip()
            except Exception:
                pass
            raise ExtractMissing(
                f"extract selector {selector!r} matched no element",
                last_error=f"expected {selector!r} on {page.url} ({title!r}); found no matching element",
            )
    if "heading" in goal or "title" in goal or "h1" in goal:
        h1 = page.locator("h1")
        if await h1.count():
            return (await h1.first.inner_text()).strip()
        return (await page.title()).strip()
    if "first" in goal and any(token in goal for token in ("item", "product", "price", "inventory", "result")):
        first = await _extract_first_item(page)
        if first:
            return first
    # Paragraph / article body: do not let a decorative h1 win over <main> or <p>.
    if "paragraph" in goal or "article" in goal:
        para = page.locator("main p, article p, #mw-content-text p, p")
        if await para.count():
            for i in range(min(await para.count(), 8)):
                text = (await para.nth(i).inner_text()).strip()
                if text:
                    return text[:4000]
        main = page.locator("main, article")
        if await main.count():
            text = (await main.first.inner_text()).strip()
            if text:
                return text[:4000]
    main = page.locator("main, article")
    if await main.count():
        text = await _content_text(main.first)
        if text:
            return text[:4000]
    text = (await _content_text(page.locator("body")))[:4000].strip()
    if not text and not _is_optional(step):
        raise ExtractMissing("extract found no text on the page", last_error=f"expected page text on {page.url}; found none")
    return text


async def _apply_input_value(loc, value: str, timeout_ms: int) -> None:
    """Fill text boxes; click radios, checkboxes, and labels that wrap them."""

    info = await loc.evaluate(
        """el => {
            const tag = (el.tagName || "").toLowerCase();
            const typ = (el.getAttribute("type") || "").toLowerCase();
            if (tag === "select") return {kind: "select"};
            if (tag === "input" && (typ === "radio" || typ === "checkbox")) return {kind: typ};
            if (tag === "label") {
              const inner = el.querySelector("input, select, textarea");
              if (inner) {
                const it = (inner.getAttribute("type") || inner.tagName || "").toLowerCase();
                return {kind: it === "radio" || it === "checkbox" || it === "select" ? it : "text"};
              }
              return {kind: "label"};
            }
            return {kind: "text"};
        }"""
    )
    kind = str((info or {}).get("kind") or "text")
    if kind in {"radio", "checkbox", "label"}:
        await loc.click(timeout=timeout_ms)
        return
    if kind == "select":
        await loc.select_option(label=str(value), timeout=timeout_ms)
        return
    await loc.fill(str(value), timeout=timeout_ms)



async def run_step(
    page: Page,
    step: WorkflowStep,
    *,
    variables: dict[str, Any] | None,
    plan: list[SelectorAttempt],
    timeout_ms: int = 8000,
) -> StepSuccess:
    typ = step.type
    variables = variables or {}
    url = normalize_wikipedia_url(interpolate_url(step.url, variables))
    value = interpolate(step.value if step.value is not None else step.default_value, variables)
    selected = interpolate(step.selectedText, variables)
    key = interpolate(step.key, variables)
    interpolated_plan = []
    for attempt in plan:
        strategy_value = interpolate(attempt.strategy.value, variables)
        strategy = attempt.strategy
        if strategy_value != strategy.value:
            strategy = strategy.model_copy(update={"value": strategy_value})
        interpolated_plan.append(type(attempt)(kind=attempt.kind, strategy=strategy))
    plan = interpolated_plan

    if typ == "navigation":
        if not url:
            raise StepFailed("navigation step is missing url")
        from browser_platform.nav_policy import assert_safe_navigation_url

        if not is_navigable_url(str(url)):
            raise StepFailed(f"navigation step is not an http(s) URL: {url!r}")
        # Local user Chrome may hit intranet hosts; still block file:// and odd schemes.
        try:
            safe = assert_safe_navigation_url(str(url), allow_private=True)
        except ValueError as exc:
            raise StepFailed(str(exc)) from exc
        # A recorded post-submit navigation is often the form action URL. Replaying
        # it with page.goto() would GET the same path and wipe the POST body.
        try:
            current = page.url
        except Exception:
            current = ""
        if not _same_page_url(current, safe):
            await page.goto(safe, wait_until="domcontentloaded")
        # Public sites (notably Wikipedia) may return a short rate-limit error page on the first hit.
        for _ in range(2):
            title = ""
            body = ""
            try:
                title = await page.title()
                body = (await page.locator("body").inner_text())[:800]
            except Exception:
                break
            if "Wikimedia Error" not in title and "Too many requests" not in body:
                break
            await page.wait_for_timeout(2000)
            await page.reload(wait_until="domcontentloaded")
        wait_s = getattr(step, "wait_time", None) or 0
        if wait_s:
            await page.wait_for_timeout(int(float(wait_s) * 1000))
        await assert_expected(page, step.expected_state, variables=variables)
        return StepSuccess(output={"url": page.url})

    if typ == "go_back":
        await page.go_back(wait_until="domcontentloaded")
        await assert_expected(page, step.expected_state, variables=variables)
        return StepSuccess(output={"url": page.url})

    if typ == "go_forward":
        await page.go_forward(wait_until="domcontentloaded")
        await assert_expected(page, step.expected_state, variables=variables)
        return StepSuccess(output={"url": page.url})

    if typ == "scroll":
        dx = step.scrollX or 0
        dy = step.scrollY or 0
        await page.mouse.wheel(dx, dy)
        await assert_expected(page, step.expected_state, variables=variables)
        return StepSuccess(output={"scrollX": dx, "scrollY": dy})

    if typ in {"extract", "extract_page_content"}:
        output = await extract_content(page, step, variables=variables)
        await assert_expected(page, step.expected_state, variables=variables)
        return StepSuccess(output=output)

    if typ == "agent":
        raise StepFailed("agent steps are not executed in the deterministic path")


    if typ not in {"click", "double_click", "right_click", "input", "select_change", "key_press"}:
        raise StepFailed(f"unsupported step type: {typ}")

    last_error = "no selector strategies to try"
    for attempt in plan:
        try:
            loc = await resolve_locator(page, attempt, timeout_ms)
            if typ == "click":
                await loc.click(timeout=timeout_ms)
            elif typ == "double_click":
                await loc.dblclick(timeout=timeout_ms)
            elif typ == "right_click":
                await loc.click(button="right", timeout=timeout_ms)
            elif typ == "input":
                if value is None:
                    raise StepFailed("input step is missing value")
                await _apply_input_value(loc, str(value), timeout_ms)
            elif typ == "select_change":
                choice = selected if selected is not None else value
                if choice is None:
                    raise StepFailed("select_change step is missing selectedText")
                before = await _first_item_text(page)
                await _apply_input_value(loc, str(choice), timeout_ms)
                await _wait_for_list_update(page, before=before)
            elif typ == "key_press":
                if not key:
                    raise StepFailed("key_press step is missing key")
                await loc.press(str(key), timeout=timeout_ms)
            if typ != "select_change":
                await page.wait_for_timeout(50)
            await assert_expected(page, step.expected_state, loc, variables=variables)
            return StepSuccess(attempt=attempt)
        except StepFailed as exc:
            last_error = str(exc)
        except PlaywrightTimeout as exc:
            last_error = f"timeout on {attempt.type}:{attempt.value}: {exc}"
        except Exception as exc:
            last_error = f"{attempt.type}:{attempt.value}: {exc}"
    raise StepFailed(f"all selector strategies failed for {typ}", last_error=last_error)


def primary_attempt(plan: list[SelectorAttempt]) -> SelectorAttempt | None:
    """The step's recorded first-choice selector (lowest-priority recorded strategy)."""

    for attempt in plan:
        if attempt.kind == "selector":
            return attempt
    return next((a for a in plan if a.kind == "heuristic"), None)


def same_attempt(left: SelectorAttempt | None, right: SelectorAttempt | None) -> bool:
    return bool(left and right and left.type == right.type and left.value == right.value)


async def probe_alternatives(page: Page, plan: list[SelectorAttempt], variables: dict[str, Any] | None, skip: SelectorAttempt | None) -> str:
    """Describe what a non-primary strategy would have matched (read-only, used for strict errors)."""

    for attempt in plan:
        if same_attempt(attempt, skip):
            continue
        value = interpolate(attempt.strategy.value, variables or {})
        strategy = attempt.strategy.model_copy(update={"value": value})
        try:
            loc = locator_for(page, strategy)
            count = await loc.count()
        except Exception:
            continue
        if count:
            return f"{attempt.kind} {attempt.type}:{value!r} matches {count} element(s)"
    return "no other strategy matches either"


_SAFE_GOAL = re.compile(r"[^\w\s-]+")

"""Record navigations and DOM actions on an existing Chrome over CDP (no tab close)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from browser_platform.authoring.elements import RECORDER_JS, is_label_element, resolve_fill_element, strategies_for_element
from browser_platform.models.workflow import SelectorStrategy


@dataclass
class RecordedEvent:
    type: str
    url: str | None = None
    value: str | None = None
    key: str | None = None
    text: str | None = None
    element: dict[str, Any] | None = None
    selectors: list[SelectorStrategy] = field(default_factory=list)
    ts: float | None = None


@dataclass
class RecorderSession:
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    events: list[RecordedEvent] = field(default_factory=list)
    _attached: set[int] = field(default_factory=set)
    _sigs: set[tuple] = field(default_factory=set)


async def attach_recorder(cdp_url: str) -> RecorderSession:
    """Attach to the user's Chrome. Does not open or close tabs."""

    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(cdp_url)
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    session = RecorderSession(playwright=pw, browser=browser, context=context)
    try:
        await context.add_init_script(RECORDER_JS)
    except Exception:
        pass

    def _on_page(page: Page) -> None:
        try:
            import asyncio
            asyncio.get_running_loop().create_task(_bind_page(session, page))
        except Exception:
            pass

    context.on("page", _on_page)
    for page in list(context.pages):
        await _bind_page(session, page)
    return session


async def _bind_page(session: RecorderSession, page: Page) -> None:
    ident = id(page)
    if ident in session._attached or page.is_closed():
        return
    session._attached.add(ident)
    try:
        await page.add_init_script(RECORDER_JS)
    except Exception:
        pass
    try:
        async def on_push(source, item):  # noqa: ARG001
            ingest_raw_events(session, [item] if isinstance(item, dict) else item)

        await page.expose_binding("__bpPushEvent", on_push)
    except Exception:
        pass
    try:
        await page.evaluate(RECORDER_JS)
    except Exception:
        pass

    def on_nav(frame) -> None:
        try:
            if frame == page.main_frame:
                session.events.append(RecordedEvent(type="navigation", url=frame.url or page.url))
        except Exception:
            pass

    try:
        page.on("framenavigated", on_nav)
    except Exception:
        pass



def _event_sig(ev: RecordedEvent) -> tuple:
    element = ev.element or {}
    return (ev.type, ev.url, ev.value, ev.key, ev.ts, element.get("id"), element.get("css") or element.get("cssSelector"))


def ingest_raw_events(session: RecorderSession, raw: Any) -> None:
    """Append in-page recorder records to the session, skipping duplicates."""

    for item in raw or []:
        if not isinstance(item, dict):
            continue
        element = item.get("element")
        typ = item.get("type") or "click"
        value = item.get("value")
        if typ in {"input", "select_change"} and isinstance(element, dict) and is_label_element(element):
            label_text = str(element.get("text") or element.get("name") or "").strip()
            if value and label_text and str(value).strip() == label_text:
                continue
            element = resolve_fill_element(element)
        ev = RecordedEvent(
            type=typ,
            url=item.get("url"),
            value=value,
            key=item.get("key"),
            text=(element or {}).get("text") if isinstance(element, dict) else None,
            element=element if isinstance(element, dict) else None,
            selectors=strategies_for_element(
                element if isinstance(element, dict) else None,
                for_fill=typ in {"input", "select_change"},
            ),
            ts=item.get("ts"),
        )
        sig = _event_sig(ev)
        if sig in session._sigs:
            continue
        session._sigs.add(sig)
        session.events.append(ev)


async def collect_events(session: RecorderSession) -> list[RecordedEvent]:
    """Flush in-page recorder buffers and merge with navigation events."""

    for page in list(session.context.pages):
        try:
            await _bind_page(session, page)
            raw = await page.evaluate("() => (window.__bpRecorder && window.__bpRecorder.drain) ? window.__bpRecorder.drain() : []")
        except Exception:
            raw = []
        ingest_raw_events(session, raw)
    return stabilize_event_order(compact_events(session.events))


async def harvest_field_snapshots(session: RecorderSession) -> None:
    """Ask every page to diff current form-field values (covers CDP insertText / JS value sets)."""

    for page in list(session.context.pages):
        if page.is_closed():
            continue
        try:
            await _bind_page(session, page)
            await page.evaluate(
                "() => (window.__bpRecorder && window.__bpRecorder.snapshotFields) ? window.__bpRecorder.snapshotFields() : []"
            )
        except Exception:
            pass


async def poll_field_changes(session: RecorderSession, stop: asyncio.Event, interval: float = 0.2) -> None:
    """Poll form values for the whole demonstration so insertText-only typing is recorded."""

    while not stop.is_set():
        await harvest_field_snapshots(session)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    await harvest_field_snapshots(session)


def compact_events(events: list[RecordedEvent]) -> list[RecordedEvent]:
    """Drop internal markers, collapse repeated inputs on the same element, skip about:blank navs."""

    out: list[RecordedEvent] = []
    for ev in events:
        if ev.type.startswith("_"):
            continue
        if ev.type == "navigation":
            url = ev.url or ""
            if not url or url.startswith("about:") or url.startswith("chrome://"):
                continue
            if out and out[-1].type == "navigation" and out[-1].url == ev.url:
                continue
            out.append(ev)
            continue
        if ev.type == "input" and out and out[-1].type == "input" and _same_element(out[-1], ev):
            out[-1] = ev
            continue
        if ev.type == "key_press" and ev.key == "Tab":
            continue
        out.append(ev)
    return out


def _same_element(a: RecordedEvent, b: RecordedEvent) -> bool:
    ae, be = a.element or {}, b.element or {}
    if ae.get("id") and ae.get("id") == be.get("id"):
        return True
    if ae.get("css") and ae.get("css") == be.get("css"):
        return True
    if ae.get("xpath") and ae.get("xpath") == be.get("xpath"):
        return True
    return bool(ae and ae == be)


def _field_key(ev: RecordedEvent) -> str:
    el = ev.element or {}
    return str(el.get("id") or el.get("css") or el.get("xpath") or ev.text or id(ev))


def stabilize_event_order(events: list[RecordedEvent]) -> list[RecordedEvent]:
    """Keep last value per field and place fills before the submit click on the same visit."""

    segments: list[list[RecordedEvent]] = []
    current: list[RecordedEvent] = []
    for ev in events:
        if ev.type == "navigation" and current:
            segments.append(current)
            current = [ev]
        else:
            current.append(ev)
    if current:
        segments.append(current)

    out: list[RecordedEvent] = []
    for segment in segments:
        navs = [e for e in segment if e.type == "navigation"]
        fills: list[RecordedEvent] = []
        seen: dict[str, int] = {}
        others: list[RecordedEvent] = []
        for ev in segment:
            if ev.type == "navigation":
                continue
            if ev.type in {"input", "select_change"}:
                if ev.value is None or ev.value == "":
                    continue
                key = _field_key(ev)
                if key in seen:
                    fills[seen[key]] = ev
                else:
                    seen[key] = len(fills)
                    fills.append(ev)
            else:
                others.append(ev)
        out.extend(navs)
        out.extend(fills)
        out.extend(others)
    return out


async def detach_recorder(session: RecorderSession) -> None:
    """Drop the CDP connection only; do not close any tabs."""

    try:
        await session.browser.close()
    except Exception:
        pass
    try:
        await session.playwright.stop()
    except Exception:
        pass

"""Interactive remote-browser recording sessions for the website admin UI.

A short-lived, headless Chromium is launched on the backend, screencast (or
screenshot) frames are streamed to the page, and the admin's clicks / typing /
scroll / URL-bar input are forwarded. Page events are captured with the same
recorder schema used by the Chrome extension, then cleaned by from-recording.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

from browser_platform.authoring.elements import RECORDER_JS
from browser_platform.chrome_launch import container_chrome_args, find_playwright_chromium

logger = logging.getLogger("browser_platform.remote_record")

MAX_SESSIONS = 2
IDLE_SECONDS = 180
VIEWPORT = {"width": 1280, "height": 800}
SCREENCAST_QUALITY = 40
SCREENCAST_MAX_WIDTH = 1024
SCREENSHOT_INTERVAL = 0.35

FrameHandler = Callable[[dict[str, Any]], Awaitable[None]]


class SessionBusy(Exception):
    """Raised when the concurrent session cap has been reached."""


class RecordSession:
    def __init__(self, *, start_url: str = "", goal: str = "", name: str = "") -> None:
        self.id = uuid.uuid4().hex
        self.start_url = (start_url or "").strip()
        self.goal = goal or ""
        self.name = name or ""
        self.created_at = time.time()
        self.last_activity = self.created_at
        self.events: list[dict[str, Any]] = []
        self.viewport = dict(VIEWPORT)
        self.latest_frame: str | None = None
        self.latest_url = "about:blank"
        self.mode = "screencast"
        self.closed = False
        self.error: str | None = None
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.cdp = None
        self._clients: set[FrameHandler] = set()
        self._lock = asyncio.Lock()
        self._shot_task: asyncio.Task[None] | None = None

    def touch(self) -> None:
        self.last_activity = time.time()

    def idle_for(self) -> float:
        return time.time() - self.last_activity

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.latest_url,
            "viewport": self.viewport,
            "event_count": len(self.events),
            "mode": self.mode,
            "idle_s": round(self.idle_for(), 1),
            "idle_limit_s": IDLE_SECONDS,
            "goal": self.goal,
            "name": self.name,
            "error": self.error,
            "has_frame": bool(self.latest_frame),
        }

    def frame_payload(self) -> dict[str, Any]:
        return {
            "type": "frame",
            "mime": "image/jpeg",
            "data": self.latest_frame or "",
            "url": self.latest_url,
            "viewport": self.viewport,
            "event_count": len(self.events),
            "mode": self.mode,
        }

    def add_client(self, handler: FrameHandler) -> None:
        self._clients.add(handler)

    def drop_client(self, handler: FrameHandler) -> None:
        self._clients.discard(handler)

    async def broadcast(self, message: dict[str, Any]) -> None:
        dead: list[FrameHandler] = []
        for handler in list(self._clients):
            try:
                await handler(message)
            except Exception:
                dead.append(handler)
        for handler in dead:
            self._clients.discard(handler)

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        executable = find_playwright_chromium()
        if executable is None:
            raise RuntimeError("Playwright Chromium is not installed on this host.")
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            executable_path=str(executable),
            headless=True,
            args=container_chrome_args(),
            handle_sigint=False,
            handle_sigterm=False,
        )
        self.context = await self.browser.new_context(
            viewport=self.viewport,
            ignore_https_errors=False,
        )
        await self.context.add_init_script(RECORDER_JS)
        self.page = await self.context.new_page()
        self.page.on("framenavigated", lambda frame: self._on_nav(frame))
        await self._start_screencast()
        target = self.start_url or "about:blank"
        if target and target != "about:blank":
            await self.navigate(target)
        else:
            self.latest_url = self.page.url
            self.events.append({"type": "navigation", "ts": int(time.time() * 1000), "url": self.latest_url, "changed": True})

    def _on_nav(self, frame: Any) -> None:
        try:
            if self.page is None or frame != self.page.main_frame:
                return
            url = frame.url
            if not url:
                return
            self.latest_url = url
            last = self.events[-1] if self.events else None
            if last and last.get("type") == "navigation" and last.get("url") == url:
                return
            self.events.append({"type": "navigation", "ts": int(time.time() * 1000), "url": url, "changed": True})
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.broadcast({"type": "url", "url": url, "event_count": len(self.events)}))
            except RuntimeError:
                pass
        except Exception:
            logger.debug("nav hook failed", exc_info=True)

    async def _start_screencast(self) -> None:
        assert self.context is not None and self.page is not None
        try:
            self.cdp = await self.context.new_cdp_session(self.page)
            self.cdp.on("Page.screencastFrame", lambda ev: asyncio.create_task(self._on_frame(ev)))
            height = int(SCREENCAST_MAX_WIDTH * self.viewport["height"] / max(1, self.viewport["width"]))
            await self.cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": SCREENCAST_QUALITY,
                    "maxWidth": SCREENCAST_MAX_WIDTH,
                    "maxHeight": height,
                    "everyNthFrame": 1,
                },
            )
            self.mode = "screencast"
        except Exception:
            logger.exception("CDP screencast failed; falling back to screenshots")
            self.mode = "screenshot"
            self._shot_task = asyncio.create_task(self._screenshot_loop())

    async def _on_frame(self, ev: dict[str, Any]) -> None:
        if self.closed:
            return
        session_id = ev.get("sessionId")
        try:
            if self.cdp is not None and session_id is not None:
                await self.cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            pass
        data = ev.get("data")
        if not data:
            return
        self.latest_frame = data
        if self.page is not None:
            self.latest_url = self.page.url
        await self.broadcast(self.frame_payload())

    async def _screenshot_loop(self) -> None:
        while not self.closed:
            try:
                await self._take_screenshot()
            except Exception:
                logger.debug("screenshot loop failed", exc_info=True)
            await asyncio.sleep(SCREENSHOT_INTERVAL)

    async def _take_screenshot(self) -> None:
        if self.page is None or self.closed:
            return
        raw = await self.page.screenshot(type="jpeg", quality=SCREENCAST_QUALITY, timeout=5000)
        import base64

        self.latest_frame = base64.b64encode(raw).decode("ascii")
        self.latest_url = self.page.url
        await self.broadcast(self.frame_payload())

    async def harvest(self) -> None:
        if self.page is None:
            return
        try:
            raw = await self.page.evaluate("() => window.__bpRecorder ? window.__bpRecorder.drain() : []")
        except Exception:
            raw = []
        for ev in raw or []:
            if isinstance(ev, dict) and ev.get("type"):
                self.events.append(ev)

    async def navigate(self, url: str) -> None:
        if self.page is None:
            raise RuntimeError("session is not running")
        from browser_platform.nav_policy import assert_safe_navigation_url

        target = url.strip()
        if target and "://" not in target:
            target = "https://" + target
        # Remote-record Chromium is server-side — block private/loopback and file://.
        target = assert_safe_navigation_url(target, allow_private=False)
        self.touch()
        await self.page.goto(target, wait_until="domcontentloaded", timeout=45000)
        self.latest_url = self.page.url
        await self.harvest()
        if self.mode == "screenshot":
            await self._take_screenshot()

    async def handle_input(self, message: dict[str, Any]) -> None:
        if self.page is None:
            raise RuntimeError("session is not running")
        typ = str(message.get("type") or "")
        self.touch()
        before = self.page.url
        if typ == "navigate":
            await self.navigate(str(message.get("url") or ""))
            return
        if typ == "click":
            x, y = float(message.get("x") or 0), float(message.get("y") or 0)
            await self.page.mouse.click(x, y)
            await asyncio.sleep(0.35)
        elif typ == "down":
            await self.page.mouse.move(float(message.get("x") or 0), float(message.get("y") or 0))
            await self.page.mouse.down()
        elif typ == "up":
            await self.page.mouse.up()
        elif typ == "move":
            await self.page.mouse.move(float(message.get("x") or 0), float(message.get("y") or 0))
        elif typ == "scroll":
            x, y = float(message.get("x") or 0), float(message.get("y") or 0)
            await self.page.mouse.move(x, y)
            await self.page.mouse.wheel(float(message.get("deltaX") or 0), float(message.get("deltaY") or 0))
        elif typ == "type":
            text = str(message.get("text") or "")
            if text:
                await self.page.keyboard.type(text, delay=15)
        elif typ == "key":
            key = str(message.get("key") or "Enter")
            await self.page.keyboard.press(key)
        else:
            raise ValueError(f"unknown input type: {typ}")
        await self.harvest()
        # Mark the last click as changed when the URL moved.
        if typ == "click" and self.events:
            for ev in reversed(self.events):
                if ev.get("type") == "click" and "changed" not in ev:
                    ev["changed"] = self.page.url != before
                    ev["url_after"] = self.page.url
                    break
        if self.page is not None:
            self.latest_url = self.page.url
        if self.mode == "screenshot":
            await self._take_screenshot()

    async def locate(self, *, selector: str | None = None, text: str | None = None, role: str | None = None) -> dict[str, Any] | None:
        if self.page is None:
            return None
        loc = None
        if role and text:
            loc = self.page.get_by_role(role, name=text)  # type: ignore[arg-type]
        elif text:
            loc = self.page.get_by_text(text, exact=False)
        elif selector:
            loc = self.page.locator(selector)
        else:
            return None
        first = loc.first
        if await first.count() == 0:
            return None
        if not await first.is_visible():
            try:
                await first.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                return None
        box = await first.bounding_box()
        if not box:
            return None
        self.touch()
        return {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2, **box}

    async def stop_and_clean(self, *, goal: str = "", name: str = "") -> dict[str, Any]:
        from browser_platform.authoring.from_recording import clean_recording

        await self.harvest()
        self.touch()
        events = list(self.events)
        result = await clean_recording(events=events, goal=goal or self.goal, name=name or self.name or None)
        payload = result.model_dump(exclude_none=True)
        payload["events"] = events
        return payload

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._shot_task is not None:
            self._shot_task.cancel()
        try:
            if self.cdp is not None:
                await self.cdp.send("Page.stopScreencast")
        except Exception:
            pass
        try:
            if self.browser is not None:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        await self.broadcast({"type": "closed", "id": self.id})
        self._clients.clear()


class SessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, RecordSession] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None

    def start_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        while True:
            try:
                await self.reap_idle()
            except Exception:
                logger.debug("reaper failed", exc_info=True)
            await asyncio.sleep(15)

    async def reap_idle(self) -> None:
        stale = [s for s in list(self.sessions.values()) if s.idle_for() >= IDLE_SECONDS or s.closed]
        for session in stale:
            await self.drop(session.id)

    async def create(self, *, start_url: str = "", goal: str = "", name: str = "") -> RecordSession:
        await self.reap_idle()
        async with self._lock:
            live = [s for s in self.sessions.values() if not s.closed]
            if len(live) >= MAX_SESSIONS:
                raise SessionBusy(f"Already running {len(live)} remote recording session(s); the cap is {MAX_SESSIONS}. Stop one, or wait for it to idle out after {IDLE_SECONDS // 60} minutes.")
            session = RecordSession(start_url=start_url, goal=goal, name=name)
            self.sessions[session.id] = session
        try:
            await session.start()
        except Exception:
            await self.drop(session.id)
            raise
        return session

    def get(self, session_id: str) -> RecordSession | None:
        session = self.sessions.get(session_id)
        if session is None or session.closed:
            return None
        return session

    async def drop(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is not None:
            await session.close()

    async def close_all(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
        for session_id in list(self.sessions):
            await self.drop(session_id)


def manager_from(app: Any) -> SessionManager:
    existing = getattr(app.state, "record_sessions", None)
    if isinstance(existing, SessionManager):
        return existing
    manager = SessionManager()
    app.state.record_sessions = manager
    manager.start_reaper()
    return manager

"""Human-in-the-loop confirmation broker and in-process event hub."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Action = Literal["confirm", "cancel", "timeout"]


@dataclass
class ConfirmationDecision:
    action: Action
    candidate_id: str | None = None
    text: str | None = None
    reason: str | None = None
    variables: dict[str, Any] | None = None
    parameters: list[dict[str, Any]] | None = None


@dataclass
class EventHub:
    """Fan-out run events (needs_confirmation, run_resumed, run_finished) to WebSocket clients."""

    _subs: set[asyncio.Queue] = field(default_factory=set)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subs.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subs.discard(queue)

    async def publish(self, event: dict[str, Any]) -> None:
        if "ts" not in event:
            event = {**event, "ts": datetime.now(timezone.utc).isoformat()}
        for queue in list(self._subs):
            try:
                queue.put_nowait(event)
            except Exception:
                self._subs.discard(queue)


class ConfirmationBroker:
    """Pause a run until a human confirms a candidate, cancels, or the timeout fires."""

    def __init__(self, event_hub: EventHub | None = None):
        self.event_hub = event_hub or EventHub()
        self._futures: dict[str, asyncio.Future] = {}
        self._payloads: dict[str, dict[str, Any]] = {}

    def pending(self, run_id: str) -> dict[str, Any] | None:
        return self._payloads.get(run_id)

    def has(self, run_id: str) -> bool:
        fut = self._futures.get(run_id)
        return bool(fut and not fut.done())

    async def wait(
        self,
        run_id: str,
        payload: dict[str, Any],
        *,
        timeout_s: float,
    ) -> ConfirmationDecision:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._futures[run_id] = future
        self._payloads[run_id] = payload
        try:
            await self.event_hub.publish(payload)
            if timeout_s is not None and timeout_s <= 0:
                return ConfirmationDecision(action="timeout", reason="timeout")
            if timeout_s is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError:
            if not future.done():
                future.cancel()
            return ConfirmationDecision(action="timeout", reason="timeout")
        finally:
            self._futures.pop(run_id, None)
            self._payloads.pop(run_id, None)

    def resolve(self, run_id: str, decision: ConfirmationDecision) -> bool:
        future = self._futures.get(run_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True

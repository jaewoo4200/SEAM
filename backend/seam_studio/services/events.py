"""In-process pub/sub event hub, keyed by project id.

WebSocket handlers (running on the event loop) subscribe with an asyncio.Queue;
the synchronous route handlers (running in the threadpool via FastAPI's sync
def endpoints) call :func:`publish` to fan an event out to every subscriber of
a project. Because publishers run in worker threads and consumers live on the
loop, each subscription captures the running loop at subscribe time and every
enqueue goes through ``loop.call_soon_threadsafe`` - the only thread-safe way
to touch an asyncio.Queue from another thread.

No external dependencies: a plain ``threading.Lock`` guards the subscriber
registry and full queues drop the event (bounded memory) rather than blocking a
solve. Events are JSON-serializable dicts; the WS layer owns the wire format.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional

# Per-subscription queue depth. A slow/absent WS reader loses the oldest events
# beyond this many rather than back-pressuring the simulate threadpool.
_QUEUE_MAXSIZE = 100


class _Subscription:
    """A single WS subscriber: its queue plus the loop that drains it."""

    __slots__ = ("queue", "loop")

    def __init__(self, queue: "asyncio.Queue[dict]", loop: asyncio.AbstractEventLoop):
        self.queue = queue
        self.loop = loop


class EventHub:
    """Thread-safe project-scoped fan-out.

    subscribe() is called from the loop (WS handler); publish() is called from
    the threadpool (sync route handlers). The registry lock is held only for
    the brief list mutation/copy, never across the enqueue.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, list[_Subscription]] = {}

    def subscribe(self, project_id: str) -> "asyncio.Queue[dict]":
        """Register a subscriber for ``project_id`` and return its queue.

        Must be called from within a running event loop (the WS handler's).
        """
        loop = asyncio.get_running_loop()
        queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        sub = _Subscription(queue, loop)
        with self._lock:
            self._subs.setdefault(project_id, []).append(sub)
        return queue

    def unsubscribe(self, project_id: str, queue: "asyncio.Queue[dict]") -> None:
        """Remove a subscriber. Idempotent; safe to call from ``finally``."""
        with self._lock:
            subs = self._subs.get(project_id)
            if not subs:
                return
            self._subs[project_id] = [s for s in subs if s.queue is not queue]
            if not self._subs[project_id]:
                del self._subs[project_id]

    def publish(self, project_id: str, event: dict) -> None:
        """Fan ``event`` out to every subscriber of ``project_id``.

        Safe to call from any thread. A full queue drops the event (never
        blocks). The publisher captures a shallow copy of the subscriber list
        under the lock, then enqueues outside it.
        """
        with self._lock:
            subs = list(self._subs.get(project_id, ()))
        for sub in subs:
            sub.loop.call_soon_threadsafe(_enqueue, sub.queue, event)

    def subscriber_count(self, project_id: str) -> int:
        """Live subscriber count for ``project_id`` (diagnostics/tests)."""
        with self._lock:
            return len(self._subs.get(project_id, ()))


def _enqueue(queue: "asyncio.Queue[dict]", event: dict) -> None:
    """Runs on the loop thread (via call_soon_threadsafe): drop when full."""
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        pass


_HUB: Optional[EventHub] = None
_HUB_LOCK = threading.Lock()


def get_hub() -> EventHub:
    """Process-wide singleton hub (lazily created, thread-safe)."""
    global _HUB
    if _HUB is None:
        with _HUB_LOCK:
            if _HUB is None:
                _HUB = EventHub()
    return _HUB


def publish_event(project_id: str, event: dict) -> None:
    """Convenience wrapper used by the publish hooks in the route handlers.

    Never raises - a telemetry failure must not break a simulate/compile call.
    """
    try:
        get_hub().publish(project_id, event)
    except Exception:  # noqa: BLE001 - event delivery is best-effort telemetry
        pass

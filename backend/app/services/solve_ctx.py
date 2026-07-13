"""Per-solve cancellation + progress plumbing.

A sync simulate endpoint enters a solve context (project id + kind) for the
duration of its threadpool run. Long service loops (trajectory steps, dataset
samples, mesh chunks, scenario frames) call ``tick()`` with no extra
arguments — the context travels via a contextvar, so no service signature has
to change. ``POST /simulate/cancel`` flags the project and the next tick
raises :class:`SolveCancelled`, which the endpoint guard translates to a 409.

Single-shot solver calls (one PathSolver invocation) cannot be interrupted
mid-call; cancellation lands at the next loop checkpoint.
"""

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from app.services.events import publish_event


class SolveCancelled(Exception):
    """Raised inside a solve loop after POST /simulate/cancel."""


_current: ContextVar[Optional[dict]] = ContextVar("solve_ctx", default=None)
_cancel_requested: set[str] = set()
_guard = threading.Lock()

# Progress events are throttled so a 20k-sample dataset run does not flood
# the WS hub; the final tick always emits so the bar reaches 100%.
_EMIT_INTERVAL_S = 0.5


@contextmanager
def solve_context(project_id: str, kind: str) -> Iterator[None]:
    with _guard:
        _cancel_requested.discard(project_id)
    token = _current.set(
        {"project_id": project_id, "kind": kind, "last_emit": 0.0}
    )
    try:
        yield
    finally:
        _current.reset(token)
        with _guard:
            _cancel_requested.discard(project_id)


def request_cancel(project_id: str) -> None:
    with _guard:
        _cancel_requested.add(project_id)


def check_cancelled() -> None:
    ctx = _current.get()
    if ctx is None:
        return
    with _guard:
        hit = ctx["project_id"] in _cancel_requested
    if hit:
        raise SolveCancelled(f"solve cancelled for {ctx['project_id']}")


def tick(done: int, total: int) -> None:
    """Cancellation checkpoint + throttled ``simulation_progress`` event.

    No-op outside a solve context, so services stay callable from tests and
    scripts without any setup.
    """
    ctx = _current.get()
    if ctx is None:
        return
    check_cancelled()
    now = time.monotonic()
    if now - ctx["last_emit"] >= _EMIT_INTERVAL_S or done >= total:
        ctx["last_emit"] = now
        publish_event(
            ctx["project_id"],
            {
                "type": "simulation_progress",
                "kind": ctx["kind"],
                "done": done,
                "total": total,
            },
        )

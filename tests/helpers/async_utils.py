"""Async testing utilities.

Provides polling helpers that replace ad-hoc ``asyncio.sleep`` calls
in tests with deterministic condition-based waiting, and bounded
cancellation cleanup that can never turn a test failure into a hang.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Any


async def wait_until(
    condition: Callable[[], Any],
    timeout: float = 5.0,
    interval: float = 0.05,
) -> bool:
    """Poll *condition* until it returns truthy or *timeout* expires.

    Supports both sync and async callables: if ``condition()`` returns a
    coroutine, it is awaited automatically.

    Returns ``True`` if the condition was met within *timeout*, ``False``
    otherwise.
    """
    deadline = time.monotonic() + timeout
    while True:
        result = condition()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(interval, remaining))


def _retrieve_task_result(task: asyncio.Future[Any]) -> None:
    """Retrieve a finished task's outcome so the loop logs no stray warning."""
    with contextlib.suppress(asyncio.CancelledError):
        task.exception()


async def bounded_cancel_and_reap(
    tasks: tuple[asyncio.Task[Any], ...],
    *,
    timeout: float = 0.2,
) -> list[asyncio.Task[Any]]:
    """Best-effort cancellation cleanup that can never turn a failure into a hang.

    Cancels every not-yet-done task, waits at most *timeout* for them to
    settle, and reaps finished tasks (including ones that completed before
    the call, and ones that finished with exceptions) so the event loop
    never reports "Task exception was never retrieved".

    Returns the tasks that were still pending after *timeout* — the caller
    decides whether that constitutes a failure.
    """
    done, active = await asyncio.wait(tasks, timeout=0)
    if done:
        await asyncio.gather(*done, return_exceptions=True)
    for task in active:
        task.cancel()
    if not active:
        return []

    settled, pending = await asyncio.wait(active, timeout=timeout)
    if settled:
        await asyncio.gather(*settled, return_exceptions=True)
    for task in pending:
        task.add_done_callback(_retrieve_task_result)
    return list(pending)

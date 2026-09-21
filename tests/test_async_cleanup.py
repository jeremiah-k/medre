"""Contracts for the shared bounded-cancellation cleanup helper.

``bounded_cancel_and_reap`` backs failure cleanup in shutdown-adjacent
tests: it must cancel, wait a bounded time, and reap every finished task
(pre-completed, exception-completing, or cancelled) without ever waiting
unboundedly on a task that refuses to settle.
"""

from __future__ import annotations

import asyncio

from tests.helpers.async_utils import bounded_cancel_and_reap


async def _fail_fast() -> None:
    raise RuntimeError("settled before cleanup")


async def test_bounded_cancel_and_reap_never_waits_for_resistant_task() -> None:
    """Failure cleanup reports a stubborn task instead of hanging the suite."""
    release = asyncio.Event()
    started = asyncio.Event()

    async def _resist_cancel() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    task = asyncio.create_task(_resist_cancel())
    await started.wait()

    pending = await bounded_cancel_and_reap((task,), timeout=0.01)

    assert pending == [task]
    release.set()
    done, still_pending = await asyncio.wait({task}, timeout=0.2)
    assert not still_pending
    await asyncio.gather(*done, return_exceptions=True)


async def test_bounded_cancel_and_reap_reaps_precompleted_tasks() -> None:
    """Tasks that finished before cleanup are reaped, exceptions included.

    An exception-completing task that is never retrieved makes the event
    loop log "Task exception was never retrieved"; with ``filterwarnings =
    error`` suites that warning becomes noise at best and an error at
    worst.
    """
    task = asyncio.create_task(_fail_fast())
    await asyncio.wait({task}, timeout=0)

    pending = await bounded_cancel_and_reap((task,), timeout=0.01)

    assert pending == []
    assert task.done()
    assert isinstance(task.exception(), RuntimeError)

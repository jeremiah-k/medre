"""Deterministic fault-bounds tests for the live-harness machinery.

Device-free, default-collected, seconds-fast: proves the harness safety
contracts that live hardware suites rely on, WITHOUT radios:

- ``bounded`` returns control at its deadline even when the awaited work
  resists cancellation (``asyncio.wait_for`` hangs in exactly that case
  because it waits for the inner task to acknowledge cancellation) — a
  bounded in-process wait is not defeated by rude inner work.
- ``launch_bounded`` (the shared build+start pattern of the live suites)
  guarantees bounded cleanup on start failure: cancellation-responsive starts
  settle before stop runs, the primary error propagates, and a failing cleanup
  never masks it.
- A cancellation-resistant ``start()`` is never raced against ``stop()``; when
  it cannot settle inside the cleanup budget, cleanup is deferred until the
  start task eventually reaches a terminal state.

Child exit / never-ready fault cases for the peer process boundary are
pinned in ``tests/test_live_peer_common.py``.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from tests.helpers.live_harness import bounded, launch_bounded


class _FakeApp:
    """Minimal app stand-in recording lifecycle calls."""

    def __init__(
        self,
        *,
        start_exception: BaseException | None = None,
        start_hang: bool = False,
        stop_exception: BaseException | None = None,
    ) -> None:
        self.start_exception = start_exception
        self.start_hang = start_hang
        self.stop_exception = stop_exception
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        if self.start_hang:
            await asyncio.Event().wait()
        if self.start_exception is not None:
            raise self.start_exception

    async def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_exception is not None:
            raise self.stop_exception


async def test_returns_at_deadline_when_work_ignores_cancellation() -> None:
    """Cancellation-swallowing work cannot defeat the deadline.

    ``asyncio.wait_for`` waits for the inner task to acknowledge
    cancellation, so a rude coroutine (swallows CancelledError, keeps
    hanging) hangs it — the harness then discovers the bad fixture
    only at some much larger outer timeout.  ``bounded`` must race a
    timer instead and return control at its own deadline.
    """
    release = asyncio.Event()

    async def rude() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Swallow ONE cancellation, then hang until released —
            # the rude SDK shape, with a test-controlled exit so the
            # loop stays clean after the assertion.
            await release.wait()

    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="rude-work"):
        await bounded(rude(), 0.2, "rude-work")
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, "bounded must return control promptly at deadline"
    # The straggler is still pending at this point (documented leak
    # mode; the process boundary reaps real ones).  Release it so the
    # loop exits clean.
    release.set()
    stragglers = [
        t
        for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done()
    ]
    if stragglers:
        await asyncio.wait(stragglers, timeout=5.0)


async def test_primary_exception_propagates_with_label() -> None:
    async def boom() -> None:
        raise ValueError("primary failure")

    with pytest.raises(ValueError, match="primary failure"):
        await bounded(boom(), 5.0, "boom-label")


async def test_result_returned_on_success() -> None:
    async def ok() -> int:
        return 7

    assert await bounded(ok(), 5.0, "ok-label") == 7


async def test_caller_cancellation_cancels_inner_operation() -> None:
    """Cancelling the waiter must not detach the operation it owns."""
    started = asyncio.Event()
    finished = asyncio.Event()

    async def inner() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    waiter = asyncio.create_task(bounded(inner(), 30.0, "cancelled-owner"))
    await started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.wait_for(finished.wait(), timeout=1.0)


async def test_start_failure_stops_app_and_reraises_primary() -> None:
    app = _FakeApp(start_exception=RuntimeError("adapter blew up"))
    with pytest.raises(RuntimeError, match="adapter blew up"):
        await launch_bounded(
            lambda: app,
            start_timeout=10.0,
            stop_timeout=10.0,
            label="fault-app",
        )
    assert app.start_calls == 1
    assert app.stop_calls == 1, "partially-started app must be stopped"


async def test_failing_cleanup_does_not_mask_primary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = _FakeApp(
        start_exception=RuntimeError("primary start error"),
        stop_exception=RuntimeError("cleanup also failed"),
    )
    with pytest.raises(RuntimeError, match="primary start error"):
        await launch_bounded(
            lambda: app,
            start_timeout=10.0,
            stop_timeout=10.0,
            label="fault-app",
        )
    assert app.stop_calls == 1
    assert "cleanup after failed start also failed" in capsys.readouterr().out




async def test_cancelled_cleanup_does_not_mask_primary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = _FakeApp(
        start_exception=RuntimeError("primary start error"),
        stop_exception=asyncio.CancelledError(),
    )
    with pytest.raises(RuntimeError, match="primary start error"):
        await launch_bounded(
            lambda: app,
            start_timeout=10.0,
            stop_timeout=10.0,
            label="fault-app",
        )
    assert app.stop_calls == 1
    assert "cleanup after failed start also failed" in capsys.readouterr().out


async def test_cancellation_resistant_start_is_not_raced_by_stop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    release = asyncio.Event()
    finished = asyncio.Event()

    class _RudeStartApp:
        def __init__(self) -> None:
            self.start_active = False
            self.stop_calls = 0
            self.stop_during_start = False

        async def start(self) -> None:
            self.start_active = True
            try:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()
            finally:
                self.start_active = False
                finished.set()

        async def stop(self) -> None:
            self.stop_calls += 1
            self.stop_during_start = self.start_active

    app = _RudeStartApp()
    with pytest.raises(RuntimeError, match="fault-app start"):
        await launch_bounded(
            lambda: app,
            start_timeout=0.02,
            stop_timeout=0.02,
            label="fault-app",
        )

    assert app.stop_calls == 0
    assert app.stop_during_start is False
    assert "deferring stop() until start settles" in capsys.readouterr().out

    release.set()
    await asyncio.wait_for(finished.wait(), timeout=1.0)
    for _ in range(100):
        if app.stop_calls == 1:
            break
        await asyncio.sleep(0)
    assert app.stop_calls == 1
    assert app.stop_during_start is False


async def test_hanging_start_hits_deadline_then_cleans_up() -> None:
    app = _FakeApp(start_hang=True)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="fault-app start"):
        await launch_bounded(
            lambda: app,
            start_timeout=0.2,
            stop_timeout=10.0,
            label="fault-app",
        )
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, "hanging start must fail at its deadline"
    assert app.stop_calls == 1, "cleanup must run after the deadline"


async def test_successful_start_returns_app_without_stop() -> None:
    app = _FakeApp()
    result = await launch_bounded(
        lambda: app,
        start_timeout=10.0,
        stop_timeout=10.0,
        label="fault-app",
    )
    assert result is app
    assert app.start_calls == 1
    assert app.stop_calls == 0

"""Retry worker stop, orphan-task, and cancellation hardening tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.helpers.async_utils import wait_until


class TestRetryWorkerStopOrphan:
    """Hardened stop() tests using the real RetryWorker.

    Proves that stop() honors the bounded grace period, reports
    abandonment honestly when the task is cancellation-resistant, and
    is idempotent.
    """

    async def test_stop_timeout_cancels_cancellation_responsive_task(self):
        """Cancellation-responsive task: stop() clears _task and emits
        retry_stopped.

        The worker's _run_loop blocks in _process_due via a storage
        call that respects ``task.cancel()`` and returns.  After the
        grace period the task is cancelled and awaited so no orphan
        remains.
        """
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        # claim_due_outbox_items that honours task.cancel() (i.e. the
        # underlying aiosqlite connection or similar cooperates).  We
        # model this as "waits on an event that is set when the task
        # is cancelled".
        _cancelled_evt = asyncio.Event()

        async def _cooperative_claim(*args, **kwargs):
            try:
                # Suspend forever, but raise if the task is cancelled.
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                _cancelled_evt.set()
                raise
            return []

        storage.claim_due_outbox_items = AsyncMock(side_effect=_cooperative_claim)
        storage.count_outbox_by_status = AsyncMock(return_value={})

        pipeline = MagicMock()
        pipeline.deliver_to_target = AsyncMock()

        event_buffer = EventBuffer(maxlen=64)

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            enabled=True,
            interval_seconds=300,
            event_buffer=event_buffer,
            stop_timeout_seconds=0.2,
        )

        await worker.start()
        # Wait deterministically until the task is actually blocked in
        # the storage call (avoids flaky asyncio.sleep(0.1)).
        await wait_until(
            lambda: storage.claim_due_outbox_items.call_count >= 1,
            timeout=2.0,
        )
        orig_task = worker._task
        assert orig_task is not None
        assert not orig_task.done()

        await worker.stop()
        # Yield so the cancellation is observed before assertions.
        await asyncio.sleep(0)

        # Cancellation-responsive: _task cleared, retry_stopped emitted.
        assert worker._task is None
        assert orig_task is not None
        assert orig_task.done()
        assert worker.state.running is False
        assert worker.state.abandoned is False
        event_types = [e.event_type.value for e in event_buffer]
        assert "retry_stopped" in event_types
        assert "retry_abandoned" not in event_types
        # Verify retry_stopped payload contains the standard counters.
        stopped_events = [
            e for e in event_buffer if e.event_type.value == "retry_stopped"
        ]
        assert len(stopped_events) == 1
        for key in ("processed", "succeeded", "failed", "dead_lettered"):
            assert (
                key in stopped_events[0].detail
            ), f"retry_stopped payload missing '{key}'"

    async def test_stop_does_not_clear_task_when_cancellation_resistant(self):
        """Cancellation-resistant task: stop() returns boundedly but does
        NOT clear _task or report a clean stop.

        Models a storage call that swallows ``CancelledError`` and
        continues blocking.  The worker's two-stage bounded cancel must
        not hang forever; it returns within ``2 * stop_timeout_seconds``
        and reports abandonment by setting ``state.abandoned = True``
        while keeping ``_task`` referencing the still-alive task.
        """
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        # claim_due_outbox_items that catches CancelledError and spins
        # until released.  This models an adapter-side bug where
        # cancellation is swallowed and the call keeps blocking.
        _release = asyncio.Event()

        async def _cancellation_resistant_claim(*args, **kwargs):
            try:
                await _release.wait()
            except asyncio.CancelledError:
                # Swallow: this is the bug we are simulating.
                pass
            while not _release.is_set():
                # Yield to the loop so the task is not a CPU spin, but
                # ignore any cancellation that arrives here too.  Each
                # await is a fresh cancel delivery point but the
                # except handler continues until released.
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    continue
            return []

        storage.claim_due_outbox_items = AsyncMock(
            side_effect=_cancellation_resistant_claim
        )
        storage.count_outbox_by_status = AsyncMock(return_value={})

        pipeline = MagicMock()
        pipeline.deliver_to_target = AsyncMock()

        event_buffer = EventBuffer(maxlen=64)

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            enabled=True,
            interval_seconds=300,
            event_buffer=event_buffer,
            stop_timeout_seconds=0.1,
        )

        try:
            await worker.start()
            await wait_until(
                lambda: storage.claim_due_outbox_items.call_count >= 1,
                timeout=2.0,
            )
            orig_task = worker._task
            assert orig_task is not None
            assert not orig_task.done()

            stop_start = asyncio.get_event_loop().time()
            await worker.stop()
            stop_elapsed = asyncio.get_event_loop().time() - stop_start

            # Hard bound: stop() must return within ~2*stop_timeout + slack
            assert stop_elapsed < 1.0, (
                f"stop() took {stop_elapsed:.3f}s, "
                f"expected < 1.0s for stop_timeout=0.1"
            )

            # Cancellation-resistant: _task is KEPT, abandoned=True,
            # running=True, retry_abandoned emitted, retry_stopped NOT.
            assert (
                worker._task is orig_task
            ), "_task must remain pointing at the still-alive task"
            assert worker.state.running is True
            assert worker.state.abandoned is True
            event_types = [e.event_type.value for e in event_buffer]
            assert "retry_abandoned" in event_types
            assert "retry_stopped" not in event_types
            # Verify retry_abandoned payload includes the timeout and
            # the standard counters.
            abandoned_events = [
                e for e in event_buffer if e.event_type.value == "retry_abandoned"
            ]
            assert len(abandoned_events) == 1
            assert abandoned_events[0].detail.get("stop_timeout_seconds") == 0.1
            for key in ("processed", "succeeded", "failed", "dead_lettered"):
                assert (
                    key in abandoned_events[0].detail
                ), f"retry_abandoned payload missing '{key}'"
        finally:
            # Release the stuck call so the task can complete and not
            # leak into other tests.
            _release.set()
            if worker._task is not None and not worker._task.done():
                # Give the task a moment to finish after release.
                try:
                    await asyncio.wait_for(worker._task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass

    async def test_start_refused_when_previous_stop_abandoned_task(self):
        """After a cancellation-resistant stop, subsequent start() is refused
        while state.abandoned is True (so a duplicate worker is not launched
        over the same outbox)."""
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        _release = asyncio.Event()

        async def _resistant_claim(*args, **kwargs):
            try:
                await _release.wait()
            except asyncio.CancelledError:
                pass
            while not _release.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    continue
            return []

        storage.claim_due_outbox_items = AsyncMock(side_effect=_resistant_claim)
        storage.count_outbox_by_status = AsyncMock(return_value={})

        pipeline = MagicMock()
        pipeline.deliver_to_target = AsyncMock()

        event_buffer = EventBuffer(maxlen=64)

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            enabled=True,
            interval_seconds=300,
            event_buffer=event_buffer,
            stop_timeout_seconds=0.1,
        )

        try:
            await worker.start()
            await wait_until(
                lambda: storage.claim_due_outbox_items.call_count >= 1,
                timeout=2.0,
            )
            await worker.stop()
            assert worker.state.abandoned is True
            assert worker._task is not None

            # Second start() must be refused: _task is still referenced,
            # call_count is unchanged.
            prev_call_count = storage.claim_due_outbox_items.call_count
            await worker.start()
            assert worker._task is not None
            assert storage.claim_due_outbox_items.call_count == prev_call_count
        finally:
            _release.set()
            if worker._task is not None and not worker._task.done():
                try:
                    await asyncio.wait_for(worker._task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass

    async def test_stop_idempotent(self):
        """Calling stop() twice is safe — second call is a no-op."""
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        storage.claim_due_outbox_items = AsyncMock(return_value=[])
        storage.count_outbox_by_status = AsyncMock(return_value={})

        pipeline = MagicMock()
        pipeline.deliver_to_target = AsyncMock()

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            enabled=True,
            interval_seconds=300,
        )

        await worker.start()
        # Wait for at least one loop iteration.
        await wait_until(
            lambda: storage.claim_due_outbox_items.call_count >= 1,
            timeout=2.0,
        )

        # First stop.
        orig_task = worker._task
        await worker.stop()
        await asyncio.sleep(0)

        assert worker._task is None
        assert orig_task is not None
        assert orig_task.done()
        assert worker.state.running is False

        # Second stop — must not raise.
        await worker.stop()
        assert worker._task is None
        assert worker.state.running is False

    async def test_stop_cleared_task_not_running(self):
        """After stop(), state.running is False regardless of path taken."""
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        storage.claim_due_outbox_items = AsyncMock(return_value=[])
        storage.count_outbox_by_status = AsyncMock(return_value={})

        pipeline = MagicMock()
        pipeline.deliver_to_target = AsyncMock()

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            enabled=True,
            interval_seconds=300,
        )

        await worker.start()
        await wait_until(
            lambda: storage.claim_due_outbox_items.call_count >= 1,
            timeout=2.0,
        )

        orig_task = worker._task
        await worker.stop()
        await asyncio.sleep(0)

        assert worker._task is None
        assert orig_task is not None
        assert orig_task.done()
        assert worker.state.running is False
        # State counters are preserved (not reset).
        assert worker.state.processed == 0

    async def test_stop_when_task_already_done(self):
        """stop() called when the background task has already completed
        naturally (not due to stop()) returns promptly with clean-stop
        semantics.

        Covers the early-exit path in the polling loop: if ``task.done()``
        is ``True`` on the first iteration, the loop exits immediately
        without ever entering ``_force_cancel_with_poll``.
        """
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()

        # A cleanly-completing claim: the task returns ``[]`` (no
        # items) on its first poll, then the ``wait_for`` sees the
        # shutdown event not yet set, waits the interval, and on
        # the next iteration the event is still not set, so it
        # loops.  To get a clean natural completion we make the
        # first claim return ``[]`` and then signal the shutdown
        # event from a side channel.  Simpler: just have the claim
        # return ``[]`` and have a separate event that ``stop()``
        # will set via the normal path — but we want the task to
        # complete *before* stop() is called.
        #
        # Cleanest approach: make the claim return ``[]`` and have
        # the run loop's ``wait_for`` see a very short interval
        # timeout, then loop.  But that's the normal running path,
        # not a "task already done" scenario.
        #
        # To get a task that completes *naturally* (not via cancel,
        # not via stop's shutdown event), we need the run loop to
        # exit on its own.  The loop exits when
        # ``self._shutdown_event.is_set()`` becomes True, which is
        # only set by ``stop()``.  So a truly "natural" completion
        # in the current implementation is only possible via a
        # crash.
        #
        # This test models the "task already done with a crash"
        # scenario: the claim raises ``_FatalCrash`` (a
        # ``BaseException`` that escapes ``_run_loop``'s ``except
        # Exception``), the task ends with that exception, and
        # ``stop()`` is called afterward.  The fix must surface the
        # crash as ``retry_failed`` (not ``retry_stopped``) so
        # operators see the real failure.
        class _FatalCrash(BaseException):
            pass

        async def _crashing_claim(*args, **kwargs):
            raise _FatalCrash("simulated unrecoverable error")

        storage.claim_due_outbox_items = AsyncMock(side_effect=_crashing_claim)
        storage.count_outbox_by_status = AsyncMock(return_value={})

        pipeline = MagicMock()
        pipeline.deliver_to_target = AsyncMock()

        event_buffer = EventBuffer(maxlen=64)

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            enabled=True,
            interval_seconds=300,
            event_buffer=event_buffer,
        )

        await worker.start()
        # Wait for the task to complete (it will end with _FatalCrash
        # because the claim raises a BaseException the loop cannot
        # catch).
        orig_task = worker._task
        assert orig_task is not None
        await wait_until(lambda: orig_task.done(), timeout=2.0)
        assert orig_task.done()

        # Now call stop() — must return promptly with clean-stop
        # cleanup (clear _task, set running=False) but surface the
        # crash as ``retry_failed`` rather than falsely reporting
        # ``retry_stopped``.  The fix re-raises the crash from
        # ``stop()`` so the caller sees the real failure.
        stop_start = asyncio.get_event_loop().time()
        with pytest.raises(_FatalCrash, match="simulated unrecoverable error"):
            await worker.stop()
        stop_elapsed = asyncio.get_event_loop().time() - stop_start

        assert stop_elapsed < 0.1, (
            f"stop() took {stop_elapsed:.3f}s for an already-done task; "
            f"expected < 0.1s"
        )
        assert worker._task is None
        assert worker.state.running is False
        assert worker.state.abandoned is False
        event_types = [e.event_type.value for e in event_buffer]
        # Crashed task must surface as ``retry_failed``, not
        # ``retry_stopped``.  The pre-fix code emitted
        # ``retry_stopped`` for any done task, hiding the crash.
        assert "retry_failed" in event_types
        assert "retry_stopped" not in event_types
        assert "retry_abandoned" not in event_types
        # Payload must include the exception text.
        failed_events = [
            e for e in event_buffer if e.event_type.value == "retry_failed"
        ]
        assert failed_events
        assert "_FatalCrash" in failed_events[0].detail["error"]
        assert "simulated unrecoverable error" in (failed_events[0].detail["error"])

    async def test_stop_cancelled_after_task_already_done(self):
        """stop() cancelled by the caller *after* the background task has
        already completed naturally must still do clean-stop cleanup.

        Regression: the old ``except asyncio.CancelledError`` branch in
        ``stop()`` only checked ``if not task.done()`` and skipped
        cleanup when the task was already done, leaking ``_task`` and
        ``state.running=True``.  The fix splits the branch: if the task
        is already done at cancellation time, do the clean-stop cleanup
        (clear ``_task``, set ``state.running=False``, emit
        ``retry_stopped``) and then re-raise.

        The test uses a sub-task for ``stop()`` and cancels it
        immediately.  Because the background task is already done,
        ``stop()`` may complete normally (in which case the cancel is
        a no-op and the clean-stop path is verified directly) or it
        may be cancelled mid-execution (in which case the new
        ``task.done()`` branch in the ``except CancelledError`` handler
        is verified).  Either way the end state must be clean.
        """
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        storage.claim_due_outbox_items = AsyncMock(return_value=[])
        storage.count_outbox_by_status = AsyncMock(return_value={})

        pipeline = MagicMock()
        pipeline.deliver_to_target = AsyncMock()

        event_buffer = EventBuffer(maxlen=64)

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            enabled=True,
            interval_seconds=300,
            event_buffer=event_buffer,
        )

        await worker.start()
        orig_task = worker._task
        assert orig_task is not None

        # Signal shutdown so the run loop exits cleanly on its next
        # iteration, ending the task without raising.  Wait for the
        # task to be truly done.
        worker._shutdown_event.set()
        await wait_until(lambda: orig_task.done(), timeout=2.0)
        assert orig_task.done()

        # Force stop() to be cancelled by the caller.  wait_for with
        # a very short timeout cancels the inner coroutine at the
        # first await point, modelling an external cancellation that
        # arrives while stop() is running.  When the background task
        # is already done, stop() may complete normally before the
        # timeout fires (in which case the cancel is a no-op and the
        # clean-stop path is verified directly) or it may be
        # cancelled mid-execution (in which case the new
        # ``task.done()`` branch in the ``except CancelledError``
        # handler is verified).  Either way the end state must be
        # clean.
        try:
            await asyncio.wait_for(worker.stop(), timeout=1e-6)
        except asyncio.TimeoutError:
            pass  # expected: wait_for cancelled stop() mid-execution

        # Clean-stop state must hold regardless of whether stop()
        # completed normally or was cancelled mid-execution.
        assert worker._task is None
        assert worker.state.running is False
        assert worker.state.abandoned is False
        event_types = [e.event_type.value for e in event_buffer]
        assert "retry_stopped" in event_types
        assert "retry_abandoned" not in event_types

    async def test_concurrent_stop_no_duplicate_events(self):
        """Concurrent stop() calls do not emit duplicate events.

        The internal ``asyncio.Lock`` serialises callers so the second
        call sees ``_task is None`` after the first finishes and
        returns without entering the polling loop.
        """
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        storage.claim_due_outbox_items = AsyncMock(return_value=[])
        storage.count_outbox_by_status = AsyncMock(return_value={})

        pipeline = MagicMock()
        pipeline.deliver_to_target = AsyncMock()

        event_buffer = EventBuffer(maxlen=64)

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            enabled=True,
            interval_seconds=300,
            event_buffer=event_buffer,
        )

        await worker.start()
        await wait_until(
            lambda: storage.claim_due_outbox_items.call_count >= 1,
            timeout=2.0,
        )

        # Launch two stop() calls concurrently.
        await asyncio.gather(worker.stop(), worker.stop())

        # Exactly one retry_stopped event.
        event_types = [e.event_type.value for e in event_buffer]
        assert (
            event_types.count("retry_stopped") == 1
        ), f"expected exactly one retry_stopped, got {event_types.count('retry_stopped')}"
        assert worker._task is None
        assert worker.state.running is False

    async def test_stop_cancelled_mid_poll(self):
        """External cancellation of stop() itself marks the worker
        abandoned and emits ``retry_abandoned`` with
        ``reason='stop_cancelled'``.

        Models the case where the caller of ``await stop()`` is
        cancelled mid-poll (e.g. ``MedreApp.stop()`` hits a shutdown
        timeout and cancels its inner cleanup).  The worker must not
        silently leave ``state.running=True`` and ``state.abandoned=False``
        — the caller needs a way to detect this state.
        """
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        _release = asyncio.Event()

        async def _resistant_claim(*args, **kwargs):
            try:
                await _release.wait()
            except asyncio.CancelledError:
                pass
            while not _release.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    continue
            return []

        storage.claim_due_outbox_items = AsyncMock(side_effect=_resistant_claim)
        storage.count_outbox_by_status = AsyncMock(return_value={})

        pipeline = MagicMock()
        pipeline.deliver_to_target = AsyncMock()

        event_buffer = EventBuffer(maxlen=64)

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            enabled=True,
            interval_seconds=300,
            event_buffer=event_buffer,
            stop_timeout_seconds=2.0,  # long enough to cancel mid-poll
        )

        try:
            await worker.start()
            await wait_until(
                lambda: storage.claim_due_outbox_items.call_count >= 1,
                timeout=2.0,
            )

            # Begin stop() in a task, then cancel it mid-poll.
            stop_task = asyncio.create_task(worker.stop())
            # Let the polling loop start.
            await asyncio.sleep(0.1)
            stop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stop_task

            # Worker must be marked abandoned so the caller can detect
            # the cancelled state and refuse a relaunch.
            assert worker.state.abandoned is True
            assert worker.state.running is True
            event_types = [e.event_type.value for e in event_buffer]
            assert "retry_abandoned" in event_types
            assert "retry_stopped" not in event_types

            # Verify the abandonment event has reason='stop_cancelled'
            # and the timeout payload.
            abandoned_events = [
                e for e in event_buffer if e.event_type.value == "retry_abandoned"
            ]
            assert len(abandoned_events) == 1
            assert abandoned_events[0].detail.get("reason") == "stop_cancelled"
            assert abandoned_events[0].detail.get("stop_timeout_seconds") == 2.0
            # Standard counters must be present.
            for key in ("processed", "succeeded", "failed", "dead_lettered"):
                assert key in abandoned_events[0].detail
        finally:
            _release.set()
            if worker._task is not None and not worker._task.done():
                try:
                    await asyncio.wait_for(worker._task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass

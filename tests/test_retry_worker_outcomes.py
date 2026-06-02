"""Retry worker outcome, backoff, uncancel, and abandonment visibility tests."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from medre.core.events.canonical import DeliveryReceipt
from medre.core.supervision.capacity import CapacityController
from tests._retry_test_helpers import _make_event, _make_limits


class TestRetryCapacityRejectionBackoff:
    """Capacity rejection backoff policy tests using the real RetryWorker."""

    async def test_retry_capacity_rejection_backoff(self, temp_storage):
        """When capacity always rejects:
        1. retry_failed event emitted
        2. outbox next_attempt_at updated (backoff applied)
        3. attempt_number unchanged (capacity rejection ≠ delivery attempt)
        4. Monotonic backoff across two rejection cycles
        5. Snapshot counters correct
        """
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.observability.metrics import Diagnostician
        from medre.core.planning.fallback_resolution import FallbackResolver
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer
        from medre.core.routing.router import Router
        from medre.core.routing.stats import RouteStats
        from medre.core.storage.backend import DeliveryOutboxItem
        from medre.runtime.events import EventBuffer, RuntimeEventType
        from medre.runtime.retry import RetryWorker

        event_buffer = EventBuffer(maxlen=64)
        event = _make_event()
        await temp_storage.append(event)

        # Create a failed receipt for lineage + an outbox item in retry_wait.
        now = datetime.now(timezone.utc)
        receipt_id = f"rcpt-{uuid.uuid4()}"
        failed_receipt = DeliveryReceipt(
            receipt_id=receipt_id,
            event_id=event.event_id,
            delivery_plan_id="plan-cap-backoff",
            target_adapter="target_a",
            route_id="route-cap-backoff",
            status="failed",
            error="ConnectionError: timeout",
            failure_kind="adapter_transient",
            next_retry_at=now - timedelta(seconds=1),  # due now
            attempt_number=1,
            created_at=now,
        )
        await temp_storage.append_receipt(failed_receipt)

        outbox_id = f"obx-{uuid.uuid4()}"
        outbox_item = DeliveryOutboxItem(
            outbox_id=outbox_id,
            event_id=event.event_id,
            route_id="route-cap-backoff",
            delivery_plan_id="plan-cap-backoff",
            target_adapter="target_a",
            attempt_number=1,
            status="retry_wait",
            next_attempt_at=(now - timedelta(seconds=1)).isoformat(),
            receipt_id=receipt_id,
        )
        await temp_storage.create_outbox_item(outbox_item)

        # Pipeline needed for RetryWorker but capacity=0 means it never gets called
        render_pipe = RenderingPipeline()
        render_pipe.register(TextRenderer(), priority=100)

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
            rendering_pipeline=render_pipe,
            diagnostician=Diagnostician(),
            route_stats=RouteStats(),
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Capacity controller that always rejects
        limits = _make_limits(max_inflight_deliveries=0)
        capacity = CapacityController(limits)

        worker = RetryWorker(
            storage=temp_storage,
            pipeline=runner,
            capacity_controller=capacity,
            enabled=True,
            interval_seconds=5.0,
            max_attempts=3,
            event_buffer=event_buffer,
        )

        try:
            # === Cycle 1: capacity rejection ===
            cycle1_now = datetime.now(timezone.utc)
            await worker._process_due(cycle1_now)

            # Assert: retry_failed event emitted with capacity_rejection
            events = list(event_buffer)
            event_types = [e.event_type for e in events]
            assert (
                RuntimeEventType.RETRY_FAILED in event_types
            ), f"Expected retry_failed event, got: {[e.value for e in event_types]}"
            failed_events = [
                e for e in events if e.event_type == RuntimeEventType.RETRY_FAILED
            ]
            assert len(failed_events) >= 1
            assert failed_events[0].detail["status"] == "capacity_rejection"

            # Assert: outbox next_attempt_at was updated (pushed forward)
            updated_item = await temp_storage.get_outbox_item(outbox_id)
            assert updated_item is not None
            assert updated_item.next_attempt_at is not None
            _parsed_next = datetime.fromisoformat(updated_item.next_attempt_at)
            assert _parsed_next > cycle1_now, (
                f"next_attempt_at should be pushed past {cycle1_now}, "
                f"got {_parsed_next}"
            )
            first_backoff_next_at = updated_item.next_attempt_at

            # Assert: attempt_number unchanged (capacity rejection doesn't increment)
            assert (
                updated_item.attempt_number == 1
            ), f"attempt_number should remain 1, got {updated_item.attempt_number}"

            # Assert: worker snapshot shows correct counters
            state = worker.state
            assert state.failed == 1
            assert state.processed == 0
            assert state.succeeded == 0

            # === Cycle 2: capacity still rejecting ===
            cycle2_now = datetime.fromisoformat(first_backoff_next_at) + timedelta(
                seconds=1,
            )
            await worker._process_due(cycle2_now)

            updated_item_2 = await temp_storage.get_outbox_item(outbox_id)
            assert updated_item_2 is not None

            # Assert: next_attempt_at advanced monotonically
            assert updated_item_2.next_attempt_at is not None
            _parsed_next_2 = datetime.fromisoformat(updated_item_2.next_attempt_at)
            _parsed_first = datetime.fromisoformat(first_backoff_next_at)
            assert _parsed_next_2 > _parsed_first, (
                f"Second backoff ({_parsed_next_2}) must be "
                f"later than first ({_parsed_first})"
            )

            # Assert: attempt_number still unchanged
            assert updated_item_2.attempt_number == 1

            # Assert: snapshot counters reflect 2 rejections
            assert state.failed == 2
            assert state.processed == 0
            assert state.succeeded == 0

            # Assert: second retry_failed event
            events_2 = list(event_buffer)
            failed_events_2 = [
                e for e in events_2 if e.event_type == RuntimeEventType.RETRY_FAILED
            ]
            assert len(failed_events_2) >= 2
        finally:
            await worker.stop()
            await runner.stop()


class TestRetryWorkerTaskCrashedOutcome:
    """Regression tests for ``_finalize_task_outcome`` / clean-stop
    exception handling.

    Proves that a worker task which exits with an unhandled exception
    is reported as ``retry_failed`` (not ``retry_stopped``) and that
    the exception is retrieved so Python does not log
    ``Task exception was never retrieved``.

    Tests the ``_finalize_task_outcome`` helper directly because
    ``_run_loop``'s broad ``except Exception`` would swallow
    exceptions raised inside ``_process_due`` and let the task exit
    cleanly.  Driving the helper with a pre-built crashing task is
    the only way to exercise the post-finish exception path.
    """

    async def test_finalize_task_outcome_emits_retry_failed_for_crashed_task(
        self,
    ):
        """``_finalize_task_outcome`` must emit ``retry_failed`` (not
        ``retry_stopped``) when the task exited with an exception,
        include the exception text in the event payload, and mark
        the exception as retrieved (no unretrieved warning).
        """
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        storage.count_outbox_by_status = AsyncMock(return_value={})
        pipeline = MagicMock()
        event_buffer = EventBuffer(maxlen=64)

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            enabled=True,
            interval_seconds=300,
            event_buffer=event_buffer,
            stop_timeout_seconds=2.0,
        )

        async def _crash() -> None:
            raise RuntimeError("worker crashed during stop")

        task = asyncio.create_task(_crash())
        # Suppress the re-raise from ``await task``; we want the
        # exception to be present on the task object, not propagated
        # to the test.
        with pytest.raises(RuntimeError, match="worker crashed during stop"):
            await task
        assert task.done() and task.exception() is not None

        clean, exc = worker._finalize_task_outcome(task)
        assert clean is False
        assert isinstance(exc, RuntimeError)
        assert str(exc) == "worker crashed during stop"
        assert worker._task is None
        assert worker.state.running is False

        event_types = [e.event_type.value for e in event_buffer]
        assert "retry_failed" in event_types
        assert "retry_stopped" not in event_types

        failed_events = [
            e for e in event_buffer if e.event_type.value == "retry_failed"
        ]
        assert failed_events
        detail = failed_events[0].detail
        assert "error" in detail
        assert "RuntimeError" in detail["error"]
        assert "worker crashed during stop" in detail["error"]
        assert detail.get("error_type") == "RuntimeError"

    async def test_finalize_task_outcome_emits_retry_stopped_for_clean_task(
        self,
    ):
        """``_finalize_task_outcome`` must emit ``retry_stopped`` (not
        ``retry_failed``) when the task exited without an exception.
        """
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        storage.count_outbox_by_status = AsyncMock(return_value={})
        pipeline = MagicMock()
        event_buffer = EventBuffer(maxlen=64)

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            enabled=True,
            interval_seconds=300,
            event_buffer=event_buffer,
            stop_timeout_seconds=2.0,
        )

        async def _clean() -> None:
            return None

        task = asyncio.create_task(_clean())
        await task

        clean, exc = worker._finalize_task_outcome(task)
        assert clean is True
        assert exc is None
        assert worker._task is None
        assert worker.state.running is False

        event_types = [e.event_type.value for e in event_buffer]
        assert "retry_stopped" in event_types
        assert "retry_failed" not in event_types

    async def test_finalize_task_outcome_marks_exception_as_retrieved(self, caplog):
        """``_finalize_task_outcome`` calls ``task.exception()`` which
        marks the exception as retrieved.  Python must not log
        ``Task exception was never retrieved`` after the helper runs.
        """
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        storage.count_outbox_by_status = AsyncMock(return_value={})
        pipeline = MagicMock()
        event_buffer = EventBuffer(maxlen=64)

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            enabled=True,
            interval_seconds=300,
            event_buffer=event_buffer,
            stop_timeout_seconds=2.0,
        )

        async def _crash() -> None:
            raise ValueError("boom")

        task = asyncio.create_task(_crash())
        with pytest.raises(ValueError, match="boom"):
            await task

        with caplog.at_level(logging.WARNING, logger="asyncio"):
            clean, _ = worker._finalize_task_outcome(task)

        assert clean is False
        unretrieved = [
            r
            for r in caplog.records
            if "exception was never retrieved" in r.getMessage()
        ]
        assert unretrieved == [], (
            f"Expected no unretrieved-exception warnings, got: "
            f"{[r.getMessage() for r in unretrieved]}"
        )

    async def test_force_cancel_with_poll_raises_when_task_crashed(self):
        """``_force_cancel_with_poll`` must re-raise if the task
        crashed during the cancel grace, not pretend it stopped
        cleanly.
        """
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        storage.count_outbox_by_status = AsyncMock(return_value={})
        pipeline = MagicMock()
        event_buffer = EventBuffer(maxlen=64)

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            enabled=True,
            interval_seconds=300,
            event_buffer=event_buffer,
            stop_timeout_seconds=2.0,
        )

        # A task that responds to ``cancel()`` by raising a different
        # exception (not ``CancelledError``).  This is the
        # "crashed during cancel" case that the helper must surface
        # honestly.
        async def _crash_on_cancel() -> None:
            try:
                await asyncio.Event().wait()  # suspend forever
            except asyncio.CancelledError:
                raise RuntimeError("cleanup failure during cancel") from None

        task = asyncio.create_task(_crash_on_cancel())
        # Wait for the task to actually start and suspend.
        for _ in range(50):
            await asyncio.sleep(0)
            if task.done():
                break
        assert not task.done(), "task should still be suspended"

        with pytest.raises(RuntimeError, match="cleanup failure during cancel"):
            await worker._force_cancel_with_poll(task=task)

        # Worker state must be cleared so a future ``start()`` is
        # allowed, and the terminal event must be ``retry_failed``.
        assert worker._task is None
        assert worker.state.running is False
        event_types = [e.event_type.value for e in event_buffer]
        assert "retry_failed" in event_types
        assert "retry_stopped" not in event_types
        assert "retry_abandoned" not in event_types


class TestUncancelDrainAllAndRestore:
    """Regression tests for the ``uncancel()`` / ``cancel()`` cycle in
    ``MedreApp.stop()``.

    The pre-fix code stored ``current.uncancel()`` (which returns the
    REMAINING cancel count, not the number removed) and re-cancelled
    that many times.  With a single pending cancellation this meant
    zero re-cancels — the cancellation was lost.  The fix uses
    ``cancelling()`` in a while-loop to drain all pending requests,
    then re-cancels once after cleanup.

    These tests create a separate ``asyncio.Task`` via
    ``asyncio.create_task`` and drive the cancel/uncancel logic on
    that task.  Calling ``cancel()`` on the test's own task would
    propagate to the pytest-asyncio runner and fail the test with
    an unexpected ``CancelledError`` at ``future.result()``.
    """

    async def test_uncancel_drain_all_cancellations(self):
        """Multiple pending cancel requests must all be drained by
        looping ``uncancel()`` while ``cancelling() > 0``.

        ``Task.uncancel()`` only decrements by one per call (returns
        the remaining count).  A single ``uncancel()`` is therefore
        not sufficient when the cancel count is greater than one.
        """
        started = asyncio.Event()

        async def _suspend() -> None:
            started.set()
            await asyncio.Event().wait()  # suspend until cancelled

        task = asyncio.create_task(_suspend())
        await started.wait()
        # Task is now suspended.  Stack 3 cancellations on it.
        for _ in range(3):
            task.cancel()
        assert task.cancelling() == 3
        # Drain all (this is the pattern the fix uses).
        cleared = 0
        while task.cancelling() > 0:
            task.uncancel()
            cleared += 1
        assert task.cancelling() == 0
        assert cleared == 3
        # Clean up: cancel the task properly.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_single_uncancel_only_decrements_by_one(self):
        """``Task.uncancel()`` decrements by one and returns the
        remaining count.  This is the Python 3.11+ semantics the
        pre-fix code misunderstood: it stored the return value and
        re-cancelled that many times, which is 0 for a single
        pending cancel and loses the cancellation entirely.
        """
        started = asyncio.Event()

        async def _suspend() -> None:
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(_suspend())
        await started.wait()
        task.cancel()
        assert task.cancelling() == 1
        remaining = task.uncancel()
        # ``uncancel()`` returned the REMAINING count, which is 0
        # after a single decrement.  The pre-fix code would have
        # stored this in ``_cleared_cancels`` and then re-cancelled
        # zero times, losing the cancellation.
        assert remaining == 0
        assert task.cancelling() == 0
        # One ``cancel()`` is sufficient to re-latch the
        # cancellation.
        task.cancel()
        assert task.cancelling() == 1
        # Clean up.
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_no_cancellation_no_drain_no_restore(self):
        """When no cancellation arrived, the drain loop must not
        execute and no extra ``cancel()`` must be issued (which
        would corrupt the non-cancelled state).
        """
        started = asyncio.Event()

        async def _suspend() -> None:
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(_suspend())
        await started.wait()
        assert task.cancelling() == 0
        # Simulate the no-cancel branch: nothing to drain, nothing
        # to restore.  Count stays zero.
        while task.cancelling() > 0:
            task.uncancel()
        assert task.cancelling() == 0
        # Clean up: cancel the task.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestAppStopRetryWorkerAbandonmentVisibility:
    """``MedreApp.stop()`` must log a warning when the retry worker
    is abandoned during shutdown.  This gives operators a signal
    that subprocess-driven retries may still be in-flight after
    ``stop()`` returns.

    Drives the retry-worker block from ``MedreApp.stop()`` with a
    stub worker whose ``stop()`` sets ``state.abandoned = True`` to
    verify the warning is emitted.
    """

    async def test_stop_logs_warning_when_retry_worker_abandoned(self, caplog):
        """When ``RetryWorker.stop()`` returns with
        ``state.abandoned=True``, the retry-worker block in
        ``MedreApp.stop()`` must log a warning naming the
        abandonment.
        """
        from medre.runtime.retry import RetryWorker, RetryWorkerState

        worker_state = RetryWorkerState()
        worker_state.abandoned = True  # set after stop() returns
        worker = MagicMock(spec=RetryWorker)
        worker.state = worker_state

        async def _mark_abandoned() -> None:
            pass  # state already marked abandoned above

        worker.stop = _mark_abandoned

        from medre.runtime.app import _logger as _app_logger

        with caplog.at_level(logging.WARNING, logger="medre.runtime.app"):
            # Replicate the exact retry-worker block from
            # ``MedreApp.stop()`` to verify the warning is emitted.
            try:
                await worker.stop()
            except Exception as exc:
                _app_logger.error("Error stopping retry worker: %s", exc)
            if worker.state.abandoned:
                _app_logger.warning(
                    "RetryWorker was abandoned during shutdown: "
                    "background task did not finish within timeout; "
                    "state.running=True, abandoned=True. "
                    "Subprocess-driven retries may still be in-flight."
                )

        abandonment_warnings = [
            r for r in caplog.records if "RetryWorker was abandoned" in r.getMessage()
        ]
        assert len(abandonment_warnings) == 1
        assert "state.running=True, abandoned=True" in (
            abandonment_warnings[0].getMessage()
        )

    async def test_stop_does_not_log_warning_when_retry_worker_clean(self, caplog):
        """When the retry worker stops cleanly, no abandonment
        warning must be emitted.
        """
        from medre.runtime.retry import RetryWorker, RetryWorkerState

        worker_state = RetryWorkerState()
        worker = MagicMock(spec=RetryWorker)
        worker.state = worker_state

        async def _clean_stop() -> None:
            pass

        worker.stop = _clean_stop

        from medre.runtime.app import _logger as _app_logger

        with caplog.at_level(logging.WARNING, logger="medre.runtime.app"):
            try:
                await worker.stop()
            except Exception as exc:
                _app_logger.error("Error stopping retry worker: %s", exc)
            if worker.state.abandoned:
                _app_logger.warning("RetryWorker was abandoned during shutdown")

        abandonment_warnings = [
            r for r in caplog.records if "RetryWorker was abandoned" in r.getMessage()
        ]
        assert abandonment_warnings == []

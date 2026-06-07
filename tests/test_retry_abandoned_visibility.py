"""RetryWorker abandoned-restart visibility and stop-timeout clarity tests.

Proves that:
1. ``RetryWorker.start()`` emits ``retry_start_refused`` with structured
   detail when ``state.abandoned`` blocks a restart attempt.
2. ``RetryWorker.stop()`` logs the effective two-stage wall-time bound
   so operators understand the timeout semantics.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from medre.runtime.events import EventBuffer
from medre.runtime.retry import RetryWorker
from tests.helpers.async_utils import wait_until

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker(
    *,
    event_buffer: EventBuffer,
    stop_timeout_seconds: float = 5.0,
    enabled: bool = True,
    interval_seconds: float = 300.0,
) -> RetryWorker:
    """Build a RetryWorker with mock storage/pipeline for visibility tests."""
    storage = MagicMock()
    storage.claim_due_outbox_items = AsyncMock(return_value=[])
    storage.count_outbox_by_status = AsyncMock(return_value={})

    pipeline = MagicMock()
    pipeline.deliver_to_target = AsyncMock()

    return RetryWorker(
        storage=storage,
        pipeline=pipeline,
        capacity_controller=None,
        enabled=enabled,
        interval_seconds=interval_seconds,
        event_buffer=event_buffer,
        stop_timeout_seconds=stop_timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Test: start() refused when abandoned — event visibility
# ---------------------------------------------------------------------------


class TestAbandonedRestartRefusedEvent:
    """start() emits retry_start_refused when state.abandoned blocks launch."""

    async def test_emits_retry_start_refused_event(self):
        """When abandoned=True, start() emits retry_start_refused with
        reason='abandoned' and state counters."""
        event_buffer = EventBuffer(maxlen=64)
        worker = _make_worker(
            event_buffer=event_buffer,
            stop_timeout_seconds=3.5,
        )

        # Simulate the abandoned state (normally set by stop()).
        worker.state.abandoned = True
        worker.state.processed = 10
        worker.state.succeeded = 7
        worker.state.failed = 2
        worker.state.dead_lettered = 1

        await worker.start()

        # The event buffer should contain exactly one retry_start_refused.
        refused_events = [
            e for e in event_buffer if e.event_type.value == "retry_start_refused"
        ]
        assert (
            len(refused_events) == 1
        ), f"expected 1 retry_start_refused event, got {len(refused_events)}"

        detail = refused_events[0].detail
        assert detail["reason"] == "abandoned"
        assert detail["stop_timeout_seconds"] == 3.5
        assert detail["processed"] == 10
        assert detail["succeeded"] == 7
        assert detail["failed"] == 2
        assert detail["dead_lettered"] == 1

    async def test_refused_event_not_emitted_when_not_abandoned(self):
        """When abandoned=False, start() does not emit retry_start_refused."""
        event_buffer = EventBuffer(maxlen=64)
        worker = _make_worker(event_buffer=event_buffer)

        assert worker.state.abandoned is False
        await worker.start()

        refused_events = [
            e for e in event_buffer if e.event_type.value == "retry_start_refused"
        ]
        assert len(refused_events) == 0

        # Clean up: stop the worker.
        await worker.stop()

    async def test_start_refused_preserves_existing_behavior(self):
        """When abandoned=True, start() does not launch a task."""
        event_buffer = EventBuffer(maxlen=64)
        worker = _make_worker(event_buffer=event_buffer)
        worker.state.abandoned = True

        await worker.start()

        assert worker._task is None, "start() must not launch a task when abandoned"
        assert worker.state.running is False

    async def test_start_refused_with_zero_counters(self):
        """retry_start_refused event is correct even with zero counters."""
        event_buffer = EventBuffer(maxlen=64)
        worker = _make_worker(
            event_buffer=event_buffer,
            stop_timeout_seconds=1.0,
        )
        worker.state.abandoned = True
        # Counters stay at default zero values.

        await worker.start()

        refused_events = [
            e for e in event_buffer if e.event_type.value == "retry_start_refused"
        ]
        assert len(refused_events) == 1
        detail = refused_events[0].detail
        assert detail["reason"] == "abandoned"
        assert detail["stop_timeout_seconds"] == 1.0
        assert detail["processed"] == 0
        assert detail["succeeded"] == 0
        assert detail["failed"] == 0
        assert detail["dead_lettered"] == 0

    async def test_multiple_start_calls_each_emit_event(self):
        """Each start() call on an abandoned worker emits a new event."""
        event_buffer = EventBuffer(maxlen=64)
        worker = _make_worker(event_buffer=event_buffer)
        worker.state.abandoned = True

        await worker.start()
        await worker.start()
        await worker.start()

        refused_events = [
            e for e in event_buffer if e.event_type.value == "retry_start_refused"
        ]
        assert len(refused_events) == 3


# ---------------------------------------------------------------------------
# Test: stop() logs effective two-stage timeout behavior
# ---------------------------------------------------------------------------


class TestStopTimeoutVisibility:
    """stop() surfaces the effective two-stage wall-time bound."""

    async def test_stop_logs_two_stage_timeout(self, caplog):
        """stop() logs the effective wall-time (~2x stop_timeout_seconds)."""
        event_buffer = EventBuffer(maxlen=64)
        worker = _make_worker(
            event_buffer=event_buffer,
            stop_timeout_seconds=0.2,
            interval_seconds=300,
        )

        await worker.start()
        # Wait for the task to be running.
        await wait_until(
            lambda: worker._task is not None,
            timeout=2.0,
        )

        with caplog.at_level(logging.INFO, logger="medre.runtime.retry"):
            await worker.stop()

        # The log message should mention the two-stage behavior and
        # the effective wall-time bound.
        relevant = [
            r for r in caplog.records if "two-stage bounded shutdown" in r.message
        ]
        assert (
            len(relevant) >= 1
        ), "stop() must log the two-stage bounded shutdown message"
        msg = relevant[0].message
        assert "stop_timeout_seconds=0.2" in msg
        assert "~0.4s" in msg  # 2 × 0.2

    async def test_stop_log_not_emitted_when_no_task(self, caplog):
        """stop() does not log the two-stage message when no task is
        running (early return path)."""
        event_buffer = EventBuffer(maxlen=64)
        worker = _make_worker(
            event_buffer=event_buffer,
            stop_timeout_seconds=0.5,
        )

        # Worker was never started — _task is None.
        assert worker._task is None

        with caplog.at_level(logging.INFO, logger="medre.runtime.retry"):
            await worker.stop()

        relevant = [
            r for r in caplog.records if "two-stage bounded shutdown" in r.message
        ]
        assert (
            len(relevant) == 0
        ), "stop() should not log two-stage message when there is no task"


# ---------------------------------------------------------------------------
# Test: abandoned + stop integration (end-to-end visibility)
# ---------------------------------------------------------------------------


class TestAbandonedStopIntegration:
    """Proves that the abandoned restart refusal works after a real
    stop-that-abandons sequence."""

    async def test_abandoned_after_cancellation_resistant_stop(self):
        """After stop() abandons a cancellation-resistant task, start()
        refuses and emits retry_start_refused."""
        event_buffer = EventBuffer(maxlen=64)

        storage = MagicMock()

        # Task that swallows CancelledError — cancellation-resistant.
        async def _swallow_cancel_forever(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Suppress cancellation — the task survives cancel().
                await asyncio.Event().wait()
            return []

        storage.claim_due_outbox_items = AsyncMock(
            side_effect=_swallow_cancel_forever,
        )
        storage.count_outbox_by_status = AsyncMock(return_value={})

        pipeline = MagicMock()
        pipeline.deliver_to_target = AsyncMock()

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            enabled=True,
            interval_seconds=300,
            event_buffer=event_buffer,
            stop_timeout_seconds=0.1,
        )

        await worker.start()
        await wait_until(
            lambda: storage.claim_due_outbox_items.call_count >= 1,
            timeout=2.0,
        )

        # stop() should abandon the cancellation-resistant task.
        await worker.stop()

        assert worker.state.abandoned is True
        assert worker._task is None

        # Now try to start again — must refuse.
        len(event_buffer)
        await worker.start()

        assert worker._task is None

        refused_events = [
            e for e in event_buffer if e.event_type.value == "retry_start_refused"
        ]
        assert len(refused_events) == 1

        detail = refused_events[0].detail
        assert detail["reason"] == "abandoned"
        assert "stop_timeout_seconds" in detail
        assert "processed" in detail
        assert "succeeded" in detail
        assert "failed" in detail
        assert "dead_lettered" in detail

"""Focused tests for RetryWorker event-buffer wiring.

Verifies that RetryWorker emits ``retry_started`` / ``retry_stopped`` lifecycle
events into the EventBuffer when one is provided, and that MedreApp wires its
own ``_event_buffer`` into the RetryWorker on construction.

No outbox rows are created, mutated, or cancelled by these tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from medre.runtime.events import EventBuffer, RuntimeEventType
from medre.runtime.retry import RetryWorker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_CLOCK_SEQUENCE: list[float] = list(range(1000))


def _fixed_clock() -> float:
    """Deterministic monotonic clock for reproducible event timestamps."""
    return _FIXED_CLOCK_SEQUENCE.pop(0)


def _make_worker(
    *,
    event_buffer: EventBuffer | None = None,
    enabled: bool = True,
    interval_seconds: float = 300.0,
    batch_size: int = 5,
    max_attempts: int = 3,
) -> RetryWorker:
    """Build a RetryWorker with fake storage/pipeline.

    The long default ``interval_seconds`` (300 s) ensures the polling loop
    does not re-enter ``_process_due`` during the test window.
    """
    storage = AsyncMock()
    storage.claim_due_outbox_items = AsyncMock(return_value=[])
    storage.count_outbox_by_status = AsyncMock(return_value={})

    pipeline = AsyncMock()

    return RetryWorker(
        storage=storage,
        pipeline=pipeline,
        capacity_controller=None,
        enabled=enabled,
        interval_seconds=interval_seconds,
        batch_size=batch_size,
        max_attempts=max_attempts,
        event_buffer=event_buffer,
    )


def _event_types(buf: EventBuffer) -> list[RuntimeEventType]:
    """Extract ordered event types from an EventBuffer."""
    return [ev.event_type for ev in buf]


# ---------------------------------------------------------------------------
# Tests — RetryWorker lifecycle events
# ---------------------------------------------------------------------------


class TestRetryWorkerEventBufferWiring:
    """RetryWorker emits lifecycle events when given an EventBuffer."""

    async def test_start_emits_retry_started(self) -> None:
        """start() records a ``retry_started`` event with config detail."""
        buf = EventBuffer(clock=_fixed_clock)
        worker = _make_worker(event_buffer=buf)

        await worker.start()
        try:
            types = _event_types(buf)
            assert RuntimeEventType.RETRY_STARTED in types

            ev = [e for e in buf if e.event_type == RuntimeEventType.RETRY_STARTED][0]
            assert ev.detail["interval"] == 300.0
            assert ev.detail["batch_size"] == 5
            assert ev.detail["max_attempts"] == 3
        finally:
            await worker.stop()

    async def test_stop_emits_retry_stopped(self) -> None:
        """stop() records a ``retry_stopped`` event with counter detail."""
        buf = EventBuffer(clock=_fixed_clock)
        worker = _make_worker(event_buffer=buf)

        await worker.start()
        await worker.stop()

        types = _event_types(buf)
        assert RuntimeEventType.RETRY_STARTED in types
        assert RuntimeEventType.RETRY_STOPPED in types

        ev = [e for e in buf if e.event_type == RuntimeEventType.RETRY_STOPPED][0]
        assert "processed" in ev.detail
        assert "succeeded" in ev.detail
        assert "failed" in ev.detail
        assert "dead_lettered" in ev.detail

    async def test_start_stop_event_ordering(self) -> None:
        """retry_started precedes retry_stopped in the buffer."""
        buf = EventBuffer(clock=_fixed_clock)
        worker = _make_worker(event_buffer=buf)

        await worker.start()
        await worker.stop()

        types = _event_types(buf)
        started_idx = types.index(RuntimeEventType.RETRY_STARTED)
        stopped_idx = types.index(RuntimeEventType.RETRY_STOPPED)
        assert started_idx < stopped_idx

    async def test_no_events_without_buffer(self) -> None:
        """RetryWorker with event_buffer=None does not crash on start/stop."""
        worker = _make_worker(event_buffer=None)

        await worker.start()
        await worker.stop()
        # No assertion needed — the test passes if no exception is raised.

    async def test_disabled_worker_emits_nothing(self) -> None:
        """An ``enabled=False`` worker does not emit retry_started."""
        buf = EventBuffer(clock=_fixed_clock)
        worker = _make_worker(event_buffer=buf, enabled=False)

        await worker.start()
        try:
            assert len(buf) == 0
        finally:
            await worker.stop()

    async def test_stop_without_start_emits_nothing(self) -> None:
        """Calling stop() without start() does not emit retry_stopped."""
        buf = EventBuffer(clock=_fixed_clock)
        worker = _make_worker(event_buffer=buf)

        await worker.stop()
        assert len(buf) == 0


# ---------------------------------------------------------------------------
# Tests — MedreApp wiring verification
# ---------------------------------------------------------------------------


class TestMedreAppRetryEventWiring:
    """Verify MedreApp passes its event_buffer to RetryWorker.

    Uses the real ``RetryWorker`` constructor signature to confirm the
    ``event_buffer`` kwarg is accepted.  A lightweight integration check
    without building the full MedreApp.
    """

    def test_retry_worker_accepts_event_buffer_kwarg(self) -> None:
        """RetryWorker constructor accepts event_buffer parameter."""
        buf = EventBuffer()
        storage = AsyncMock()
        pipeline = AsyncMock()

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            event_buffer=buf,
        )
        assert worker._event_buffer is buf

    def test_retry_worker_default_event_buffer_is_none(self) -> None:
        """RetryWorker defaults event_buffer to None."""
        storage = AsyncMock()
        pipeline = AsyncMock()

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
        )
        assert worker._event_buffer is None

    def test_medre_app_source_wires_event_buffer(self) -> None:
        """Static check: MedreApp.start() passes event_buffer to RetryWorker.

        Reads the source of MedreApp.start() and verifies the RetryWorker
        construction includes ``event_buffer=self._event_buffer``.
        """
        import inspect

        import medre.runtime.app as app_mod

        source = inspect.getsource(app_mod.MedreApp.start)
        # The RetryWorker construction must include event_buffer wiring.
        assert "event_buffer=self._event_buffer" in source, (
            "MedreApp.start() must pass event_buffer=self._event_buffer "
            "to the RetryWorker constructor"
        )

    async def test_full_start_stop_lifecycle_in_buffer(self) -> None:
        """End-to-end: start+stop records both events with correct detail."""
        buf = EventBuffer(clock=_fixed_clock)
        worker = _make_worker(
            event_buffer=buf,
            interval_seconds=42.0,
            batch_size=10,
            max_attempts=7,
        )

        await worker.start()
        await worker.stop()

        events = list(buf)
        assert len(events) == 2

        started = events[0]
        assert started.event_type == RuntimeEventType.RETRY_STARTED
        assert started.detail["interval"] == 42.0
        assert started.detail["batch_size"] == 10
        assert started.detail["max_attempts"] == 7

        stopped = events[1]
        assert stopped.event_type == RuntimeEventType.RETRY_STOPPED
        assert stopped.detail["processed"] == 0
        assert stopped.detail["succeeded"] == 0
        assert stopped.detail["failed"] == 0
        assert stopped.detail["dead_lettered"] == 0

"""Tests for shutdown-safe callback completion in MeshtasticAdapter.

Verifies that terminal and native-ref callbacks complete before stop()
returns, that exceptions in callbacks are observed (not "Task exception
was never retrieved"), and that no untracked tasks linger after stop.

Four scenarios:

1. stop() waits for an in-flight ``_report_queue_terminal`` callback
2. stop() waits for an in-flight ``_record_delayed_outbound_ref`` callback
3. A callback that raises during stop is observed, not silently lost
4. No untracked asyncio tasks remain after a full stop cycle
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.queue import (
    QueueDeliveryResult,
    QueueTerminalResult,
)
from medre.core.contracts.adapter import (
    AdapterContext,
    AdapterDeliveryResult,
)
from tests.helpers.meshtastic import make_meshtastic_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(
    adapter_id: str = "mesh-test",
    record_outbound_terminal=None,
    record_outbound_native_ref=None,
) -> AdapterContext:
    """Build a minimal AdapterContext for testing."""

    async def noop_publish(event: Any) -> None:
        pass

    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=noop_publish,
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
        record_outbound_terminal=record_outbound_terminal,
        record_outbound_native_ref=record_outbound_native_ref,
    )


def _terminal_result(
    event_id: str = "evt-1",
    outcome: str = "exhausted",
    error: str = "test exhausted",
) -> QueueTerminalResult:
    """Build a QueueTerminalResult for testing."""

    return QueueTerminalResult(
        item={
            "event_id": event_id,
            "outbox_id": "ob-1",
            "delivery_plan_id": "dp-1",
            "attempt_number": 1,
            "payload": {"text": "hello"},
            "channel_index": 0,
        },
        outcome=outcome,  # type: ignore[arg-type]
        error=error,
    )


def _delivery_result(
    event_id: str = "evt-1",
    native_message_id: str = "12345",
) -> QueueDeliveryResult:
    """Build a QueueDeliveryResult for testing."""
    return QueueDeliveryResult(
        item={
            "event_id": event_id,
            "payload": {"text": "hello"},
            "channel_index": 0,
        },
        delivery_result=AdapterDeliveryResult(
            native_message_id=native_message_id,
            native_channel_id="0",
        ),
    )


# ===================================================================
# 1. Terminal callback completes before stop returns
# ===================================================================


class TestStopWaitsForTerminalCallback:
    """stop() waits for in-flight _report_queue_terminal to complete."""

    async def test_terminal_callback_completes_before_stop_returns(
        self,
    ) -> None:
        """When stop() cancels the drain task mid-terminal-callback, the
        callback completes before _drain_background_tasks returns."""

        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        callback_started = asyncio.Event()
        allow_callback_finish = asyncio.Event()
        callback_finished = asyncio.Event()

        async def slow_terminal(record):
            callback_started.set()
            await allow_callback_finish.wait()
            callback_finished.set()

        adapter.ctx = _make_ctx(record_outbound_terminal=slow_terminal)
        adapter._started = True

        # Make send_one return a terminal result on the first call, then
        # return None (queue-empty) so the loop sleeps.
        result = _terminal_result()
        send_count = 0

        async def mock_send_one():
            nonlocal send_count
            send_count += 1
            if send_count == 1:
                return result
            return None

        adapter.send_one = mock_send_one

        # Avoid side-effects from _report_cancelled_and_drain.
        adapter._report_cancelled_and_drain = AsyncMock()

        drain_task = asyncio.ensure_future(adapter._process_queue())
        adapter._drain_task = drain_task

        # Wait until the terminal callback is running.
        await asyncio.wait_for(callback_started.wait(), timeout=2.0)

        # Cancel the drain task (simulating stop() cancelling _drain_task).
        drain_task.cancel()

        # Wait for the drain task to finish handling CancelledError.
        try:
            await drain_task
        except asyncio.CancelledError:
            pass

        # The callback should still be running (we haven't released it).
        assert not callback_finished.is_set()

        # The callback task should now be tracked in _background_tasks.
        assert len(adapter._background_tasks) > 0

        # Release the callback and drain background tasks.
        allow_callback_finish.set()
        await adapter._drain_background_tasks(timeout=5.0)

        # The callback has completed.
        assert callback_finished.is_set()
        assert len(adapter._background_tasks) == 0


# ===================================================================
# 2. Native-ref callback completes before stop returns
# ===================================================================


class TestStopWaitsForNativeRefCallback:
    """stop() waits for in-flight _record_delayed_outbound_ref to complete."""

    async def test_native_ref_callback_completes_before_stop_returns(
        self,
    ) -> None:
        """When stop() cancels the drain task mid-native-ref-callback, the
        callback completes before _drain_background_tasks returns."""

        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        callback_started = asyncio.Event()
        allow_callback_finish = asyncio.Event()
        callback_finished = asyncio.Event()

        async def slow_native_ref(record):
            callback_started.set()
            await allow_callback_finish.wait()
            callback_finished.set()

        adapter.ctx = _make_ctx(record_outbound_native_ref=slow_native_ref)
        adapter._started = True

        result = _delivery_result()
        send_count = 0

        async def mock_send_one():
            nonlocal send_count
            send_count += 1
            if send_count == 1:
                return result
            return None

        adapter.send_one = mock_send_one
        adapter._report_cancelled_and_drain = AsyncMock()

        drain_task = asyncio.ensure_future(adapter._process_queue())
        adapter._drain_task = drain_task

        await asyncio.wait_for(callback_started.wait(), timeout=2.0)

        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass

        assert not callback_finished.is_set()
        assert len(adapter._background_tasks) > 0

        allow_callback_finish.set()
        await adapter._drain_background_tasks(timeout=5.0)

        assert callback_finished.is_set()
        assert len(adapter._background_tasks) == 0


# ===================================================================
# 3. Callback exception observed, not "Task exception was never retrieved"
# ===================================================================


class TestCallbackExceptionLogged:
    """Exceptions in callbacks during stop are observed, not untracked."""

    async def test_callback_exception_logged_not_unobserved(self) -> None:
        """When a terminal callback raises during stop, the exception is
        caught by _report_queue_terminal's own try/except (logged) and
        does not produce 'Task exception was never retrieved'."""

        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        callback_started = asyncio.Event()
        allow_callback_finish = asyncio.Event()

        async def failing_terminal(record):
            callback_started.set()
            await allow_callback_finish.wait()
            raise RuntimeError("callback boom")

        adapter.ctx = _make_ctx(record_outbound_terminal=failing_terminal)
        adapter._started = True

        result = _terminal_result()
        send_count = 0

        async def mock_send_one():
            nonlocal send_count
            send_count += 1
            if send_count == 1:
                return result
            return None

        adapter.send_one = mock_send_one
        adapter._report_cancelled_and_drain = AsyncMock()

        drain_task = asyncio.ensure_future(adapter._process_queue())
        adapter._drain_task = drain_task

        await asyncio.wait_for(callback_started.wait(), timeout=2.0)

        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass

        # Release the failing callback.
        allow_callback_finish.set()

        # _drain_background_tasks gathers with return_exceptions=True, so
        # the exception is retrieved (not "never retrieved").
        await adapter._drain_background_tasks(timeout=5.0)

        # All tasks cleaned up.
        assert len(adapter._background_tasks) == 0


# ===================================================================
# 4. No untracked tasks after stop
# ===================================================================


class TestNoUntrackedTasksAfterStop:
    """No lingering asyncio tasks after stop() completes."""

    async def test_no_untracked_tasks_after_stop(self) -> None:
        """After a full start → send_one → cancel → drain cycle, no
        background tasks remain tracked."""

        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        records_seen: list[Any] = []

        async def noop_terminal(record):
            records_seen.append(record)

        adapter.ctx = _make_ctx(record_outbound_terminal=noop_terminal)
        adapter._started = True

        # send_one returns a terminal result on the first call, then
        # sleeps (empty-queue simulation).
        result = _terminal_result()
        results = [result]

        async def mock_send_one():
            if results:
                return results.pop(0)
            await asyncio.sleep(10)  # block until cancelled

        adapter.send_one = mock_send_one
        adapter._report_cancelled_and_drain = AsyncMock()

        drain_task = asyncio.ensure_future(adapter._process_queue())
        adapter._drain_task = drain_task

        # Give the loop a moment to process the result and run the callback.
        await asyncio.sleep(0.2)

        # Cancel and drain (simulating stop).
        drain_task.cancel()
        try:
            await asyncio.wait_for(drain_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

        await adapter._drain_background_tasks(timeout=5.0)

        # No tracked background tasks remain.
        assert len(adapter._background_tasks) == 0

        # The callback should have completed (callback is fast/async).
        assert len(records_seen) == 1

        # Verify no stray adapter-related asyncio tasks remain.
        current_task = asyncio.current_task()
        all_tasks = asyncio.all_tasks()
        stray = [
            t
            for t in all_tasks
            if t is not current_task
            and t.get_coro() is not None
            and "meshtastic" in (t.get_coro().__qualname__ or "")
        ]
        assert len(stray) == 0


# ===================================================================
# 5. Delayed outbound ref callback exception logs error (lines 981-988)
# ===================================================================


class TestDelayedOutboundRefExceptionLogged:
    """When _record_delayed_outbound_ref raises a non-CancelledError
    inside the shielded try/except, the exception is logged (lines
    981-988) and the drain loop continues."""

    async def test_native_ref_exception_logged_in_drain_loop(self) -> None:
        """A native-ref callback that raises is caught by the outer
        except Exception block and logged; the drain loop keeps running."""

        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        async def failing_native_ref(record):
            raise RuntimeError("native-ref boom")

        adapter.ctx = _make_ctx(record_outbound_native_ref=failing_native_ref)
        adapter._started = True

        call_count = 0

        async def mock_send_one():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _delivery_result()
            # Second call returns None so loop sleeps; we cancel shortly.
            await asyncio.sleep(10)

        adapter.send_one = mock_send_one

        drain_task = asyncio.ensure_future(adapter._process_queue())
        adapter._drain_task = drain_task

        # Wait for the first send to complete (including the failed callback).
        await asyncio.sleep(0.3)

        # Drain task should still be running (exception was caught).
        assert not drain_task.done()

        # Clean up.
        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass

        await adapter._drain_background_tasks(timeout=2.0)

    async def test_native_ref_exception_ctx_none_no_crash(self) -> None:
        """When ctx is set to None mid-flight, the exception handler
        at line 982-983 skips logging but does not crash."""

        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        async def failing_native_ref(record):
            raise RuntimeError("native-ref boom with no ctx")

        adapter.ctx = _make_ctx(record_outbound_native_ref=failing_native_ref)
        adapter._started = True

        call_count = 0

        async def mock_send_one():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Remove ctx before returning so the exception handler
                # sees ctx=None.
                adapter.ctx = None
                return _delivery_result()
            await asyncio.sleep(10)

        adapter.send_one = mock_send_one

        drain_task = asyncio.ensure_future(adapter._process_queue())
        adapter._drain_task = drain_task

        await asyncio.sleep(0.3)
        assert not drain_task.done()

        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass


# ===================================================================
# 6. Generic exception in drain loop triggers sleep (line 997)
# ===================================================================


class TestGenericExceptionTriggersSleep:
    """A generic Exception (not CancelledError) from send_one triggers
    the catch-all except block which logs the error and sleeps for 1s
    (line 997)."""

    async def test_generic_exception_sleeps_and_continues(self) -> None:
        """send_one raising RuntimeError → logged + sleep(1.0) → continues."""

        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        adapter.ctx = _make_ctx()
        adapter._started = True

        call_count = 0

        async def mock_send_one():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("generic drain boom")
            return None

        adapter.send_one = mock_send_one

        # Patch asyncio.sleep to detect the 1.0 sleep call and avoid
        # actually waiting.
        original_sleep = asyncio.sleep
        sleep_args: list[float] = []

        async def tracking_sleep(delay):
            sleep_args.append(delay)
            # Short-circuit the 1.0 sleep so the test runs fast.
            if delay == 1.0:
                return
            await original_sleep(delay)

        adapter._report_cancelled_and_drain = AsyncMock()

        # Monkey-patch sleep on the module to intercept the call at line 997.
        import medre.adapters.meshtastic.adapter as adapter_mod

        original_mod_sleep = adapter_mod.asyncio.sleep
        adapter_mod.asyncio.sleep = tracking_sleep

        try:
            drain_task = asyncio.ensure_future(adapter._process_queue())
            adapter._drain_task = drain_task

            # Wait for the error to be caught and the sleep to happen.
            await asyncio.sleep(0.3)

            drain_task.cancel()
            try:
                await drain_task
            except asyncio.CancelledError:
                pass

            # Verify the 1.0 sleep was requested.
            assert 1.0 in sleep_args, (
                f"Expected sleep(1.0) in drain loop error handler; "
                f"got sleeps: {sleep_args}"
            )
            # send_one was called (the first call raised).
            assert call_count >= 1
        finally:
            adapter_mod.asyncio.sleep = original_mod_sleep


# ===================================================================
# 7. _report_queue_terminal callback raises (lines 1029, 1033)
# ===================================================================


class TestReportQueueTerminalCallbackRaises:
    """_report_queue_terminal catches and logs exceptions from the
    record_outbound_terminal callback (lines 1029-1040)."""

    async def test_callback_exception_caught_and_logged(self) -> None:
        """When record_outbound_terminal raises, _report_queue_terminal
        catches it (lines 1032-1040) and does not propagate."""

        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        async def failing_terminal(record):
            raise RuntimeError("terminal callback boom")

        adapter.ctx = _make_ctx(record_outbound_terminal=failing_terminal)

        result = _terminal_result()

        # Should NOT raise despite the callback raising.
        await adapter._report_queue_terminal(result)

    async def test_callback_exception_with_no_ctx(self) -> None:
        """_report_queue_terminal with ctx=None does not crash when the
        result has no callback to call."""

        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        adapter.ctx = None

        result = _terminal_result()

        # Should not raise — callback is None, so the if block is skipped.
        await adapter._report_queue_terminal(result)


# ===================================================================
# 8. _report_cancelled_and_drain paths (lines 1062-1119)
# ===================================================================


class TestReportCancelledAndDrainPaths:
    """Covers the _report_cancelled_and_drain method's various branches:
    - cancelled_item present with callback (line 1062)
    - cancelled-item callback raises (lines 1079-1081)
    - drain_all with callback for remaining items (line 1091)
    - abandoned-item callback raises (lines 1109-1119)
    - no-callback drain_all path (lines 1117-1119)
    """

    async def test_cancelled_item_with_callback_reports_cancelled(self) -> None:
        """When pop_cancelled_item returns an item and callback is set,
        a 'cancelled' QueueTerminalRecord is sent to the callback."""

        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        records: list[Any] = []

        async def capture_terminal(record):
            records.append(record)

        adapter.ctx = _make_ctx(record_outbound_terminal=capture_terminal)

        # Enqueue an item and simulate it being the cancelled in-flight item.
        await adapter._queue.enqueue(
            {
                "event_id": "evt-cancelled-1",
                "outbox_id": "ob-cancelled",
                "delivery_plan_id": "dp-cancel",
                "attempt_number": 2,
                "payload": {"text": "cancel me"},
                "channel_index": 1,
            },
            channel_index=1,
        )
        # Process one to dequeue it, then mark it as cancelled.
        # Instead, directly set the cancelled item on the queue.
        adapter._queue._last_cancelled_item = {
            "event_id": "evt-cancelled-1",
            "outbox_id": "ob-cancelled",
            "delivery_plan_id": "dp-cancel",
            "attempt_number": 2,
            "payload": {"text": "cancel me"},
            "channel_index": 1,
        }

        await adapter._report_cancelled_and_drain()

        assert len(records) >= 1
        cancelled_rec = records[0]
        assert cancelled_rec.outcome == "cancelled"
        assert cancelled_rec.event_id == "evt-cancelled-1"
        assert cancelled_rec.attempt_number == 2
        assert cancelled_rec.native_channel_id == "1"

    async def test_cancelled_item_callback_exception_logged(self) -> None:
        """When the cancelled-item callback raises, the exception is
        caught and logged (lines 1079-1086)."""

        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        async def failing_terminal(record):
            raise RuntimeError("cancelled callback boom")

        adapter.ctx = _make_ctx(record_outbound_terminal=failing_terminal)

        # Set up a cancelled item.
        adapter._queue._last_cancelled_item = {
            "event_id": "evt-fail-cancel",
            "outbox_id": "ob-fail",
            "delivery_plan_id": "dp-fail",
            "attempt_number": 1,
            "channel_index": 0,
        }

        # Should not raise — exception is caught internally.
        await adapter._report_cancelled_and_drain()

    async def test_drain_all_remaining_with_callback(self) -> None:
        """When there's a cancelled item and remaining items in the queue,
        drain_all reports each as 'abandoned' via the callback (line 1091)."""

        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        records: list[Any] = []

        async def capture_terminal(record):
            records.append(record)

        adapter.ctx = _make_ctx(record_outbound_terminal=capture_terminal)

        # Set up a cancelled item so the drain path activates.
        adapter._queue._last_cancelled_item = {
            "event_id": "evt-inflight",
            "outbox_id": "ob-inflight",
            "delivery_plan_id": "dp-1",
            "attempt_number": 1,
            "channel_index": 0,
        }

        # Enqueue remaining items that will be drained as abandoned.
        # enqueue(payload, channel_index, event_id=..., ...)
        await adapter._queue.enqueue(
            {"text": "abandoned 1"},
            channel_index=2,
            event_id="evt-remain-1",
            outbox_id="ob-remain-1",
            delivery_plan_id="dp-remain",
            attempt_number=1,
        )
        await adapter._queue.enqueue(
            {"text": "abandoned 2"},
            channel_index=3,
            event_id="evt-remain-2",
            outbox_id="ob-remain-2",
            delivery_plan_id="dp-remain",
            attempt_number=3,
        )

        await adapter._report_cancelled_and_drain()

        # Should have: 1 cancelled + 2 abandoned = 3 records.
        assert len(records) == 3
        outcomes = [r.outcome for r in records]
        assert outcomes[0] == "cancelled"
        assert outcomes[1] == "abandoned"
        assert outcomes[2] == "abandoned"

        # Verify attempt_number flows through.
        abandoned = [r for r in records if r.outcome == "abandoned"]
        assert abandoned[0].attempt_number == 1
        assert abandoned[1].attempt_number == 3
        assert abandoned[0].native_channel_id == "2"
        assert abandoned[1].native_channel_id == "3"

    async def test_abandoned_callback_exception_logged(self) -> None:
        """When the abandoned-item callback raises for a remaining item,
        the exception is caught and logged (lines 1109-1116)."""

        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        call_count = 0

        async def failing_on_second(record):
            nonlocal call_count
            call_count += 1
            if record.outcome == "abandoned":
                raise RuntimeError("abandoned callback boom")

        adapter.ctx = _make_ctx(record_outbound_terminal=failing_on_second)

        # Set up cancelled + remaining items.
        adapter._queue._last_cancelled_item = {
            "event_id": "evt-inflight",
            "channel_index": 0,
        }
        await adapter._queue.enqueue(
            {"event_id": "evt-remain", "channel_index": 0},
            channel_index=0,
        )

        # Should not raise — abandoned callback exception caught.
        await adapter._report_cancelled_and_drain()
        assert call_count >= 2  # cancelled + abandoned

    async def test_no_callback_drain_all_silent(self) -> None:
        """When callback is None, remaining items are drained silently
        via queue.drain_all() (lines 1117-1119)."""

        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        # No record_outbound_terminal → callback is None.
        adapter.ctx = _make_ctx()

        # Set up cancelled item so drain path activates.
        adapter._queue._last_cancelled_item = {
            "event_id": "evt-inflight",
            "channel_index": 0,
        }

        # Enqueue remaining items.
        await adapter._queue.enqueue(
            {"event_id": "evt-remain", "channel_index": 0},
            channel_index=0,
        )

        await adapter._report_cancelled_and_drain()

        # Queue should be empty after drain_all.
        assert adapter._queue.pending_count == 0

    async def test_no_cancelled_item_no_drain(self) -> None:
        """When pop_cancelled_item returns None, no cancelled record is
        created and drain_all is not called."""

        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        records: list[Any] = []

        async def capture_terminal(record):
            records.append(record)

        adapter.ctx = _make_ctx(record_outbound_terminal=capture_terminal)

        # Enqueue items but don't set cancelled item.
        await adapter._queue.enqueue(
            {"event_id": "evt-queued", "channel_index": 0},
            channel_index=0,
        )

        await adapter._report_cancelled_and_drain()

        # No records produced, items remain in queue.
        assert len(records) == 0
        assert adapter._queue.pending_count == 1

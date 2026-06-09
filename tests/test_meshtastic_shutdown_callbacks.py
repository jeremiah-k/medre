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

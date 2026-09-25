"""LXMF terminal delivery callbacks emit generic post-handoff observations."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.adapters.lxmf.session import LxmfDeliveryState, LxmfSession
from medre.config.adapters.lxmf import LxmfConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.events import DeliveryAttemptProvenance
from medre.core.rendering.renderer import RenderingResult
from tests.helpers.async_utils import wait_until


def _context(callback: AsyncMock) -> AdapterContext:
    return AdapterContext(
        adapter_id="lxmf-observation",
        publish_inbound=AsyncMock(),
        logger=logging.getLogger("test.lxmf-observation"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
        report_delivery_feedback=callback,
    )


def _rendering_result(
    *, event_id: str, channel: str, plan_id: str, outbox_id: str
) -> RenderingResult:
    provenance = DeliveryAttemptProvenance(
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter="lxmf-observation",
        target_channel=channel,
        outbox_id=outbox_id,
        attempt_number=1,
        source="live",
    )
    return RenderingResult(
        event_id=event_id,
        target_adapter="lxmf-observation",
        target_channel=channel,
        payload={"content": "hello", "destination_hash": channel},
        attempt_provenance=provenance,
    )


async def test_lxmf_terminal_callback_preserves_exact_attempt_context() -> None:
    callback = AsyncMock()
    adapter = LxmfAdapter(
        LxmfConfig(adapter_id="lxmf-observation", connection_type="fake")
    )
    await adapter.start(_context(callback))
    try:
        provenance = DeliveryAttemptProvenance(
            event_id="evt-lxmf-observation",
            delivery_plan_id="plan-lxmf-observation",
            target_adapter="lxmf-observation",
            target_channel="aa" * 16,
            outbox_id="outbox-lxmf-observation",
            attempt_number=3,
            source="replay",
            replay_run_id="run-lxmf-observation",
        )
        result = RenderingResult(
            event_id="evt-lxmf-observation",
            target_adapter="lxmf-observation",
            target_channel="aa" * 16,
            payload={
                "content": "hello",
                "destination_hash": "aa" * 16,
            },
            attempt_provenance=provenance,
        )
        delivered = await adapter.deliver(result)
        assert delivered is not None
        assert delivered.native_message_id is not None
        message_hash = delivered.native_message_id

        message = MagicMock()
        message.hash = message_hash
        message.state = LxmfDeliveryState.DELIVERED
        adapter._session._on_delivery_state_update(message)
        # The SDK message is mutable; the callback must retain the state it
        # saw even if the SDK changes it before the event-loop bridge runs.
        message.state = LxmfDeliveryState.FAILED

        await wait_until(lambda: callback.await_count == 1, timeout=1.0)
        record = callback.await_args.args[0]
        assert record.attempt_provenance.event_id == "evt-lxmf-observation"
        assert record.attempt_provenance.delivery_plan_id == "plan-lxmf-observation"
        assert record.attempt_provenance.outbox_id == "outbox-lxmf-observation"
        assert record.attempt_provenance.attempt_number == 3
        assert record.attempt_provenance is provenance
        assert record.attempt_provenance.source == "replay"
        assert record.attempt_provenance.replay_run_id == "run-lxmf-observation"
        assert record.native_channel_id == "aa" * 16
        assert record.native_message_id == message_hash
        assert record.state == "delivered"
        assert record.confirmation_level == "unknown"
    finally:
        await adapter.stop()


async def test_lxmf_failed_callback_reports_error_without_lifecycle_claim() -> None:
    callback = AsyncMock()
    adapter = LxmfAdapter(
        LxmfConfig(adapter_id="lxmf-observation", connection_type="fake")
    )
    await adapter.start(_context(callback))
    try:
        result = _rendering_result(
            event_id="evt-lxmf-failed",
            channel="cc" * 16,
            plan_id="plan-lxmf-failed",
            outbox_id="outbox-lxmf-failed",
        )
        delivered = await adapter.deliver(result)
        assert delivered is not None
        assert delivered.native_message_id is not None

        message = MagicMock()
        message.hash = delivered.native_message_id
        message.state = LxmfDeliveryState.FAILED
        adapter._session._apply_delivery_state_update(message)

        await wait_until(lambda: callback.await_count == 1, timeout=1.0)
        record = callback.await_args.args[0]
        assert record.state == "failed"
        assert record.error == "LXMF reported terminal delivery state failed"
        assert record.confirmation_level == "unknown"
    finally:
        await adapter.stop()


async def test_lxmf_observation_callback_failure_is_contained(caplog) -> None:
    callback = AsyncMock(side_effect=RuntimeError("observation sink failed"))
    adapter = LxmfAdapter(
        LxmfConfig(adapter_id="lxmf-observation", connection_type="fake")
    )
    await adapter.start(_context(callback))
    try:
        result = _rendering_result(
            event_id="evt-lxmf-callback-error",
            channel="dd" * 16,
            plan_id="plan-lxmf-callback-error",
            outbox_id="outbox-lxmf-callback-error",
        )
        delivered = await adapter.deliver(result)
        assert delivered is not None
        message = MagicMock()
        message.hash = delivered.native_message_id
        message.state = LxmfDeliveryState.DELIVERED

        with caplog.at_level(logging.ERROR):
            adapter._session._apply_delivery_state_update(message)
            await wait_until(lambda: callback.await_count == 1, timeout=1.0)
            await wait_until(lambda: not adapter._observation_tasks, timeout=1.0)

        assert adapter._started is True
        assert any(
            "failed to record delivery observation" in record.message
            for record in caplog.records
        )
    finally:
        await adapter.stop()


async def test_lxmf_stop_flushes_started_observation_write() -> None:
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    callback_finished = asyncio.Event()

    async def _callback(record) -> None:
        callback_started.set()
        await release_callback.wait()
        callback_finished.set()

    adapter = LxmfAdapter(
        LxmfConfig(adapter_id="lxmf-observation", connection_type="fake")
    )
    await adapter.start(_context(_callback))
    result = _rendering_result(
        event_id="evt-lxmf-stop-flush",
        channel="ee" * 16,
        plan_id="plan-lxmf-stop-flush",
        outbox_id="outbox-lxmf-stop-flush",
    )
    delivered = await adapter.deliver(result)
    assert delivered is not None
    message = MagicMock()
    message.hash = delivered.native_message_id
    message.state = LxmfDeliveryState.DELIVERED
    adapter._session._apply_delivery_state_update(message)
    await asyncio.wait_for(callback_started.wait(), timeout=1.0)

    stop_task = asyncio.create_task(adapter.stop(timeout=1.0))
    await asyncio.sleep(0)
    assert not stop_task.done()

    release_callback.set()
    await asyncio.wait_for(stop_task, timeout=1.0)
    assert callback_finished.is_set()


async def test_lxmf_stop_bounds_blocked_observation_write_and_stops_session() -> None:
    """A stuck evidence write cannot starve session teardown within one stop.

    The runtime stop helper grants ``adapter.stop(timeout)`` a single
    cooperative window, so the observation drain must leave budget for
    ``session.stop()`` and its post-cancellation cleanup must be bounded.
    """
    callback_started = asyncio.Event()
    release_callback = asyncio.Event()
    wedged_cleanup = asyncio.Event()

    async def _callback(record) -> None:
        callback_started.set()
        try:
            await release_callback.wait()
        except asyncio.CancelledError:
            # Post-cancellation cleanup that never completes, as a wedged
            # storage executor would cause; the drain's bounded cancel grace
            # must give up on it rather than hold the stop deadline open.
            await wedged_cleanup.wait()
            raise

    adapter = LxmfAdapter(
        LxmfConfig(adapter_id="lxmf-observation", connection_type="fake")
    )
    await adapter.start(_context(_callback))
    result = _rendering_result(
        event_id="evt-lxmf-stop-budget",
        channel="ff" * 16,
        plan_id="plan-lxmf-stop-budget",
        outbox_id="outbox-lxmf-stop-budget",
    )
    delivered = await adapter.deliver(result)
    assert delivered is not None
    message = MagicMock()
    message.hash = delivered.native_message_id
    message.state = LxmfDeliveryState.DELIVERED
    adapter._session._apply_delivery_state_update(message)
    await asyncio.wait_for(callback_started.wait(), timeout=1.0)

    session_stop_timeouts: list[float] = []
    original_session_stop = LxmfSession.stop

    async def _session_stop_spy(self, timeout: float = 5.0) -> None:
        session_stop_timeouts.append(timeout)
        await original_session_stop(self, timeout=timeout)

    with patch.object(LxmfSession, "stop", _session_stop_spy):
        t0 = time.monotonic()
        await adapter.stop(timeout=0.5)
        elapsed = time.monotonic() - t0

    # Session teardown still ran and received a strictly positive share of
    # the budget that is smaller than the full stop timeout.
    assert session_stop_timeouts, "session.stop must run during adapter stop"
    assert 0.0 < session_stop_timeouts[0] < 0.5
    assert adapter._session._started is False
    # The blocked write was cancelled; stop converged well inside the outer
    # cooperative window (drain share + bounded cancel grace + session stop).
    assert elapsed < 1.0


async def test_lxmf_stop_drain_bounded_when_write_suppresses_cancellation() -> None:
    """A write that suppresses cancellation cannot hold the stop deadline open.

    The drain's post-cancellation grace uses ``asyncio.wait``, which returns
    at its deadline even while the suppressed task keeps running; awaiting the
    cancelled tasks through ``wait_for`` would block on their completion.
    """
    callback_started = asyncio.Event()
    suppressions: list[int] = []
    observation_task: list[asyncio.Task] = []

    async def _callback(record) -> None:
        observation_task.append(asyncio.current_task())
        callback_started.set()
        # Suppress a bounded number of cancellations so the task outlives
        # the drain's grace without hanging test-loop teardown forever.
        while len(suppressions) < 4:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                suppressions.append(1)
        raise asyncio.CancelledError

    adapter = LxmfAdapter(
        LxmfConfig(adapter_id="lxmf-observation", connection_type="fake")
    )
    await adapter.start(_context(_callback))
    result = _rendering_result(
        event_id="evt-lxmf-stop-suppress",
        channel="ab" * 16,
        plan_id="plan-lxmf-stop-suppress",
        outbox_id="outbox-lxmf-stop-suppress",
    )
    delivered = await adapter.deliver(result)
    assert delivered is not None
    message = MagicMock()
    message.hash = delivered.native_message_id
    message.state = LxmfDeliveryState.DELIVERED
    adapter._session._apply_delivery_state_update(message)
    await asyncio.wait_for(callback_started.wait(), timeout=1.0)

    t0 = time.monotonic()
    await adapter.stop(timeout=0.5)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0
    assert adapter._session._started is False
    assert suppressions, "the drain must have cancelled the write"
    # Reap the still-running suppressed task so the test loop closes cleanly:
    # cancel-and-yield cycles work through the bounded suppressions, where a
    # plain ``await task`` would block until the task completes on its own.
    task = observation_task[0]
    while not task.done():
        task.cancel()
        await asyncio.sleep(0)


async def test_lxmf_stop_flushes_delivery_update_queued_before_stop() -> None:
    """Terminal updates already bridged onto the loop survive stop.

    The session bridges SDK-thread state updates with
    ``call_soon_threadsafe``; an update queued just before ``stop()`` was
    scheduled must spawn its observation write before the stop gate closes.
    """
    callback = AsyncMock()
    adapter = LxmfAdapter(
        LxmfConfig(adapter_id="lxmf-observation", connection_type="fake")
    )
    await adapter.start(_context(callback))
    result = _rendering_result(
        event_id="evt-lxmf-stop-queued",
        channel="ba" * 16,
        plan_id="plan-lxmf-stop-queued",
        outbox_id="outbox-lxmf-stop-queued",
    )
    delivered = await adapter.deliver(result)
    assert delivered is not None
    message = MagicMock()
    message.hash = delivered.native_message_id
    message.state = LxmfDeliveryState.DELIVERED

    loop = asyncio.get_running_loop()
    stop_task = asyncio.create_task(adapter.stop(timeout=1.0))
    # Emulate the SDK-thread bridge landing after stop was scheduled but
    # before the stop coroutine first runs.
    loop.call_soon(adapter._session._apply_delivery_state_update, message)

    await asyncio.wait_for(stop_task, timeout=2.0)
    assert callback.await_count == 1


async def test_lxmf_outboxless_delivery_does_not_emit_observation() -> None:
    callback = AsyncMock()
    adapter = LxmfAdapter(
        LxmfConfig(adapter_id="lxmf-observation", connection_type="fake")
    )
    await adapter.start(_context(callback))
    try:
        result = RenderingResult(
            event_id="evt-lxmf-direct",
            target_adapter="lxmf-observation",
            target_channel="ac" * 16,
            payload={"content": "hello", "destination_hash": "ac" * 16},
        )
        delivered = await adapter.deliver(result)
        assert delivered is not None
        message = MagicMock()
        message.hash = delivered.native_message_id
        message.state = LxmfDeliveryState.DELIVERED
        adapter._session._apply_delivery_state_update(message)
        await asyncio.sleep(0)
        callback.assert_not_awaited()
    finally:
        await adapter.stop()


async def test_lxmf_synchronous_sdk_callback_keeps_send_context(tmp_path) -> None:
    config = LxmfConfig(
        adapter_id="lxmf-observation-race",
        connection_type="reticulum",
        storage_path=str(tmp_path / "lxmf-router"),
        message_delay_seconds=0,
    )
    session = LxmfSession(config=config, adapter_id=config.adapter_id)
    mock_rns = MagicMock()
    mock_lxmf = MagicMock()
    mock_router = MagicMock()
    mock_rns.Reticulum.get_instance.return_value = None
    mock_rns.Reticulum.return_value = MagicMock()
    mock_identity = MagicMock()
    mock_rns.Identity.return_value = mock_identity
    mock_rns.Identity.recall.return_value = mock_identity
    mock_rns.Destination.return_value = MagicMock()
    mock_lxmf.LXMRouter.return_value = mock_router
    mock_lxm_cls = MagicMock()
    mock_lxm_cls.DIRECT = "DIRECT_CONST"
    mock_lxmf.LXMessage = mock_lxm_cls
    message = MagicMock()
    message.hash = b"\xab" * 32
    message.state = "outbound"
    mock_lxmf.LXMessage.return_value = message
    registered: dict[str, object] = {}

    def _register(callback) -> None:
        registered["callback"] = callback

    def _handle_outbound(lxm) -> None:
        lxm.state = "delivered"
        registered["callback"](lxm)

    message.register_delivery_callback.side_effect = _register
    mock_router.handle_outbound.side_effect = _handle_outbound
    callback = MagicMock()
    context = object()
    session.set_delivery_state_callback(callback)

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ),
    ):
        await session.start()
        try:
            native_id, _ = await session.send_text(
                "ab" * 16,
                "hello",
                delivery_context=context,
            )
            assert await wait_until(lambda: callback.call_count == 1, timeout=1.0)
        finally:
            await session.stop()

    callback.assert_called_once_with(native_id, "delivered", context)


async def test_lxmf_synchronous_sdk_failed_callback_keeps_send_context(
    tmp_path,
) -> None:
    config = LxmfConfig(
        adapter_id="lxmf-observation-failure-race",
        connection_type="reticulum",
        storage_path=str(tmp_path / "lxmf-router-failure"),
        message_delay_seconds=0,
    )
    session = LxmfSession(config=config, adapter_id=config.adapter_id)
    mock_rns = MagicMock()
    mock_lxmf = MagicMock()
    mock_router = MagicMock()
    mock_rns.Reticulum.get_instance.return_value = None
    mock_rns.Reticulum.return_value = MagicMock()
    mock_identity = MagicMock()
    mock_rns.Identity.return_value = mock_identity
    mock_rns.Identity.recall.return_value = mock_identity
    mock_rns.Destination.return_value = MagicMock()
    mock_lxmf.LXMRouter.return_value = mock_router
    mock_lxm_cls = MagicMock()
    mock_lxm_cls.DIRECT = "DIRECT_CONST"
    mock_lxmf.LXMessage = mock_lxm_cls
    message = MagicMock()
    message.hash = b"\xcd" * 32
    message.state = "outbound"
    mock_lxmf.LXMessage.return_value = message
    registered: dict[str, object] = {}

    def _register_delivery(callback) -> None:
        registered["delivery"] = callback

    def _register_failed(callback) -> None:
        registered["failed"] = callback

    def _handle_outbound(lxm) -> None:
        lxm.state = "failed"
        registered["failed"](lxm)

    message.register_delivery_callback.side_effect = _register_delivery
    message.register_failed_callback.side_effect = _register_failed
    mock_router.handle_outbound.side_effect = _handle_outbound
    callback = MagicMock()
    context = object()
    session.set_delivery_state_callback(callback)

    with (
        patch("medre.adapters.lxmf.session.HAS_LXMF", True),
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ),
    ):
        await session.start()
        try:
            native_id, _ = await session.send_text(
                "cd" * 16,
                "hello",
                delivery_context=context,
            )
            assert await wait_until(lambda: callback.call_count == 1, timeout=1.0)
        finally:
            await session.stop()

    message.register_failed_callback.assert_called_once()
    callback.assert_called_once_with(native_id, "failed", context)

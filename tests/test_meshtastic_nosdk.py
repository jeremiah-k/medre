"""No-hardware Meshtastic adapter tests.

These tests exercise the MEDRE ``MeshtasticAdapter`` in ``connection_type="fake"``
mode, which bypasses the ``meshtastic`` (mtjk) SDK entirely.  They validate that
the adapter, session, codec, and queue modules are usable in isolation without
real hardware.

These tests always run (no env vars required, no radio hardware needed).

**Test classes:**

- ``TestMeshtasticNoSdkLifecycle`` — start/stop, deliver, simulate_inbound,
  import safety, diagnostics shape, idempotent stop.
- ``TestMeshtasticDiagnostics`` — focused diagnostics contract tests: shape,
  no secret leakage, connection type, queue fields, session keys.
- ``TestMeshtasticDrainLifecycle`` — drain task creation, idempotent start,
  stop cancellation, diagnostics ``drain_task_running`` field.
- ``TestMeshtasticDeliverLifecycle`` — deliver() at different lifecycle stages
  in fake mode (before start, after stop, multiple stops).
- ``TestMeshtasticQueueMetrics`` — queue counter accuracy, diagnostics key
  presence, metric stability across lifecycle.
- ``TestMeshtasticFailureClassification`` — scaffold tests verifying failure
  classification fields exist in session diagnostics.
"""

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_BOUND_TIMEOUT_SECONDS = 15.0


async def _bounded(coro, timeout: float = _BOUND_TIMEOUT_SECONDS):
    """Run *coro* with an ``asyncio.wait_for`` timeout guard."""
    return await asyncio.wait_for(coro, timeout=timeout)


def _make_context():
    """Build an AdapterContext suitable for no-hardware tests."""
    from medre.core.contracts.adapter import AdapterContext

    return AdapterContext(
        adapter_id="meshtastic-nosdk-test",
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger("test.meshtastic-nosdk"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _make_fake_config(adapter_id: str = "meshtastic-fake-test"):
    """Build a ``MeshtasticConfig`` for fake (no-hardware) mode."""
    from medre.config.adapters.meshtastic import MeshtasticConfig

    return MeshtasticConfig(
        adapter_id=adapter_id,
        connection_type="fake",
    )


def _make_rendering_result(
    text: str = "test message",
    event_id: str = "evt-test-001",
    channel_index: int = 0,
):
    """Build a minimal ``RenderingResult`` for deliver() tests."""
    from medre.core.rendering.renderer import RenderingResult

    return RenderingResult(
        event_id=event_id,
        target_adapter="meshtastic-fake-test",
        target_channel=None,
        payload={"text": text, "channel_index": channel_index},
    )


def _make_text_packet(
    text: str = "hello from mesh",
    from_id: str = "!deadbeef",
    channel: int = 0,
    packet_id: int = 42,
):
    """Build a minimal Meshtastic text packet dict for simulate_inbound."""
    return {
        "id": packet_id,
        "from": from_id,
        "to": 0xFFFFFFFF,
        "channel": channel,
        "decoded": {
            "portnum": "TEXT_MESSAGE_APP",
            "payload": text.encode("utf-8"),
            "text": text,
        },
    }


# ---------------------------------------------------------------------------
# TestMeshtasticNoSdkLifecycle — exercises adapter WITHOUT mtjk installed
# ---------------------------------------------------------------------------


class TestMeshtasticNoSdkLifecycle:
    """Lifecycle tests that run entirely without the ``mtjk`` SDK.

    All tests use ``connection_type="fake"`` which bypasses the
    ``meshtastic`` import entirely.  These validate that the adapter,
    session, codec, and queue modules are usable in isolation.

    These tests always run (no env var required) because fake mode
    never touches real hardware or the mtjk package.
    """

    async def test_fake_start_stop_lifecycle(self):
        """Fake adapter start/stop completes without mtjk."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        try:
            await _bounded(adapter.start(ctx))
            assert adapter.diagnostics()["started"] is True
            info = await _bounded(adapter.health_check())
            assert info.health == "healthy"
        finally:
            await _bounded(adapter.stop())

    async def test_fake_mode_no_mtjk_import(self):
        """Fake mode works even if ``HAS_MESHTASTIC`` is ``False``."""

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        with patch("medre.adapters.meshtastic.session.HAS_MESHTASTIC", False):
            try:
                await _bounded(adapter.start(ctx))
                assert adapter.diagnostics()["started"] is True
            finally:
                await _bounded(adapter.stop())

    async def test_fake_simulate_inbound_no_mtjk(self):
        """simulate_inbound works without mtjk in fake mode."""

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        with patch("medre.adapters.meshtastic.session.HAS_MESHTASTIC", False):
            try:
                await _bounded(adapter.start(ctx))
                packet = _make_text_packet(text="fake inbound test")
                # simulate_inbound publishes via the publish_inbound mock
                await _bounded(adapter.simulate_inbound(packet))
                # Verify the mock was called with a CanonicalEvent
                ctx.publish_inbound.assert_called_once()  # type: ignore[attr-defined]
                canonical = ctx.publish_inbound.call_args[0][0]  # type: ignore[attr-defined]
                assert canonical.payload.get("body") == "fake inbound test"
            finally:
                await _bounded(adapter.stop())

    async def test_fake_deliver_enqueues_no_mtjk(self):
        """deliver() enqueues and returns AdapterDeliveryResult without mtjk."""

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        with patch("medre.adapters.meshtastic.session.HAS_MESHTASTIC", False):
            try:
                await _bounded(adapter.start(ctx))
                result_obj = _make_rendering_result(text="fake deliver test")
                delivery = await _bounded(adapter.deliver(result_obj))
                # Fake mode returns AdapterDeliveryResult (not None)
                assert delivery is not None
                assert delivery.delivery_note == "locally enqueued"
                assert delivery.native_channel_id == "0"
                # native_message_id is None for queued delivery
                assert delivery.native_message_id is None
                # Verify the queue accepted the payload
                assert adapter.queue.pending_count > 0
            finally:
                await _bounded(adapter.stop())

    async def test_concrete_module_import_without_mtjk(self):
        """``from medre.adapters.meshtastic.adapter import MeshtasticAdapter``
        succeeds regardless of whether mtjk is installed.

        The adapter module uses lazy/guarded imports for ``meshtastic``,
        so the import itself must never fail due to a missing SDK.
        """
        # If this test is running, the import already succeeded at module
        # level.  Re-import to prove idempotency.
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        assert MeshtasticAdapter is not None

    async def test_diagnostics_shape_when_not_started(self):
        """diagnostics() returns valid shape before start()."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)

        diag = adapter.diagnostics()
        assert diag["adapter_id"] == "meshtastic-fake-test"
        assert diag["platform"] == "meshtastic"
        assert diag["started"] is False
        assert diag["connection_type"] == "fake"
        assert "queue_pending" in diag
        assert "queue_total_sent" in diag
        assert "queue_total_failed" in diag
        assert "queue_total_rejected" in diag
        assert "background_tasks" in diag
        # No session before start
        assert "session" not in diag

    async def test_non_fake_raises_without_mtjk(self):
        """Non-fake connection types raise MeshtasticConnectionError
        when ``mtjk`` is not available.

        Uses ``unittest.mock.patch`` to simulate a missing SDK.
        """

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.adapters.meshtastic.errors import MeshtasticConnectionError

        from medre.config.adapters.meshtastic import MeshtasticConfig

        tcp_config = MeshtasticConfig(
            adapter_id="meshtastic-tcp-nosdk",
            connection_type="tcp",
            host="localhost",
        )
        adapter = MeshtasticAdapter(tcp_config)
        ctx = _make_context()

        with patch("medre.adapters.meshtastic.session.HAS_MESHTASTIC", False):
            with pytest.raises(MeshtasticConnectionError, match="mtjk not installed"):
                await _bounded(adapter.start(ctx))

    async def test_stop_idempotency_without_sdk(self):
        """Calling stop() multiple times is safe without mtjk."""

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        with patch("medre.adapters.meshtastic.session.HAS_MESHTASTIC", False):
            await _bounded(adapter.start(ctx))
            # First stop
            await _bounded(adapter.stop())
            # Second stop (idempotent, should not raise)
            await _bounded(adapter.stop())
            # Third stop (still safe)
            await _bounded(adapter.stop())
            assert adapter.diagnostics()["started"] is False


# ---------------------------------------------------------------------------
# TestMeshtasticDiagnostics — diagnostics shape tests (no SDK required)
# ---------------------------------------------------------------------------


class TestMeshtasticDiagnostics:
    """Diagnostics-specific tests that run without real hardware.

    All tests use fake mode and validate the diagnostic contract:
    correct shape, no secret leakage, and expected keys.
    """

    async def test_diagnostics_shape_without_sdk(self):
        """diagnostics() returns expected shape even without start()."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config("diag-nosdk")
        adapter = MeshtasticAdapter(config)

        diag = adapter.diagnostics()

        # Required adapter-level keys
        assert diag["adapter_id"] == "diag-nosdk"
        assert diag["platform"] == "meshtastic"
        assert diag["started"] is False
        assert diag["connection_type"] == "fake"
        assert isinstance(diag["queue_pending"], int)
        assert isinstance(diag["queue_total_sent"], int)
        assert isinstance(diag["queue_total_failed"], int)
        assert isinstance(diag["queue_total_rejected"], int)
        assert isinstance(diag["background_tasks"], int)

        # No session before start
        assert "session" not in diag

    async def test_diagnostics_no_secrets_after_start(self):
        """diagnostics() does NOT expose serial paths, host IPs, or secrets
        after start() in fake mode."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config("diag-no-leak")
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        try:
            await _bounded(adapter.start(ctx))
            diag = adapter.diagnostics()
            diag_str = str(diag)

            # Fake mode should not leak any connection parameters
            for sensitive in ("password", "secret", "token", "api_key",
                              "private_key", "auth_token"):
                assert sensitive not in diag_str, (
                    f"Sensitive word {sensitive!r} found in diagnostics"
                )

            # Recursively scan diagnostics for sensitive key substrings
            # at any nesting depth.
            sensitive_keys = {"password", "secret", "token", "api_key",
                              "private_key", "auth_token", "host",
                              "serial_port"}
            def _check(obj, path=""):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        fp = f"{path}.{k}" if path else k
                        assert k not in sensitive_keys, (
                            f"Sensitive key {k!r} found at {fp}"
                        )
                        _check(v, fp)
                elif isinstance(obj, list):
                    for i, item in enumerate(obj):
                        _check(item, f"{path}[{i}]")
            _check(diag)
        finally:
            await _bounded(adapter.stop())

    async def test_diagnostics_contains_connection_type_and_queue(self):
        """diagnostics() contains connection_type, started, and queue info."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config("diag-fields")
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        try:
            await _bounded(adapter.start(ctx))
            diag = adapter.diagnostics()

            # connection_type is present
            assert diag["connection_type"] == "fake"

            # started flag
            assert diag["started"] is True

            # Queue info fields
            assert "queue_pending" in diag
            assert "queue_total_sent" in diag
            assert "queue_total_failed" in diag
            assert "queue_total_rejected" in diag
            assert diag["queue_pending"] >= 0
            assert diag["queue_total_sent"] >= 0
            assert diag["queue_total_failed"] >= 0
            assert diag["queue_total_rejected"] >= 0
        finally:
            await _bounded(adapter.stop())

    async def test_diagnostics_session_keys_present(self):
        """After start(), diagnostics includes session keys:
        connected, reconnecting, etc."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config("diag-session")
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        try:
            await _bounded(adapter.start(ctx))
            diag = adapter.diagnostics()

            assert "session" in diag
            session = diag["session"]

            # All documented session keys must be present
            expected_keys = {
                "connected",
                "reconnecting",
                "reconnect_attempts",
                "last_packet_time",
                "node_id",
                "channel_count",
                "transient_delivery_failures",
                "permanent_delivery_failures",
                "last_error",
            }
            actual_keys = set(session.keys())
            assert expected_keys == actual_keys, (
                f"Session keys mismatch. "
                f"Missing: {expected_keys - actual_keys}, "
                f"Extra: {actual_keys - expected_keys}"
            )

            # In fake mode, session.connected is False because client is None.
            # session.connected = (client is not None and started) → False.
            assert session["connected"] is False
            assert session["reconnecting"] is False
            assert session["reconnect_attempts"] == 0
            assert session["transient_delivery_failures"] == 0
            assert session["permanent_delivery_failures"] == 0
        finally:
            await _bounded(adapter.stop())


# ---------------------------------------------------------------------------
# TestMeshtasticDrainLifecycle — drain task lifecycle tests (no SDK required)
# ---------------------------------------------------------------------------


class TestMeshtasticDrainLifecycle:
    """Drain task lifecycle tests in fake mode.

    Validates that ``start()`` creates a drain task, ``stop()`` cancels
    it, repeated starts don't duplicate tasks, and diagnostics reports
    the correct ``drain_task_running`` state.
    """

    async def test_start_creates_drain_task(self):
        """After start(), drain task is running and diagnostics confirms it."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        try:
            await _bounded(adapter.start(ctx))
            assert adapter._drain_task is not None
            assert not adapter._drain_task.done()
            diag = adapter.diagnostics()
            assert diag["drain_task_running"] is True
        finally:
            await _bounded(adapter.stop())

    async def test_repeated_start_no_duplicate_drain(self):
        """Calling start() twice does not create a second drain task."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        try:
            await _bounded(adapter.start(ctx))
            first_task = adapter._drain_task
            assert first_task is not None

            # Second start is a no-op (idempotent)
            await _bounded(adapter.start(ctx))
            assert adapter._drain_task is first_task
        finally:
            await _bounded(adapter.stop())

    async def test_stop_cancels_drain_task(self):
        """After stop(), drain task is cancelled and diagnostics reports False."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        await _bounded(adapter.start(ctx))
        assert adapter._drain_task is not None

        await _bounded(adapter.stop())
        assert adapter._drain_task is None
        assert adapter.diagnostics()["started"] is False
        diag = adapter.diagnostics()
        assert diag["drain_task_running"] is False

    async def test_stop_idempotent_drain(self):
        """Calling stop() multiple times is safe; drain_task stays None/done."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        await _bounded(adapter.start(ctx))
        await _bounded(adapter.stop())
        assert adapter._drain_task is None

        # Second stop
        await _bounded(adapter.stop())
        assert adapter._drain_task is None

        # Third stop
        await _bounded(adapter.stop())
        assert adapter._drain_task is None
        assert adapter.diagnostics()["started"] is False

    async def test_diagnostics_includes_drain_task_running(self):
        """diagnostics() dict contains drain_task_running as a bool."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)

        # Before start
        diag = adapter.diagnostics()
        assert "drain_task_running" in diag
        assert isinstance(diag["drain_task_running"], bool)
        assert diag["drain_task_running"] is False

        ctx = _make_context()
        try:
            await _bounded(adapter.start(ctx))
            diag = adapter.diagnostics()
            assert "drain_task_running" in diag
            assert isinstance(diag["drain_task_running"], bool)
            assert diag["drain_task_running"] is True
        finally:
            await _bounded(adapter.stop())


# ---------------------------------------------------------------------------
# TestMeshtasticDeliverLifecycle — deliver() lifecycle stage tests
# ---------------------------------------------------------------------------


class TestMeshtasticDeliverLifecycle:
    """Tests for deliver() behaviour at different lifecycle stages in fake mode.

    In fake mode, deliver() does not require start() — the queue is always
    available.  These tests document and verify that fake-mode deliver()
    works before start, after stop, and across multiple stop cycles.
    """

    async def test_deliver_before_start_works_in_fake_mode(self):
        """In fake mode, deliver() works before start().

        The adapter's deliver() guards against non-started state only for
        non-fake connection types.  Fake mode bypasses this check because
        the queue is created in __init__ and does not depend on start().
        """
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)

        result_obj = _make_rendering_result(text="pre-start deliver")
        delivery = await _bounded(adapter.deliver(result_obj))
        assert delivery is not None
        assert delivery.delivery_note == "locally enqueued"

    async def test_deliver_after_stop_still_works_fake(self):
        """In fake mode, deliver() after stop() still enqueues.

        The queue is an __init__-created object independent of the session
        lifecycle, so stopping the adapter does not destroy the queue.
        """
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        try:
            await _bounded(adapter.start(ctx))
        finally:
            await _bounded(adapter.stop())

        # Queue still exists after stop
        result_obj = _make_rendering_result(text="post-stop deliver")
        delivery = await _bounded(adapter.deliver(result_obj))
        assert delivery is not None
        assert delivery.delivery_note == "locally enqueued"

    async def test_deliver_increments_queue_pending(self):
        """After deliver(), queue_pending reflects the enqueued items."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)

        # Before any deliver
        assert adapter.diagnostics()["queue_pending"] == 0

        result1 = _make_rendering_result(text="msg-1", event_id="e1")
        await _bounded(adapter.deliver(result1))
        assert adapter.diagnostics()["queue_pending"] == 1

        result2 = _make_rendering_result(text="msg-2", event_id="e2")
        await _bounded(adapter.deliver(result2))
        assert adapter.diagnostics()["queue_pending"] == 2

    async def test_deliver_after_multiple_stops(self):
        """Multiple stop() calls don't break deliver() in fake mode."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        await _bounded(adapter.start(ctx))
        await _bounded(adapter.stop())
        await _bounded(adapter.stop())
        await _bounded(adapter.stop())

        result_obj = _make_rendering_result(text="after multi-stop")
        delivery = await _bounded(adapter.deliver(result_obj))
        assert delivery is not None
        assert delivery.delivery_note == "locally enqueued"


# ---------------------------------------------------------------------------
# TestMeshtasticQueueMetrics — queue counter accuracy tests
# ---------------------------------------------------------------------------


class TestMeshtasticQueueMetrics:
    """Tests for queue metric accuracy and diagnostics key presence.

    Validates that all queue counters start at zero, reflect deliver()
    operations, and survive the adapter lifecycle (start/stop).
    """

    async def test_initial_queue_metrics_zero(self):
        """After construction but before any operations, all counters are 0."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)

        diag = adapter.diagnostics()
        assert diag["queue_pending"] == 0
        assert diag["queue_total_sent"] == 0
        assert diag["queue_total_failed"] == 0
        assert diag["queue_total_rejected"] == 0

    async def test_queue_metrics_after_deliver(self):
        """After multiple deliver() calls, queue_pending reflects count."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)

        for i in range(5):
            result_obj = _make_rendering_result(
                text=f"msg-{i}", event_id=f"e-{i}",
            )
            await _bounded(adapter.deliver(result_obj))

        diag = adapter.diagnostics()
        assert diag["queue_pending"] == 5

    async def test_diagnostics_queue_keys_present(self):
        """diagnostics() always has the four queue metric keys."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)

        diag = adapter.diagnostics()
        required_keys = (
            "queue_pending",
            "queue_total_sent",
            "queue_total_failed",
            "queue_total_rejected",
        )
        for key in required_keys:
            assert key in diag, f"Missing key {key!r} in diagnostics"
            assert isinstance(diag[key], int), (
                f"Key {key!r} should be int, got {type(diag[key]).__name__}"
            )

    async def test_drain_task_running_false_before_start(self):
        """Before start(), diagnostics shows drain_task_running=False."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)

        diag = adapter.diagnostics()
        assert "drain_task_running" in diag
        assert diag["drain_task_running"] is False

    async def test_queue_metrics_after_stop(self):
        """After stop(), diagnostics still has valid queue metrics.

        Enqueued items persist in the queue across the stop boundary
        because the queue is an __init__-created object.
        """
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        # Enqueue before start
        result_obj = _make_rendering_result(text="persistent msg")
        await _bounded(adapter.deliver(result_obj))
        assert adapter.diagnostics()["queue_pending"] == 1

        try:
            await _bounded(adapter.start(ctx))
        finally:
            await _bounded(adapter.stop())

        # Queue metrics still present and accurate after stop
        diag = adapter.diagnostics()
        assert diag["queue_pending"] == 1
        assert diag["queue_total_sent"] >= 0
        assert diag["queue_total_failed"] >= 0
        assert diag["queue_total_rejected"] >= 0


# ---------------------------------------------------------------------------
# TestMeshtasticFailureClassification — failure classification scaffold tests
# ---------------------------------------------------------------------------


class TestMeshtasticFailureClassification:
    """Scaffold tests for failure classification field presence.

    Full failure-classification testing requires a real SDK client or
    carefully mocked session.send() to exercise the transient/permanent
    error paths.  These tests verify that the diagnostic fields exist
    and start at zero, confirming the plumbing is in place.
    """

    async def test_transient_failure_field_exists_and_starts_zero(self):
        """Session diagnostics contains transient_delivery_failures starting at 0.

        The ``transient_delivery_failures`` counter is tracked by
        :class:`~medre.adapters.meshtastic.session.MeshtasticSession`.
        Incrementing it requires ``session.send()`` to be exercised with
        a transient error (e.g. timeout, connection reset).  This scaffold
        test only confirms the field exists and initializes correctly.
        """
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        try:
            await _bounded(adapter.start(ctx))
            diag = adapter.diagnostics()
            assert "session" in diag
            session = diag["session"]
            assert "transient_delivery_failures" in session
            assert session["transient_delivery_failures"] == 0
        finally:
            await _bounded(adapter.stop())

    async def test_permanent_failure_field_exists_and_starts_zero(self):
        """Session diagnostics contains permanent_delivery_failures starting at 0.

        The ``permanent_delivery_failures`` counter is tracked by
        :class:`~medre.adapters.meshtastic.session.MeshtasticSession`.
        Incrementing it requires ``session.send()`` to be exercised with
        a permanent error (e.g. invalid payload, encoding failure).  This
        scaffold test only confirms the field exists and initializes correctly.
        """
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        try:
            await _bounded(adapter.start(ctx))
            diag = adapter.diagnostics()
            assert "session" in diag
            session = diag["session"]
            assert "permanent_delivery_failures" in session
            assert session["permanent_delivery_failures"] == 0
        finally:
            await _bounded(adapter.stop())

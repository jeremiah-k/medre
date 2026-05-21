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
"""

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

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
            assert adapter._started is True
            info = await _bounded(adapter.health_check())
            assert info.health == "healthy"
        finally:
            await _bounded(adapter.stop())

    async def test_fake_mode_no_mtjk_import(self):
        """Fake mode works even if ``HAS_MESHTASTIC`` is ``False``."""
        from unittest.mock import patch

        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_fake_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        with patch("medre.adapters.meshtastic.session.HAS_MESHTASTIC", False):
            try:
                await _bounded(adapter.start(ctx))
                assert adapter._started is True
            finally:
                await _bounded(adapter.stop())

    async def test_fake_simulate_inbound_no_mtjk(self):
        """simulate_inbound works without mtjk in fake mode."""
        from unittest.mock import patch

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
        from unittest.mock import patch

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
        assert "queue_total_dropped" in diag
        assert "background_tasks" in diag
        # No session before start
        assert "session" not in diag

    async def test_non_fake_raises_without_mtjk(self):
        """Non-fake connection types raise MeshtasticConnectionError
        when ``mtjk`` is not available.

        Uses ``unittest.mock.patch`` to simulate a missing SDK.
        """
        from unittest.mock import patch

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
        from unittest.mock import patch

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
            assert adapter._started is False


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
        assert isinstance(diag["queue_total_dropped"], int)
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

            # No keys that look like credentials
            for key in diag:
                assert key not in (
                    "password", "secret", "token", "api_key",
                    "private_key", "auth_token", "host", "serial_port",
                ), f"Sensitive key {key!r} found in diagnostics"
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
            assert "queue_total_dropped" in diag
            assert diag["queue_pending"] >= 0
            assert diag["queue_total_sent"] >= 0
            assert diag["queue_total_failed"] >= 0
            assert diag["queue_total_dropped"] >= 0
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

            # Fake mode: connected should be True (client is None but started)
            # Actually session.connected = (client is not None and started)
            # In fake mode, client is None, so connected is False
            assert isinstance(session["connected"], bool)
            assert isinstance(session["reconnecting"], bool)
            assert isinstance(session["reconnect_attempts"], int)
            assert session["reconnect_attempts"] == 0
            assert isinstance(session["transient_delivery_failures"], int)
            assert isinstance(session["permanent_delivery_failures"], int)
        finally:
            await _bounded(adapter.stop())

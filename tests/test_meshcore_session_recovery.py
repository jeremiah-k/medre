"""Tests for MeshCoreSession: reconnect, error classification, callback
normalization, sync callback support, failed-start cleanup, and adapter
reality audit fixes (send_appstart, expected_ack).

Tests exercise the real connection wiring against a fake meshcore module
that matches the PyPI meshcore 2.3.7 API surface.
"""

from __future__ import annotations

import logging
import sys
from unittest.mock import AsyncMock, patch

import pytest

from medre.adapters.meshcore.errors import (
    MeshCoreConnectionError,
    MeshCoreSendError,
)
from medre.adapters.meshcore.session import MeshCoreSession
from medre.config.adapters.meshcore import MeshCoreConfig
from tests.helpers.meshcore_session import (
    MockEvent,
    MockEventType,
    build_mock_meshcore_module,
)


def _make_config(**overrides) -> MeshCoreConfig:
    defaults = dict(adapter_id="session-test")
    defaults.update(overrides)
    return MeshCoreConfig(**defaults)


# ===================================================================
# Reconnect exponential backoff verification
# ===================================================================


class TestMockedSDKReconnectBackoff:
    """Verify bounded exponential backoff with max attempts."""

    async def test_reconnect_stops_after_max_attempts(self) -> None:
        """Reconnect loop stops after _RECONNECT_MAX_ATTEMPTS failures."""
        import medre.adapters.meshcore.session as session_mod

        orig_base = session_mod._RECONNECT_BASE_DELAY
        orig_max_delay = session_mod._RECONNECT_MAX_DELAY
        orig_max_attempts = session_mod._RECONNECT_MAX_ATTEMPTS
        orig_jitter = session_mod._RECONNECT_JITTER_FRACTION

        session_mod._RECONNECT_BASE_DELAY = 0.01
        session_mod._RECONNECT_MAX_DELAY = 0.02
        session_mod._RECONNECT_MAX_ATTEMPTS = 4
        session_mod._RECONNECT_JITTER_FRACTION = 0.0

        try:
            config = _make_config(connection_type="fake")
            session = MeshCoreSession(config, "backoff-test")

            async def noop(pkt: dict) -> None:
                pass

            await session.start(noop)
            await session.stop()

            async def _failing_connect():
                raise RuntimeError("simulated failure")

            session._connect_real = _failing_connect
            session._stop_requested = False

            await session._reconnect_loop()

            assert session.reconnect_attempts == 4
            assert session.reconnecting is False
            assert session.last_error is not None
            assert "4" in session.last_error
        finally:
            session_mod._RECONNECT_BASE_DELAY = orig_base
            session_mod._RECONNECT_MAX_DELAY = orig_max_delay
            session_mod._RECONNECT_MAX_ATTEMPTS = orig_max_attempts
            session_mod._RECONNECT_JITTER_FRACTION = orig_jitter

    async def test_reconnect_succeeds_mid_loop(self) -> None:
        """Reconnect loop exits early on successful reconnection."""
        import medre.adapters.meshcore.session as session_mod

        orig_base = session_mod._RECONNECT_BASE_DELAY
        orig_max_delay = session_mod._RECONNECT_MAX_DELAY
        orig_max_attempts = session_mod._RECONNECT_MAX_ATTEMPTS
        orig_jitter = session_mod._RECONNECT_JITTER_FRACTION

        session_mod._RECONNECT_BASE_DELAY = 0.01
        session_mod._RECONNECT_MAX_DELAY = 0.02
        session_mod._RECONNECT_MAX_ATTEMPTS = 10
        session_mod._RECONNECT_JITTER_FRACTION = 0.0

        try:
            config = _make_config(connection_type="fake")
            session = MeshCoreSession(config, "mid-reconnect-test")

            async def noop(pkt: dict) -> None:
                pass

            await session.start(noop)
            await session.stop()

            call_count = 0

            async def _succeed_on_second():
                nonlocal call_count
                call_count += 1
                if call_count < 2:
                    raise RuntimeError("not yet")
                # Mimic real _connect_real which sets connected on success.
                session._diag.connected = True

            session._connect_real = _succeed_on_second
            session._stop_requested = False

            await session._reconnect_loop()

            assert session.reconnect_attempts == 0  # reset after successful reconnect
            assert session.reconnecting is False
            assert session.connected is True
        finally:
            session_mod._RECONNECT_BASE_DELAY = orig_base
            session_mod._RECONNECT_MAX_DELAY = orig_max_delay
            session_mod._RECONNECT_MAX_ATTEMPTS = orig_max_attempts
            session_mod._RECONNECT_JITTER_FRACTION = orig_jitter


# ===================================================================
# Transient vs permanent error classification
# ===================================================================


class TestMockedSDKErrorClassification:
    """Verify transient and permanent error counters track correctly."""

    async def test_sdk_error_is_permanent(self) -> None:
        """SDK ERROR response increments permanent counter only."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        mock_inst.commands.send_msg.return_value = MockEvent(
            event_type=MockEventType.ERROR,
            payload={"reason": "node_busy"},
        )

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "perm-err-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda _pkt: None)

        with pytest.raises(MeshCoreSendError, match="SDK send error"):
            await session.send_text("aabbcc", "perm test")

        assert session.permanent_delivery_failures == 1
        assert session.transient_delivery_failures == 0

        await session.stop()

    async def test_oserror_is_transient_then_permanent(self) -> None:
        """Transient OSError exhausts retries, then becomes permanent."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        mock_inst.commands.send_msg.side_effect = OSError("serial port error")

        config = _make_config(connection_type="serial", serial_port="/dev/ttyUSB0")
        session = MeshCoreSession(config, "transient-test-2")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda _pkt: None)

        with pytest.raises(MeshCoreSendError, match="Send failed after 3 attempts"):
            await session.send_text("aabbcc", "transient test")

        assert session.transient_delivery_failures == 3
        assert session.permanent_delivery_failures == 1

        await session.stop()

    async def test_transient_recovery_on_second_attempt(self) -> None:
        """Transient failure on first attempt, success on second — no permanent error."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        call_count = 0

        async def _flaky_send(*args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("transient glitch")
            return MockEvent(
                event_type=MockEventType.MSG_SENT,
                payload={"expected_ack": b"\x00\x00\x00\x01"},
            )

        mock_inst.commands.send_msg.side_effect = _flaky_send

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "recovery-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda _pkt: None)

        result = await session.send_text("aabbcc", "recover me")

        assert result == "00000001"  # expected_ack 4-byte bytes → hex string
        assert session.transient_delivery_failures == 1
        assert session.permanent_delivery_failures == 0
        assert call_count == 2

        await session.stop()

    async def test_diagnostics_track_error_counters(self) -> None:
        """Diagnostics snapshot includes accurate error counters after failures."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        mock_inst.commands.send_msg.return_value = MockEvent(
            event_type=MockEventType.ERROR,
            payload={"reason": "bad_state"},
        )

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "diag-err-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda _pkt: None)

        with pytest.raises(MeshCoreSendError):
            await session.send_text("x", "first fail")

        with pytest.raises(MeshCoreSendError):
            await session.send_text("x", "second fail")

        diag = session.diagnostics()
        assert diag["permanent_delivery_failures"] == 2
        assert diag["transient_delivery_failures"] == 0

        await session.stop()


# ===================================================================
# Inbound callback normalization edge cases
# ===================================================================


class TestMockedSDKCallbackNormalization:
    """Verify _on_sdk_event normalizes various payload shapes to plain dicts."""

    async def test_event_with_non_dict_payload(self) -> None:
        """Event with non-dict payload is normalized to empty dict."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "non-dict-test")
        received: list[dict] = []

        async def callback(pkt: dict) -> None:
            received.append(pkt)

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(callback)

        # Event with non-dict payload (e.g., string)
        event = MockEvent(
            event_type=MockEventType.CONTACT_MSG_RECV,
            payload="not a dict",
        )
        await session._on_sdk_event(event)

        assert len(received) == 1
        assert received[0] == {}

        await session.stop()

    async def test_event_with_none_payload(self) -> None:
        """Event with None payload is normalized to empty dict."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "none-payload-test")
        received: list[dict] = []

        async def callback(pkt: dict) -> None:
            received.append(pkt)

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(callback)

        event = MockEvent(
            event_type=MockEventType.CONTACT_MSG_RECV,
            payload=None,
        )
        await session._on_sdk_event(event)

        assert len(received) == 1
        assert received[0] == {}

        await session.stop()

    async def test_callback_exception_does_not_crash_session(self) -> None:
        """Exception in user callback is caught; session remains connected."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "cb-exc-test")

        async def bad_callback(pkt: dict) -> None:
            raise ValueError("callback blew up")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(bad_callback)

        event = MockEvent(
            event_type=MockEventType.CONTACT_MSG_RECV,
            payload={"text": "trigger error", "type": "PRIV"},
        )
        # Should NOT raise
        await session._on_sdk_event(event)

        assert session.connected is True
        assert session.last_message_time is not None

        await session.stop()

    async def test_last_message_time_updated_on_each_event(self) -> None:
        """last_message_time advances with each inbound event."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "time-test")

        async def callback(pkt: dict) -> None:
            pass

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(callback)

        assert session.last_message_time is None

        await session._on_sdk_event(
            MockEvent(
                event_type=MockEventType.CONTACT_MSG_RECV,
                payload={"text": "first"},
            )
        )
        t1 = session.last_message_time
        assert t1 is not None

        import asyncio

        await asyncio.sleep(0.01)

        await session._on_sdk_event(
            MockEvent(
                event_type=MockEventType.CHANNEL_MSG_RECV,
                payload={"text": "second"},
            )
        )
        t2 = session.last_message_time
        assert t2 is not None
        assert t2 >= t1

        await session.stop()


# ===================================================================
# Tranche 6: Sync callback support (no false TypeError)
# ===================================================================


class TestTranche6SyncCallback:
    """Sync inbound callbacks work without false TypeError logging."""

    async def test_sync_callback_receives_payload(self) -> None:
        """Sync callback receives payload dict without TypeError."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "sync-cb-test")
        received: list[dict] = []

        def sync_callback(pkt: dict) -> None:
            received.append(pkt)

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(sync_callback)

        event = MockEvent(
            event_type=MockEventType.CONTACT_MSG_RECV,
            payload={"text": "sync hello", "type": "PRIV"},
        )
        await session._on_sdk_event(event)

        assert len(received) == 1
        assert received[0]["text"] == "sync hello"
        assert session.last_message_time is not None

        await session.stop()

    async def test_sync_callback_no_typeerror_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Sync callback does NOT produce TypeError in logs."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "sync-cb-log-test")

        def sync_callback(pkt: dict) -> None:
            pass  # returns None — would previously cause await None

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(sync_callback)

        with caplog.at_level(logging.ERROR):
            event = MockEvent(
                event_type=MockEventType.CONTACT_MSG_RECV,
                payload={"text": "test", "type": "PRIV"},
            )
            await session._on_sdk_event(event)

        type_errors = [r for r in caplog.records if "TypeError" in r.message]
        assert (
            len(type_errors) == 0
        ), f"Unexpected TypeError in logs: {[r.message for r in type_errors]}"

        await session.stop()

    async def test_async_callback_still_works(self) -> None:
        """Async callbacks still receive payloads after sync fix."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "async-cb-test")
        received: list[dict] = []

        async def async_callback(pkt: dict) -> None:
            received.append(pkt)

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(async_callback)

        event = MockEvent(
            event_type=MockEventType.CONTACT_MSG_RECV,
            payload={"text": "async hello", "type": "PRIV"},
        )
        await session._on_sdk_event(event)

        assert len(received) == 1
        assert received[0]["text"] == "async hello"

        await session.stop()

    async def test_callback_exception_caught_session_survives(self) -> None:
        """Callback exception is caught; session remains connected."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "cb-exc-sync-test")

        def bad_sync(pkt: dict) -> None:
            raise RuntimeError("sync callback explosion")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(bad_sync)

        event = MockEvent(
            event_type=MockEventType.CONTACT_MSG_RECV,
            payload={"text": "trigger", "type": "PRIV"},
        )
        await session._on_sdk_event(event)

        assert session.connected is True

        await session.stop()


# ===================================================================
# Tranche 6: Failed-start state cleanup
# ===================================================================


class TestTranche6FailedStartCleanup:
    """Failed MeshCore start does not retain callbacks or report
    connected/reconnecting in diagnostics."""

    async def test_non_fake_start_clears_callback(self) -> None:
        """Non-fake start that fails clears _message_callback."""
        config = _make_config(connection_type="tcp", host="1.2.3.4")
        session = MeshCoreSession(config, "fail-cleanup-test")

        async def noop(pkt: dict) -> None:
            pass

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", False),
            pytest.raises(MeshCoreConnectionError),
        ):
            await session.start(noop)

        assert session._started is False
        assert session._message_callback is None

    async def test_failed_start_diagnostics_not_connected(self) -> None:
        """Diagnostics after failed start do not report connected/reconnecting."""
        config = _make_config(connection_type="tcp", host="1.2.3.4")
        session = MeshCoreSession(config, "diag-fail-test")

        async def noop(pkt: dict) -> None:
            pass

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", False),
            pytest.raises(MeshCoreConnectionError),
        ):
            await session.start(noop)

        diag = session.diagnostics()
        assert diag["connected"] is False
        assert diag["reconnecting"] is False


# ===================================================================
# Tranche: Adapter Reality Audit — appstart + expected_ack
# ===================================================================


class TestSendAppstart:
    """Verify send_appstart is called after connect and cleans up on failure."""

    async def test_send_appstart_called_after_connect(self) -> None:
        """send_appstart is called once during _connect_real()."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "appstart-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda _pkt: None)

        mock_inst.commands.send_appstart.assert_awaited_once()
        assert session.connected is True

        await session.stop()

    async def test_send_appstart_failure_cleans_up(self) -> None:
        """send_appstart failure raises MeshCoreConnectionError and cleans up."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        # Make send_appstart return an error event.
        error_event = MockEvent(
            event_type=MockEventType.ERROR,
            payload={"reason": "firmware rejected"},
        )
        mock_inst.commands.send_appstart = AsyncMock(return_value=error_event)

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "appstart-fail-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            with pytest.raises(MeshCoreConnectionError, match="send_appstart"):
                await session.start(lambda _pkt: None)

        assert session._meshcore is None
        assert session.connected is False
        assert len(session._subscriptions) == 0

    async def test_send_appstart_exception_cleans_up(self) -> None:
        """send_appstart raising an exception triggers full cleanup."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        mock_inst.commands.send_appstart = AsyncMock(
            side_effect=OSError("serial write timeout")
        )

        config = _make_config(connection_type="serial", serial_port="/dev/ttyUSB0")
        session = MeshCoreSession(config, "appstart-exc-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            with pytest.raises(MeshCoreConnectionError, match="send_appstart failed"):
                await session.start(lambda _pkt: None)

        assert session._meshcore is None
        assert session.connected is False

    async def test_reconnect_calls_appstart(self) -> None:
        """Reconnect goes through _connect_real which sends appstart."""
        import medre.adapters.meshcore.session as session_mod

        orig_base = session_mod._RECONNECT_BASE_DELAY
        orig_max_delay = session_mod._RECONNECT_MAX_DELAY
        orig_max_attempts = session_mod._RECONNECT_MAX_ATTEMPTS
        orig_jitter = session_mod._RECONNECT_JITTER_FRACTION

        session_mod._RECONNECT_BASE_DELAY = 0.01
        session_mod._RECONNECT_MAX_DELAY = 0.02
        session_mod._RECONNECT_MAX_ATTEMPTS = 10
        session_mod._RECONNECT_JITTER_FRACTION = 0.0

        try:
            mock_mc, mock_inst = build_mock_meshcore_module()

            config = _make_config(connection_type="tcp", host="localhost")
            session = MeshCoreSession(config, "reconnect-appstart-test")

            with (
                patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
                patch.dict(sys.modules, {"meshcore": mock_mc}),
            ):
                await session.start(lambda _pkt: None)

            # Reset appstart call count from initial connect.
            mock_inst.commands.send_appstart.reset_mock()

            # Simulate disconnect + reconnect via _connect_real directly.
            session._diag.connected = False
            session._diag.reconnect_attempts = 0
            with (
                patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
                patch.dict(sys.modules, {"meshcore": mock_mc}),
            ):
                await session._connect_real()

            # Appstart should have been called again on reconnect.
            mock_inst.commands.send_appstart.assert_awaited_once()
            assert session.connected is True

            await session.stop()
        finally:
            session_mod._RECONNECT_BASE_DELAY = orig_base
            session_mod._RECONNECT_MAX_DELAY = orig_max_delay
            session_mod._RECONNECT_MAX_ATTEMPTS = orig_max_attempts
            session_mod._RECONNECT_JITTER_FRACTION = orig_jitter


class TestExpectedAckAsNativeId:
    """Verify expected_ack (bytes) is used as native_id for DMs."""

    async def test_expected_ack_used_as_native_id_for_dm(self) -> None:
        """DM send returns expected_ack hex as native_id."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        mock_inst.commands.send_msg.return_value = MockEvent(
            event_type=MockEventType.MSG_SENT,
            payload={"expected_ack": b"\x01\x02\x03\x04"},
        )

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "ack-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda _pkt: None)

        result = await session.send_text("aabbcc", "test dm")

        assert result == "01020304"

        await session.stop()

    async def test_native_id_none_for_channel_send(self) -> None:
        """Channel send with OK response and no expected_ack returns None."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        mock_inst.commands.send_chan_msg.return_value = MockEvent(
            event_type=MockEventType.OK,
            payload={},
        )

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "chan-no-id-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda _pkt: None)

        result = await session.send_text("ignored", "chan msg", channel_index=0)

        assert result is None

        await session.stop()

    async def test_expected_ack_from_attributes_fallback(self) -> None:
        """expected_ack in attributes is used when payload has none."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        event = MockEvent(
            event_type=MockEventType.MSG_SENT,
            payload={},
            attributes={"expected_ack": b"\xab\xcd\xef\x01"},
        )
        mock_inst.commands.send_msg.return_value = event

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "attr-ack-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda _pkt: None)

        result = await session.send_text("aabbcc", "attr ack")

        assert result == "abcdef01"

        await session.stop()

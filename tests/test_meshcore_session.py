"""Tests for MeshCoreSession: lifecycle, reconnect, send, diagnostics.

All tests use fake mode (no SDK or hardware required).
Mocked SDK tests exercise the real connection wiring against a fake meshcore
module that matches the PyPI meshcore 2.3.7 API surface.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.config.adapters.meshcore import MeshCoreConfig
from medre.adapters.meshcore.errors import (
    MeshCoreConnectionError,
    MeshCoreSendError,
)
from medre.adapters.meshcore.session import MeshCoreSession


def _make_config(**overrides) -> MeshCoreConfig:
    defaults = dict(adapter_id="session-test")
    defaults.update(overrides)
    return MeshCoreConfig(**defaults)


def _make_contact_packet(
    text: str = "hello",
    sender: str = "abc123",
    timestamp: int = 42,
) -> dict:
    return {
        "text": text,
        "pubkey_prefix": sender,
        "sender_timestamp": timestamp,
        "type": "PRIV",
        "txt_type": 0,
    }


def _make_channel_packet(
    text: str = "hello channel",
    channel_idx: int = 0,
    timestamp: int = 42,
) -> dict:
    return {
        "text": text,
        "channel_idx": channel_idx,
        "sender_timestamp": timestamp,
        "type": "CHAN",
        "txt_type": 0,
        "pubkey_prefix": "chan_sender",
    }


# ===================================================================
# Lifecycle
# ===================================================================


class TestMeshCoreSessionLifecycle:
    """Session start/stop/health transitions."""

    async def test_initial_state(self) -> None:
        config = _make_config()
        session = MeshCoreSession(config, "test-1")
        assert session.connected is False
        assert session.reconnecting is False
        assert session.reconnect_attempts == 0
        assert session.last_message_time is None
        assert session.last_error is None
        assert session.transient_delivery_failures == 0
        assert session.permanent_delivery_failures == 0

    async def test_start_fake_mode(self) -> None:
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")
        received: list[dict] = []

        async def callback(pkt: dict) -> None:
            received.append(pkt)

        await session.start(callback)
        assert session.connected is True
        assert session.reconnecting is False

    async def test_start_is_idempotent(self) -> None:
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)
        await session.start(noop)
        assert session.connected is True

    async def test_stop_fake_mode(self) -> None:
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)
        await session.stop()
        assert session.connected is False

    async def test_stop_without_start_is_noop(self) -> None:
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")
        await session.stop()  # should not raise
        assert session.connected is False

    async def test_repeated_start_stop(self) -> None:
        """Start/stop/start/stop cycle works correctly."""
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)
        assert session.connected is True
        await session.stop()
        assert session.connected is False

        # Start again
        await session.start(noop)
        assert session.connected is True
        await session.stop()
        assert session.connected is False

    async def test_non_fake_raises_without_sdk(self) -> None:
        """Non-fake mode raises MeshCoreConnectionError without SDK."""
        from medre.adapters.meshcore.errors import MeshCoreConnectionError

        config = _make_config(connection_type="tcp", host="1.2.3.4")
        session = MeshCoreSession(config, "test-1")

        async def noop(pkt: dict) -> None:
            pass

        with pytest.raises(MeshCoreConnectionError):
            await session.start(noop)


# ===================================================================
# Inbound message callback
# ===================================================================


class TestMeshCoreSessionInbound:
    """Session message callback forwards payloads correctly."""

    async def test_fake_mode_callback_not_invoked_automatically(self) -> None:
        """In fake mode, no messages are received automatically."""
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")
        received: list[dict] = []

        async def callback(pkt: dict) -> None:
            received.append(pkt)

        await session.start(callback)
        # No messages should have been received.
        assert len(received) == 0


# ===================================================================
# Outbound send
# ===================================================================


class TestMeshCoreSessionSend:
    """Session send_text in fake mode."""

    async def test_send_text_fake_mode_returns_none(self) -> None:
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)
        result = await session.send_text("contact1", "hello")
        assert result is None

    async def test_send_text_when_not_connected_raises(self) -> None:
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")
        # Not started — not connected.
        with pytest.raises(MeshCoreSendError, match="not connected"):
            await session.send_text("contact1", "hello")

    async def test_send_text_channel(self) -> None:
        """Sending to a channel in fake mode returns None."""
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)
        result = await session.send_text("ignored", "hello", channel_index=0)
        assert result is None


# ===================================================================
# Diagnostics
# ===================================================================


class TestMeshCoreSessionDiagnostics:
    """Session diagnostics snapshot."""

    async def test_diagnostics_initial(self) -> None:
        config = _make_config()
        session = MeshCoreSession(config, "test-1")
        diag = session.diagnostics()
        assert diag["connected"] is False
        assert diag["reconnecting"] is False
        assert diag["reconnect_attempts"] == 0
        assert diag["last_message_time"] is None
        assert diag["last_error"] is None
        assert diag["transient_delivery_failures"] == 0
        assert diag["permanent_delivery_failures"] == 0
        assert diag["peer_count"] is None
        assert diag["mode"] == "fake"

    async def test_diagnostics_after_start(self) -> None:
        config = _make_config()
        session = MeshCoreSession(config, "test-1")

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)
        diag = session.diagnostics()
        assert diag["connected"] is True
        assert diag["mode"] == "fake"

    async def test_diagnostics_no_secrets(self) -> None:
        """Diagnostics never expose secrets or private keys."""
        config = _make_config()
        session = MeshCoreSession(config, "test-1")
        diag = session.diagnostics()
        diag_str = str(diag)
        assert "private_key" not in diag_str
        assert "secret" not in diag_str
        assert "password" not in diag_str


# ===================================================================
# Reconnect
# ===================================================================


class TestMeshCoreSessionReconnect:
    """Reconnect loop diagnostics and guards."""

    async def test_stop_prevents_reconnect(self) -> None:
        """Setting _stop_requested prevents reconnect loop."""
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)
        # Simulate stop_requested
        session._stop_requested = True
        # Attempting reconnect loop should exit immediately
        await session._reconnect_loop()
        assert session.reconnect_attempts == 0

    async def test_reconnect_attempts_bounded(self) -> None:
        """Reconnect loop does not exceed max attempts."""
        import medre.adapters.meshcore.session as session_mod

        # Save original constants and set very short delays.
        orig_base = session_mod._RECONNECT_BASE_DELAY
        orig_max_delay = session_mod._RECONNECT_MAX_DELAY
        orig_max_attempts = session_mod._RECONNECT_MAX_ATTEMPTS

        session_mod._RECONNECT_BASE_DELAY = 0.01
        session_mod._RECONNECT_MAX_DELAY = 0.02
        session_mod._RECONNECT_MAX_ATTEMPTS = 3

        try:
            config = _make_config(connection_type="fake")
            session = MeshCoreSession(config, "test-1")

            async def noop(pkt: dict) -> None:
                pass

            await session.start(noop)
            await session.stop()

            async def _failing_connect():
                raise RuntimeError("test failure")

            session._connect_real = _failing_connect
            session._stop_requested = False

            await session._reconnect_loop()

            assert session.reconnect_attempts == 3  # max attempts
        finally:
            session_mod._RECONNECT_BASE_DELAY = orig_base
            session_mod._RECONNECT_MAX_DELAY = orig_max_delay
            session_mod._RECONNECT_MAX_ATTEMPTS = orig_max_attempts


# ===================================================================
# Diagnostics counter tracking
# ===================================================================


class TestMeshCoreSessionCounters:
    """Transient/permanent failure counters are tracked."""

    async def test_initial_counters_zero(self) -> None:
        config = _make_config()
        session = MeshCoreSession(config, "test-1")
        assert session.transient_delivery_failures == 0
        assert session.permanent_delivery_failures == 0

    async def test_diagnostics_includes_counters(self) -> None:
        config = _make_config()
        session = MeshCoreSession(config, "test-1")
        diag = session.diagnostics()
        assert "transient_delivery_failures" in diag
        assert "permanent_delivery_failures" in diag


# ===================================================================
# Mocked SDK tests — verify wiring against meshcore 2.3.7 API surface
# ===================================================================

# ---------------------------------------------------------------------------
# Mock SDK types matching the real PyPI meshcore 2.3.7 shapes
# ---------------------------------------------------------------------------


class _MockEventType(Enum):
    """Minimal EventType subset used by session._subscribe_events."""

    CONTACT_MSG_RECV = "contact_message"
    CHANNEL_MSG_RECV = "channel_message"
    DISCONNECTED = "disconnected"
    MSG_SENT = "message_sent"
    OK = "command_ok"
    ERROR = "command_error"


class _MockEvent:
    """Mimics meshcore.events.Event (type, payload, attributes, is_error)."""

    def __init__(
        self,
        type: _MockEventType,
        payload: Any = None,
        attributes: dict | None = None,
    ) -> None:
        self.type = type
        self.payload = payload
        self.attributes = attributes or {}

    def is_error(self) -> bool:
        return self.type == _MockEventType.ERROR


def _build_mock_meshcore_module() -> tuple[MagicMock, AsyncMock]:
    """Build a mock ``meshcore`` module and return (module, meshcore_instance).

    The instance is what ``await MeshCore.create_tcp(...)`` (etc.) returns —
    it carries ``disconnect``, ``subscribe``, ``unsubscribe``,
    and ``commands.send_msg`` / ``commands.send_chan_msg``.
    """
    mock_mc = MagicMock()
    mock_mc.EventType = _MockEventType

    # The MeshCore instance returned by factory methods.
    instance = AsyncMock()
    instance.disconnect = AsyncMock()
    instance.subscribe = MagicMock(return_value=MagicMock())
    instance.unsubscribe = MagicMock()
    instance.commands = AsyncMock()
    instance.commands.send_msg = AsyncMock()
    instance.commands.send_chan_msg = AsyncMock()

    # Factory methods: MeshCore.create_tcp/create_serial/create_ble
    # These are async class methods that return the instance.
    mock_mc.MeshCore = MagicMock()
    mock_mc.MeshCore.create_tcp = AsyncMock(return_value=instance)
    mock_mc.MeshCore.create_serial = AsyncMock(return_value=instance)
    mock_mc.MeshCore.create_ble = AsyncMock(return_value=instance)

    return mock_mc, instance


def _install_mock_module(mock_mc: MagicMock) -> None:
    """Insert mock meshcore module into sys.modules so deferred import finds it."""
    sys.modules["meshcore"] = mock_mc


def _remove_mock_module() -> None:
    sys.modules.pop("meshcore", None)


class TestMockedSDKSerialStartup:
    """Verify serial-mode startup wiring against mocked SDK 2.3.7 API."""

    async def test_serial_constructor_args(self) -> None:
        """MeshCore.create_serial is called with (port, baudrate) positional args."""
        mock_mc, mock_inst = _build_mock_meshcore_module()

        config = _make_config(
            connection_type="serial",
            serial_port="/dev/ttyACM0",
            serial_baudrate=57600,
        )
        session = MeshCoreSession(config, "serial-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda pkt: None)

        # create_serial should have been called with (port, baudrate).
        mock_mc.MeshCore.create_serial.assert_awaited_once_with(
            "/dev/ttyACM0", 57600
        )
        assert session.connected is True

        # Cleanup.
        await session.stop()

    async def test_serial_default_baudrate(self) -> None:
        """Default baudrate is 115200 when not overridden in config."""
        mock_mc, mock_inst = _build_mock_meshcore_module()

        config = _make_config(
            connection_type="serial",
            serial_port="/dev/ttyUSB0",
        )
        session = MeshCoreSession(config, "serial-default")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda pkt: None)

        mock_mc.MeshCore.create_serial.assert_awaited_once_with(
            "/dev/ttyUSB0", 115200
        )

        await session.stop()


class TestMockedSDKTCPStartup:
    """Verify TCP-mode startup wiring against mocked SDK 2.3.7 API."""

    async def test_tcp_constructor_args(self) -> None:
        """MeshCore.create_tcp is called with (host, port) positional args."""
        mock_mc, mock_inst = _build_mock_meshcore_module()

        config = _make_config(
            connection_type="tcp",
            host="meshcore.local",
            port=4403,
        )
        session = MeshCoreSession(config, "tcp-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda pkt: None)

        mock_mc.MeshCore.create_tcp.assert_awaited_once_with("meshcore.local", 4403)
        assert session.connected is True

        await session.stop()


class TestMockedSDKEventSubscription:
    """Verify subscribe is called for CONTACT_MSG_RECV, CHANNEL_MSG_RECV,
    DISCONNECTED during startup."""

    async def test_subscriptions_registered(self) -> None:
        """Three subscriptions are registered on the SDK client."""
        mock_mc, mock_inst = _build_mock_meshcore_module()

        config = _make_config(
            connection_type="tcp",
            host="localhost",
        )
        session = MeshCoreSession(config, "sub-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda pkt: None)

        # subscribe should have been called 3 times.
        assert mock_inst.subscribe.call_count == 3

        called_event_types = [
            call.args[0] for call in mock_inst.subscribe.call_args_list
        ]
        assert _MockEventType.CONTACT_MSG_RECV in called_event_types
        assert _MockEventType.CHANNEL_MSG_RECV in called_event_types
        assert _MockEventType.DISCONNECTED in called_event_types

        await session.stop()


class TestMockedSDKEventCallbackPayload:
    """Verify that _on_sdk_event extracts payload dict from SDK Event objects."""

    async def test_sdk_event_payload_forwarded_as_dict(self) -> None:
        """_on_sdk_event receives SDK Event with .payload dict → callback gets dict."""
        mock_mc, mock_inst = _build_mock_meshcore_module()

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "cb-test")
        received: list[dict] = []

        async def callback(pkt: dict) -> None:
            received.append(pkt)

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(callback)

        # Simulate an SDK inbound event.
        sdk_event = _MockEvent(
            type=_MockEventType.CONTACT_MSG_RECV,
            payload={
                "text": "hello from radio",
                "pubkey_prefix": "aabbcc",
                "sender_timestamp": 1234,
                "type": "PRIV",
                "txt_type": 0,
            },
        )
        await session._on_sdk_event(sdk_event)

        assert len(received) == 1
        assert received[0]["text"] == "hello from radio"
        assert received[0]["pubkey_prefix"] == "aabbcc"
        assert received[0]["type"] == "PRIV"
        assert session.last_message_time is not None

        await session.stop()

    async def test_sdk_event_dict_passthrough(self) -> None:
        """If event is already a dict, it passes through directly."""
        mock_mc, mock_inst = _build_mock_meshcore_module()

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "cb-dict-test")
        received: list[dict] = []

        async def callback(pkt: dict) -> None:
            received.append(pkt)

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(callback)

        raw_dict = {"text": "raw dict event", "type": "CHAN", "channel_idx": 0}
        await session._on_sdk_event(raw_dict)

        assert len(received) == 1
        assert received[0]["text"] == "raw dict event"

        await session.stop()


class TestMockedSDKSendMsg:
    """Verify send_msg delegates to SDK commands.send_msg correctly."""

    async def test_send_msg_calls_commands(self) -> None:
        """send_text(contact_id, text) → commands.send_msg(contact_id, text)."""
        mock_mc, mock_inst = _build_mock_meshcore_module()

        # Successful send returns MSG_SENT event (no message_id).
        mock_inst.commands.send_msg.return_value = _MockEvent(
            type=_MockEventType.MSG_SENT,
            payload={"expected_ack": "deadbeef"},
        )

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "send-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda pkt: None)

        result = await session.send_text("aabbccddeeff", "test message")

        mock_inst.commands.send_msg.assert_awaited_once_with(
            "aabbccddeeff", "test message"
        )
        # No message_id in MSG_SENT payload → returns None.
        assert result is None

        await session.stop()


class TestMockedSDKSendChanMsg:
    """Verify send_chan_msg delegates to SDK commands.send_chan_msg correctly."""

    async def test_send_chan_msg_calls_commands(self) -> None:
        """send_text(contact_id, text, channel_index=2) → commands.send_chan_msg(2, text)."""
        mock_mc, mock_inst = _build_mock_meshcore_module()

        # Successful channel send returns OK event.
        mock_inst.commands.send_chan_msg.return_value = _MockEvent(
            type=_MockEventType.OK,
            payload={},
        )

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "chan-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda pkt: None)

        result = await session.send_text("ignored", "chan hello", channel_index=2)

        mock_inst.commands.send_chan_msg.assert_awaited_once_with(2, "chan hello")
        # No message_id in OK payload → returns None.
        assert result is None

        await session.stop()


class TestMockedSDKSendError:
    """Verify SDK error responses raise MeshCoreSendError."""

    async def test_send_msg_sdk_error_raises(self) -> None:
        """When commands.send_msg returns ERROR event, MeshCoreSendError is raised."""
        mock_mc, mock_inst = _build_mock_meshcore_module()

        mock_inst.commands.send_msg.return_value = _MockEvent(
            type=_MockEventType.ERROR,
            payload={"reason": "node_busy"},
        )

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "err-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda pkt: None)

        with pytest.raises(MeshCoreSendError, match="SDK send error"):
            await session.send_text("aabbcc", "will fail")

        # Permanent failure counter incremented.
        assert session.permanent_delivery_failures == 1

        await session.stop()

    async def test_send_msg_transient_failure_exhausted(self) -> None:
        """When send_msg raises transient exceptions 3 times, MeshCoreSendError."""
        mock_mc, mock_inst = _build_mock_meshcore_module()

        mock_inst.commands.send_msg.side_effect = OSError("serial write failed")

        config = _make_config(connection_type="serial", serial_port="/dev/ttyUSB0")
        session = MeshCoreSession(config, "transient-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda pkt: None)

        with pytest.raises(MeshCoreSendError, match="Send failed after 3 attempts"):
            await session.send_text("aabbcc", "retry me")

        # 3 transient + 1 permanent.
        assert session.transient_delivery_failures == 3
        assert session.permanent_delivery_failures == 1

        await session.stop()


class TestMockedSDKStartupFailureCleanup:
    """Verify failed startup cleans up SDK client state."""

    async def test_connect_failure_sets_meshcore_none(self) -> None:
        """When create_serial raises, _meshcore is reset to None."""
        mock_mc, mock_inst = _build_mock_meshcore_module()
        mock_mc.MeshCore.create_serial.side_effect = OSError("port not found")

        config = _make_config(
            connection_type="serial",
            serial_port="/dev/nonexistent",
        )
        session = MeshCoreSession(config, "fail-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            with pytest.raises(MeshCoreConnectionError, match="Failed to connect"):
                await session.start(lambda pkt: None)

        # _meshcore should have been cleaned up.
        assert session._meshcore is None
        assert session.connected is False


class TestMockedSDKDisconnectIdempotent:
    """Verify stop()/disconnect is idempotent with mocked SDK."""

    async def test_stop_twice_no_error(self) -> None:
        """Calling stop() twice does not raise."""
        mock_mc, mock_inst = _build_mock_meshcore_module()

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "idem-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda pkt: None)

        await session.stop()
        assert session.connected is False

        # Second stop should be a no-op (started=False early return).
        await session.stop()
        assert session.connected is False

        # disconnect should only have been called once.
        mock_inst.disconnect.assert_awaited_once()

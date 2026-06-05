"""Tests for MeshCoreSession: mocked SDK startup and send wiring.

Tests exercise the real connection wiring against a fake meshcore module
that matches the PyPI meshcore 2.3.7 API surface. Covers:
- Serial, TCP, BLE startup constructor args
- Event subscription registration
- Event callback payload forwarding
- Send_msg / send_chan_msg delegation
- SDK error responses and startup failure cleanup
- Disconnect idempotency
- Send_text return values (message_id / expected_ack)
"""

from __future__ import annotations

import sys
from unittest.mock import patch

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
# Serial startup
# ===================================================================


class TestMockedSDKSerialStartup:
    """Verify serial-mode startup wiring against mocked SDK 2.3.7 API."""

    async def test_serial_constructor_args(self) -> None:
        """MeshCore.create_serial is called with (port, baudrate) positional args."""
        mock_mc, mock_inst = build_mock_meshcore_module()

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
        mock_mc.MeshCore.create_serial.assert_awaited_once_with("/dev/ttyACM0", 57600)
        assert session.connected is True

        # Cleanup.
        await session.stop()

    async def test_serial_default_baudrate(self) -> None:
        """Default baudrate is 115200 when not overridden in config."""
        mock_mc, mock_inst = build_mock_meshcore_module()

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

        mock_mc.MeshCore.create_serial.assert_awaited_once_with("/dev/ttyUSB0", 115200)

        await session.stop()


# ===================================================================
# TCP startup
# ===================================================================


class TestMockedSDKTCPStartup:
    """Verify TCP-mode startup wiring against mocked SDK 2.3.7 API."""

    async def test_tcp_constructor_args(self) -> None:
        """MeshCore.create_tcp is called with (host, port) positional args."""
        mock_mc, mock_inst = build_mock_meshcore_module()

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


# ===================================================================
# Event subscription
# ===================================================================


class TestMockedSDKEventSubscription:
    """Verify subscribe is called for CONTACT_MSG_RECV, CHANNEL_MSG_RECV,
    DISCONNECTED during startup."""

    async def test_subscriptions_registered(self) -> None:
        """Three subscriptions are registered on the SDK client."""
        mock_mc, mock_inst = build_mock_meshcore_module()

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
        assert MockEventType.CONTACT_MSG_RECV in called_event_types
        assert MockEventType.CHANNEL_MSG_RECV in called_event_types
        assert MockEventType.DISCONNECTED in called_event_types

        await session.stop()


# ===================================================================
# Event callback payload
# ===================================================================


class TestMockedSDKEventCallbackPayload:
    """Verify that _on_sdk_event extracts payload dict from SDK Event objects."""

    async def test_sdk_event_payload_forwarded_as_dict(self) -> None:
        """_on_sdk_event receives SDK Event with .payload dict → callback gets dict."""
        mock_mc, mock_inst = build_mock_meshcore_module()

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
        sdk_event = MockEvent(
            event_type=MockEventType.CONTACT_MSG_RECV,
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
        mock_mc, mock_inst = build_mock_meshcore_module()

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


# ===================================================================
# Send_msg / send_chan_msg delegation
# ===================================================================


class TestMockedSDKSendMsg:
    """Verify send_msg delegates to SDK commands.send_msg correctly."""

    async def test_send_msg_calls_commands(self) -> None:
        """send_text(contact_id, text) → commands.send_msg(contact_id, text)."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        # Successful send returns MSG_SENT event with expected_ack as 4-byte bytes.
        mock_inst.commands.send_msg.return_value = MockEvent(
            event_type=MockEventType.MSG_SENT,
            payload={"expected_ack": b"\xde\xad\xbe\xef"},
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
        # expected_ack 4-byte bytes → hex string.
        assert result == "deadbeef"

        await session.stop()


class TestMockedSDKSendChanMsg:
    """Verify send_chan_msg delegates to SDK commands.send_chan_msg correctly."""

    async def test_send_chan_msg_calls_commands(self) -> None:
        """send_text(contact_id, text, channel_index=2) → commands.send_chan_msg(2, text)."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        # Successful channel send returns OK event.
        mock_inst.commands.send_chan_msg.return_value = MockEvent(
            event_type=MockEventType.OK,
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


# ===================================================================
# SDK error responses
# ===================================================================


class TestMockedSDKSendError:
    """Verify SDK error responses raise MeshCoreSendError."""

    async def test_send_msg_sdk_error_raises(self) -> None:
        """When commands.send_msg returns ERROR event, MeshCoreSendError is raised."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        mock_inst.commands.send_msg.return_value = MockEvent(
            event_type=MockEventType.ERROR,
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
        mock_mc, mock_inst = build_mock_meshcore_module()

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


# ===================================================================
# Startup failure cleanup
# ===================================================================


class TestMockedSDKStartupFailureCleanup:
    """Verify failed startup cleans up SDK client state."""

    async def test_connect_failure_sets_meshcore_none(self) -> None:
        """When create_serial raises, _meshcore is reset to None."""
        mock_mc, mock_inst = build_mock_meshcore_module()
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

    async def test_subscription_failure_cleans_up(self) -> None:
        """When subscribe raises after connection succeeds, full cleanup occurs.

        The client is created successfully but event subscription fails.
        _cleanup_failed_start must clear _meshcore, _message_callback,
        subscriptions, and connected flag.
        """
        mock_mc, mock_inst = build_mock_meshcore_module()
        # Make subscribe raise after connection succeeds.
        mock_inst.subscribe.side_effect = RuntimeError("subscription failed")

        config = _make_config(
            connection_type="tcp",
            host="localhost",
        )
        session = MeshCoreSession(config, "sub-fail-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            with pytest.raises(
                MeshCoreConnectionError, match="Failed to subscribe to events"
            ):
                await session.start(lambda pkt: None)

        # Full cleanup: meshcore client released, callback cleared,
        # connected flag false, subscriptions empty.
        assert session._meshcore is None
        assert session._message_callback is None
        assert session.connected is False
        assert session.reconnecting is False
        assert len(session._subscriptions) == 0
        assert session.last_error is not None
        assert "subscription failed" in str(session.last_error)
        mock_inst.disconnect.assert_awaited_once()

    async def test_connect_failure_clears_callback(self) -> None:
        """Failed connection clears _message_callback via _cleanup_failed_start."""
        mock_mc, mock_inst = build_mock_meshcore_module()
        mock_mc.MeshCore.create_tcp.side_effect = OSError("connection refused")

        config = _make_config(connection_type="tcp", host="unreachable")
        session = MeshCoreSession(config, "cb-fail-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            with pytest.raises(MeshCoreConnectionError):
                await session.start(lambda pkt: None)

        assert session._message_callback is None
        assert session._started is False


# ===================================================================
# Disconnect idempotency
# ===================================================================


class TestMockedSDKDisconnectIdempotent:
    """Verify stop()/disconnect is idempotent with mocked SDK."""

    async def test_stop_twice_no_error(self) -> None:
        """Calling stop() twice does not raise."""
        mock_mc, mock_inst = build_mock_meshcore_module()

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


# ===================================================================
# BLE startup
# ===================================================================


class TestMockedSDKBLEStartup:
    """Verify BLE-mode startup wiring against mocked SDK 2.3.7 API."""

    async def test_ble_constructor_args(self) -> None:
        """MeshCore.create_ble is called with address keyword arg."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        config = _make_config(
            connection_type="ble",
            ble_address="AA:BB:CC:DD:EE:FF",
        )
        session = MeshCoreSession(config, "ble-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda pkt: None)

        mock_mc.MeshCore.create_ble.assert_awaited_once_with(
            address="AA:BB:CC:DD:EE:FF",
        )
        assert session.connected is True

        await session.stop()

    async def test_ble_subscriptions_registered(self) -> None:
        """BLE mode registers the same 3 subscriptions as TCP/serial."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        config = _make_config(
            connection_type="ble",
            ble_address="AA:BB:CC:DD:EE:FF",
        )
        session = MeshCoreSession(config, "ble-sub-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda pkt: None)

        assert mock_inst.subscribe.call_count == 3

        # Verify exact event types subscribed (order-insensitive).
        subscribed_types = [call.args[0] for call in mock_inst.subscribe.call_args_list]
        assert set(subscribed_types) == {
            MockEventType.CONTACT_MSG_RECV,
            MockEventType.CHANNEL_MSG_RECV,
            MockEventType.DISCONNECTED,
        }

        await session.stop()


# ===================================================================
# Send_text return value with message_id
# ===================================================================


class TestMockedSDKSendMsgWithId:
    """Verify send_text returns native ID when SDK provides one."""

    async def test_send_msg_returns_message_id_from_payload(self) -> None:
        """When MSG_SENT payload has message_id, send_text returns it as str."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        # expected_ack is None, message_id is 42
        mock_inst.commands.send_msg.return_value = MockEvent(
            event_type=MockEventType.MSG_SENT,
            payload={"expected_ack": None, "message_id": 42},
        )

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "id-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda pkt: None)

        result = await session.send_text("aabbcc", "test with id")

        assert result == "42"
        assert isinstance(result, str)

        await session.stop()

    async def test_send_msg_returns_message_id_from_attributes(self) -> None:
        """When Event.attributes has message_id, send_text returns it."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        # No expected_ack or message_id in payload, but message_id in attributes
        event = MockEvent(
            event_type=MockEventType.MSG_SENT,
            payload={},
            attributes={"message_id": "pkt-99"},
        )
        mock_inst.commands.send_msg.return_value = event

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "attr-id-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda pkt: None)

        result = await session.send_text("aabbcc", "attr id test")

        assert result == "pkt-99"

        await session.stop()

    async def test_send_chan_msg_returns_none_for_ok(self) -> None:
        """Channel send with OK response and no message_id returns None."""
        mock_mc, mock_inst = build_mock_meshcore_module()

        mock_inst.commands.send_chan_msg.return_value = MockEvent(
            event_type=MockEventType.OK,
            payload={},
        )

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "chan-ok-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            await session.start(lambda pkt: None)

        result = await session.send_text("ignored", "chan msg", channel_index=0)

        assert result is None

        await session.stop()


# ===================================================================
# send_appstart failure cleanup (lines 529-533)
# ===================================================================


class TestSendAppstartFailureCleanup:
    """When send_appstart raises, disconnect/cleanup happens correctly."""

    async def test_appstart_error_disconnects_and_clears_state(self) -> None:
        """send_appstart raising causes disconnect, _meshcore=None, subscriptions cleared."""
        mock_mc, mock_inst = build_mock_meshcore_module()
        # Make send_appstart raise an exception.
        mock_inst.commands.send_appstart.side_effect = RuntimeError("appstart rejected")

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "appstart-fail-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            with pytest.raises(MeshCoreConnectionError, match="send_appstart failed"):
                await session.start(lambda pkt: None)

        # _meshcore must be cleaned up (set to None).
        assert session._meshcore is None
        # Connected flag must be False.
        assert session.connected is False
        # Subscriptions must be cleared.
        assert len(session._subscriptions) == 0
        # SDK disconnect should have been called.
        mock_inst.disconnect.assert_awaited_once()
        # last_error must reflect the failure.
        assert session.last_error is not None
        assert "appstart rejected" in str(session.last_error)

    async def test_appstart_disconnect_error_suppressed(self) -> None:
        """When send_appstart fails AND disconnect also fails, no secondary exception."""
        mock_mc, mock_inst = build_mock_meshcore_module()
        mock_inst.commands.send_appstart.side_effect = RuntimeError("appstart boom")
        # disconnect also raises — should be suppressed.
        mock_inst.disconnect.side_effect = OSError("socket closed")

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "appstart-dc-err-test")

        with (
            patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
            patch.dict(sys.modules, {"meshcore": mock_mc}),
        ):
            with pytest.raises(MeshCoreConnectionError, match="send_appstart failed"):
                await session.start(lambda pkt: None)

        # Despite disconnect error, cleanup still occurs.
        assert session._meshcore is None
        assert session.connected is False

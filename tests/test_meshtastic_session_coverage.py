"""Tests for MeshtasticSession: get_node_info, _create_client branches, _refresh_node_id, lazy node_id refresh.

Covers uncovered lines in session.py:
- get_node_info (lines 229-249): node lookup via SDK client.nodes dict
- _create_client (lines 646-676): TCP, serial, BLE, and unsupported types
- _refresh_node_id (lines 730-742): populate _node_id from interface.myInfo.myNodeNum
- _on_receive lazy refresh: late myInfo activates self-echo without reconnect
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from medre.adapters.meshtastic.errors import MeshtasticConnectionError
from medre.adapters.meshtastic.session import MeshtasticSession
from medre.config.adapters.meshtastic import MeshtasticConfig


def _make_session(
    config: MeshtasticConfig | None = None,
    client: Any = None,
) -> MeshtasticSession:
    """Build a MeshtasticSession with optional pre-set _client."""
    if config is None:
        config = MeshtasticConfig(adapter_id="mesh-1")
    session = MeshtasticSession(config, adapter_id="mesh-1", platform="meshtastic")
    if client is not None:
        session._client = client
    return session


# ===================================================================
# get_node_info
# ===================================================================


class TestGetNodeInfo:
    """MeshtasticSession.get_node_info node lookup edge cases."""

    def test_client_none_returns_none(self) -> None:
        session = _make_session(client=None)
        assert session.get_node_info("!abc123") is None

    def test_client_nodes_not_dict_returns_none(self) -> None:
        client = MagicMock()
        client.nodes = "not a dict"
        session = _make_session(client=client)
        assert session.get_node_info("!abc123") is None

    def test_client_nodes_missing_returns_none(self) -> None:
        """Client with no 'nodes' attribute (getattr returns None)."""
        client = MagicMock(spec=[])  # no attributes
        session = _make_session(client=client)
        assert session.get_node_info("!abc123") is None

    def test_node_id_not_in_nodes_returns_none(self) -> None:
        client = MagicMock()
        client.nodes = {"!other": {}}
        session = _make_session(client=client)
        assert session.get_node_info("!abc123") is None

    def test_node_info_not_dict_returns_none(self) -> None:
        client = MagicMock()
        client.nodes = {"!abc123": "not a dict"}
        session = _make_session(client=client)
        assert session.get_node_info("!abc123") is None

    def test_user_info_not_dict_returns_none(self) -> None:
        client = MagicMock()
        client.nodes = {"!abc123": {"user": "not a dict"}}
        session = _make_session(client=client)
        assert session.get_node_info("!abc123") is None

    def test_user_info_missing_returns_none(self) -> None:
        """Node info without a 'user' key → None."""
        client = MagicMock()
        client.nodes = {"!abc123": {"hopLimit": 3}}
        session = _make_session(client=client)
        assert session.get_node_info("!abc123") is None

    def test_both_names_empty_returns_none(self) -> None:
        client = MagicMock()
        client.nodes = {"!abc123": {"user": {"longName": "", "shortName": ""}}}
        session = _make_session(client=client)
        assert session.get_node_info("!abc123") is None

    def test_only_longname_present(self) -> None:
        client = MagicMock()
        client.nodes = {"!abc123": {"user": {"longName": "LongNode", "shortName": ""}}}
        session = _make_session(client=client)
        result = session.get_node_info("!abc123")
        assert result == {"longname": "LongNode"}

    def test_only_shortname_present(self) -> None:
        client = MagicMock()
        client.nodes = {"!abc123": {"user": {"longName": "", "shortName": "SN"}}}
        session = _make_session(client=client)
        result = session.get_node_info("!abc123")
        assert result == {"shortname": "SN"}

    def test_both_names_present(self) -> None:
        client = MagicMock()
        client.nodes = {
            "!abc123": {"user": {"longName": "LongNode", "shortName": "SN"}}
        }
        session = _make_session(client=client)
        result = session.get_node_info("!abc123")
        assert result == {"longname": "LongNode", "shortname": "SN"}

    def test_longname_none_falsy_not_included(self) -> None:
        client = MagicMock()
        client.nodes = {"!abc123": {"user": {"longName": None, "shortName": "SN"}}}
        session = _make_session(client=client)
        result = session.get_node_info("!abc123")
        assert result == {"shortname": "SN"}

    def test_shortname_none_falsy_not_included(self) -> None:
        client = MagicMock()
        client.nodes = {
            "!abc123": {"user": {"longName": "LongNode", "shortName": None}}
        }
        session = _make_session(client=client)
        result = session.get_node_info("!abc123")
        assert result == {"longname": "LongNode"}

    def test_both_names_none_returns_none(self) -> None:
        client = MagicMock()
        client.nodes = {"!abc123": {"user": {"longName": None, "shortName": None}}}
        session = _make_session(client=client)
        assert session.get_node_info("!abc123") is None


# ===================================================================
# _create_client TCP branch
# ===================================================================


def _setup_fake_module(
    monkeypatch: pytest.MonkeyPatch, module_name: str, class_name: str, fake_class: Any
) -> None:
    """Inject a fake class into sys.modules so `from X import Y` works."""
    mod = types.ModuleType(module_name)
    setattr(mod, class_name, fake_class)
    monkeypatch.setitem(sys.modules, module_name, mod)


class TestCreateClientTcp:
    """MeshtasticSession._create_client TCP connection branch."""

    def test_tcp_with_host_and_port(self, monkeypatch) -> None:
        """TCP connection creates TCPInterface with correct hostname/port."""
        fake_tcp = MagicMock(return_value="tcp_iface")
        _setup_fake_module(
            monkeypatch, "meshtastic.tcp_interface", "TCPInterface", fake_tcp
        )
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        config = MeshtasticConfig(
            adapter_id="mesh-1", connection_type="tcp", host="192.168.1.1", port=4403
        )
        session = _make_session(config)

        result = session._create_client()
        fake_tcp.assert_called_once_with(hostname="192.168.1.1", portNumber=4403)
        assert result == "tcp_iface"

    def test_tcp_with_host_none_raises_runtime_error(self, monkeypatch) -> None:
        """TCP connection with host=None raises RuntimeError."""
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)
        config = MeshtasticConfig(adapter_id="mesh-1", connection_type="tcp")
        object.__setattr__(config, "host", None)
        session = _make_session(config)

        with pytest.raises(MeshtasticConnectionError, match="config.host must be set"):
            session._create_client()

    def test_tcp_with_port_none_defaults_to_4403(self, monkeypatch) -> None:
        """TCP connection with port=None uses default port 4403."""
        fake_tcp = MagicMock(return_value="tcp_iface")
        _setup_fake_module(
            monkeypatch, "meshtastic.tcp_interface", "TCPInterface", fake_tcp
        )
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        config = MeshtasticConfig(
            adapter_id="mesh-1", connection_type="tcp", host="10.0.0.1"
        )
        session = _make_session(config)

        session._create_client()
        fake_tcp.assert_called_once_with(hostname="10.0.0.1", portNumber=4403)


# ===================================================================
# _create_client Serial branch
# ===================================================================


class TestCreateClientSerial:
    """MeshtasticSession._create_client serial connection branch."""

    def test_serial_creates_serial_interface(self, monkeypatch) -> None:
        """Serial connection creates SerialInterface with correct devPath."""
        fake_serial = MagicMock(return_value="serial_iface")
        _setup_fake_module(
            monkeypatch, "meshtastic.serial_interface", "SerialInterface", fake_serial
        )
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        config = MeshtasticConfig(
            adapter_id="mesh-1", connection_type="serial", serial_port="/dev/ttyUSB0"
        )
        session = _make_session(config)

        result = session._create_client()
        fake_serial.assert_called_once_with(devPath="/dev/ttyUSB0")
        assert result == "serial_iface"


# ===================================================================
# _create_client BLE branch
# ===================================================================


class TestCreateClientBle:
    """MeshtasticSession._create_client BLE connection branch."""

    def test_ble_with_address_creates_ble_interface(self, monkeypatch) -> None:
        """BLE connection creates BLEInterface with correct address."""
        fake_ble = MagicMock(return_value="ble_iface")
        _setup_fake_module(
            monkeypatch, "meshtastic.ble_interface", "BLEInterface", fake_ble
        )
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        config = MeshtasticConfig(
            adapter_id="mesh-1",
            connection_type="ble",
            ble_address="AA:BB:CC:DD:EE:FF",
        )
        session = _make_session(config)

        result = session._create_client()
        fake_ble.assert_called_once_with(address="AA:BB:CC:DD:EE:FF")
        assert result == "ble_iface"

    def test_ble_with_address_none_raises_runtime_error(self, monkeypatch) -> None:
        """BLE connection with ble_address=None raises RuntimeError."""
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)
        config = MeshtasticConfig(adapter_id="mesh-1", connection_type="ble")
        object.__setattr__(config, "ble_address", None)
        session = _make_session(config)

        with pytest.raises(
            MeshtasticConnectionError, match="config.ble_address must be set"
        ):
            session._create_client()


# ===================================================================
# _create_client unsupported type
# ===================================================================


class TestCreateClientUnsupported:
    """MeshtasticSession._create_client unsupported connection_type."""

    def test_unsupported_connection_type_raises(self, monkeypatch) -> None:
        """Unsupported connection_type raises MeshtasticConnectionError."""
        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)
        config = MeshtasticConfig(adapter_id="mesh-1", connection_type="tcp")
        # Force connection_type to an unsupported value
        object.__setattr__(config, "connection_type", "usb")
        session = _make_session(config)

        with pytest.raises(MeshtasticConnectionError, match="Unsupported"):
            session._create_client()


# ===================================================================
# _refresh_node_id
# ===================================================================


class TestRefreshNodeId:
    """MeshtasticSession._refresh_node_id edge cases.

    _refresh_node_id reads client.myInfo.myNodeNum and formats it as
    "!{node_num:08x}".  It guards against missing attributes, non-int
    values, and negative node numbers.
    """

    def test_happy_path_formats_hex(self) -> None:
        """myInfo.myNodeNum = 0x12345678 → _node_id == '!12345678'."""
        my_info = MagicMock()
        my_info.myNodeNum = 0x12345678
        client = MagicMock()
        client.myInfo = my_info
        session = _make_session(client=client)

        session._refresh_node_id()

        assert session._node_id == "!12345678"

    def test_missing_my_info_attribute(self) -> None:
        """Client with no myInfo attribute → _node_id stays None."""
        client = MagicMock(spec=[])  # no attributes
        session = _make_session(client=client)

        session._refresh_node_id()

        assert session._node_id is None

    def test_my_info_none(self) -> None:
        """Client where myInfo is None → _node_id stays None."""
        client = MagicMock()
        client.myInfo = None
        session = _make_session(client=client)

        session._refresh_node_id()

        assert session._node_id is None

    def test_my_node_num_non_int(self) -> None:
        """myInfo.myNodeNum = 'garbage' → _node_id stays None."""
        my_info = MagicMock()
        my_info.myNodeNum = "garbage"
        client = MagicMock()
        client.myInfo = my_info
        session = _make_session(client=client)

        session._refresh_node_id()

        assert session._node_id is None

    def test_my_node_num_negative(self) -> None:
        """myInfo.myNodeNum = -1 → _node_id stays None."""
        my_info = MagicMock()
        my_info.myNodeNum = -1
        client = MagicMock()
        client.myInfo = my_info
        session = _make_session(client=client)

        session._refresh_node_id()

        assert session._node_id is None

    def test_my_node_num_zero(self) -> None:
        """myInfo.myNodeNum = 0 → _node_id == '!00000000'."""
        my_info = MagicMock()
        my_info.myNodeNum = 0
        client = MagicMock()
        client.myInfo = my_info
        session = _make_session(client=client)

        session._refresh_node_id()

        assert session._node_id == "!00000000"

    def test_client_none_resets_to_none(self) -> None:
        """No client → _node_id is reset to None regardless of prior value."""
        session = _make_session(client=None)
        session._node_id = "!deadbeef"

        session._refresh_node_id()

        assert session._node_id is None

    def test_refresh_overwrites_previous_value(self) -> None:
        """Subsequent call with valid client overwrites prior _node_id."""
        my_info = MagicMock()
        my_info.myNodeNum = 0xAABBCCDD
        client = MagicMock()
        client.myInfo = my_info
        session = _make_session(client=client)
        session._node_id = "!00000000"

        session._refresh_node_id()

        assert session._node_id == "!aabbccdd"


# ===================================================================
# _unsubscribe_callbacks exception path (line 725)
# ===================================================================


class TestUnsubscribeCallbacksExceptionPath:
    """_unsubscribe_callbacks swallows exceptions from pubsub import/unsub.

    When ``from pubsub import pub`` fails or ``pub.unsubscribe``
    raises, the method must not propagate the error.
    """

    def test_pubsub_import_fails_swallows_exception(self, monkeypatch) -> None:
        """Importing pubsub fails → _unsubscribe_callbacks does not raise."""
        # Make pubsub import fail by removing it from sys.modules and
        # patching the import to raise.
        config = MeshtasticConfig(adapter_id="mesh-1")
        session = _make_session(config)
        session._subscribed = True

        monkeypatch.setitem(sys.modules, "pubsub", None)

        # Must not raise
        session._unsubscribe_callbacks()
        assert session._subscribed is False

    def test_pubsub_unsubscribe_raises_swallows_exception(self, monkeypatch) -> None:
        """pub.unsubscribe raises → _unsubscribe_callbacks does not raise."""
        config = MeshtasticConfig(adapter_id="mesh-1")
        session = _make_session(config)
        session._subscribed = True

        fake_pub = MagicMock()
        fake_pub.unsubscribe.side_effect = RuntimeError("pubsub internal error")

        fake_pubsub = types.ModuleType("pubsub")
        fake_pubsub.pub = fake_pub
        monkeypatch.setitem(sys.modules, "pubsub", fake_pubsub)

        # Must not raise
        session._unsubscribe_callbacks()
        assert session._subscribed is False

    def test_not_subscribed_returns_early(self) -> None:
        """_subscribed=False → no pubsub interaction at all."""
        config = MeshtasticConfig(adapter_id="mesh-1")
        session = _make_session(config)
        session._subscribed = False

        session._unsubscribe_callbacks()
        assert session._subscribed is False


# ===================================================================
# Reconnect success path (lines 834-838)
# ===================================================================


class TestReconnectSuccessPath:
    """_reconnect_loop success resets reconnect state.

    Covers session.py lines 834-838: after a successful reconnect, the
    session resubscribes, creates a new client, refreshes node ID,
    and resets ``_reconnect_attempts``, ``_reconnecting``, ``_last_error``.
    """

    async def test_reconnect_success_resets_state(self, monkeypatch) -> None:
        """Successful reconnect resets counters and flags."""
        config = MeshtasticConfig(adapter_id="mesh-1", connection_type="tcp")
        session = _make_session(config)

        # Pre-set error state to verify it gets cleared.
        session._reconnect_attempts = 2
        session._reconnecting = True
        session._last_error = "Connection lost"

        # Mock sleep to avoid actual delays.
        async def fake_sleep(duration):
            pass

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        # Mock lifecycle methods via patch.object (class uses __slots__).
        fake_client = MagicMock()
        mock_unsub = MagicMock()
        mock_sub = MagicMock()
        mock_refresh = MagicMock()

        with (
            patch.object(type(session), "_create_client", return_value=fake_client),
            patch.object(type(session), "_unsubscribe_callbacks", mock_unsub),
            patch.object(type(session), "_subscribe_callbacks", mock_sub),
            patch.object(type(session), "_refresh_node_id", mock_refresh),
        ):
            await session._reconnect_loop()

        # Verify reconnect success state reset.
        assert session._reconnect_attempts == 0
        assert session._reconnecting is False
        assert session._last_error is None
        assert session._client is fake_client
        mock_sub.assert_called_once()
        mock_refresh.assert_called_once()

    async def test_reconnect_success_unsubscribes_first(self, monkeypatch) -> None:
        """Reconnect calls _unsubscribe_callbacks before creating new client."""
        config = MeshtasticConfig(adapter_id="mesh-1", connection_type="tcp")
        session = _make_session(config)
        session._reconnect_attempts = 0
        session._reconnecting = True

        async def fake_sleep(duration):
            pass

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        fake_client = MagicMock()
        call_order: list[str] = []

        def tracking_unsub(self):
            call_order.append("unsubscribe")

        def tracking_create(self):
            call_order.append("create_client")
            return fake_client

        def tracking_sub(self):
            call_order.append("subscribe")

        def tracking_refresh(self):
            call_order.append("refresh_node_id")

        with (
            patch.object(type(session), "_create_client", tracking_create),
            patch.object(type(session), "_unsubscribe_callbacks", tracking_unsub),
            patch.object(type(session), "_subscribe_callbacks", tracking_sub),
            patch.object(type(session), "_refresh_node_id", tracking_refresh),
        ):
            await session._reconnect_loop()

        assert call_order == [
            "unsubscribe",
            "create_client",
            "subscribe",
            "refresh_node_id",
        ]


# ===================================================================
# _on_receive lazy _node_id refresh
# ===================================================================


class TestOnReceiveLazyNodeIdRefresh:
    """_on_receive lazily refreshes _node_id when it is still None.

    When ``interface.myInfo.myNodeNum`` was not available at connect
    time, the first inbound packet should trigger a refresh so that
    self-echo detection can activate without a reconnect.
    """

    def test_lazy_refresh_when_node_id_none(self) -> None:
        """_on_receive triggers _refresh_node_id when _node_id is None."""
        my_info = MagicMock()
        my_info.myNodeNum = 0xDEADBEEF
        client = MagicMock()
        client.myInfo = my_info

        session = _make_session(client=client)
        assert session._node_id is None

        callback = MagicMock()
        session._message_callback = callback
        session._on_receive({"fromId": "!aabbccdd"})

        # _node_id must now be populated from myInfo
        assert session._node_id == "!deadbeef"
        callback.assert_called_once_with({"fromId": "!aabbccdd"})

    def test_no_refresh_when_node_id_already_set(self) -> None:
        """_on_receive skips refresh when _node_id is already populated."""
        my_info = MagicMock()
        my_info.myNodeNum = 0xDEADBEEF
        client = MagicMock()
        client.myInfo = my_info

        session = _make_session(client=client)
        session._node_id = "!alreadyset"

        callback = MagicMock()
        session._message_callback = callback
        session._on_receive({"fromId": "!aabbccdd"})

        # _node_id must remain unchanged (not overwritten by refresh)
        assert session._node_id == "!alreadyset"
        callback.assert_called_once()

    def test_no_refresh_when_client_none(self) -> None:
        """_on_receive does not call _refresh_node_id when client is None."""
        session = _make_session(client=None)
        assert session._node_id is None

        callback = MagicMock()
        session._message_callback = callback
        session._on_receive({"fromId": "!aabbccdd"})

        # _node_id stays None (no crash, no refresh attempt)
        assert session._node_id is None
        callback.assert_called_once()

    def test_lazy_refresh_still_none_if_myinfo_unavailable(self) -> None:
        """Lazy refresh attempt that finds no myInfo leaves _node_id None."""
        client = MagicMock()
        client.myInfo = None

        session = _make_session(client=client)
        assert session._node_id is None

        session._on_receive({"fromId": "!aabbccdd"})

        # Still None — but no crash either
        assert session._node_id is None

    def test_subsequent_packet_refreshes_after_becoming_available(
        self,
    ) -> None:
        """Second packet triggers successful refresh after myInfo appears."""
        client = MagicMock()
        client.myInfo = None

        session = _make_session(client=client)

        # First packet: myInfo not available yet
        session._on_receive({"fromId": "!aabbccdd"})
        assert session._node_id is None

        # myInfo becomes available between packets
        my_info = MagicMock()
        my_info.myNodeNum = 0xCAFEBABE
        client.myInfo = my_info

        # Second packet: lazy refresh succeeds
        session._on_receive({"fromId": "!aabbccdd"})
        assert session._node_id == "!cafebabe"


# ===================================================================
# Connection-lost subscription
# ===================================================================


def _patch_pubsub_for_session(
    monkeypatch: pytest.MonkeyPatch,
    subscribe_fn=None,
    unsubscribe_fn=None,
) -> dict[str, list]:
    """Patch pubsub module for session connection-lost tests.

    Returns a dict with ``subscribe_calls`` and ``unsubscribe_calls``
    lists for asserting subscription behavior.
    """
    calls: dict[str, list] = {"subscribe_calls": [], "unsubscribe_calls": []}

    def tracking_subscribe(callback, topic):
        calls["subscribe_calls"].append((callback, topic))

    def tracking_unsubscribe(callback, topic):
        calls["unsubscribe_calls"].append((callback, topic))

    fake_pubsub = types.ModuleType("pubsub")
    fake_pub = types.ModuleType("pubsub.pub")
    fake_pub.subscribe = subscribe_fn or tracking_subscribe
    fake_pub.unsubscribe = unsubscribe_fn or tracking_unsubscribe
    fake_pubsub.pub = fake_pub
    monkeypatch.setitem(sys.modules, "pubsub", fake_pubsub)
    monkeypatch.setitem(sys.modules, "pubsub.pub", fake_pub)

    return calls


class TestConnectionLostSubscription:
    """Session subscribes to meshtastic.connection.lost for automatic reconnect."""

    async def test_subscribe_callbacks_subscribes_to_connection_lost(
        self, monkeypatch
    ) -> None:
        """_subscribe_callbacks() subscribes to meshtastic.connection.lost."""
        config = MeshtasticConfig(
            adapter_id="mesh-1", connection_type="tcp", host="1.2.3.4"
        )
        session = _make_session(config)

        class FakeClient:
            def close(self):
                pass

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        def fake_create_client(session_self):
            return FakeClient()

        monkeypatch.setattr(type(session), "_create_client", fake_create_client)

        calls = _patch_pubsub_for_session(monkeypatch)

        await session.start()
        try:
            # Both meshtastic.receive and meshtastic.connection.lost subscribed
            topics = [c[1] for c in calls["subscribe_calls"]]
            assert "meshtastic.receive" in topics
            assert "meshtastic.connection.lost" in topics
            assert session._subscribed_connection_lost is True
        finally:
            await session.stop()

    async def test_connection_lost_subscription_failure_non_fatal(
        self, monkeypatch
    ) -> None:
        """Failure to subscribe to connection.lost is non-fatal (session still usable)."""
        config = MeshtasticConfig(
            adapter_id="mesh-1", connection_type="tcp", host="1.2.3.4"
        )
        session = _make_session(config)

        class FakeClient:
            def close(self):
                pass

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        def fake_create_client(session_self):
            return FakeClient()

        monkeypatch.setattr(type(session), "_create_client", fake_create_client)

        subscribe_call_count = {"count": 0}

        def selective_subscribe(callback, topic):
            subscribe_call_count["count"] += 1
            if topic == "meshtastic.connection.lost":
                raise RuntimeError("connection.lost topic unavailable")

        _patch_pubsub_for_session(monkeypatch, subscribe_fn=selective_subscribe)

        # Should NOT raise — connection.lost failure is non-fatal
        await session.start()
        try:
            assert session._started is True
            assert session._subscribed is True
            assert session._subscribed_connection_lost is False
        finally:
            await session.stop()

    async def test_unsubscribe_cleans_up_connection_lost(self, monkeypatch) -> None:
        """stop() unsubscribes from meshtastic.connection.lost."""
        config = MeshtasticConfig(
            adapter_id="mesh-1", connection_type="tcp", host="1.2.3.4"
        )
        session = _make_session(config)

        class FakeClient:
            def close(self):
                pass

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        def fake_create_client(session_self):
            return FakeClient()

        monkeypatch.setattr(type(session), "_create_client", fake_create_client)

        calls = _patch_pubsub_for_session(monkeypatch)

        await session.start()
        assert session._subscribed_connection_lost is True
        await session.stop()

        # Check unsubscribe was called for connection.lost
        unsub_topics = [c[1] for c in calls["unsubscribe_calls"]]
        assert "meshtastic.connection.lost" in unsub_topics
        assert session._subscribed_connection_lost is False

    async def test_on_connection_lost_guarded_after_stop(self, monkeypatch) -> None:
        """_on_connection_lost is a no-op after stop() sets _stop_requested."""
        config = MeshtasticConfig(
            adapter_id="mesh-1", connection_type="tcp", host="1.2.3.4"
        )
        session = _make_session(config)

        class FakeClient:
            def close(self):
                pass

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        def fake_create_client(session_self):
            return FakeClient()

        monkeypatch.setattr(type(session), "_create_client", fake_create_client)

        _patch_pubsub_for_session(monkeypatch)

        await session.start()
        await session.stop()

        # Simulate a late connection-lost event arriving after stop.
        # Patch notify_connection_lost on the class (session uses __slots__
        # so instance attribute assignment is not allowed).
        notify_called = {"called": False}

        def tracking_notify(_self):
            notify_called["called"] = True

        with patch.object(type(session), "notify_connection_lost", tracking_notify):
            session._on_connection_lost()

        # notify_connection_lost must NOT have been called (guarded by
        # _stop_requested or not _started)
        assert notify_called["called"] is False

    async def test_on_connection_lost_delegates_to_notify(self, monkeypatch) -> None:
        """_on_connection_lost calls notify_connection_lost when started."""
        config = MeshtasticConfig(
            adapter_id="mesh-1", connection_type="tcp", host="1.2.3.4"
        )
        session = _make_session(config)

        class FakeClient:
            def close(self):
                pass

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        def fake_create_client(session_self):
            return FakeClient()

        monkeypatch.setattr(type(session), "_create_client", fake_create_client)

        _patch_pubsub_for_session(monkeypatch)

        await session.start()
        try:
            notify_called = {"called": False}

            def tracking_notify(_self):
                notify_called["called"] = True

            with patch.object(type(session), "notify_connection_lost", tracking_notify):
                session._on_connection_lost()

            assert notify_called["called"] is True
        finally:
            await session.stop()

    async def test_on_connection_lost_ignores_when_not_started(self) -> None:
        """_on_connection_lost is a no-op when _started is False."""
        config = MeshtasticConfig(adapter_id="mesh-1")
        session = _make_session(config)
        assert session._started is False

        notify_called = {"called": False}

        def tracking_notify(_self):
            notify_called["called"] = True

        with patch.object(type(session), "notify_connection_lost", tracking_notify):
            session._on_connection_lost()

        assert notify_called["called"] is False


class TestNotifyConnectionLostThreadSafety:
    """notify_connection_lost uses call_soon_threadsafe for cross-thread scheduling."""

    async def test_notify_connection_lost_schedules_via_loop(self, monkeypatch) -> None:
        """notify_connection_lost uses loop.call_soon_threadsafe to schedule reconnect."""
        config = MeshtasticConfig(
            adapter_id="mesh-1", connection_type="tcp", host="1.2.3.4"
        )
        session = _make_session(config)

        class FakeClient:
            def close(self):
                pass

        monkeypatch.setattr("medre.adapters.meshtastic.session.HAS_MESHTASTIC", True)

        def fake_create_client(session_self):
            return FakeClient()

        monkeypatch.setattr(type(session), "_create_client", fake_create_client)

        _patch_pubsub_for_session(monkeypatch)

        await session.start()
        try:
            scheduled = {"called": False}
            loop = session._loop
            assert loop is not None

            original_call_ssoon = loop.call_soon_threadsafe

            def tracking_call_ssoon(callback, *args):
                scheduled["called"] = True
                original_call_ssoon(callback, *args)

            loop.call_soon_threadsafe = tracking_call_ssoon  # type: ignore[assignment]

            session.notify_connection_lost()

            assert scheduled["called"] is True
            assert session._last_error == "Connection lost"
        finally:
            # Ensure reconnect task doesn't run (it would fail without
            # a real client to reconnect)
            session._stop_requested = True
            if (
                session._reconnect_task is not None
                and not session._reconnect_task.done()
            ):
                session._reconnect_task.cancel()
                try:
                    await asyncio.wait_for(session._reconnect_task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            await session.stop()

    async def test_notify_connection_lost_noop_when_no_loop(self) -> None:
        """notify_connection_lost is a no-op when _loop is None."""
        config = MeshtasticConfig(adapter_id="mesh-1")
        session = _make_session(config)
        session._loop = None
        session._started = True

        # Should not raise
        session.notify_connection_lost()
        assert session._last_error == "Connection lost"

    async def test_notify_connection_lost_noop_when_already_reconnecting(
        self,
    ) -> None:
        """notify_connection_lost is a no-op when already reconnecting."""
        config = MeshtasticConfig(adapter_id="mesh-1")
        session = _make_session(config)
        session._reconnecting = True

        # Should not schedule a reconnect
        session.notify_connection_lost()
        assert session._reconnect_task is None

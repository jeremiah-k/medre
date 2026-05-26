"""Tests for MeshtasticSession: get_node_info, _create_client branches.

Covers uncovered lines in session.py:
- get_node_info (lines 229-249): node lookup via SDK client.nodes dict
- _create_client (lines 646-676): TCP, serial, BLE, and unsupported types
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

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
        # Must set up fake module so the `from meshtastic.tcp_interface import TCPInterface`
        # succeeds; only then does the host=None guard trigger.
        _setup_fake_module(
            monkeypatch, "meshtastic.tcp_interface", "TCPInterface", MagicMock()
        )
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
        # Must set up fake module so the `from meshtastic.ble_interface import BLEInterface`
        # succeeds; only then does the ble_address=None guard trigger.
        _setup_fake_module(
            monkeypatch, "meshtastic.ble_interface", "BLEInterface", MagicMock()
        )
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

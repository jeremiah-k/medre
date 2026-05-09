"""Tests for MeshCoreConfig: valid/invalid configuration, validation
chaining, and edge cases.
"""

from __future__ import annotations

import pytest

from medre.adapters.meshcore.config import MeshCoreConfig
from medre.adapters.meshcore.errors import MeshCoreConfigError


class TestMeshCoreConfigValid:
    """Valid MeshCoreConfig cases."""

    def test_minimal_valid_config(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1")
        result = config.validate()
        assert result is config

    def test_all_fields_valid(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-2",
            connection_type="tcp",
            host="192.168.1.100",
            port=4403,
            default_channel=1,
            message_delay_seconds=1.0,
            meshnet_name="testnet",
        )
        result = config.validate()
        assert result.adapter_id == "meshcore-2"
        assert result.connection_type == "tcp"
        assert result.host == "192.168.1.100"

    def test_fake_connection_type(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", connection_type="fake")
        assert config.validate() is config

    def test_serial_connection_type(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            connection_type="serial",
            serial_port="/dev/ttyUSB0",
        )
        assert config.validate() is config

    def test_ble_connection_type(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            connection_type="ble",
        )
        assert config.validate() is config

    def test_default_values(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1")
        assert config.connection_type == "fake"
        assert config.default_channel == 0
        assert config.message_delay_seconds == 0.5
        assert config.sync_timeout_ms == 30000
        assert config.channel_mapping == {}

    def test_validate_returns_self_for_chaining(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1")
        assert config.validate() is config


class TestMeshCoreConfigInvalid:
    """Invalid MeshCoreConfig cases."""

    def test_empty_adapter_id_raises(self) -> None:
        config = MeshCoreConfig(adapter_id="")
        with pytest.raises(MeshCoreConfigError, match="adapter_id"):
            config.validate()

    def test_invalid_connection_type_raises(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", connection_type="wifi")
        with pytest.raises(MeshCoreConfigError, match="connection_type"):
            config.validate()

    def test_negative_message_delay_raises(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", message_delay_seconds=-1.0)
        with pytest.raises(MeshCoreConfigError, match="message_delay_seconds"):
            config.validate()

    def test_zero_message_delay_is_valid(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", message_delay_seconds=0.0)
        assert config.validate() is config

    def test_negative_default_channel_raises(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", default_channel=-1)
        with pytest.raises(MeshCoreConfigError, match="default_channel"):
            config.validate()

    def test_tcp_without_host_raises(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            connection_type="tcp",
        )
        with pytest.raises(MeshCoreConfigError, match="host"):
            config.validate()

    def test_tcp_with_host_is_valid(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            connection_type="tcp",
            host="192.168.1.100",
        )
        assert config.validate() is config

    def test_config_error_is_also_value_error(self) -> None:
        config = MeshCoreConfig(adapter_id="")
        with pytest.raises(ValueError):
            config.validate()

    def test_config_error_is_meshcore_error(self) -> None:
        from medre.adapters.meshcore.errors import MeshCoreError
        config = MeshCoreConfig(adapter_id="")
        with pytest.raises(MeshCoreError):
            config.validate()

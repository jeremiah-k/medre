"""Tests for MeshCoreConfig: valid/invalid configuration, validation
chaining, non-fake validation, identity/pubkey fields, secret guard,
and edge cases.
"""

from __future__ import annotations

import pytest

from medre.config.adapters.errors import MeshCoreConfigError
from medre.config.adapters.meshcore import MeshCoreConfig


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
            port=4000,
            default_channel=1,
            message_delay_seconds=1.0,
            meshnet_name="testnet",
            identity="node-alpha",
            pubkey="aabbccdd",
            node_config={"freq": 868.0},
        )
        result = config.validate()
        assert result.adapter_id == "meshcore-2"
        assert result.connection_type == "tcp"
        assert result.host == "192.168.1.100"
        assert result.identity == "node-alpha"
        assert result.pubkey == "aabbccdd"

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
            ble_address="AA:BB:CC:DD:EE:FF",
        )
        assert config.validate() is config

    def test_default_values(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1")
        assert config.connection_type == "fake"
        assert config.default_channel == 0
        assert config.message_delay_seconds == 0.5
        assert config.identity is None
        assert config.pubkey is None
        assert config.node_config == {}
        assert config.max_text_bytes == 512

    def test_validate_returns_self_for_chaining(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1")
        assert config.validate() is config

    def test_identity_optional(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", identity="my-node")
        assert config.validate().identity == "my-node"

    def test_pubkey_optional_valid_hex(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", pubkey="deadbeef42")
        assert config.validate().pubkey == "deadbeef42"

    def test_pubkey_uppercase_hex(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", pubkey="AABBCC")
        assert config.validate().pubkey == "AABBCC"

    def test_node_config_allowed_keys(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            node_config={"freq": 868.0, "region": "eu"},
        )
        assert config.validate().node_config["freq"] == 868.0


class TestMeshCoreConfigInvalid:
    """Invalid MeshCoreConfig cases."""

    def test_empty_adapter_id_raises(self) -> None:
        config = MeshCoreConfig(adapter_id="")
        with pytest.raises(MeshCoreConfigError, match="adapter_id"):
            config.validate()

    def test_invalid_connection_type_raises(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", connection_type="wifi")  # type: ignore[arg-type]
        with pytest.raises(MeshCoreConfigError, match="connection_type"):
            config.validate()

    def test_negative_message_delay_raises(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", message_delay_seconds=-1.0)
        with pytest.raises(MeshCoreConfigError, match="message_delay_seconds"):
            config.validate()

    def test_zero_message_delay_is_valid(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", message_delay_seconds=0.0)
        assert config.validate() is config

    def test_nan_message_delay_raises(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1", message_delay_seconds=float("nan")
        )
        with pytest.raises(
            MeshCoreConfigError, match="message_delay_seconds must be finite"
        ):
            config.validate()

    def test_inf_message_delay_raises(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1", message_delay_seconds=float("inf")
        )
        with pytest.raises(
            MeshCoreConfigError, match="message_delay_seconds must be finite"
        ):
            config.validate()

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

    def test_serial_without_serial_port_raises(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            connection_type="serial",
        )
        with pytest.raises(MeshCoreConfigError, match="serial_port"):
            config.validate()

    def test_ble_without_ble_address_raises(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            connection_type="ble",
        )
        with pytest.raises(MeshCoreConfigError, match="ble_address"):
            config.validate()

    def test_config_error_is_also_value_error(self) -> None:
        config = MeshCoreConfig(adapter_id="")
        with pytest.raises(ValueError):
            config.validate()


class TestMeshCoreConfigMaxTextBytes:
    """max_text_bytes validation: type, range, and edge cases."""

    def test_default_is_512(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1")
        assert config.max_text_bytes == 512

    def test_custom_value_is_valid(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", max_text_bytes=1024)
        assert config.validate().max_text_bytes == 1024

    def test_zero_is_valid(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", max_text_bytes=0)
        assert config.validate().max_text_bytes == 0

    def test_negative_raises(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", max_text_bytes=-1)
        with pytest.raises(MeshCoreConfigError, match="max_text_bytes must be >= 0"):
            config.validate()

    def test_bool_raises(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", max_text_bytes=True)  # type: ignore[arg-type]
        with pytest.raises(
            MeshCoreConfigError, match="max_text_bytes must be an int, got bool"
        ):
            config.validate()

    def test_false_bool_raises(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", max_text_bytes=False)  # type: ignore[arg-type]
        with pytest.raises(
            MeshCoreConfigError, match="max_text_bytes must be an int, got bool"
        ):
            config.validate()

    def test_non_int_raises(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", max_text_bytes="512")  # type: ignore[arg-type]
        with pytest.raises(
            MeshCoreConfigError, match="max_text_bytes must be an int, got str"
        ):
            config.validate()

    def test_float_raises(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", max_text_bytes=512.0)  # type: ignore[arg-type]
        with pytest.raises(
            MeshCoreConfigError, match="max_text_bytes must be an int, got float"
        ):
            config.validate()


class TestMeshCoreConfigIdentity:
    """Identity / pubkey field validation."""

    def test_empty_identity_raises(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", identity="")
        with pytest.raises(MeshCoreConfigError, match="identity"):
            config.validate()

    def test_none_identity_is_valid(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", identity=None)
        assert config.validate().identity is None

    def test_empty_pubkey_raises(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", pubkey="")
        with pytest.raises(MeshCoreConfigError, match="pubkey"):
            config.validate()

    def test_non_hex_pubkey_raises(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", pubkey="xyz-!@#")
        with pytest.raises(MeshCoreConfigError, match="hexadecimal"):
            config.validate()

    def test_none_pubkey_is_valid(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1", pubkey=None)
        assert config.validate().pubkey is None


class TestMeshCoreConfigSecretGuard:
    """node_config must not contain secret keys."""

    def test_private_key_in_node_config_raises(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            node_config={"private_key": "abc"},
        )
        with pytest.raises(MeshCoreConfigError, match="secret keys"):
            config.validate()

    def test_secret_in_node_config_raises(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            node_config={"secret": "shh"},
        )
        with pytest.raises(MeshCoreConfigError, match="secret keys"):
            config.validate()

    def test_password_in_node_config_raises(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            node_config={"password": "nope"},
        )
        with pytest.raises(MeshCoreConfigError, match="secret keys"):
            config.validate()

    def test_clean_node_config_passes(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            node_config={"channel": 0, "region": "us"},
        )
        assert config.validate() is config

    def test_pubkey_is_not_considered_secret(self) -> None:
        """pubkey field is explicitly typed and allowed — it is a public key."""
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            pubkey="deadbeef",
        )
        assert config.validate().pubkey == "deadbeef"


class TestMeshCoreConfigNonFakeRequiresField:
    """Non-fake connection types clearly require their associated fields."""

    def test_tcp_requires_host(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            connection_type="tcp",
            host=None,
        )
        with pytest.raises(MeshCoreConfigError, match="host.*tcp"):
            config.validate()

    def test_serial_requires_serial_port(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            connection_type="serial",
            serial_port=None,
        )
        with pytest.raises(MeshCoreConfigError, match="serial_port.*serial"):
            config.validate()

    def test_ble_requires_ble_address(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            connection_type="ble",
            ble_address=None,
        )
        with pytest.raises(MeshCoreConfigError, match="ble_address.*ble"):
            config.validate()


class TestMeshCoreConfigSerialBaudrate:
    """serial_baudrate validation for serial connection type."""

    def test_valid_baudrate(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            connection_type="serial",
            serial_port="/dev/ttyUSB0",
            serial_baudrate=9600,
        )
        assert config.validate().serial_baudrate == 9600

    def test_default_baudrate_is_valid(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            connection_type="serial",
            serial_port="/dev/ttyUSB0",
        )
        assert config.validate().serial_baudrate == 115200

    def test_zero_baudrate_raises(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            connection_type="serial",
            serial_port="/dev/ttyUSB0",
            serial_baudrate=0,
        )
        with pytest.raises(MeshCoreConfigError, match="serial_baudrate must be > 0"):
            config.validate()

    def test_negative_baudrate_raises(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            connection_type="serial",
            serial_port="/dev/ttyUSB0",
            serial_baudrate=-1,
        )
        with pytest.raises(MeshCoreConfigError, match="serial_baudrate must be > 0"):
            config.validate()

    def test_bool_baudrate_raises(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            connection_type="serial",
            serial_port="/dev/ttyUSB0",
            serial_baudrate=True,  # type: ignore[arg-type]
        )
        with pytest.raises(
            MeshCoreConfigError, match="serial_baudrate must be an integer"
        ):
            config.validate()

    def test_float_baudrate_raises(self) -> None:
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            connection_type="serial",
            serial_port="/dev/ttyUSB0",
            serial_baudrate=9600.0,  # type: ignore[arg-type]
        )
        with pytest.raises(
            MeshCoreConfigError, match="serial_baudrate must be an integer"
        ):
            config.validate()

    def test_baudrate_not_validated_for_non_serial(self) -> None:
        """baudrate validation only applies when connection_type='serial'."""
        config = MeshCoreConfig(
            adapter_id="meshcore-1",
            connection_type="tcp",
            host="192.168.1.1",
            serial_baudrate=0,  # Invalid but not validated for tcp
        )
        assert config.validate().serial_baudrate == 0

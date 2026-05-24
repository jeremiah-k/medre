"""Tests for MeshtasticConfig: valid/invalid configuration, validation
chaining, and edge cases.
"""

from __future__ import annotations

import pytest

from medre.config.adapters.errors import MeshtasticConfigError
from medre.config.adapters.meshtastic import MeshtasticConfig


class TestMeshtasticConfigValid:
    """Valid MeshtasticConfig cases."""

    def test_minimal_valid_config(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1")
        result = config.validate()
        assert result is config

    def test_all_fields_valid(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-2",
            connection_type="tcp",
            host="192.168.1.100",
            port=4403,
            default_channel=1,
            message_delay_seconds=1.0,
            meshnet_name="testnet",
        )
        result = config.validate()
        assert result.adapter_id == "mesh-2"
        assert result.connection_type == "tcp"
        assert result.host == "192.168.1.100"

    def test_fake_connection_type(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", connection_type="fake")
        assert config.validate() is config

    def test_serial_connection_type(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1",
            connection_type="serial",
            serial_port="/dev/ttyUSB0",
        )
        assert config.validate() is config

    def test_ble_connection_type(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1",
            connection_type="ble",
            ble_address="AA:BB:CC:DD:EE:FF",
        )
        assert config.validate() is config

    def test_default_values(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1")
        assert config.connection_type == "fake"
        assert config.default_channel == 0
        assert config.message_delay_seconds == 0.5
        assert config.sync_timeout_ms == 30000
        assert config.channel_mapping == {}
        assert config.ble_address is None

    def test_validate_returns_self_for_chaining(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1")
        assert config.validate() is config

    def test_ble_address_field_stored(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1",
            connection_type="ble",
            ble_address="AA:BB:CC:DD:EE:FF",
        )
        assert config.ble_address == "AA:BB:CC:DD:EE:FF"


class TestMeshtasticConfigInvalid:
    """Invalid MeshtasticConfig cases."""

    def test_empty_adapter_id_raises(self) -> None:
        config = MeshtasticConfig(adapter_id="")
        with pytest.raises(MeshtasticConfigError, match="adapter_id"):
            config.validate()

    def test_invalid_connection_type_raises(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", connection_type="wifi")
        with pytest.raises(MeshtasticConfigError, match="connection_type"):
            config.validate()

    def test_negative_message_delay_raises(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", message_delay_seconds=-1.0)
        with pytest.raises(MeshtasticConfigError, match="message_delay_seconds"):
            config.validate()

    def test_zero_message_delay_is_valid(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", message_delay_seconds=0.0)
        assert config.validate() is config

    def test_negative_default_channel_raises(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", default_channel=-1)
        with pytest.raises(MeshtasticConfigError, match="default_channel"):
            config.validate()

    def test_tcp_without_host_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1",
            connection_type="tcp",
        )
        with pytest.raises(MeshtasticConfigError, match="host"):
            config.validate()

    def test_tcp_with_host_is_valid(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1",
            connection_type="tcp",
            host="192.168.1.100",
        )
        assert config.validate() is config

    def test_config_error_is_also_value_error(self) -> None:
        config = MeshtasticConfig(adapter_id="")
        with pytest.raises(ValueError):
            config.validate()

    def test_config_error_is_value_error(self) -> None:
        config = MeshtasticConfig(adapter_id="")
        with pytest.raises(ValueError):
            config.validate()

    def test_ble_without_address_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1",
            connection_type="ble",
        )
        with pytest.raises(MeshtasticConfigError, match="ble_address"):
            config.validate()

    def test_ble_with_address_is_valid(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1",
            connection_type="ble",
            ble_address="AA:BB:CC:DD:EE:FF",
        )
        assert config.validate() is config

    def test_serial_without_serial_port_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1",
            connection_type="serial",
        )
        with pytest.raises(MeshtasticConfigError, match="serial_port"):
            config.validate()

    def test_serial_with_blank_serial_port_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1",
            connection_type="serial",
            serial_port="",
        )
        with pytest.raises(MeshtasticConfigError, match="serial_port"):
            config.validate()

    def test_serial_with_serial_port_is_valid(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1",
            connection_type="serial",
            serial_port="/dev/ttyUSB0",
        )
        assert config.validate() is config

    # -- startup_backlog_suppress_seconds validation --

    def test_startup_backlog_default_is_valid(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1")
        assert config.startup_backlog_suppress_seconds == 5.0
        assert config.validate() is config

    def test_startup_backlog_zero_is_valid(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", startup_backlog_suppress_seconds=0
        )
        assert config.validate() is config

    def test_startup_backlog_positive_int_is_valid(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", startup_backlog_suppress_seconds=10
        )
        assert config.validate() is config

    def test_startup_backlog_positive_float_is_valid(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", startup_backlog_suppress_seconds=2.5
        )
        assert config.validate() is config

    def test_startup_backlog_bool_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", startup_backlog_suppress_seconds=True  # type: ignore[arg-type]
        )
        with pytest.raises(
            MeshtasticConfigError, match="startup_backlog_suppress_seconds"
        ):
            config.validate()

    def test_startup_backlog_false_bool_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", startup_backlog_suppress_seconds=False  # type: ignore[arg-type]
        )
        with pytest.raises(
            MeshtasticConfigError, match="startup_backlog_suppress_seconds"
        ):
            config.validate()

    def test_startup_backlog_negative_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", startup_backlog_suppress_seconds=-1.0
        )
        with pytest.raises(
            MeshtasticConfigError, match="startup_backlog_suppress_seconds"
        ):
            config.validate()

    def test_startup_backlog_string_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", startup_backlog_suppress_seconds="5"  # type: ignore[arg-type]
        )
        with pytest.raises(
            MeshtasticConfigError, match="startup_backlog_suppress_seconds"
        ):
            config.validate()

    def test_startup_backlog_none_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", startup_backlog_suppress_seconds=None  # type: ignore[arg-type]
        )
        with pytest.raises(
            MeshtasticConfigError, match="startup_backlog_suppress_seconds"
        ):
            config.validate()

    def test_startup_backlog_inf_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", startup_backlog_suppress_seconds=float("inf")
        )
        with pytest.raises(
            MeshtasticConfigError, match="startup_backlog_suppress_seconds"
        ):
            config.validate()

    def test_startup_backlog_negative_inf_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", startup_backlog_suppress_seconds=float("-inf")
        )
        with pytest.raises(
            MeshtasticConfigError, match="startup_backlog_suppress_seconds"
        ):
            config.validate()

    def test_startup_backlog_nan_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", startup_backlog_suppress_seconds=float("nan")
        )
        with pytest.raises(
            MeshtasticConfigError, match="startup_backlog_suppress_seconds"
        ):
            config.validate()

    def test_max_text_bytes_bool_raises(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", max_text_bytes=True)  # type: ignore[arg-type]
        with pytest.raises(MeshtasticConfigError, match="max_text_bytes"):
            config.validate()

    def test_max_text_bytes_string_raises(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", max_text_bytes="227")  # type: ignore[arg-type]
        with pytest.raises(MeshtasticConfigError, match="max_text_bytes"):
            config.validate()

    def test_max_text_bytes_negative_raises(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", max_text_bytes=-1)
        with pytest.raises(MeshtasticConfigError, match="max_text_bytes"):
            config.validate()

    def test_max_text_bytes_zero_is_valid(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", max_text_bytes=0)
        assert config.validate() is config

    # -- queue_send_max_attempts validation --

    def test_queue_send_max_attempts_default_is_3(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1")
        assert config.queue_send_max_attempts == 3

    def test_queue_send_max_attempts_positive_int_valid(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", queue_send_max_attempts=5)
        assert config.validate().queue_send_max_attempts == 5

    def test_queue_send_max_attempts_bool_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", queue_send_max_attempts=True  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="queue_send_max_attempts"):
            config.validate()

    def test_queue_send_max_attempts_string_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", queue_send_max_attempts="3"  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="queue_send_max_attempts"):
            config.validate()

    def test_queue_send_max_attempts_zero_raises(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", queue_send_max_attempts=0)
        with pytest.raises(MeshtasticConfigError, match="queue_send_max_attempts"):
            config.validate()

    def test_queue_send_max_attempts_negative_raises(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", queue_send_max_attempts=-1)
        with pytest.raises(MeshtasticConfigError, match="queue_send_max_attempts"):
            config.validate()

    # -- outbound_mode validation --

    def test_outbound_mode_default_is_enabled(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1")
        assert config.outbound_mode == "enabled"

    def test_outbound_mode_enabled_is_valid(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", outbound_mode="enabled")
        assert config.validate() is config
        assert config.outbound_mode == "enabled"

    def test_outbound_mode_listen_only_is_valid(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", outbound_mode="listen_only")
        assert config.validate() is config
        assert config.outbound_mode == "listen_only"

    def test_outbound_mode_invalid_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", outbound_mode="disabled"  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="outbound_mode"):
            config.validate()

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

    # -- encrypted_action validation --

    def test_encrypted_action_default_is_drop(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1")
        assert config.encrypted_action == "drop"

    def test_encrypted_action_drop_is_valid(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", encrypted_action="drop")
        assert config.validate().encrypted_action == "drop"

    def test_encrypted_action_deferred_is_valid(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", encrypted_action="deferred")
        assert config.validate().encrypted_action == "deferred"

    def test_encrypted_action_invalid_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", encrypted_action="allow"  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="encrypted_action"):
            config.validate()

    # -- chat_portnums validation --

    def test_chat_portnums_default_is_empty_frozenset(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1")
        assert config.chat_portnums == frozenset()

    def test_chat_portnums_frozenset_strings_valid(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", chat_portnums=frozenset({"1", "3"})
        )
        assert config.validate().chat_portnums == frozenset({"1", "3"})

    def test_chat_portnums_set_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", chat_portnums={"1"}  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="chat_portnums"):
            config.validate()

    def test_chat_portnums_list_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", chat_portnums=["1"]  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="chat_portnums"):
            config.validate()

    def test_chat_portnums_non_string_items_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", chat_portnums=frozenset({1, 2})  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="chat_portnums"):
            config.validate()

    # -- disabled_portnums validation --

    def test_disabled_portnums_default_is_empty_frozenset(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1")
        assert config.disabled_portnums == frozenset()

    def test_disabled_portnums_frozenset_strings_valid(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", disabled_portnums=frozenset({"66", "67"})
        )
        assert config.validate().disabled_portnums == frozenset({"66", "67"})

    def test_disabled_portnums_set_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", disabled_portnums={"66"}  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="disabled_portnums"):
            config.validate()

    def test_disabled_portnums_list_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", disabled_portnums=["66"]  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="disabled_portnums"):
            config.validate()

    def test_disabled_portnums_non_string_items_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", disabled_portnums=frozenset({66})  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="disabled_portnums"):
            config.validate()

    # -- detection_sensor_relay validation --

    def test_detection_sensor_relay_default_is_false(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1")
        assert config.detection_sensor_relay is False

    def test_detection_sensor_relay_true_is_valid(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", detection_sensor_relay=True)
        assert config.validate().detection_sensor_relay is True

    def test_detection_sensor_relay_false_is_valid(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", detection_sensor_relay=False)
        assert config.validate().detection_sensor_relay is False

    def test_detection_sensor_relay_int_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", detection_sensor_relay=1  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="detection_sensor_relay"):
            config.validate()

    def test_detection_sensor_relay_string_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", detection_sensor_relay="true"  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="detection_sensor_relay"):
            config.validate()

    # -- origin_label validation --

    def test_origin_label_default_is_empty_string(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1")
        assert config.origin_label == ""

    def test_origin_label_empty_string_is_valid(self) -> None:
        config = MeshtasticConfig(adapter_id="mesh-1", origin_label="")
        assert config.validate().origin_label == ""

    def test_origin_label_valid_string_accepted(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", origin_label="Meshtastic Node Alpha"
        )
        assert config.validate().origin_label == "Meshtastic Node Alpha"

    def test_origin_label_bool_true_rejected(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", origin_label=True  # type: ignore[arg-type]
        )
        with pytest.raises(
            MeshtasticConfigError, match="origin_label must be a str, got bool"
        ):
            config.validate()

    def test_origin_label_bool_false_rejected(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", origin_label=False  # type: ignore[arg-type]
        )
        with pytest.raises(
            MeshtasticConfigError, match="origin_label must be a str, got bool"
        ):
            config.validate()

    def test_origin_label_int_rejected(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", origin_label=42  # type: ignore[arg-type]
        )
        with pytest.raises(
            MeshtasticConfigError, match="origin_label must be a str, got int"
        ):
            config.validate()

    def test_origin_label_none_rejected(self) -> None:
        config = MeshtasticConfig(
            adapter_id="mesh-1", origin_label=None  # type: ignore[arg-type]
        )
        with pytest.raises(
            MeshtasticConfigError, match="origin_label must be a str, got NoneType"
        ):
            config.validate()

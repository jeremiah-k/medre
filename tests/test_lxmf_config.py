"""Tests for LxmfConfig: valid/invalid configuration, validation
chaining, edge cases, non-fake mode validation, identity_path,
stamp_cost, and metadata safety.
"""

from __future__ import annotations

import pytest

from medre.config.adapters.errors import LxmfConfigError
from medre.config.adapters.lxmf import LxmfConfig


class TestLxmfConfigValid:
    """Valid LxmfConfig cases."""

    def test_minimal_valid_config(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1")
        result = config.validate()
        assert result is config

    def test_fake_connection_type(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", connection_type="fake")
        assert config.validate() is config

    def test_default_values(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1")
        assert config.connection_type == "fake"
        assert config.default_delivery_method == "direct"
        assert config.default_channel == 0
        assert config.message_delay_seconds == 0.5
        assert config.identity_path is None
        assert config.stamp_cost == 8
        assert config.metadata_embedding is True

    def test_validate_returns_self_for_chaining(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1")
        assert config.validate() is config

    def test_identity_path_valid_string(self) -> None:
        config = LxmfConfig(
            adapter_id="lxmf-1",
            identity_path="/path/to/identity",
        )
        assert config.validate().identity_path == "/path/to/identity"

    def test_identity_path_none_is_valid(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", identity_path=None)
        assert config.validate().identity_path is None

    def test_stamp_cost_zero_is_valid(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", stamp_cost=0)
        assert config.validate().stamp_cost == 0

    def test_stamp_cost_positive_is_valid(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", stamp_cost=16)
        assert config.validate().stamp_cost == 16


class TestLxmfConfigConnectionType:
    """connection_type validation (shape only)."""

    def test_reticulum_is_valid_shape(self) -> None:
        """Config accepts 'reticulum' as a valid connection_type shape.

        Shape validation does not check whether the SDK is installed;
        runtime availability is LxmfAdapter.start()'s responsibility.
        """
        config = LxmfConfig(
            adapter_id="lxmf-1",
            connection_type="reticulum",
            storage_path="/tmp/medre-test-lxmf",
        )
        assert config.validate() is config

    def test_unknown_connection_type_rejected(self) -> None:
        """Unknown connection_type is rejected with clear error."""
        with pytest.raises(LxmfConfigError, match="connection_type must be one of"):
            LxmfConfig(
                adapter_id="lxmf-1",
                connection_type="carrier_pigeon",
            ).validate()


class TestLxmfConfigDeliveryMethod:
    """default_delivery_method accepts named methods."""

    def test_default_delivery_method_valid(self) -> None:
        config = LxmfConfig(
            adapter_id="lxmf-1",
            default_delivery_method="direct",
        )
        assert config.validate().default_delivery_method == "direct"

    @pytest.mark.parametrize(
        "method",
        [
            "direct",
            "opportunistic",
            "propagated",
            "paper",
        ],
    )
    def test_default_delivery_method_accepts_all(self, method: str) -> None:
        config = LxmfConfig(
            adapter_id="lxmf-1",
            default_delivery_method=method,
        )
        assert config.validate().default_delivery_method == method

    def test_invalid_delivery_method_rejected(self) -> None:
        with pytest.raises(LxmfConfigError, match="default_delivery_method"):
            LxmfConfig(
                adapter_id="lxmf-1",
                default_delivery_method="carrier_pigeon",
            ).validate()


class TestLxmfConfigIdentityPath:
    """identity_path validation."""

    def test_empty_string_identity_path_rejected(self) -> None:
        with pytest.raises(LxmfConfigError, match="identity_path must be a non-empty"):
            LxmfConfig(
                adapter_id="lxmf-1",
                identity_path="",
            ).validate()

    def test_whitespace_only_identity_path_rejected(self) -> None:
        with pytest.raises(LxmfConfigError, match="identity_path must be a non-empty"):
            LxmfConfig(
                adapter_id="lxmf-1",
                identity_path="   ",
            ).validate()


class TestLxmfConfigStampCost:
    """stamp_cost validation."""

    def test_negative_stamp_cost_raises(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", stamp_cost=-1)
        with pytest.raises(LxmfConfigError, match="stamp_cost"):
            config.validate()

    def test_positive_stamp_cost_valid(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", stamp_cost=32)
        assert config.validate().stamp_cost == 32

    def test_bool_true_stamp_cost_raises(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", stamp_cost=True)  # type: ignore[arg-type]
        with pytest.raises(
            LxmfConfigError, match="stamp_cost must be an integer, not a boolean"
        ):
            config.validate()

    def test_bool_false_stamp_cost_raises(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", stamp_cost=False)  # type: ignore[arg-type]
        with pytest.raises(
            LxmfConfigError, match="stamp_cost must be an integer, not a boolean"
        ):
            config.validate()

    def test_string_stamp_cost_raises(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", stamp_cost="8")  # type: ignore[arg-type]
        with pytest.raises(LxmfConfigError, match="stamp_cost must be an integer"):
            config.validate()


class TestLxmfConfigInvalid:
    """Other invalid LxmfConfig cases."""

    def test_empty_adapter_id_raises(self) -> None:
        config = LxmfConfig(adapter_id="")
        with pytest.raises(LxmfConfigError, match="adapter_id"):
            config.validate()

    def test_negative_message_delay_raises(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", message_delay_seconds=-1.0)
        with pytest.raises(LxmfConfigError, match="message_delay_seconds"):
            config.validate()

    def test_nan_message_delay_raises(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", message_delay_seconds=float("nan"))
        with pytest.raises(
            LxmfConfigError, match="message_delay_seconds must be finite"
        ):
            config.validate()

    def test_inf_message_delay_raises(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", message_delay_seconds=float("inf"))
        with pytest.raises(
            LxmfConfigError, match="message_delay_seconds must be finite"
        ):
            config.validate()

    def test_zero_message_delay_is_valid(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", message_delay_seconds=0.0)
        assert config.validate() is config

    def test_negative_default_channel_raises(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", default_channel=-1)
        with pytest.raises(LxmfConfigError, match="default_channel"):
            config.validate()

    def test_config_error_is_also_value_error(self) -> None:
        config = LxmfConfig(adapter_id="")
        with pytest.raises(ValueError):
            config.validate()

    def test_config_error_is_value_error(self) -> None:
        config = LxmfConfig(adapter_id="")
        with pytest.raises(ValueError):
            config.validate()


class TestLxmfConfigMetadataSafety:
    """metadata_embedding remains safe — no secrets in envelopes."""

    def test_metadata_embedding_default_true(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1")
        assert config.metadata_embedding is True

    def test_metadata_embedding_can_be_disabled(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", metadata_embedding=False)
        assert config.validate().metadata_embedding is False

    def test_no_secret_fields_in_config(self) -> None:
        """Config fields do not contain private keys or secrets."""
        config = LxmfConfig(adapter_id="lxmf-1")
        # Verify no key-like field names
        for field_name in ("private_key", "secret", "password", "token"):
            assert not hasattr(
                config, field_name
            ), f"LxmfConfig must not have secret-like field: {field_name}"


class TestLxmfConfigMessageType:
    """message_delay_seconds type validation."""

    def test_bool_true_raises(self) -> None:
        config = LxmfConfig(
            adapter_id="lxmf-1",
            message_delay_seconds=True,  # type: ignore[arg-type]
        )
        with pytest.raises(
            LxmfConfigError,
            match="message_delay_seconds must be int or float, got bool",
        ):
            config.validate()

    def test_bool_false_raises(self) -> None:
        config = LxmfConfig(
            adapter_id="lxmf-1",
            message_delay_seconds=False,  # type: ignore[arg-type]
        )
        with pytest.raises(
            LxmfConfigError,
            match="message_delay_seconds must be int or float, got bool",
        ):
            config.validate()

    def test_string_raises(self) -> None:
        config = LxmfConfig(
            adapter_id="lxmf-1",
            message_delay_seconds="0",  # type: ignore[arg-type]
        )
        with pytest.raises(
            LxmfConfigError, match="message_delay_seconds must be int or float, got str"
        ):
            config.validate()

    def test_none_raises(self) -> None:
        config = LxmfConfig(
            adapter_id="lxmf-1",
            message_delay_seconds=None,  # type: ignore[arg-type]
        )
        with pytest.raises(
            LxmfConfigError,
            match="message_delay_seconds must be int or float, got NoneType",
        ):
            config.validate()

    def test_zero_int_is_valid(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", message_delay_seconds=0)
        assert config.validate().message_delay_seconds == 0

    def test_zero_float_is_valid(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", message_delay_seconds=0.0)
        assert config.validate().message_delay_seconds == 0.0

    def test_positive_int_is_valid(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", message_delay_seconds=1)
        assert config.validate().message_delay_seconds == 1

    def test_positive_float_is_valid(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", message_delay_seconds=1.5)
        assert config.validate().message_delay_seconds == 1.5


class TestLxmfConfigDefaultChannelType:
    """default_channel type validation."""

    def test_bool_true_raises(self) -> None:
        config = LxmfConfig(
            adapter_id="lxmf-1",
            default_channel=True,  # type: ignore[arg-type]
        )
        with pytest.raises(
            LxmfConfigError, match="default_channel must be an int, got bool"
        ):
            config.validate()

    def test_bool_false_raises(self) -> None:
        config = LxmfConfig(
            adapter_id="lxmf-1",
            default_channel=False,  # type: ignore[arg-type]
        )
        with pytest.raises(
            LxmfConfigError, match="default_channel must be an int, got bool"
        ):
            config.validate()

    def test_float_raises(self) -> None:
        config = LxmfConfig(
            adapter_id="lxmf-1",
            default_channel=1.5,  # type: ignore[arg-type]
        )
        with pytest.raises(
            LxmfConfigError, match="default_channel must be an int, got float"
        ):
            config.validate()

    def test_string_raises(self) -> None:
        config = LxmfConfig(
            adapter_id="lxmf-1",
            default_channel="0",  # type: ignore[arg-type]
        )
        with pytest.raises(
            LxmfConfigError, match="default_channel must be an int, got str"
        ):
            config.validate()

    def test_none_raises(self) -> None:
        config = LxmfConfig(
            adapter_id="lxmf-1",
            default_channel=None,  # type: ignore[arg-type]
        )
        with pytest.raises(
            LxmfConfigError, match="default_channel must be an int, got NoneType"
        ):
            config.validate()

    def test_zero_is_valid(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", default_channel=0)
        assert config.validate().default_channel == 0

    def test_positive_int_is_valid(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", default_channel=1)
        assert config.validate().default_channel == 1

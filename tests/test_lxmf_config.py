"""Tests for LxmfConfig: valid/invalid configuration, validation
chaining, and edge cases.
"""

from __future__ import annotations

import pytest

from medre.adapters.lxmf.config import LxmfConfig
from medre.adapters.lxmf.errors import LxmfConfigError


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

    def test_validate_returns_self_for_chaining(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1")
        assert config.validate() is config


class TestLxmfConfigConnectionType:
    """connection_type must be 'fake' in tranche 1."""

    def test_non_fake_connection_type_rejected(self) -> None:
        # Literal["fake"] means only "fake" is accepted at the type
        # level, but we also validate at runtime.
        with pytest.raises(LxmfConfigError, match="connection_type"):
            LxmfConfig(
                adapter_id="lxmf-1",
                connection_type="direct",  # type: ignore[call-arg]
            ).validate()


class TestLxmfConfigDeliveryMethod:
    """default_delivery_method accepts named methods."""

    def test_default_delivery_method_valid(self) -> None:
        config = LxmfConfig(
            adapter_id="lxmf-1",
            default_delivery_method="direct",
        )
        assert config.validate().default_delivery_method == "direct"

    @pytest.mark.parametrize("method", [
        "direct", "opportunistic", "propagated", "paper",
    ])
    def test_default_delivery_method_accepts_all(self, method: str) -> None:
        config = LxmfConfig(
            adapter_id="lxmf-1",
            default_delivery_method=method,  # type: ignore[call-arg]
        )
        assert config.validate().default_delivery_method == method

    def test_invalid_delivery_method_rejected(self) -> None:
        with pytest.raises(LxmfConfigError, match="default_delivery_method"):
            LxmfConfig(
                adapter_id="lxmf-1",
                default_delivery_method="carrier_pigeon",  # type: ignore[call-arg]
            ).validate()


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

    def test_zero_message_delay_is_valid(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", message_delay_seconds=0.0)
        assert config.validate() is config

    def test_negative_default_channel_raises(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", default_channel=-1)
        with pytest.raises(LxmfConfigError, match="default_channel"):
            config.validate()

    def test_negative_stamp_cost_raises(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1", stamp_cost=-1)
        with pytest.raises(LxmfConfigError, match="stamp_cost"):
            config.validate()

    def test_config_error_is_also_value_error(self) -> None:
        config = LxmfConfig(adapter_id="")
        with pytest.raises(ValueError):
            config.validate()

    def test_config_error_is_lxmf_error(self) -> None:
        from medre.adapters.lxmf.errors import LxmfError
        config = LxmfConfig(adapter_id="")
        with pytest.raises(LxmfError):
            config.validate()

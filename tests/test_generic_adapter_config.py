"""Transport-neutral configuration validation without installed providers."""

from dataclasses import dataclass
from typing import Self

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.model import GenericAdapterRuntimeConfig


@dataclass(frozen=True)
class SensorConfig:
    adapter_id: str
    label: str = "sensor"

    def validate(self) -> Self:
        return self


@pytest.mark.parametrize("enabled", ["false", 0, 1, None, [], {}])
def test_enabled_requires_boolean(enabled: object) -> None:
    with pytest.raises(ConfigValidationError, match="enabled must be a boolean"):
        GenericAdapterRuntimeConfig.from_transport_dict(
            "sensor",
            {"enabled": enabled},
            transport="community_sensor",
            config_cls=SensorConfig,
        )


@pytest.mark.parametrize("enabled", [False, True])
def test_boolean_enabled_is_preserved(enabled: bool) -> None:
    config = GenericAdapterRuntimeConfig.from_transport_dict(
        "sensor",
        {"enabled": enabled},
        transport="community_sensor",
        config_cls=SensorConfig,
    )
    assert config.enabled is enabled
    assert config.config.adapter_id == "sensor"

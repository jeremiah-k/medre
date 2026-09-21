"""Validation for Meshtastic runtime resilience configuration."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from medre.config.adapters.errors import MeshtasticConfigError
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.config.sample import generate_sample_config


def _config(**overrides: object) -> MeshtasticConfig:
    values: dict[str, object] = {"adapter_id": "mesh"}
    values.update(overrides)
    return MeshtasticConfig(**values)  # type: ignore[arg-type]


_RESILIENCE_DEFAULTS: dict[str, int | float] = {
    "queue_max_size": 1024,
    "queue_warning_threshold_pct": 75.0,
    "queue_critical_threshold_pct": 90.0,
    "reconnect_backoff_initial_seconds": 1.0,
    "reconnect_backoff_max_seconds": 30.0,
    "tcp_liveness_interval_seconds": 60.0,
    "tcp_liveness_timeout_seconds": 30.0,
}


def test_resilience_defaults_match_schema_example_and_sample() -> None:
    root = Path(__file__).resolve().parents[1]
    config = _config().validate()
    schema = json.loads(
        (root / "docs/schemas/adapter-config.schema.json").read_text(encoding="utf-8")
    )
    meshtastic_schema = next(
        branch
        for branch in schema["oneOf"]
        if branch.get("title") == "MeshtasticConfig"
    )
    properties = meshtastic_schema["properties"]
    example = json.loads(
        (
            root / "docs/schemas/examples/adapter-config-meshtastic-example.json"
        ).read_text(encoding="utf-8")
    )
    sample = generate_sample_config()

    for field_name, expected in _RESILIENCE_DEFAULTS.items():
        assert getattr(config, field_name) == expected
        assert properties[field_name]["default"] == expected
        assert example[field_name] == expected
        assert f"# {field_name}: {expected}" in sample


def test_resilience_defaults_are_safe_and_bounded() -> None:
    config = _config().validate()
    assert config.queue_max_size == 1024
    assert config.queue_warning_threshold_pct == 75.0
    assert config.queue_critical_threshold_pct == 90.0
    assert config.reconnect_backoff_initial_seconds == 1.0
    assert config.reconnect_backoff_max_seconds == 30.0
    assert config.tcp_liveness_interval_seconds == 60.0
    assert config.tcp_liveness_timeout_seconds == 30.0


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_queue_max_size_must_be_positive_int(value: object) -> None:
    with pytest.raises(MeshtasticConfigError, match="queue_max_size"):
        _config(queue_max_size=value).validate()


@pytest.mark.parametrize(
    ("warning", "critical"),
    [(0, 90), (75, 100), (90, 75), (75, 75)],
)
def test_queue_thresholds_are_ordered_percentages(
    warning: float,
    critical: float,
) -> None:
    with pytest.raises(MeshtasticConfigError, match="queue_.*threshold"):
        _config(
            queue_warning_threshold_pct=warning,
            queue_critical_threshold_pct=critical,
        ).validate()


@pytest.mark.parametrize(
    "field_name",
    [
        "queue_warning_threshold_pct",
        "queue_critical_threshold_pct",
        "reconnect_backoff_initial_seconds",
        "reconnect_backoff_max_seconds",
        "tcp_liveness_interval_seconds",
        "tcp_liveness_timeout_seconds",
    ],
)
def test_resilience_float_values_must_be_finite(field_name: str) -> None:
    with pytest.raises(MeshtasticConfigError, match=field_name):
        _config(**{field_name: math.inf}).validate()


def test_reconnect_backoff_cap_must_cover_initial_delay() -> None:
    with pytest.raises(MeshtasticConfigError, match="reconnect_backoff_max_seconds"):
        _config(
            reconnect_backoff_initial_seconds=10,
            reconnect_backoff_max_seconds=5,
        ).validate()


def test_zero_tcp_liveness_interval_explicitly_disables_probe() -> None:
    assert (
        _config(tcp_liveness_interval_seconds=0)
        .validate()
        .tcp_liveness_interval_seconds
        == 0
    )


def test_tcp_liveness_timeout_must_be_positive() -> None:
    with pytest.raises(MeshtasticConfigError, match="tcp_liveness_timeout_seconds"):
        _config(tcp_liveness_timeout_seconds=0).validate()

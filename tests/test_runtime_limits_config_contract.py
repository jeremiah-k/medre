# medre-test-tier: core
"""Keep YAML, schema, runtime limits, and admission capacity in agreement."""

from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path

import pytest
from medre.config.errors import ConfigValidationError
from medre.config.loader import _RUNTIME_LIMITS_KNOWN_KEYS, load_config
from medre.config.model import RuntimeLimits
from medre.core.supervision.capacity import CapacityController

_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA = _ROOT / "docs/schemas/runtime-config.schema.json"


def test_every_runtime_limit_is_configurable_and_in_schema() -> None:
    fields = {field.name for field in dataclasses.fields(RuntimeLimits)}
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    properties = schema["properties"]["runtime"]["properties"]["limits"]["properties"]
    assert fields == _RUNTIME_LIMITS_KNOWN_KEYS == set(properties)
    for field in dataclasses.fields(RuntimeLimits):
        assert properties[field.name]["default"] == field.default


def test_inbound_admission_limits_reach_capacity_controller(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "runtime:\n"
        "  limits:\n"
        "    max_inflight_inbound_admissions: 7\n"
        "    inbound_admission_timeout_seconds: 0.25\n",
        encoding="utf-8",
    )
    config, _, _ = load_config(path)
    assert config.limits.max_inflight_inbound_admissions == 7
    assert config.limits.inbound_admission_timeout_seconds == 0.25
    capacity = CapacityController(config.limits)
    assert capacity.inbound_limit == 7
    assert capacity._inbound_timeout == 0.25


@pytest.mark.parametrize("name", [
    "max_inflight_deliveries", "max_inflight_replay_events",
    "max_inflight_inbound_admissions",
])
@pytest.mark.parametrize("value", [True, False, 0, -1, "5", 0.5, None])
def test_concurrency_bounds_reject_invalid_values(name: str, value: object) -> None:
    with pytest.raises(ConfigValidationError, match=name):
        RuntimeLimits(**{name: value}).validate()


@pytest.mark.parametrize("name", [
    "shutdown_drain_timeout_seconds", "delivery_acquire_timeout_seconds",
    "inbound_admission_timeout_seconds",
])
@pytest.mark.parametrize(
    "value", [True, False, 0, -0.1, float("nan"), float("inf"), float("-inf"), "5", None]
)
def test_timeout_bounds_reject_invalid_values(name: str, value: object) -> None:
    with pytest.raises(ConfigValidationError, match=name):
        RuntimeLimits(**{name: value}).validate()


@pytest.mark.parametrize("value", [False, "bad", float('nan'), float('inf'), -1])
def test_invalid_yaml_limits_raise_config_error(tmp_path: Path, value: object) -> None:
    # Use YAML scalars directly so the operator-facing load path is tested.
    encoded = {False: "false", "bad": "bad", -1: "-1"}.get(value)
    if encoded is None:
        encoded = ".nan" if isinstance(value, float) and math.isnan(value) else ".inf"
    path = tmp_path / "config.yaml"
    path.write_text(
        "runtime:\n  limits:\n    inbound_admission_timeout_seconds: " + encoded + "\n",
        encoding="utf-8",
    )
    with pytest.raises(
        ConfigValidationError, match="inbound_admission_timeout_seconds"
    ):
        load_config(path)


@pytest.mark.parametrize("value", [True, 0, -1, 0.5, "10", None])
def test_invalid_runtime_shutdown_timeout_is_rejected(
    tmp_path: Path, value: object
) -> None:
    from medre.config.model import RuntimeOptions

    with pytest.raises(ConfigValidationError, match="runtime.shutdown_timeout_seconds"):
        RuntimeOptions(shutdown_timeout_seconds=value).validate()


@pytest.mark.parametrize("value", ["", "   ", None, 42])
def test_invalid_runtime_name_is_rejected(value: object) -> None:
    from medre.config.model import RuntimeOptions

    with pytest.raises(ConfigValidationError, match="runtime.name"):
        RuntimeOptions(name=value).validate()


@pytest.mark.parametrize("yaml_value", ["false", "-1", "0", "0.5", "abc"])
def test_invalid_yaml_shutdown_timeout_is_rejected(
    tmp_path: Path, yaml_value: str
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"runtime:\n  shutdown_timeout_seconds: {yaml_value}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigValidationError, match="runtime.shutdown_timeout_seconds"):
        load_config(path)


def test_builder_rejects_invalid_programmatic_limits(tmp_path: Path) -> None:
    from medre.config.model import RuntimeConfig
    from medre.config.paths import resolve
    from medre.runtime.builder import RuntimeBuilder
    from medre.runtime.errors import RuntimeConfigError

    config = RuntimeConfig(
        limits=RuntimeLimits(inbound_admission_timeout_seconds=float("nan")),
    )
    with pytest.raises(RuntimeConfigError, match="inbound_admission_timeout_seconds"):
        RuntimeBuilder(config, resolve()).build()


def test_builder_rejects_invalid_programmatic_runtime_name() -> None:
    from medre.config.model import RuntimeConfig, RuntimeOptions
    from medre.config.paths import resolve
    from medre.runtime.builder import RuntimeBuilder
    from medre.runtime.errors import RuntimeConfigError

    config = RuntimeConfig(runtime=RuntimeOptions(name=" "))
    with pytest.raises(RuntimeConfigError, match="runtime.name"):
        RuntimeBuilder(config, resolve()).build()


def test_fractional_drain_timeout_allowed_by_yaml_and_schema(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "runtime:\n  limits:\n    shutdown_drain_timeout_seconds: 0.25\n",
        encoding="utf-8",
    )
    config, _, _ = load_config(path)
    assert config.limits.shutdown_drain_timeout_seconds == 0.25
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    assert (
        schema["properties"]["runtime"]["properties"]["limits"]["properties"]
        ["shutdown_drain_timeout_seconds"]["type"] == "number"
    )

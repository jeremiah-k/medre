"""Fail-closed route/retry configuration boundaries, including direct construction."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from medre.config.errors import ConfigValidationError
from medre.config.loader import load_config
from medre.config.model import RetryConfig, RuntimeConfig
from medre.config.routes import BridgePolicy, RouteConfig, RouteRetryConfig
from medre.runtime.builder import RuntimeBuilder


@pytest.mark.parametrize("route_id", ["relay\n", "relay\r\n"])
def test_route_ids_reject_trailing_newlines_in_model_and_editor_schema(route_id):
    values = {"source_adapters": ["source"], "dest_adapters": ["target"]}
    with pytest.raises(ConfigValidationError, match="Invalid route ID"):
        RouteConfig.from_dict(route_id, values)
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "docs/schemas/runtime-config.schema.json").read_text())
    assert list(
        Draft202012Validator(schema).iter_errors({"routes": {route_id: values}})
    )


def _route(**overrides: object) -> dict[str, object]:
    return {
        "source_adapters": ["source"],
        "dest_adapters": ["target"],
        **overrides,
    }


@pytest.mark.parametrize("enabled", ["false", "true", 0, 1, None, [], {}])
def test_route_enabled_requires_boolean_when_parsed(enabled: object) -> None:
    with pytest.raises(ConfigValidationError, match="enabled.*boolean"):
        RouteConfig.from_dict("relay", _route(enabled=enabled))


@pytest.mark.parametrize("enabled", ["false", 0, 1, None])
def test_route_enabled_requires_boolean_when_constructed(enabled: object) -> None:
    with pytest.raises(ConfigValidationError, match="enabled.*boolean"):
        RouteConfig("relay", ("source",), ("target",), enabled=enabled)


def test_real_boolean_disabled_route_remains_disabled() -> None:
    route = RouteConfig.from_dict("relay", _route(enabled=False))
    assert route.enabled is False


@pytest.mark.parametrize("source", [[True], [0], [None], [""], [" source "]])
def test_route_rejects_nonstrings_and_unnormalized_source_ids(
    source: list[object],
) -> None:
    with pytest.raises(ConfigValidationError, match="source_adapters.*normalized"):
        RouteConfig.from_dict("relay", _route(source_adapters=source))


@pytest.mark.parametrize("target", [[True], [1], [None], [""], [" target "]])
def test_route_rejects_nonstrings_and_unnormalized_target_ids(
    target: list[object],
) -> None:
    with pytest.raises(ConfigValidationError, match="dest_adapters.*normalized"):
        RouteConfig.from_dict("relay", _route(dest_adapters=target))


@pytest.mark.parametrize("name", ["source_adapters", "dest_adapters"])
def test_route_constructor_rejects_nonstring_ids(name: str) -> None:
    values = {"source_adapters": ("source",), "dest_adapters": ("target",)}
    values[name] = (42,)
    with pytest.raises(ConfigValidationError, match="normalized"):
        RouteConfig(route_id="relay", **values)


@pytest.mark.parametrize("section", ["policy", "retry", "context_map"])
def test_explicit_null_route_control_blocks_are_rejected(section: str) -> None:
    with pytest.raises(ConfigValidationError, match="must be a mapping, not null"):
        RouteConfig.from_dict("relay", _route(**{section: None}))


@pytest.mark.parametrize("field", BridgePolicy._KNOWN_FIELDS)
def test_explicit_null_policy_allowlist_is_a_config_error(field: str) -> None:
    with pytest.raises(ConfigValidationError, match="must be a list, not null"):
        BridgePolicy.from_dict(
            {field: None}, route_id="relay", section_path="routes.relay"
        )
    assert BridgePolicy.from_dict({}) == BridgePolicy()


@pytest.mark.parametrize("field", ["backoff_base", "max_delay_seconds"])
@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_route_retry_rejects_nonfinite_durations(field: str, bad: float) -> None:
    with pytest.raises(ConfigValidationError, match="finite"):
        RouteRetryConfig.from_dict(
            {field: bad}, route_id="relay", section_path="routes.relay"
        )
    with pytest.raises(ConfigValidationError, match="finite"):
        RouteRetryConfig(**{field: bad})


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, True, "5", 0, -1])
def test_global_retry_rejects_invalid_intervals_for_direct_construction(
    bad: object,
) -> None:
    with pytest.raises(ConfigValidationError, match="retry.interval_seconds"):
        RetryConfig(interval_seconds=bad).validate()


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_yaml_global_retry_rejects_nonfinite_interval(tmp_path, bad: float) -> None:
    path = tmp_path / "config.yaml"
    encoded = "-.inf" if bad == -math.inf else (".nan" if math.isnan(bad) else ".inf")
    path.write_text(f"retry:\n  interval_seconds: {encoded}\n", encoding="utf-8")
    with pytest.raises(ConfigValidationError, match="finite"):
        load_config(str(path))


def test_builder_rejects_invalid_programmatic_retry_config(tmp_path) -> None:
    from medre.config.paths import resolve
    from medre.runtime.errors import RuntimeConfigError

    config = RuntimeConfig(retry=RetryConfig(interval_seconds=float("nan")))
    with pytest.raises(RuntimeConfigError, match="retry.interval_seconds"):
        RuntimeBuilder(config, resolve(environ={"MEDRE_HOME": str(tmp_path)})).build()

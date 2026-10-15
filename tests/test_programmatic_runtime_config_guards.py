"""Direct RuntimeConfig construction must enforce YAML-equivalent safety checks."""

from dataclasses import replace

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.model import (
    AdapterConfigSet,
    AttachmentConfig,
    GenericAdapterRuntimeConfig,
    RuntimeConfig,
)
from medre.config.paths import resolve
from medre.config.routes import RouteConfig, RouteConfigSet
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeConfigError


@pytest.mark.parametrize("enabled", ["false", 0, 1, None])
def test_programmatic_adapter_enabled_cannot_coerce_to_truthy(
    monkeypatch: pytest.MonkeyPatch, enabled: object
) -> None:
    monkeypatch.setattr("medre.config.model.registered_transports", lambda: ("sensor",))
    rtc = GenericAdapterRuntimeConfig(adapter_id="sensor", enabled=enabled)
    adapters = AdapterConfigSet(sensor={"sensor": rtc})
    with pytest.raises(ConfigValidationError, match="enabled must be a boolean"):
        adapters.validate()
    with pytest.raises(RuntimeConfigError, match="enabled must be a boolean"):
        RuntimeBuilder(RuntimeConfig(adapters=adapters), resolve()).build()


@pytest.mark.parametrize("kind", ["disabled", "", None, True])
def test_programmatic_adapter_kind_cannot_fall_through(
    monkeypatch: pytest.MonkeyPatch, kind: object
) -> None:
    monkeypatch.setattr("medre.config.model.registered_transports", lambda: ("sensor",))
    adapters = AdapterConfigSet(
        sensor={
            "sensor": GenericAdapterRuntimeConfig(
                adapter_id="sensor", adapter_kind=kind
            )
        }
    )
    with pytest.raises(RuntimeConfigError, match="adapter_kind must be"):
        RuntimeBuilder(RuntimeConfig(adapters=adapters), resolve()).build()


def test_programmatic_adapter_id_collision_rejected_before_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("medre.config.model.registered_transports", lambda: ("sensor",))
    adapters = AdapterConfigSet(
        sensor={
            "one": GenericAdapterRuntimeConfig(adapter_id="dup"),
            "two": GenericAdapterRuntimeConfig(adapter_id="dup"),
        }
    )
    with pytest.raises(RuntimeConfigError, match="Duplicate adapter"):
        RuntimeBuilder(RuntimeConfig(adapters=adapters), resolve()).build()


def test_programmatic_routes_reject_duplicate_route_ids() -> None:
    route = RouteConfig(route_id="dup", source_adapters=("a",), dest_adapters=("b",))
    with pytest.raises(RuntimeConfigError, match="Duplicate route ID"):
        RuntimeBuilder(
            RuntimeConfig(routes=RouteConfigSet((route, replace(route)))), resolve()
        ).build()


def test_programmatic_attachment_limits_validated_even_when_disabled() -> None:
    config = RuntimeConfig(attachments=AttachmentConfig(max_retained_bytes=0))
    with pytest.raises(RuntimeConfigError, match="max_retained_bytes"):
        RuntimeBuilder(config, resolve()).build()

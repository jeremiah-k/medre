"""Discovery contracts independent of any installed transport distribution."""

from dataclasses import dataclass, replace
from importlib import metadata
from typing import Any

import pytest
from medre.adapter_registry import (
    AdapterTypeRegistry,
    _discover_installed_adapters,
    registered_transports,
)
from medre.adapter_spec import DestinationSpec, SymbolRef
from tests.helpers.adapter_spec import make_adapter_spec


@dataclass
class Provider:
    name: str = "community_sensor"
    value: str = "independent_sensor.plugin:spec"
    result: Any = None

    def load(self) -> Any:
        return lambda: (
            self.result if self.result is not None else make_adapter_spec(self.name)
        )


def test_entry_point_discovery_matches_public_registry() -> None:
    assert (
        tuple(spec.transport for spec in _discover_installed_adapters())
        == registered_transports()
    )


def test_discovery_needs_no_installed_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: ())
    assert tuple(_discover_installed_adapters()) == ()


def test_duplicate_entry_points_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = Provider()
    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: [provider, provider])
    registry = _discover_installed_adapters()
    assert not tuple(registry)
    assert len(registry.failures) == 2


def test_discovery_order_has_no_transport_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        metadata,
        "entry_points",
        lambda **kwargs: [Provider("matrix"), Provider("a_sensor"), Provider("lxmf")],
    )
    assert tuple(spec.transport for spec in _discover_installed_adapters()) == (
        "a_sensor",
        "lxmf",
        "matrix",
    )


def test_invalid_external_provider_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenProvider(Provider):
        def load(self) -> Any:
            raise ImportError("unavailable provider")

    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: [BrokenProvider()])
    registry = _discover_installed_adapters()
    assert not tuple(registry)
    assert len(registry.failures) == 1
    assert registry.failures[0].transport == "community_sensor"
    assert "ImportError" in registry.failures[0].reason


@pytest.mark.parametrize("result", [object(), replace(make_adapter_spec("wrong_name"))])
def test_invalid_provider_contract_is_rejected(
    result: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        metadata, "entry_points", lambda **kwargs: [Provider(result=result)]
    )
    registry = _discover_installed_adapters()
    assert not tuple(registry)
    assert len(registry.failures) == 1


@pytest.mark.parametrize("kind", ["channel", "Bad Kind"])
def test_registry_rejects_reserved_or_invalid_destination(kind: str) -> None:
    destination = DestinationSpec(kind, SymbolRef("builtins", "object"))
    with pytest.raises(ValueError, match="destination kind"):
        AdapterTypeRegistry(
            [replace(make_adapter_spec("sensor"), destinations=(destination,))]
        )


def test_registry_rejects_duplicate_addressing_owners() -> None:
    destination = DestinationSpec("sensor_device", SymbolRef("builtins", "object"))
    with pytest.raises(ValueError, match="duplicate or reserved destination kind"):
        AdapterTypeRegistry(
            [
                replace(make_adapter_spec("one"), destinations=(destination,)),
                replace(make_adapter_spec("two"), destinations=(destination,)),
            ]
        )


def test_conflicting_providers_cannot_hijack_native_kinds(monkeypatch):
    destination = DestinationSpec("sensor_device", SymbolRef("builtins", "object"))
    providers = [
        Provider(
            name, result=replace(make_adapter_spec(name), destinations=(destination,))
        )
        for name in ("one", "two")
    ]
    providers.append(Provider("healthy"))
    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: providers)
    registry = _discover_installed_adapters()
    assert registry.transports() == ("healthy",)
    assert {failure.transport for failure in registry.failures} == {"one", "two"}


@pytest.mark.parametrize(
    "declaration",
    [("medre", "WARNING"), ("root", "INFO"), ("sdk", "INVALID"), ("", "INFO")],
)
def test_provider_cannot_override_core_logging_or_declare_invalid_levels(declaration):
    with pytest.raises(ValueError, match="dependency logger"):
        AdapterTypeRegistry(
            (replace(make_adapter_spec("private_chat"), logger_levels=(declaration,)),)
        )


def test_fake_declaration_can_be_omitted():
    from dataclasses import fields

    from medre.adapter_spec import AdapterSpec

    base = make_adapter_spec("private_chat")
    values = {
        field.name: getattr(base, field.name)
        for field in fields(base)
        if field.name != "fake_adapter"
    }
    spec = AdapterSpec(**values)
    assert spec.fake_adapter is None
    assert AdapterTypeRegistry((spec,)).get("private_chat") is spec

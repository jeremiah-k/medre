"""Discovery contracts independent of any installed transport distribution."""

from dataclasses import dataclass, replace
from importlib import metadata
from typing import Any

import pytest
from medre.adapter_registry import (
    AdapterSpec,
    AdapterTypeRegistry,
    DestinationSpec,
    SymbolRef,
    _discover_installed_specs,
    registered_transports,
)


def _spec(transport: str) -> AdapterSpec:
    symbol = SymbolRef("builtins", "object")
    return AdapterSpec(
        transport=transport,
        config=symbol,
        runtime_config=None,
        adapter=symbol,
        fake_adapter=symbol,
        renderer_factory=None,
        dependency_probe=None,
        distribution=None,
        import_names=(),
        sdk_import_roots=(),
        native_namespace_reader=symbol,
        versioned_namespace_reader=symbol,
        attribution_projector=symbol,
    )


@dataclass
class Provider:
    name: str = "community_sensor"
    value: str = "independent_sensor.plugin:spec"
    result: Any = None

    def load(self) -> Any:
        return lambda: self.result if self.result is not None else _spec(self.name)


def test_entry_point_discovery_matches_public_registry() -> None:
    assert (
        tuple(spec.transport for spec in _discover_installed_specs())
        == registered_transports()
    )


def test_discovery_needs_no_installed_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: ())
    assert _discover_installed_specs() == ()


def test_duplicate_entry_points_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = Provider()
    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: [provider, provider])
    with pytest.raises(ValueError, match="duplicate adapter transport"):
        AdapterTypeRegistry(_discover_installed_specs())


def test_discovery_order_has_no_transport_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        metadata,
        "entry_points",
        lambda **kwargs: [Provider("matrix"), Provider("a_sensor"), Provider("lxmf")],
    )
    assert tuple(spec.transport for spec in _discover_installed_specs()) == (
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
    with pytest.raises(
        RuntimeError, match="cannot load MEDRE adapter 'community_sensor'"
    ) as error:
        _discover_installed_specs()
    assert isinstance(error.value.__cause__, ImportError)


@pytest.mark.parametrize("result", [object(), replace(_spec("wrong_name"))])
def test_invalid_provider_contract_is_rejected(
    result: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        metadata, "entry_points", lambda **kwargs: [Provider(result=result)]
    )
    with pytest.raises((TypeError, ValueError), match="MEDRE adapter"):
        _discover_installed_specs()


@pytest.mark.parametrize("kind", ["channel", "Bad Kind"])
def test_registry_rejects_reserved_or_invalid_destination(kind: str) -> None:
    destination = DestinationSpec(kind, SymbolRef("builtins", "object"))
    with pytest.raises(ValueError, match="destination kind"):
        AdapterTypeRegistry([replace(_spec("sensor"), destinations=(destination,))])


def test_registry_rejects_duplicate_addressing_owners() -> None:
    destination = DestinationSpec("sensor_device", SymbolRef("builtins", "object"))
    with pytest.raises(ValueError, match="duplicate or reserved destination kind"):
        AdapterTypeRegistry(
            [
                replace(_spec("one"), destinations=(destination,)),
                replace(_spec("two"), destinations=(destination,)),
            ]
        )

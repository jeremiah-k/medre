"""External adapters register through installable Python distributions."""

from __future__ import annotations

import pytest

from medre.adapter_registry import (
    AdapterSpec,
    AdapterTypeRegistry,
    SymbolRef,
    _discover_installed_specs,
    registered_transports,
)


def test_entry_point_discovery_matches_public_registry() -> None:
    assert tuple(spec.transport for spec in _discover_installed_specs()) == (
        registered_transports()
    )


def test_external_adapter_entry_points_are_discoverable() -> None:
    from importlib.metadata import entry_points

    entries = entry_points(group="medre.adapters")
    assert {ep.name for ep in entries} == set(registered_transports())
    for ep in entries:
        spec = ep.load()()
        assert isinstance(spec, AdapterSpec)
        assert isinstance(spec.adapter, SymbolRef)
        assert spec.transport == ep.name


def test_duplicate_entry_points_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib import metadata

    entries = list(metadata.entry_points(group="medre.adapters"))
    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: [entries[0], entries[0]])
    with pytest.raises(ValueError, match="duplicate adapter transport"):
        AdapterTypeRegistry(_discover_installed_specs())


def test_invalid_external_provider_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib import metadata

    class BrokenProvider:
        name = "broken"
        value = "nonexistent.plugin:load"
        def load(self) -> object:
            raise ImportError("unavailable provider")

    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: [BrokenProvider()])
    with pytest.raises(RuntimeError, match="cannot load MEDRE adapter 'broken'"):
        _discover_installed_specs()

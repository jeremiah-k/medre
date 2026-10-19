"""Plugin type discovery contracts independent of installed distributions."""

import subprocess
import sys
from dataclasses import dataclass, replace
from importlib import metadata
from typing import Any

import pytest

from medre.plugins.capabilities import PluginCapability
from medre.plugins.registry import (
    _discover_installed_plugins,
    registered_plugins,
)
from medre.plugins.stages import PluginStage
from tests.helpers.plugin_spec import make_plugin_spec


def test_contract_types_do_not_trigger_discovery():
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            """
from importlib import metadata
from unittest.mock import patch
with patch.object(metadata, "entry_points", side_effect=AssertionError("eager discovery")):
    from medre.plugins import PluginSpec, PluginCapability, PluginStage, PluginInstanceConfig
    from medre.plugins.decisions import DerivedEvent, Pass, Derive, Supersede, Allow, Deny
    assert PluginSpec and PluginCapability and PluginStage and PluginInstanceConfig
    assert DerivedEvent and Pass and Derive and Supersede and Allow and Deny
""",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@dataclass
class Provider:
    name: str = "community_filter"
    value: str = "independent_plugin.plugin:spec"
    result: Any = None

    def load(self) -> Any:
        return lambda: (
            self.result if self.result is not None else make_plugin_spec(self.name)
        )


def test_entry_point_discovery_matches_public_registry() -> None:
    assert tuple(spec.plugin_id for spec in _discover_installed_plugins()) == (
        registered_plugins()
    )


def test_discovery_needs_no_installed_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: ())
    assert tuple(_discover_installed_plugins()) == ()


def test_discovery_order_has_no_type_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        metadata,
        "entry_points",
        lambda **kwargs: [Provider("warm"), Provider("a_filter"), Provider("digest")],
    )
    assert tuple(spec.plugin_id for spec in _discover_installed_plugins()) == (
        "a_filter",
        "digest",
        "warm",
    )


def test_invalid_external_provider_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenProvider(Provider):
        def load(self) -> Any:
            raise ImportError("unavailable provider")

    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: [BrokenProvider()])
    registry = _discover_installed_plugins()
    assert not tuple(registry)
    assert len(registry.failures) == 1
    assert registry.failures[0].plugin_id == "community_filter"
    assert "ImportError" in registry.failures[0].reason


@pytest.mark.parametrize("result", [object(), replace(make_plugin_spec("wrong_name"))])
def test_invalid_provider_contract_is_rejected(
    result: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        metadata, "entry_points", lambda **kwargs: [Provider(result=result)]
    )
    registry = _discover_installed_plugins()
    assert not tuple(registry)
    assert len(registry.failures) == 1


def test_duplicate_entry_points_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = Provider()
    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: [provider, provider])
    registry = _discover_installed_plugins()
    assert not tuple(registry)
    assert len(registry.failures) == 2


def test_conflicting_providers_cannot_hijack_a_type(monkeypatch):
    providers = [Provider("one"), Provider("one"), Provider("healthy")]
    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: providers)
    registry = _discover_installed_plugins()
    assert registry.plugin_ids() == ("healthy",)
    assert {failure.plugin_id for failure in registry.failures} == {"one"}


def test_reserved_capability_provider_is_quarantined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = replace(
        make_plugin_spec("route_editor"),
        capabilities=frozenset(
            {PluginCapability.READ_EVENTS, PluginCapability.MODIFY_ROUTES}
        ),
        stages=frozenset({PluginStage.EVENT_POLICY}),
    )
    monkeypatch.setattr(
        metadata,
        "entry_points",
        lambda **kwargs: [Provider(name="route_editor", result=spec)],
    )
    registry = _discover_installed_plugins()
    assert not tuple(registry)
    assert len(registry.failures) == 1
    assert "reserved capability" in registry.failures[0].reason

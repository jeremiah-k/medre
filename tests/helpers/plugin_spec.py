"""Minimal metadata declarations for provider-independent plugin tests."""

from medre.plugins.capabilities import PluginCapability
from medre.plugins.spec import PluginSpec
from medre.plugins.stages import PluginStage
from medre.registration import SymbolRef


def make_plugin_spec(plugin_id: str) -> PluginSpec:
    symbol = SymbolRef("builtins", "object")
    return PluginSpec(
        plugin_id=plugin_id,
        version="1.0.0",
        capabilities=frozenset({PluginCapability.READ_EVENTS}),
        stages=frozenset({PluginStage.ENRICH}),
        plugin=symbol,
    )

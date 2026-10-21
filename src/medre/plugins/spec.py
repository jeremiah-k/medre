"""Public plugin-author imports for the kernel spec contract."""

from medre.core.contracts.plugins.spec import (
    PluginSpec,
    validate_plugin_spec,
)

__all__ = ["PluginSpec", "validate_plugin_spec"]

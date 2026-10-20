"""Explicit plugin assembly; configuration and CLI discovery never call this.

Typed configuration is normalized before it reaches the canonical host. Each
enabled instance receives its own implementation. Runtime YAML activation is
separate from this programmatic assembly seam.
"""

from dataclasses import replace
from typing import Sequence

import msgspec

from medre.core.contracts.plugins.instances import (
    PluginInstanceConfig,
    missing_required_grants,
    unexpected_grants,
)
from medre.core.engine.plugin_executor import (
    PluginBinding,
    PluginExecutionLimits,
    PluginExecutor,
)
from medre.core.storage.backend import StorageBackend
from medre.plugins.registry import PLUGIN_REGISTRY, PluginTypeRegistry


def build_plugin_executor(
    storage: StorageBackend,
    instances: Sequence[PluginInstanceConfig],
    *,
    registry: PluginTypeRegistry = PLUGIN_REGISTRY,
    limits: PluginExecutionLimits | None = None,
) -> PluginExecutor:
    """Resolve only enabled implementations after validating declared config."""
    bindings = []
    instances = tuple(instances)
    identities = set()
    for instance in instances:
        spec = registry.require(instance.plugin_id)
        identity = (instance.plugin_id, instance.instance_id)
        if identity in identities:
            raise ValueError("duplicate plugin instance identity")
        identities.add(identity)
        if instance.enabled and (
            missing_required_grants(spec, instance) or unexpected_grants(spec, instance)
        ):
            raise PermissionError("plugin instance has missing or unexpected grants")
    normalized = []
    for instance in instances:
        spec = registry.require(instance.plugin_id)
        if instance.enabled:
            if spec.config is None:
                if instance.config:
                    raise ValueError("plugin type declares no configuration fields")
            else:
                model = spec.config.load()
                parsed = msgspec.convert(dict(instance.config), type=model)
                values = msgspec.to_builtins(parsed)
                if not isinstance(values, dict):
                    raise TypeError(
                        "plugin configuration model must describe an object"
                    )
                instance = replace(instance, config=values)
        normalized.append((spec, instance))
    # Validate the complete plan before importing or constructing implementations.
    for spec, instance in normalized:
        implementation = spec.plugin.load()() if instance.enabled else None
        bindings.append(PluginBinding(spec, instance, implementation))
    return PluginExecutor(storage, tuple(bindings), limits=limits)

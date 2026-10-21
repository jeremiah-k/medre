"""Configured plugin instances and operator capability grants.

A registered plugin type may back multiple configured instances. Each
instance independently owns its configuration, capability grants, namespaced
state, execution ordering, diagnostics, failure state, and provenance
identity. Loading instances from configuration arrives with the operator
surface; these value types are the contract that loading produces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from medre.core.contracts.json_values import freeze_json_mapping
from medre.core.contracts.plugins.capabilities import (
    HOST_GRANTABLE_CAPABILITIES,
    PluginCapability,
)
from medre.core.contracts.plugins.spec import PluginSpec
from medre.registration import valid_registration_name

__all__ = [
    "PluginInstanceConfig",
    "effective_capabilities",
    "missing_required_grants",
    "unexpected_grants",
]


@dataclass(frozen=True, slots=True)
class PluginInstanceConfig:
    """One configured instance of a registered plugin type.

    ``grants`` are the capabilities the operator authorizes for this
    instance; ``config`` is validated against the plugin type's config model
    when the instance is loaded.
    """

    plugin_id: str
    instance_id: str
    enabled: bool = True
    grants: frozenset[PluginCapability] = frozenset()
    config: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not valid_registration_name(self.plugin_id):
            raise ValueError(f"invalid plugin type id: {self.plugin_id!r}")
        if not valid_registration_name(self.instance_id):
            raise ValueError(f"invalid plugin instance id: {self.instance_id!r}")
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a boolean")
        grants = frozenset(self.grants)
        if any(not isinstance(grant, PluginCapability) for grant in grants):
            raise TypeError("grants must contain PluginCapability members")
        object.__setattr__(self, "grants", grants)
        object.__setattr__(
            self, "config", freeze_json_mapping(self.config, label="plugin config")
        )


def _verify_binding(spec: PluginSpec, instance: PluginInstanceConfig) -> None:
    if spec.plugin_id != instance.plugin_id:
        raise ValueError("plugin instance belongs to a different plugin type")


def effective_capabilities(
    spec: PluginSpec, instance: PluginInstanceConfig
) -> frozenset[PluginCapability]:
    """Capabilities the runtime grants this instance.

    The effective set is the intersection of what the plugin type declares,
    what the operator granted, and what this MEDRE host supports.
    """
    _verify_binding(spec, instance)
    return spec.capabilities & instance.grants & HOST_GRANTABLE_CAPABILITIES


def missing_required_grants(
    spec: PluginSpec, instance: PluginInstanceConfig
) -> frozenset[PluginCapability]:
    """Declared requirements the instance does not grant.

    A non-empty result is a configuration readiness diagnostic: the instance
    cannot run with the capabilities it needs.
    """
    return spec.capabilities - effective_capabilities(spec, instance)


def unexpected_grants(
    spec: PluginSpec, instance: PluginInstanceConfig
) -> frozenset[PluginCapability]:
    """Granted capabilities the plugin type never declared.

    A non-empty result is an operator configuration diagnostic; grants should
    name capabilities the plugin type actually requires.
    """
    _verify_binding(spec, instance)
    return instance.grants - spec.capabilities

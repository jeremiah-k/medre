"""MEDRE plugin contract: canonical-boundary extensions beside adapters.

Adapters and plugins share registration machinery (``medre.registration``),
declarative SDK-free
discovery, quarantine semantics, and operator-owned distribution, but carry
separate contracts: adapters own the closed transport boundary; plugins
observe and derive canonical events and evaluate scoped policy.

This package defines the contract — capability vocabulary, stage
subscriptions, typed decisions, the declarative ``PluginSpec``, configured
instances with operator grants, the runtime context, and the stage
protocols. Type discovery lives in ``medre.plugins.registry``. Durable
decision records and host persistence are defined in
``medre.plugins.records`` and the core storage backend. Pipeline execution
and operator configuration follow the contract in
``docs/spec/plugins.md``.
"""

from medre.plugins.capabilities import (
    HOST_GRANTABLE_CAPABILITIES,
    PluginCapability,
)
from medre.plugins.context import (
    EnrichPlugin,
    Plugin,
    PluginContext,
    PolicyPlugin,
    TransformPlugin,
)
from medre.plugins.decisions import (
    Allow,
    Deny,
    Derive,
    DerivedEvent,
    EnrichOutcome,
    Pass,
    PolicyOutcome,
    Supersede,
    TransformOutcome,
)
from medre.plugins.instances import (
    PluginInstanceConfig,
    effective_capabilities,
    missing_required_grants,
    unexpected_grants,
)
from medre.plugins.spec import PluginSpec
from medre.plugins.stages import PluginStage
from medre.plugins.validation import PluginCapabilityError, validate_plugin_decision

__all__ = [
    "Allow",
    "Deny",
    "Derive",
    "DerivedEvent",
    "EnrichOutcome",
    "EnrichPlugin",
    "HOST_GRANTABLE_CAPABILITIES",
    "Pass",
    "Plugin",
    "PluginCapability",
    "PluginCapabilityError",
    "validate_plugin_decision",
    "PluginContext",
    "PluginInstanceConfig",
    "PluginSpec",
    "PluginStage",
    "PolicyOutcome",
    "PolicyPlugin",
    "Supersede",
    "TransformOutcome",
    "TransformPlugin",
    "effective_capabilities",
    "missing_required_grants",
    "unexpected_grants",
]

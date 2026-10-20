"""Declarative registration metadata for one installed plugin type.

Plugin packages register zero-argument factories through Python entry points
in the ``medre.plugins`` group; each factory returns a :class:`PluginSpec`.
The spec is SDK-free declaration data: implementation references stay lazy
``SymbolRef`` strings so discovery never imports plugin implementations.
Pure validation lives in :func:`validate_plugin_spec` and is shared by
discovery and explicit execution assembly.
"""

from __future__ import annotations

from dataclasses import dataclass

from medre.core.contracts.plugins.capabilities import PluginCapability
from medre.core.contracts.plugins.stages import PluginStage
from medre.registration import SymbolRef

__all__ = ["PluginSpec", "validate_plugin_spec"]


@dataclass(frozen=True, slots=True)
class PluginSpec:
    """Declarative assembly metadata for one plugin type.

    ``capabilities`` declares what plugin implementations require; operators
    grant capabilities per configured instance, and the runtime grants only
    the intersection (see ``docs/spec/plugins.md``). ``version`` identifies
    the plugin implementation in durable decision records. ``input_kinds``
    scopes stage subscriptions (empty means all for enrichment/policy), while
    ``output_kinds`` bounds every derived proposal from any stage. ``plugin`` loads
    to a zero-argument factory returning an object satisfying the stage
    protocols declared in ``stages``.
    """

    plugin_id: str
    version: str
    capabilities: frozenset[PluginCapability]
    stages: frozenset[PluginStage]
    plugin: SymbolRef
    input_kinds: tuple[str, ...] = ()
    output_kinds: tuple[str, ...] = ()
    config: SymbolRef | None = None
    logger_levels: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        # Snapshot every caller-owned container before registry validation.
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        object.__setattr__(self, "stages", frozenset(self.stages))
        object.__setattr__(self, "input_kinds", tuple(self.input_kinds))
        object.__setattr__(self, "output_kinds", tuple(self.output_kinds))
        object.__setattr__(
            self, "logger_levels", tuple(tuple(pair) for pair in self.logger_levels)
        )


def validate_plugin_spec(spec: PluginSpec) -> None:
    """Validate SDK-free assembly declarations without resolving references."""
    from medre.core.contracts.plugins.capabilities import HOST_GRANTABLE_CAPABILITIES
    from medre.core.contracts.plugins.decisions import PLUGIN_OUTPUT_KINDS
    from medre.core.events.kinds import is_registered
    from medre.core.observability.log_levels import VALID_LEVEL_NAMES
    from medre.registration import valid_registration_name

    if not isinstance(spec, PluginSpec):
        raise TypeError("registry entries must be PluginSpec declarations")
    plugin_id = spec.plugin_id
    if not valid_registration_name(plugin_id):
        raise ValueError(f"invalid plugin id: {plugin_id!r}")
    if not isinstance(spec.version, str) or not spec.version.strip():
        raise ValueError(f"plugin {plugin_id!r} must declare a version")
    for field, reference in (("plugin", spec.plugin), ("config", spec.config)):
        if field == "config" and reference is None:
            continue
        if (
            not isinstance(reference, SymbolRef)
            or not isinstance(reference.module, str)
            or not reference.module
            or not all(part.isidentifier() for part in reference.module.split("."))
            or not isinstance(reference.name, str)
            or not reference.name.isidentifier()
        ):
            raise ValueError(f"plugin {plugin_id!r} has an invalid {field} SymbolRef")
    capabilities = spec.capabilities
    if not capabilities:
        raise ValueError(f"plugin {plugin_id!r} must declare at least one capability")
    for capability in capabilities:
        if not isinstance(capability, PluginCapability):
            raise ValueError(
                f"invalid capability declaration for {plugin_id!r}: " f"{capability!r}"
            )
        if capability not in HOST_GRANTABLE_CAPABILITIES:
            raise ValueError(
                f"plugin {plugin_id!r} declares reserved capability "
                f"{capability.value!r}; the service is not implemented by this host"
            )
    if PluginCapability.READ_EVENTS not in capabilities:
        raise ValueError(
            f"plugin {plugin_id!r} must declare read_events; every "
            "stage subscription processes canonical events"
        )
    stages = spec.stages
    if not stages:
        raise ValueError(f"plugin {plugin_id!r} must subscribe to at least one stage")
    for stage in stages:
        if not isinstance(stage, PluginStage):
            raise ValueError(f"invalid stage subscription for {plugin_id!r}: {stage!r}")
    if (
        PluginStage.EVENT_POLICY in stages
        and PluginCapability.SUPPRESS_EVENTS not in capabilities
    ):
        raise ValueError(
            f"plugin {plugin_id!r} must declare suppress_events for event_policy"
        )
    if PluginStage.TRANSFORM in stages and not spec.input_kinds:
        raise ValueError(
            f"plugin {plugin_id!r} must declare input_kinds for the transform stage"
        )
    if (
        PluginStage.TRANSFORM in stages
        and PluginCapability.EMIT_EVENTS not in capabilities
    ):
        raise ValueError("transform stage requires emit_events")
    if PluginCapability.EMIT_EVENTS in capabilities and not spec.output_kinds:
        raise ValueError("emit_events requires declared output_kinds")
    if spec.output_kinds and PluginCapability.EMIT_EVENTS not in capabilities:
        raise ValueError("output_kinds requires emit_events")
    for kind in (*spec.input_kinds, *spec.output_kinds):
        if not isinstance(kind, str) or not is_registered(kind):
            raise ValueError(
                f"plugin {plugin_id!r} declares unregistered event " f"kind: {kind!r}"
            )
    if any(kind not in PLUGIN_OUTPUT_KINDS for kind in spec.output_kinds):
        raise ValueError("plugin outputs must not contain core-owned evidence kinds")
    logger_names = set()
    for logger_name, level in spec.logger_levels:
        if (
            not isinstance(logger_name, str)
            or not logger_name
            or logger_name in {"root", "medre"}
            or logger_name.startswith("medre.")
            or logger_name in logger_names
            or level not in VALID_LEVEL_NAMES
        ):
            raise ValueError(f"invalid dependency logger declaration for {plugin_id!r}")
        logger_names.add(logger_name)

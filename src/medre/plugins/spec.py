"""Declarative registration metadata for one installed plugin type.

Plugin packages register zero-argument factories through Python entry points
in the ``medre.plugins`` group; each factory returns a :class:`PluginSpec`.
The spec is SDK-free declaration data: implementation references stay lazy
``SymbolRef`` strings so discovery never imports plugin implementations.
Validation lives in :class:`medre.plugins.registry.PluginTypeRegistry`.
"""

from __future__ import annotations

from dataclasses import dataclass

from medre.plugins.capabilities import PluginCapability
from medre.plugins.stages import PluginStage
from medre.registration import SymbolRef

__all__ = ["PluginSpec"]


@dataclass(frozen=True, slots=True)
class PluginSpec:
    """Declarative assembly metadata for one plugin type.

    ``capabilities`` declares what plugin implementations require; operators
    grant capabilities per configured instance, and the runtime grants only
    the intersection (see ``docs/spec/plugins.md``). ``version`` identifies
    the plugin implementation in durable decision records. ``plugin`` loads
    to a zero-argument factory returning an object satisfying the stage
    protocols declared in ``stages``.
    """

    plugin_id: str
    version: str
    capabilities: frozenset[PluginCapability]
    stages: frozenset[PluginStage]
    plugin: SymbolRef
    transform_inputs: tuple[str, ...] = ()
    transform_outputs: tuple[str, ...] = ()
    config: SymbolRef | None = None
    logger_levels: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        # Snapshot every caller-owned container before registry validation.
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        object.__setattr__(self, "stages", frozenset(self.stages))
        object.__setattr__(self, "transform_inputs", tuple(self.transform_inputs))
        object.__setattr__(self, "transform_outputs", tuple(self.transform_outputs))
        object.__setattr__(
            self, "logger_levels", tuple(tuple(pair) for pair in self.logger_levels)
        )

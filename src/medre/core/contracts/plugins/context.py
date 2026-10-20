"""The plugin runtime context and stage protocols.

Plugins operate exclusively on canonical events and runtime APIs. They do
not construct transport-native payloads and do not construct canonical
events themselves; every derived event is a proposal the host constructs,
stamps, and admits.

This module holds the typed contract. ``PluginContext`` carries the
always-granted surface, normalized config, and invocation-scoped buffered
state. Additional capability-gated services remain reserved as defined in
``docs/spec/plugins.md``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from medre.core.contracts.json_values import freeze_json_mapping
from medre.core.contracts.plugins.decisions import (
    EnrichOutcome,
    PolicyOutcome,
    TransformOutcome,
)
from medre.core.contracts.plugins.records import PluginExecutionKey
from medre.core.events.canonical import CanonicalEvent

__all__ = [
    "EnrichPlugin",
    "Plugin",
    "PluginContext",
    "PluginState",
    "PolicyPlugin",
    "TransformPlugin",
]


class PluginState(Protocol):
    """Invocation-scoped buffered state; writes commit only with a decision."""

    def read(self) -> Mapping[str, Any]: ...
    def write(self, values: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class PluginContext:
    """Scoped runtime services handed to one plugin instance.

    Always granted: identity, a scoped logger, a UTC wall clock, and the
    runtime shutdown event (an ``asyncio.Event``). Capability-gated services
    are granted per ``docs/spec/plugins.md`` by the execution runtime.
    """

    plugin_id: str
    instance_id: str
    logger: logging.Logger
    clock: Callable[[], datetime]
    shutdown_event: asyncio.Event
    config: Mapping[str, Any] = field(default_factory=dict)
    state: PluginState | None = None
    _execution: Callable[[], PluginExecutionKey] | None = field(
        default=None, repr=False, compare=False
    )

    def current_execution(self) -> PluginExecutionKey:
        """Return the active immutable key; lifecycle/late calls have no identity."""
        from medre.core.contracts.plugins.validation import PluginCapabilityError

        if self._execution is None:
            raise PluginCapabilityError("no active plugin invocation")
        return self._execution()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "config",
            freeze_json_mapping(self.config, label="plugin context config"),
        )


@runtime_checkable
class Plugin(Protocol):
    """Lifecycle every plugin implementation must satisfy.

    Identity, version, capabilities, and stage subscriptions live on the
    declarative :class:`~medre.plugins.spec.PluginSpec`; implementations
    carry behavior only.
    """

    async def initialize(self, context: PluginContext) -> None:
        """Initialize with the runtime-provided context for this instance."""
        ...

    async def shutdown(self) -> None:
        """Release resources before the plugin stops."""
        ...


@runtime_checkable
class EnrichPlugin(Protocol):
    """Enrichment stage: additive derivation; the original event continues."""

    async def enrich(self, event: CanonicalEvent) -> EnrichOutcome: ...


@runtime_checkable
class TransformPlugin(Protocol):
    """Transform stage: derive additional or superseding events."""

    async def transform(self, event: CanonicalEvent) -> TransformOutcome: ...


@runtime_checkable
class PolicyPlugin(Protocol):
    """Event-policy stage: scoped delivery verdicts; never an edit."""

    async def evaluate(self, event: CanonicalEvent) -> PolicyOutcome: ...

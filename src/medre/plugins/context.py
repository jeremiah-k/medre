"""The plugin runtime context and stage protocols.

Plugins operate exclusively on canonical events and runtime APIs. They do
not construct transport-native payloads and do not construct canonical
events themselves; every derived event is a proposal the host constructs,
stamps, and admits.

This module holds the typed contract. ``PluginContext`` carries the
always-granted surface; capability-gated services (route views, storage
reads, namespaced state) join the context with the execution runtime, as
defined in ``docs/spec/plugins.md``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from medre.core.events.canonical import CanonicalEvent
from medre.plugins.decisions import (
    EnrichOutcome,
    PolicyOutcome,
    TransformOutcome,
)

__all__ = [
    "EnrichPlugin",
    "Plugin",
    "PluginContext",
    "PolicyPlugin",
    "TransformPlugin",
]


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

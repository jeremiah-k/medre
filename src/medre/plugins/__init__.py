"""Minimal plugin boundary scaffolding for the medre framework.

This module provides the protocol-neutral boundary that plugins operate
within.  Plugins see only canonical events and runtime APIs — they must
**not** directly emit transport-native payloads (raw Matrix events,
Meshtastic packets, etc.).

The scaffolding is deliberately minimal: a :class:`Plugin` protocol,
a :class:`PluginCapability` enum, a :class:`PluginBoundaryError`, and a
:class:`validate_plugin_payload` validator.  No plugin runtime, loader,
or lifecycle manager is included — that belongs in a future Track.

Public symbols
--------------
* :class:`PluginCapability` – capabilities a plugin may declare.
* :class:`Plugin` – protocol every plugin must satisfy.
* :class:`PluginBoundaryError` – raised when a plugin violates the
  canonical-event boundary.
* :func:`validate_plugin_payload` – validator that rejects non-canonical
  payloads produced by plugins.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, Sequence, runtime_checkable

from medre.core.events.canonical import CanonicalEvent


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class PluginCapability(str, Enum):
    """Capabilities a plugin may declare at load time.

    The runtime grants only what a plugin declares.  Attempting to use a
    service that requires an undeclared capability raises a runtime error.
    """

    READ_EVENTS = "read_events"
    EMIT_EVENTS = "emit_events"
    READ_ROUTES = "read_routes"
    MODIFY_ROUTES = "modify_routes"
    READ_IDENTITY = "read_identity"
    READ_STORAGE = "read_storage"
    ACCESS_TELEMETRY = "access_telemetry"


# ---------------------------------------------------------------------------
# Plugin protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Plugin(Protocol):
    """Minimal protocol that every plugin must satisfy.

    Plugins operate exclusively on :class:`CanonicalEvent` instances and
    runtime APIs.  They must **not** directly construct or emit
    transport-native payloads (e.g. raw Matrix JSON, Meshtastic protobuf).
    All output flows through the event pipeline as canonical events.

    Attributes
    ----------
    name:
        Human-readable plugin name.
    version:
        Semantic version string.
    capabilities:
        The set of capabilities this plugin requires.
    """

    name: str
    version: str
    capabilities: set[PluginCapability]

    async def initialize(self, context: object) -> None:
        """Initialize the plugin with a runtime-provided context.

        Parameters
        ----------
        context:
            Opaque plugin context provided by the runtime.  Typed as
            ``object`` to avoid coupling to a concrete context class
            until the runtime is implemented.
        """
        ...

    async def handle_event(
        self, event: CanonicalEvent
    ) -> list[CanonicalEvent]:
        """Process an inbound canonical event.

        Returns zero or more derived canonical events that the runtime
        will feed back into the pipeline.  All returned events must be
        valid :class:`CanonicalEvent` instances — **not** transport-native
        payloads.

        Parameters
        ----------
        event:
            The inbound canonical event.

        Returns
        -------
        list[CanonicalEvent]
            Derived events to inject into the pipeline.  May be empty.
        """
        ...

    async def shutdown(self) -> None:
        """Clean up resources before the plugin is unloaded."""
        ...


# ---------------------------------------------------------------------------
# Boundary enforcement
# ---------------------------------------------------------------------------


class PluginBoundaryError(TypeError):
    """Raised when a plugin violates the canonical-event boundary.

    This error indicates that a plugin attempted to emit a non-canonical
    payload (e.g. a raw dict representing a Matrix event, a bytes object
    containing a Meshtastic packet, or any non-:class:`CanonicalEvent`
    value) from its ``handle_event`` method.
    """


def validate_plugin_payload(
    outputs: Sequence[object],
    plugin_name: str,
) -> list[CanonicalEvent]:
    """Validate that all plugin outputs are canonical events.

    Plugins must not emit transport-native payloads.  This validator
    checks every element of the output list and raises
    :class:`PluginBoundaryError` if any element is not a
    :class:`CanonicalEvent` instance.

    Parameters
    ----------
    outputs:
        The list returned by ``Plugin.handle_event``.
    plugin_name:
        The plugin's name, used in error messages.

    Returns
    -------
    list[CanonicalEvent]
        The validated list of canonical events.

    Raises
    ------
    PluginBoundaryError
        If any element in *outputs* is not a :class:`CanonicalEvent`.
    """
    validated: list[CanonicalEvent] = []
    for item in outputs:
        if not isinstance(item, CanonicalEvent):
            raise PluginBoundaryError(
                f"Plugin {plugin_name!r} emitted a non-canonical payload: "
                f"expected CanonicalEvent, got {type(item).__name__}. "
                "Plugins must emit canonical events; transport-native "
                "payloads are not allowed."
            )
        validated.append(item)
    return validated

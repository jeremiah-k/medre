"""Capability vocabulary separating plugin declaration from authorization.

A plugin type declares the capabilities its implementation requires. An
operator grants capabilities to each configured instance. The runtime grants
only the intersection of what is declared, what the operator granted, and
what this MEDRE host supports. This is an API-level restriction on trusted
in-process code, not a security sandbox.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "HOST_GRANTABLE_CAPABILITIES",
    "PluginCapability",
]


class PluginCapability(str, Enum):
    """Services a plugin type may declare and an operator may grant.

    The runtime grants only what a plugin's configured instance receives.
    Attempting to use a service that requires an ungranted capability raises
    a runtime error.
    """

    READ_EVENTS = "read_events"
    EMIT_EVENTS = "emit_events"
    SUPPRESS_EVENTS = "suppress_events"
    READ_ROUTES = "read_routes"
    MODIFY_ROUTES = "modify_routes"
    READ_IDENTITY = "read_identity"
    READ_STORAGE = "read_storage"
    ACCESS_TELEMETRY = "access_telemetry"


HOST_GRANTABLE_CAPABILITIES: frozenset[PluginCapability] = frozenset(
    {
        PluginCapability.READ_EVENTS,
        PluginCapability.EMIT_EVENTS,
        PluginCapability.SUPPRESS_EVENTS,
    }
)
"""Implemented authority. Route/identity/storage/telemetry services stay reserved."""

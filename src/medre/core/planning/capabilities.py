"""Capability checking helpers for adapter event-kind support.

This module provides the single source of truth for determining whether
a given event kind is supported (natively or via fallback) by an
adapter's declared capabilities.  It is used by both the live pipeline
(:mod:`~medre.core.engine.pipeline`) and the replay engine
(:mod:`~medre.core.engine.replay`) so that capability semantics are
consistent across live and replay delivery paths.

Capability level semantics (three-level string fields):

* ``"native"``      – first-class support; deliver natively.
* ``"fallback"``    – no native support, but the adapter can receive a
  degraded / textual representation.  The event is **not** suppressed.
* ``"unsupported"`` – the adapter cannot handle this feature at all;
  delivery should be suppressed.

Public symbols
--------------
* :func:`capability_unsupported` – return a reason string when the
  event kind is unsupported by the given capabilities.
* :func:`resolve_adapter_capabilities` – resolve the
  :class:`AdapterCapabilities` for a target from the adapter registry.
"""

from __future__ import annotations

from typing import Any

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind


def capability_unsupported(
    event: CanonicalEvent,
    caps: AdapterCapabilities,
) -> str | None:
    """Return a reason string if the event kind is unsupported by *caps*.

    Returns ``None`` when the event is deliverable — either natively
    (``"native"``) or via fallback text (``"fallback"``).  Only
    ``"unsupported"`` capability values cause a suppression reason to
    be returned.

    Parameters
    ----------
    event:
        The canonical event whose kind is being checked.
    caps:
        The target adapter's declared capabilities.

    Returns
    -------
    str | None
        A human-readable reason when the event should be suppressed, or
        ``None`` when the event is deliverable.
    """
    kind = event.event_kind

    if kind == EventKind.MESSAGE_REACTED and caps.reactions == "unsupported":
        return f"reactions unsupported by adapter (event_kind={kind})"

    if kind == EventKind.MESSAGE_EDITED and caps.edits == "unsupported":
        return f"edits unsupported by adapter (event_kind={kind})"

    if kind == EventKind.MESSAGE_DELETED and caps.deletes == "unsupported":
        return f"deletes unsupported by adapter (event_kind={kind})"

    if kind == EventKind.MESSAGE_FILE and not caps.attachments:
        return f"attachments unsupported by adapter (event_kind={kind})"

    if kind in (EventKind.MESSAGE_CREATED, EventKind.MESSAGE_TEXT) and not caps.text:
        return f"text unsupported by adapter (event_kind={kind})"

    if kind == EventKind.PRESENCE_CHANGED and not caps.presence:
        return f"presence unsupported by adapter (event_kind={kind})"

    if kind in (EventKind.TELEMETRY_RECEIVED, EventKind.TELEMETRY_POSITION):
        if not caps.metadata_fields:
            return f"metadata_fields unsupported by adapter (event_kind={kind})"

    # Reply-carrying events require reply support.
    if event.relations:
        for rel in event.relations:
            if rel.relation_type == "reply" and caps.replies == "unsupported":
                return "replies unsupported by adapter (event has reply relation)"

    return None


def resolve_adapter_capabilities(
    adapters: dict[str, Any],
    target: Any,
) -> AdapterCapabilities | None:
    """Resolve the :class:`AdapterCapabilities` for a target adapter.

    Looks up the target's adapter in the *adapters* registry and
    returns its declared capabilities.  Returns ``None`` when the
    adapter is not in the registry (truly missing).  Returns a default
    :class:`AdapterCapabilities` when the adapter exists but does not
    report capabilities.

    Parameters
    ----------
    adapters:
        Mapping of adapter ID to adapter instance.
    target:
        A :class:`~medre.core.routing.models.RouteTarget` (or any
        object with an ``adapter`` attribute).

    Returns
    -------
    AdapterCapabilities | None
        The resolved capabilities, ``None`` when the adapter is missing
        from the registry, or a default instance when the adapter exists
        but has no ``_capabilities`` attribute.
    """
    adapter_id = getattr(target, "adapter", None)
    if adapter_id is None:
        return None

    adapter = adapters.get(adapter_id)
    if adapter is None:
        return None

    caps = getattr(adapter, "_capabilities", None)
    if isinstance(caps, AdapterCapabilities):
        return caps
    return AdapterCapabilities()

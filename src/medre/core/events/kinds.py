"""Event kind registry with string constants for the canonical event model.

Every event in the framework carries an ``event_kind`` string drawn from
this registry.  Kinds follow a ``<domain>.<action>`` naming convention and
are organised into four top-level domains:

* **message** – user-facing message lifecycle events.
* **telemetry** – device / sensor telemetry.
* **presence** – online / offline state changes.
* **identity** – identity material updates.
* **delivery** – transport-level delivery tracking.
* **system** – framework-internal bookkeeping.
* **plugin** – extension-defined custom kinds.
"""

from __future__ import annotations


class EventKind:
    """Central registry of all well-known event kind strings.

    Every constant is a plain ``str`` so that downstream code can compare
    with ``==`` without importing this class.  The class merely serves as
    a discoverable namespace.
    """

    # -- Message lifecycle ------------------------------------------------

    MESSAGE_CREATED: str = "message.created"
    """A new message has entered the system."""

    MESSAGE_TEXT: str = "message.text"
    """A plain-text message payload."""

    MESSAGE_REACTED: str = "message.reacted"
    """A reaction (emoji, vote, …) was attached to a message."""

    MESSAGE_EDITED: str = "message.edited"
    """An existing message body was edited."""

    MESSAGE_DELETED: str = "message.deleted"
    """A message was soft- or hard-deleted."""

    MESSAGE_FILE: str = "message.file"
    """A file attachment message."""

    # -- Telemetry --------------------------------------------------------

    TELEMETRY_RECEIVED: str = "telemetry.received"
    """Raw telemetry data received from a node."""

    TELEMETRY_POSITION: str = "telemetry.position"
    """A geographic-position telemetry report."""

    # -- Presence ---------------------------------------------------------

    PRESENCE_CHANGED: str = "presence.changed"
    """A node or user's presence state changed."""

    # -- Identity ---------------------------------------------------------

    IDENTITY_UPDATED: str = "identity.updated"
    """Identity material (keys, profile) was updated."""

    # -- Delivery ---------------------------------------------------------

    DELIVERY_QUEUED: str = "delivery.queued"
    """Message enqueued for delivery."""

    DELIVERY_SENT: str = "delivery.sent"
    """Message handed off to the transport layer."""

    DELIVERY_FAILED: str = "delivery.failed"
    """Delivery attempt failed (recoverable or permanent)."""

    # -- System -----------------------------------------------------------

    SYSTEM_AUDIT: str = "system.audit"
    """An audit-log entry produced by the framework."""

    SYSTEM_LIFECYCLE: str = "system.lifecycle"
    """A lifecycle event (start, stop, reload, …)."""

    # -- Plugin -----------------------------------------------------------

    PLUGIN_CUSTOM: str = "plugin.custom"
    """Reserved kind for plugin-defined events that do not map to a
    built-in kind.  Plugins should append a sub-kind in the payload
    rather than inventing new top-level kinds."""


KNOWN_KINDS: frozenset[str] = frozenset(
    [
        EventKind.MESSAGE_CREATED,
        EventKind.MESSAGE_TEXT,
        EventKind.MESSAGE_REACTED,
        EventKind.MESSAGE_EDITED,
        EventKind.MESSAGE_DELETED,
        EventKind.MESSAGE_FILE,
        EventKind.TELEMETRY_RECEIVED,
        EventKind.TELEMETRY_POSITION,
        EventKind.PRESENCE_CHANGED,
        EventKind.IDENTITY_UPDATED,
        EventKind.DELIVERY_QUEUED,
        EventKind.DELIVERY_SENT,
        EventKind.DELIVERY_FAILED,
        EventKind.SYSTEM_AUDIT,
        EventKind.SYSTEM_LIFECYCLE,
        EventKind.PLUGIN_CUSTOM,
    ]
)
"""Immutable set of every kind defined in :class:`EventKind`.

Useful for fast membership tests and for iterating over all known kinds.
"""


def is_registered(kind: str) -> bool:
    """Return ``True`` if *kind* is recognised by the built-in registry.

    Parameters
    ----------
    kind:
        An event kind string, e.g. ``"message.created"``.

    Returns
    -------
    bool
        Whether the kind appears in :data:`KNOWN_KINDS`.
    """
    return kind in KNOWN_KINDS

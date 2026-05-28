"""Core canonical event model and supporting value types.

This module defines the central data structures that every other subsystem
(storage, routing, planning, adapters) depends on:

* :class:`CanonicalEvent` – the universal, immutable event envelope.
* :class:`EventRelation` – typed links between events (reply, reaction …).
* :class:`NativeRef` – reference to a message in an adapter's native format.
* :class:`NativeMessageRef` – persisted mapping between canonical and native IDs.
* :class:`DeliveryReceipt` – per-adapter delivery status record.
* :class:`EventRecordKind` – discriminant for the kind of stored record.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

import msgspec
from msgspec.structs import force_setattr

from medre.core.events.metadata import EventMetadata, _FrozenDict

# Re-export canonical constants from schema to avoid circular imports.
# These are imported here for validation use only.
from medre.core.events.schema import VALID_RELATION_TYPES

# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------


class EventRecordKind(Enum):
    """Discriminant describing what kind of stored record an entry represents.

    Attributes
    ----------
    SOURCE_EVENT:
        An event captured directly from an external source / adapter.
    DERIVED_EVENT:
        An event synthesised from one or more source events (e.g. a
        summarised telemetry event).
    DELIVERY_ARTIFACT:
        An artifact produced by the delivery planning pipeline.
    RECEIPT_EVENT:
        A delivery-receipt record tracking outbound delivery progress.
    """

    SOURCE_EVENT = "source_event"
    DERIVED_EVENT = "derived_event"
    DELIVERY_ARTIFACT = "delivery_artifact"
    RECEIPT_EVENT = "receipt_event"


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


class NativeRef(msgspec.Struct, frozen=True):
    """Reference to a message in an adapter's native ID space.

    Attributes
    ----------
    adapter:
        Name of the adapter that owns the native namespace.
    native_channel_id:
        Channel / conversation ID in the adapter's native format.
    native_message_id:
        Message ID in the adapter's native format.
    native_thread_id:
        Thread / parent message ID in the adapter's native format, if
        the adapter supports threaded conversations.  **Reserved** — no
        adapter currently populates this field; it is always ``None`` at
        runtime.
    """

    adapter: str
    native_channel_id: str | None
    native_message_id: str
    native_thread_id: str | None = None


class EventRelation(msgspec.Struct, frozen=True):
    """A typed link from one event to another.

    Relations model replies, reactions, edits, deletes, and threads.

    Attributes
    ----------
    relation_type:
        The kind of relationship (``"reply"``, ``"reaction"``,
        ``"edit"``, ``"delete"``, ``"thread"``).
    target_event_id:
        The canonical event ID of the target event, if known.
    target_native_ref:
        A native-space reference to the target, used when the canonical
        ID has not been resolved yet.
    key:
        Optional discriminator (e.g. the emoji for a reaction relation).
    fallback_text:
        Human-readable text describing the relation's semantic meaning,
        used by fallback rendering strategies when the target adapter
        cannot represent the relation natively.  Typically a short
        summary of the target event (e.g. the first few words of the
        original message for a reply, or the emoji for a reaction).
    metadata:
        Arbitrary key-value metadata attached to this relation.
    """

    relation_type: Literal["reply", "reaction", "edit", "delete", "thread"]
    target_event_id: str | None
    target_native_ref: NativeRef | None
    key: str | None
    fallback_text: str | None
    metadata: dict[str, object] = msgspec.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, _FrozenDict):
            force_setattr(self, "metadata", _FrozenDict(self.metadata))
        if self.relation_type not in VALID_RELATION_TYPES:
            raise ValueError(
                f"relation_type must be one of {sorted(VALID_RELATION_TYPES)}, "
                f"got {self.relation_type!r}"
            )


class NativeMessageRef(msgspec.Struct, frozen=True):
    """Persisted mapping between a canonical event and a native message.

    Instances are created by adapters when an event is materialised into
    a native message or when an inbound native message is ingested.

    Attributes
    ----------
    id:
        Unique identifier for this mapping record.
    event_id:
        The canonical event ID this mapping refers to.
    adapter:
        Name of the adapter that owns the native namespace.
    native_channel_id:
        Channel / conversation ID in the adapter's native format.
    native_message_id:
        Message ID in the adapter's native format.
    native_thread_id:
        Thread ID in the adapter's native format, if applicable.
        **Reserved** — no adapter currently populates this field; it
        is always ``None`` at runtime.
    native_relation_id:
        ID of the related native entity (e.g. the message being replied
        to) in the adapter's native format.  **Reserved** — no adapter
        currently populates this field.
    direction:
        Whether the message was ``"inbound"`` or ``"outbound"``.
    metadata:
        Adapter-specific metadata about this mapping.
    created_at:
        Timestamp when this mapping was created.
    """

    id: str
    event_id: str
    adapter: str
    native_channel_id: str | None
    native_message_id: str
    native_thread_id: str | None
    native_relation_id: str | None
    direction: Literal["inbound", "outbound"]
    metadata: dict[str, object] = msgspec.field(default_factory=dict)
    created_at: datetime = msgspec.field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, _FrozenDict):
            force_setattr(self, "metadata", _FrozenDict(self.metadata))


class DeliveryReceipt(msgspec.Struct, frozen=True):
    """Per-adapter delivery status record for an outbound event.

    Delivery receipts track the lifecycle of an event as it travels
    through the delivery pipeline toward a target adapter.

    Attributes
    ----------
    sequence:
        Monotonically increasing sequence number within the delivery plan.
    receipt_id:
        Unique identifier for this receipt record.
    event_id:
        The canonical event being delivered.
    delivery_plan_id:
        Identifier of the delivery plan this receipt belongs to.
    target_adapter:
        Name of the adapter the event is being delivered to.
    route_id:
        Identifier of the route that triggered this delivery.
    status:
        Current delivery status.
    error:
        Error message if the delivery failed.
    adapter_message_id:
        Native message ID assigned by the target adapter after
        adapter-reported handoff, when available.
    next_retry_at:
        Scheduled time for the next retry attempt, if applicable.
    attempt_number:
        1-indexed attempt number for this receipt.  The first delivery
        attempt is ``1``, a retry is ``2``, and so on.  Enables receipt
        lineage ordering without relying on timestamps.
    parent_receipt_id:
        Receipt ID of the preceding attempt in this delivery chain.
        ``None`` for the first attempt.  Together with
        ``attempt_number`` this provides an explicit receipt lineage.
    source:
        Origin of this receipt: ``"live"``, ``"retry"``, or ``"replay"``.
    replay_run_id:
        When ``source="replay"``, the ``run_id`` of the replay execution
        that produced this receipt.  ``None`` for live and retry deliveries.
    created_at:
        Timestamp when this receipt was created.
    """

    sequence: int = 0
    receipt_id: str = ""
    event_id: str = ""
    delivery_plan_id: str = ""
    target_adapter: str = ""
    target_channel: str | None = None
    route_id: str = ""
    status: Literal[
        "queued",
        "sent",
        "failed",
        "dead_lettered",
        "suppressed",
    ] = "queued"
    error: str | None = None
    failure_kind: str | None = None
    adapter_message_id: str | None = None
    next_retry_at: datetime | None = None
    attempt_number: int = 1
    parent_receipt_id: str | None = None
    source: str = "live"
    replay_run_id: str | None = None
    retry_max_attempts: int | None = None
    retry_backoff_base: float | None = None
    retry_max_delay: float | None = None
    retry_jitter: bool | None = None
    created_at: datetime = msgspec.field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ---------------------------------------------------------------------------
# Canonical event
# ---------------------------------------------------------------------------


class CanonicalEvent(msgspec.Struct, frozen=True):
    """The universal, immutable event envelope for the medre.

    Every event – whether sourced from an external adapter, synthesised
    by the framework, or produced as a delivery artifact – is represented
    as a ``CanonicalEvent``.  All fields are immutable (``frozen=True``).

    Attributes
    ----------
    event_id:
        Globally unique event identifier (UUIDv7 or similar time-sortable
        identifier).
    event_kind:
        Event kind string from the :mod:`~medre.core.events.kinds`
        registry.
    schema_version:
        Schema version number for the payload structure.
    timestamp:
        The moment the event occurred (UTC).
    source_adapter:
        Name of the adapter that produced the event.
    source_transport_id:
        Identifier of the transport that carried the event.
    source_channel_id:
        Channel / conversation ID at the source, if applicable.
    parent_event_id:
        ID of the parent event in a derivation chain, if this event was
        derived from another event.
    lineage:
        Ordered list of event IDs forming the derivation ancestry.
    relations:
        Typed links to other events (replies, reactions, edits …).
    payload:
        The event-specific data payload.
    metadata:
        Structured metadata namespaces.
    depth:
        Depth in the derivation tree (0 for source events).
    trace_id:
        Distributed tracing correlation ID.
    """

    event_id: str
    event_kind: str
    schema_version: int
    timestamp: datetime
    source_adapter: str
    source_transport_id: str
    source_channel_id: str | None
    parent_event_id: str | None
    lineage: tuple[str, ...]
    relations: tuple[EventRelation, ...]
    payload: dict[str, object]
    metadata: EventMetadata
    source_native_ref: NativeRef | None = None
    depth: int = 0
    trace_id: str | None = None

    def __post_init__(self) -> None:
        """Validate invariants and enforce deep immutability after construction.

        Converts mutable list/dict constructor inputs to immutable storage
        (tuples and :class:`_FrozenDict`) so that downstream code cannot
        mutate canonical event internals in place.

        Raises :class:`ValueError` if any invariant is violated.
        """
        # -- Normalise mutable containers to immutable storage ---------------
        if isinstance(self.lineage, list):
            force_setattr(self, "lineage", tuple(self.lineage))
        if isinstance(self.relations, list):
            force_setattr(self, "relations", tuple(self.relations))
        if not isinstance(self.payload, _FrozenDict):
            force_setattr(self, "payload", _FrozenDict(self.payload))

        # -- Invariant checks -----------------------------------------------
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("event_id must be a non-empty string")
        if not isinstance(self.event_kind, str) or not self.event_kind:
            raise ValueError("event_kind must be a non-empty string")
        if self.schema_version < 1:
            raise ValueError("schema_version must be >= 1")
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (UTC)")
        if self.depth < 0:
            raise ValueError("depth must be >= 0")
        if self.lineage is None:
            raise ValueError("lineage must not be None")
        if self.relations is None:
            raise ValueError("relations must not be None")
        for _i, eid in enumerate(self.lineage):
            if not isinstance(eid, str) or not eid:
                raise ValueError(
                    f"lineage[{_i}] must be a non-empty string, got {eid!r}"
                )

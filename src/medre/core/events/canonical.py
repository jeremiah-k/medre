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

import msgspec
from datetime import datetime
from enum import Enum
from typing import Literal

from medre.core.events.metadata import EventMetadata


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
        the adapter supports threaded conversations.
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
        Human-readable fallback text for the target when the event body
        is not available.
    metadata:
        Arbitrary key-value metadata attached to this relation.
    """

    relation_type: Literal["reply", "reaction", "edit", "delete", "thread"]
    target_event_id: str | None
    target_native_ref: NativeRef | None
    key: str | None
    fallback_text: str | None
    metadata: dict[str, object] = msgspec.field(default_factory=dict)


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
    native_relation_id:
        ID of the related native entity (e.g. the message being replied
        to) in the adapter's native format.
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
    created_at: datetime = msgspec.field(default_factory=datetime.now)


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
    status:
        Current delivery status.
    error:
        Error message if the delivery failed.
    adapter_message_id:
        Native message ID assigned by the target adapter, once accepted.
    next_retry_at:
        Scheduled time for the next retry attempt, if applicable.
    created_at:
        Timestamp when this receipt was created.
    """

    sequence: int = 0
    receipt_id: str = ""
    event_id: str = ""
    delivery_plan_id: str = ""
    target_adapter: str = ""
    status: Literal[
        "accepted",
        "queued",
        "sent",
        "confirmed",
        "failed",
        "dead_lettered",
    ] = "accepted"
    error: str | None = None
    adapter_message_id: str | None = None
    next_retry_at: datetime | None = None
    created_at: datetime = msgspec.field(default_factory=datetime.now)


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
    lineage: list[str]
    relations: list[EventRelation]
    payload: dict[str, object]
    metadata: EventMetadata
    depth: int = 0
    trace_id: str | None = None

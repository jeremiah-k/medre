"""Serialisation and deserialisation helpers for the SQLite storage layer.

Pure functions that convert between Python domain objects and the raw
values stored in SQLite rows.  No dependency on sibling submodules.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import msgspec

from medre.core.events import (
    CanonicalEvent,
    DeliveryObservation,
    DeliveryReceipt,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.events.metadata import RoutingMetadata
from medre.core.storage.backend import DeliveryOutboxItem


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _encode_json(value: Any) -> str:
    """Encode a value as a JSON string for SQLite storage."""
    return msgspec.json.encode(value).decode()


def _decode_json(text: str) -> Any:
    """Decode a JSON string from SQLite."""
    return msgspec.json.decode(text)


def _serialize_metadata(metadata: EventMetadata) -> str:
    """Serialise the opaque metadata namespaces to a JSON string.

    The ``routing`` namespace never persists here for typed metadata:
    routing facts are typed rows in ``event_routing_facts`` written by the
    same admission transaction (see :func:`routing_facts_op`).  Absent
    namespaces and the empty ``custom`` mapping are omitted, so an event
    without opaque metadata serialises to ``{}`` and keeps the
    ``opaque_envelope`` blocker inactive.  A namespace that is present stays
    present: a non-``None`` typed struct (even one whose fields are all
    ``None``) and a non-empty ``custom`` mapping persist verbatim, so a
    composed read reconstructs exactly the admitted :class:`EventMetadata`.

    A non-``EventMetadata`` metadata value (an untyped mapping installed by
    ``msgspec.structs.replace``, which skips field validation) persists
    verbatim exactly as before this separation; such a value never carries
    typed routing facts and never writes a facts row.
    """
    if not isinstance(metadata, EventMetadata):
        return _encode_json(metadata)
    namespaces: dict[str, Any] = {}
    if metadata.transport is not None:
        namespaces["transport"] = msgspec.to_builtins(metadata.transport)
    if metadata.radio is not None:
        namespaces["radio"] = msgspec.to_builtins(metadata.radio)
    if metadata.telemetry is not None:
        namespaces["telemetry"] = msgspec.to_builtins(metadata.telemetry)
    if metadata.native is not None:
        namespaces["native"] = msgspec.to_builtins(metadata.native)
    if metadata.custom:
        namespaces["custom"] = msgspec.to_builtins(metadata.custom)
    return _encode_json(namespaces)


_ROUTING_FACTS_INSERT = """
INSERT INTO event_routing_facts
    (event_id, matched_routes, fanout_group, route_trace, recorded_at)
VALUES (?, ?, ?, ?, ?)
"""


def routing_facts_op(
    event: CanonicalEvent, recorded_at: str
) -> tuple[str, tuple[Any, ...]] | None:
    """Return the typed routing-facts insert op for an admission transaction.

    ``None`` when the event carries no routing namespace: absence of a row
    is the standing fact that no routing decision was accepted at
    admission.  The op MUST run in the same transaction as the canonical
    event row.  Duplicate admissions never execute their ops, so the row is
    written exactly once with the originally accepted decision.  An untyped
    metadata mapping (see :func:`_serialize_metadata`) never writes a row.
    """
    routing = getattr(event.metadata, "routing", None)
    if routing is None:
        return None
    # Struct construction does not validate annotations. Check the declared
    # fact shape before opaque routing is omitted from the envelope.
    if not isinstance(routing, RoutingMetadata):
        raise ValueError("Admission routing facts require typed routing metadata")
    msgspec.convert(msgspec.structs.asdict(routing), type=RoutingMetadata)
    return (
        _ROUTING_FACTS_INSERT,
        (
            event.event_id,
            _encode_json(routing.matched_routes),
            routing.fanout_group,
            _encode_json(routing.route_trace),
            recorded_at,
        ),
    )


def _deserialize_metadata(raw: str) -> EventMetadata:
    """Reconstruct an :class:`EventMetadata` from its JSON representation."""
    return msgspec.json.decode(raw, type=EventMetadata)


def _row_to_event(
    row: dict[str, Any],
    relations: list[EventRelation],
) -> CanonicalEvent:
    """Map a database row (plus pre-fetched relations) to a :class:`CanonicalEvent`."""
    # Reconstruct source_native_ref from split nullable columns.
    source_native_ref: NativeRef | None = None
    if row.get("source_native_adapter") and row.get("source_native_message_id"):
        source_native_ref = NativeRef(
            adapter=row["source_native_adapter"],
            native_channel_id=row.get("source_native_channel_id"),
            native_message_id=row["source_native_message_id"],
            native_thread_id=row.get("source_native_thread_id"),
        )
    return CanonicalEvent(
        event_id=row["event_id"],
        event_kind=row["event_kind"],
        schema_version=row["schema_version"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        source_adapter=row["source_adapter"],
        source_transport_id=row["source_transport_id"],
        source_channel_id=row["source_channel_id"],
        parent_event_id=row["parent_event_id"],
        lineage=tuple(_decode_json(row["lineage"])),
        relations=tuple(relations),
        payload=_decode_json(row["payload"]),
        metadata=_deserialize_metadata(row["metadata"]),
        depth=row["depth"],
        trace_id=row["trace_id"],
        root_event_id=row["root_event_id"],
        conversation_id=row["conversation_id"],
        source_native_ref=source_native_ref,
    )


def _row_to_relation(row: dict[str, Any]) -> EventRelation:
    """Map an ``event_relations`` row to an :class:`EventRelation`."""
    target_native_ref: NativeRef | None = None
    if row.get("target_native_adapter") and row.get("target_native_message_id"):
        target_native_ref = NativeRef(
            adapter=row["target_native_adapter"],
            native_channel_id=row["target_native_channel_id"],
            native_message_id=row["target_native_message_id"],
            native_thread_id=row.get("target_native_thread_id"),
        )
    return EventRelation(
        relation_type=row["relation_type"],
        target_event_id=row["target_event_id"],
        target_native_ref=target_native_ref,
        key=row["key"],
        fallback_text=row.get("resolved_fallback_text"),
        metadata=_decode_json(row["metadata"]),
    )


def _row_to_delivery_observation(row: dict[str, Any]) -> DeliveryObservation:
    """Map a ``delivery_observations`` row to a delivery observation."""
    return DeliveryObservation(
        sequence=row["sequence"],
        observation_id=row["observation_id"],
        event_id=row["event_id"],
        delivery_plan_id=row["delivery_plan_id"],
        target_adapter=row["target_adapter"],
        target_channel=row.get("target_channel"),
        native_channel_id=row.get("native_channel_id"),
        outbox_id=row["outbox_id"],
        attempt_number=row["attempt_number"],
        adapter_message_id=row.get("adapter_message_id"),
        state=row["state"],
        confirmation_level=row.get("confirmation_level") or "unknown",
        error=row.get("error"),
        metadata=_decode_json(row.get("metadata") or "{}"),
        observed_at=datetime.fromisoformat(row["observed_at"]),
    )


def _row_to_receipt(row: dict[str, Any]) -> DeliveryReceipt:
    """Map a ``delivery_receipts`` row to a :class:`DeliveryReceipt`."""
    # Map SQLite INTEGER (0/1) to Python bool for retry_jitter.
    raw_jitter = row.get("retry_jitter")
    jitter_val: bool | None = None
    if raw_jitter is not None:
        jitter_val = bool(raw_jitter)
    return DeliveryReceipt(
        sequence=row["sequence"],
        receipt_id=row["receipt_id"],
        event_id=row["event_id"],
        delivery_plan_id=row["delivery_plan_id"],
        target_adapter=row["target_adapter"],
        target_channel=row.get("target_channel"),
        route_id=row.get("route_id", ""),
        status=row["status"],
        receipt_kind=row.get("receipt_kind"),
        error=row["error"],
        failure_kind=row.get("failure_kind"),
        capability_level=row.get("capability_level"),
        capability_field=row.get("capability_field"),
        capability_reason=row.get("capability_reason"),
        delivery_strategy=row.get("delivery_strategy"),
        adapter_message_id=row["adapter_message_id"],
        next_retry_at=(
            datetime.fromisoformat(row["next_retry_at"])
            if row["next_retry_at"]
            else None
        ),
        attempt_number=row.get("attempt_number", 1),
        parent_receipt_id=row.get("parent_receipt_id"),
        source=row.get("source", "live"),
        replay_run_id=row.get("replay_run_id"),
        retry_max_attempts=row.get("retry_max_attempts"),
        retry_backoff_base=row.get("retry_backoff_base"),
        retry_max_delay=row.get("retry_max_delay"),
        retry_jitter=jitter_val,
        rendering_evidence=row.get("rendering_evidence"),
        outbox_id=row.get("outbox_id"),
        confirmation_level=row.get("confirmation_level") or "unknown",
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_native_ref(row: dict[str, Any]) -> NativeMessageRef:
    """Map a ``native_message_refs`` row to a :class:`NativeMessageRef`."""
    return NativeMessageRef(
        id=row["id"],
        event_id=row["event_id"],
        adapter=row["adapter"],
        native_channel_id=row["native_channel_id"],
        native_message_id=row["native_message_id"],
        native_thread_id=row.get("native_thread_id"),
        native_relation_id=row.get("native_relation_id"),
        direction=row["direction"],
        metadata=_decode_json(row["metadata"]) if row.get("metadata") else {},
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_outbox_item(row: dict[str, Any]) -> DeliveryOutboxItem:
    """Map a ``delivery_outbox`` row to a :class:`DeliveryOutboxItem`."""
    meta_raw = row.get("metadata", "{}")
    try:
        meta: dict[str, Any] = (
            _decode_json(meta_raw) if isinstance(meta_raw, str) else {}
        )
    except msgspec.DecodeError:
        meta = {}
    return DeliveryOutboxItem(
        outbox_id=row["outbox_id"],
        event_id=row["event_id"],
        route_id=row.get("route_id", ""),
        delivery_plan_id=row["delivery_plan_id"],
        target_adapter=row["target_adapter"],
        target_channel=row.get("target_channel"),
        target_address=row.get("target_address"),
        attempt_number=row.get("attempt_number", 1),
        active_attempt=row.get("active_attempt"),
        status=row.get("status", "pending"),
        failure_kind=row.get("failure_kind"),
        failure_kind_detail=row.get("failure_kind_detail"),
        next_attempt_at=row.get("next_attempt_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        last_attempt_at=row.get("last_attempt_at"),
        locked_at=row.get("locked_at"),
        lease_until=row.get("lease_until"),
        worker_id=row.get("worker_id"),
        payload_hash=row.get("payload_hash"),
        receipt_id=row.get("receipt_id"),
        parent_receipt_id=row.get("parent_receipt_id"),
        error_summary=row.get("error_summary"),
        dispatch_source=row.get("dispatch_source"),
        replay_run_id=row.get("replay_run_id"),
        metadata=meta,
    )


def _ensure_iso(value: str | datetime | None) -> str | None:
    """Coerce a value to an ISO-8601 string for SQLite storage.

    Accepts ``None`` (pass-through), an existing ``str`` (pass-through),
    or a ``datetime`` instance (converted via ``.isoformat()``).  This
    avoids passing raw ``datetime`` objects to SQLite, which triggers
    Python 3.12's ``DeprecationWarning`` for the default datetime adapter.

    NOTE: The diagnostics layer has a separate datetime→ISO path
    (_to_iso / _parse_iso_timestamp in lifecycle_convergence.py).
    These serve different contexts (storage vs diagnostics) and
    should not be unified — each has domain-specific edge cases.
    """
    if value is None or isinstance(value, str):
        return value
    return value.isoformat()


def _add_seconds_iso(iso_str: str, seconds: int) -> str:
    """Add *seconds* to an ISO-8601 string and return the new ISO string."""
    try:
        dt = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return iso_str
    return (dt + timedelta(seconds=seconds)).isoformat()

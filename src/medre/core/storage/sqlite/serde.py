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
    DeliveryReceipt,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
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
    """Serialise an :class:`EventMetadata` instance to a JSON string."""
    return msgspec.json.encode(metadata).decode()


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
        relation_type=row["relation_type"],  # type: ignore[arg-type]
        target_event_id=row["target_event_id"],
        target_native_ref=target_native_ref,
        key=row["key"],
        fallback_text=row["fallback_text"],
        metadata=_decode_json(row["metadata"]),
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
        status=row["status"],  # type: ignore[arg-type]
        error=row["error"],
        failure_kind=row.get("failure_kind"),
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

"""Authoritative column-level classification contract for persistent storage.

Every persistent table and column in the storage schema carries exactly one
:class:`ColumnClass`.  The registry below is **declared data**, not inferred
from DDL or runtime behavior; the schema-walk guard test proves it covers the
real SQLite schema and the equivalence guard test proves it agrees with the
enforced content inventory and the collector's conservative decisions.

Classes partition columns by ownership semantics, not by physical type:

1. ``immutable_identity`` — identifiers and correlation facts that never
   change after write (event IDs, native message IDs, relation targets,
   attempt fingerprints, recorded write-once timestamps).
2. ``operational_authority`` — durable delivery/recovery facts that guarded
   transitions compare or advance (statuses, ownership and lease facts,
   duplicate-prevention authority, settlement and expiry bounds,
   confirmation levels).
3. ``independently_owned_content`` — free-form values whose lifetime is
   content ownership rather than any delivery fact (rendered text, opaque
   envelope JSON, provider diagnostic text, retained blobs).  These mirror
   the enforced ``CONTENT_SURFACES`` inventory exactly.
4. ``unclassified`` — no reviewed column-level classification yet; any
   consumer MUST treat the column conservatively as potentially
   content-bearing.

This module classifies columns only.  A column class never authorizes any
read, deletion, or collection: family-level collection conservatism remains
governed by ``docs/spec/content-collection-guard.md`` and
``docs/spec/content-copy-classification.md``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum


class ColumnClass(str, Enum):
    """Ownership class of one persistent storage column."""

    IMMUTABLE_IDENTITY = "immutable_identity"
    OPERATIONAL_AUTHORITY = "operational_authority"
    INDEPENDENTLY_OWNED_CONTENT = "independently_owned_content"
    UNCLASSIFIED = "unclassified"


#: Content-potential classes: a column of either class may hold content whose
#: lifetime is content ownership or is not yet reviewed.
CONTENT_POTENTIAL_CLASSES: frozenset[ColumnClass] = frozenset(
    {ColumnClass.INDEPENDENTLY_OWNED_CONTENT, ColumnClass.UNCLASSIFIED}
)


@dataclass(frozen=True, slots=True)
class ColumnClassification:
    """One declared table → column → class registry entry."""

    table: str
    column: str
    column_class: ColumnClass

    @property
    def has_content_potential(self) -> bool:
        """Whether the column's class may hold independently owned content."""
        return self.column_class in CONTENT_POTENTIAL_CLASSES


#: The declared registry: every persistent storage table with the class of
#: every column.  Tables excluded on purpose:
#:
#: * ``delivery_status`` is a VIEW; it persists no values of its own and its
#:   columns are classified through ``delivery_receipts``.
#: * SQLite-internal tables (``sqlite_sequence``) are not MEDRE storage.
#:
#: A table not listed here fails the schema-walk guard test, so every new
#: persistent table and column MUST be added with a reviewed class.
STORAGE_COLUMN_CLASSES: dict[str, dict[str, ColumnClass]] = {
    "canonical_events": {
        "event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "event_kind": ColumnClass.IMMUTABLE_IDENTITY,
        "schema_version": ColumnClass.IMMUTABLE_IDENTITY,
        "timestamp": ColumnClass.IMMUTABLE_IDENTITY,
        "source_adapter": ColumnClass.IMMUTABLE_IDENTITY,
        "source_transport_id": ColumnClass.IMMUTABLE_IDENTITY,
        "source_channel_id": ColumnClass.IMMUTABLE_IDENTITY,
        "parent_event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "lineage": ColumnClass.IMMUTABLE_IDENTITY,
        "payload": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "metadata": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "depth": ColumnClass.IMMUTABLE_IDENTITY,
        "trace_id": ColumnClass.IMMUTABLE_IDENTITY,
        "root_event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "conversation_id": ColumnClass.IMMUTABLE_IDENTITY,
        "source_native_adapter": ColumnClass.IMMUTABLE_IDENTITY,
        "source_native_channel_id": ColumnClass.IMMUTABLE_IDENTITY,
        "source_native_message_id": ColumnClass.IMMUTABLE_IDENTITY,
        "source_native_thread_id": ColumnClass.IMMUTABLE_IDENTITY,
        "created_at": ColumnClass.IMMUTABLE_IDENTITY,
    },
    "event_body_identity": {
        "event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "version": ColumnClass.IMMUTABLE_IDENTITY,
        # Immutable admission fact, but the guarded collector validates it
        # fail-closed, so it is delivery-adjacent authority, not correlation.
        "present_at_admission": ColumnClass.OPERATIONAL_AUTHORITY,
        "recorded_at": ColumnClass.IMMUTABLE_IDENTITY,
    },
    "event_body_content": {
        "event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "field": ColumnClass.IMMUTABLE_IDENTITY,
        "body": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "created_at": ColumnClass.IMMUTABLE_IDENTITY,
    },
    "event_content_dependencies": {
        "event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "owner_kind": ColumnClass.IMMUTABLE_IDENTITY,
        "owner_id": ColumnClass.IMMUTABLE_IDENTITY,
        "acquired_at": ColumnClass.IMMUTABLE_IDENTITY,
        "deadline": ColumnClass.OPERATIONAL_AUTHORITY,
        "released_at": ColumnClass.OPERATIONAL_AUTHORITY,
    },
    "event_content_access_policies": {
        "event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "version": ColumnClass.IMMUTABLE_IDENTITY,
        "hard_ceiling": ColumnClass.OPERATIONAL_AUTHORITY,
        "allowed_consumers": ColumnClass.OPERATIONAL_AUTHORITY,
        "max_optional_hold_microseconds": ColumnClass.OPERATIONAL_AUTHORITY,
        "created_at": ColumnClass.IMMUTABLE_IDENTITY,
    },
    "event_content_access_grants": {
        "event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "scope": ColumnClass.IMMUTABLE_IDENTITY,
        "consumer": ColumnClass.IMMUTABLE_IDENTITY,
        "issued_at": ColumnClass.IMMUTABLE_IDENTITY,
        "expires_at": ColumnClass.OPERATIONAL_AUTHORITY,
        "revoked_at": ColumnClass.OPERATIONAL_AUTHORITY,
        "expired_at": ColumnClass.OPERATIONAL_AUTHORITY,
    },
    "event_content_collection_log": {
        "event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "collected_at": ColumnClass.IMMUTABLE_IDENTITY,
        "policy": ColumnClass.IMMUTABLE_IDENTITY,
        "rows_removed": ColumnClass.IMMUTABLE_IDENTITY,
        "logical_bytes_removed": ColumnClass.IMMUTABLE_IDENTITY,
    },
    "event_relations": {
        "id": ColumnClass.IMMUTABLE_IDENTITY,
        "event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "relation_type": ColumnClass.IMMUTABLE_IDENTITY,
        "target_event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "target_native_adapter": ColumnClass.IMMUTABLE_IDENTITY,
        "target_native_channel_id": ColumnClass.IMMUTABLE_IDENTITY,
        "target_native_message_id": ColumnClass.IMMUTABLE_IDENTITY,
        "target_native_thread_id": ColumnClass.IMMUTABLE_IDENTITY,
        "key": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "metadata": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "created_at": ColumnClass.IMMUTABLE_IDENTITY,
    },
    "event_relation_excerpts": {
        "relation_id": ColumnClass.IMMUTABLE_IDENTITY,
        "excerpt": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
    },
    "conversation_membership": {
        "event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "root_event_id": ColumnClass.OPERATIONAL_AUTHORITY,
        "conversation_id": ColumnClass.OPERATIONAL_AUTHORITY,
        "resolved_target_event_id": ColumnClass.OPERATIONAL_AUTHORITY,
        "relation_type": ColumnClass.OPERATIONAL_AUTHORITY,
        "depth": ColumnClass.OPERATIONAL_AUTHORITY,
        "resolution_state": ColumnClass.OPERATIONAL_AUTHORITY,
        "updated_at": ColumnClass.OPERATIONAL_AUTHORITY,
    },
    "conversation_projection_state": {
        "singleton_id": ColumnClass.IMMUTABLE_IDENTITY,
        "projection_revision": ColumnClass.OPERATIONAL_AUTHORITY,
        "status": ColumnClass.OPERATIONAL_AUTHORITY,
        "last_event_id": ColumnClass.OPERATIONAL_AUTHORITY,
        "updated_at": ColumnClass.OPERATIONAL_AUTHORITY,
    },
    "native_message_refs": {
        "id": ColumnClass.IMMUTABLE_IDENTITY,
        "event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "adapter": ColumnClass.IMMUTABLE_IDENTITY,
        "native_channel_id": ColumnClass.IMMUTABLE_IDENTITY,
        "native_message_id": ColumnClass.IMMUTABLE_IDENTITY,
        "native_thread_id": ColumnClass.IMMUTABLE_IDENTITY,
        "native_relation_id": ColumnClass.IMMUTABLE_IDENTITY,
        "direction": ColumnClass.IMMUTABLE_IDENTITY,
        "metadata": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "created_at": ColumnClass.IMMUTABLE_IDENTITY,
    },
    "delivery_receipts": {
        "sequence": ColumnClass.IMMUTABLE_IDENTITY,
        "receipt_id": ColumnClass.IMMUTABLE_IDENTITY,
        "event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "delivery_plan_id": ColumnClass.IMMUTABLE_IDENTITY,
        "target_adapter": ColumnClass.IMMUTABLE_IDENTITY,
        # Structured route correlation within the append-only evidence
        # family; the inventory does not review it as free-form text.
        "target_channel": ColumnClass.IMMUTABLE_IDENTITY,
        "route_id": ColumnClass.IMMUTABLE_IDENTITY,
        "status": ColumnClass.OPERATIONAL_AUTHORITY,
        "receipt_kind": ColumnClass.IMMUTABLE_IDENTITY,
        "error": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "failure_kind": ColumnClass.OPERATIONAL_AUTHORITY,
        "capability_level": ColumnClass.OPERATIONAL_AUTHORITY,
        "capability_field": ColumnClass.OPERATIONAL_AUTHORITY,
        "capability_reason": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "delivery_strategy": ColumnClass.OPERATIONAL_AUTHORITY,
        "adapter_message_id": ColumnClass.IMMUTABLE_IDENTITY,
        "next_retry_at": ColumnClass.OPERATIONAL_AUTHORITY,
        "attempt_number": ColumnClass.IMMUTABLE_IDENTITY,
        "parent_receipt_id": ColumnClass.IMMUTABLE_IDENTITY,
        "source": ColumnClass.IMMUTABLE_IDENTITY,
        "replay_run_id": ColumnClass.IMMUTABLE_IDENTITY,
        "retry_max_attempts": ColumnClass.OPERATIONAL_AUTHORITY,
        "retry_backoff_base": ColumnClass.OPERATIONAL_AUTHORITY,
        "retry_max_delay": ColumnClass.OPERATIONAL_AUTHORITY,
        "retry_jitter": ColumnClass.OPERATIONAL_AUTHORITY,
        "rendering_evidence": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "outbox_id": ColumnClass.IMMUTABLE_IDENTITY,
        "confirmation_level": ColumnClass.OPERATIONAL_AUTHORITY,
        "created_at": ColumnClass.IMMUTABLE_IDENTITY,
    },
    "delivery_outbox": {
        "outbox_id": ColumnClass.IMMUTABLE_IDENTITY,
        "event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "route_id": ColumnClass.IMMUTABLE_IDENTITY,
        "delivery_plan_id": ColumnClass.IMMUTABLE_IDENTITY,
        "target_adapter": ColumnClass.IMMUTABLE_IDENTITY,
        "target_channel": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "target_address": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "attempt_number": ColumnClass.OPERATIONAL_AUTHORITY,
        "active_attempt": ColumnClass.OPERATIONAL_AUTHORITY,
        "status": ColumnClass.OPERATIONAL_AUTHORITY,
        "failure_kind": ColumnClass.OPERATIONAL_AUTHORITY,
        "failure_kind_detail": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "next_attempt_at": ColumnClass.OPERATIONAL_AUTHORITY,
        "created_at": ColumnClass.IMMUTABLE_IDENTITY,
        "updated_at": ColumnClass.OPERATIONAL_AUTHORITY,
        "last_attempt_at": ColumnClass.OPERATIONAL_AUTHORITY,
        "locked_at": ColumnClass.OPERATIONAL_AUTHORITY,
        "lease_until": ColumnClass.OPERATIONAL_AUTHORITY,
        "worker_id": ColumnClass.OPERATIONAL_AUTHORITY,
        "payload_hash": ColumnClass.OPERATIONAL_AUTHORITY,
        "receipt_id": ColumnClass.OPERATIONAL_AUTHORITY,
        "parent_receipt_id": ColumnClass.IMMUTABLE_IDENTITY,
        "error_summary": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "dispatch_source": ColumnClass.OPERATIONAL_AUTHORITY,
        "replay_run_id": ColumnClass.IMMUTABLE_IDENTITY,
        "metadata": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
    },
    "delivery_observations": {
        "sequence": ColumnClass.IMMUTABLE_IDENTITY,
        "observation_id": ColumnClass.IMMUTABLE_IDENTITY,
        "event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "delivery_plan_id": ColumnClass.IMMUTABLE_IDENTITY,
        "target_adapter": ColumnClass.IMMUTABLE_IDENTITY,
        "target_channel": ColumnClass.IMMUTABLE_IDENTITY,
        "native_channel_id": ColumnClass.IMMUTABLE_IDENTITY,
        "outbox_id": ColumnClass.IMMUTABLE_IDENTITY,
        "attempt_number": ColumnClass.IMMUTABLE_IDENTITY,
        "adapter_message_id": ColumnClass.IMMUTABLE_IDENTITY,
        "state": ColumnClass.OPERATIONAL_AUTHORITY,
        "confirmation_level": ColumnClass.OPERATIONAL_AUTHORITY,
        "error": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "metadata": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "observed_at": ColumnClass.IMMUTABLE_IDENTITY,
    },
    "radio_attempt_progress": {
        "outbox_id": ColumnClass.IMMUTABLE_IDENTITY,
        "attempt_number": ColumnClass.IMMUTABLE_IDENTITY,
        "plan_fingerprint": ColumnClass.IMMUTABLE_IDENTITY,
        "plan_json": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "provenance_json": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "parts_json": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "revision": ColumnClass.OPERATIONAL_AUTHORITY,
        "created_at": ColumnClass.IMMUTABLE_IDENTITY,
        "updated_at": ColumnClass.OPERATIONAL_AUTHORITY,
    },
    "durable_ingress_work": {
        "event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "provenance": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "status": ColumnClass.OPERATIONAL_AUTHORITY,
        "attempts": ColumnClass.OPERATIONAL_AUTHORITY,
        "last_error": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "created_at": ColumnClass.IMMUTABLE_IDENTITY,
        "updated_at": ColumnClass.OPERATIONAL_AUTHORITY,
        "locked_at": ColumnClass.OPERATIONAL_AUTHORITY,
        "lease_until": ColumnClass.OPERATIONAL_AUTHORITY,
        "worker_id": ColumnClass.OPERATIONAL_AUTHORITY,
    },
    "adapter_checkpoints": {
        "adapter_id": ColumnClass.IMMUTABLE_IDENTITY,
        "stream": ColumnClass.IMMUTABLE_IDENTITY,
        "cursor": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "metadata": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "updated_at": ColumnClass.OPERATIONAL_AUTHORITY,
    },
    "attachment_blobs": {
        "content_ref": ColumnClass.IMMUTABLE_IDENTITY,
        "size_bytes": ColumnClass.IMMUTABLE_IDENTITY,
        "media_kind": ColumnClass.IMMUTABLE_IDENTITY,
        "mime_type": ColumnClass.IMMUTABLE_IDENTITY,
        "created_at": ColumnClass.IMMUTABLE_IDENTITY,
        "data": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
    },
    # The association row is the only authority for loading retained bytes
    # for an event; content_ref is that load authority, not mere correlation.
    "event_attachment_associations": {
        "event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "content_ref": ColumnClass.OPERATIONAL_AUTHORITY,
        "created_at": ColumnClass.IMMUTABLE_IDENTITY,
    },
    # Counters-only aggregate; the (window_start, source, outcome) key never
    # changes per row while count and freshness timestamps mutate.
    "inbound_pressure_observations": {
        "window_start": ColumnClass.IMMUTABLE_IDENTITY,
        "source": ColumnClass.IMMUTABLE_IDENTITY,
        "outcome": ColumnClass.IMMUTABLE_IDENTITY,
        "count": ColumnClass.OPERATIONAL_AUTHORITY,
        "first_seen_at": ColumnClass.OPERATIONAL_AUTHORITY,
        "last_seen_at": ColumnClass.OPERATIONAL_AUTHORITY,
    },
    "plugin_state": {
        "plugin_id": ColumnClass.IMMUTABLE_IDENTITY,
        "key": ColumnClass.IMMUTABLE_IDENTITY,
        "value": ColumnClass.INDEPENDENTLY_OWNED_CONTENT,
        "updated_at": ColumnClass.OPERATIONAL_AUTHORITY,
    },
    "operator_delivery_decisions": {
        "decision_id": ColumnClass.IMMUTABLE_IDENTITY,
        "actor_id": ColumnClass.IMMUTABLE_IDENTITY,
        "event_id": ColumnClass.IMMUTABLE_IDENTITY,
        "delivery_plan_id": ColumnClass.IMMUTABLE_IDENTITY,
        "target_adapter": ColumnClass.IMMUTABLE_IDENTITY,
        # The copy family is conservatively blocked as unreviewed; its
        # route-target text has no reviewed column-level classification.
        "target_channel": ColumnClass.UNCLASSIFIED,
        "outbox_id": ColumnClass.IMMUTABLE_IDENTITY,
        "observed_generation": ColumnClass.OPERATIONAL_AUTHORITY,
        "observed_updated_at": ColumnClass.OPERATIONAL_AUTHORITY,
        "observed_status": ColumnClass.OPERATIONAL_AUTHORITY,
        "action": ColumnClass.OPERATIONAL_AUTHORITY,
        "recorded_at": ColumnClass.IMMUTABLE_IDENTITY,
    },
    "_medre_schema_meta": {
        "key": ColumnClass.IMMUTABLE_IDENTITY,
        "value": ColumnClass.OPERATIONAL_AUTHORITY,
    },
}

#: Precomputed entry index for O(1) lookups and deterministic iteration.
_ENTRIES: dict[tuple[str, str], ColumnClass] = {
    (table, column): column_class
    for table, columns in STORAGE_COLUMN_CLASSES.items()
    for column, column_class in columns.items()
}


def export_registry() -> dict[str, object]:
    """Export the versioned declared registry without inferring any classes."""
    return {
        "format": "medre-storage-column-classification",
        "version": 1,
        "classes": [member.value for member in ColumnClass],
        "tables": [
            {
                "table": table,
                "columns": [
                    {"column": column, "column_class": cls.value}
                    for column, cls in columns.items()
                ],
            }
            for table, columns in STORAGE_COLUMN_CLASSES.items()
        ],
    }


def known_tables() -> tuple[str, ...]:
    """Return every classified persistent table in declaration order."""
    return tuple(STORAGE_COLUMN_CLASSES)


def has_table(table: str) -> bool:
    """Whether *table* has a classification registry entry."""
    return table in STORAGE_COLUMN_CLASSES


def columns_for_table(table: str) -> tuple[str, ...]:
    """Return the classified columns of *table* in declaration order.

    Raises
    ------
    KeyError
        If *table* is not classified.
    """
    return tuple(STORAGE_COLUMN_CLASSES[table])


def column_class(table: str, column: str) -> ColumnClass | None:
    """Return the class of one column, or ``None`` when unclassified.

    ``None`` means the registry does not cover the column; callers MUST
    treat an uncovered persistent column as conservatively blocked.
    """
    return _ENTRIES.get((table, column))


def entries() -> tuple[ColumnClassification, ...]:
    """Return every registry entry in declaration order."""
    return tuple(
        ColumnClassification(table, column, column_class)
        for (table, column), column_class in _ENTRIES.items()
    )


def iter_entries() -> Iterator[ColumnClassification]:
    """Iterate every registry entry in declaration order."""
    return iter(entries())


def entries_of_class(column_class: ColumnClass) -> tuple[ColumnClassification, ...]:
    """Return every entry classified as *column_class*."""
    return tuple(entry for entry in entries() if entry.column_class == column_class)


def content_potential_entries() -> tuple[ColumnClassification, ...]:
    """Return every entry that may hold independently owned content."""
    return tuple(entry for entry in entries() if entry.has_content_potential)


def validate_registry() -> None:
    """Validate declared-registry invariants that typing alone cannot express.

    Raises
    ------
    ValueError
        If a table or column name is malformed, a table has no columns, or
        the same (table, column) pair is declared twice.
    """
    seen: set[tuple[str, str]] = set()
    for table, columns in STORAGE_COLUMN_CLASSES.items():
        if not table or table != table.lower() or table.startswith("sqlite_"):
            raise ValueError(f"Invalid classified table name: {table!r}")
        if not columns:
            raise ValueError(f"Classified table without columns: {table!r}")
        for column, column_class in columns.items():
            if not column or column != column.lower():
                raise ValueError(f"Invalid classified column name: {table}.{column}")
            if not isinstance(column_class, ColumnClass):
                raise ValueError(f"Non-ColumnClass entry for {table}.{column}")
            key = (table, column)
            if key in seen:
                raise ValueError(f"Duplicate classification entry: {table}.{column}")
            seen.add(key)
    if not seen:
        raise ValueError("The classification registry is empty")

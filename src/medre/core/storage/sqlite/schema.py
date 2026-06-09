"""Schema DDL, indexes, and schema-version metadata.

This module owns all database-shape definitions used by the SQLite storage
backend.  No SQL content should be changed without a corresponding schema
version bump (see :data:`_EXPECTED_SCHEMA_VERSION`).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS canonical_events (
    event_id TEXT PRIMARY KEY,
    event_kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    source_adapter TEXT NOT NULL,
    source_transport_id TEXT NOT NULL,
    source_channel_id TEXT,
    parent_event_id TEXT,
    lineage TEXT NOT NULL DEFAULT '[]',
    payload TEXT NOT NULL DEFAULT '{}',
    metadata TEXT NOT NULL DEFAULT '{}',
    depth INTEGER NOT NULL DEFAULT 0,
    trace_id TEXT,
    root_event_id TEXT,
    conversation_id TEXT,
    source_native_adapter TEXT,
    source_native_channel_id TEXT,
    source_native_message_id TEXT,
    source_native_thread_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    relation_type TEXT NOT NULL,
    target_event_id TEXT,
    target_native_adapter TEXT,
    target_native_channel_id TEXT,
    target_native_message_id TEXT,
    target_native_thread_id TEXT,
    key TEXT,
    fallback_text TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS native_message_refs (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    adapter TEXT NOT NULL,
    native_channel_id TEXT,
    native_message_id TEXT NOT NULL,
    native_thread_id TEXT,
    native_relation_id TEXT,
    direction TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(adapter, native_channel_id, native_message_id)
);

CREATE TABLE IF NOT EXISTS delivery_receipts (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT UNIQUE NOT NULL,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    delivery_plan_id TEXT NOT NULL,
    target_adapter TEXT NOT NULL,
    target_channel TEXT,
    route_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    error TEXT,
    failure_kind TEXT,
    adapter_message_id TEXT,
    next_retry_at TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    parent_receipt_id TEXT,
    source TEXT NOT NULL DEFAULT 'live',
    replay_run_id TEXT,
    retry_max_attempts INTEGER,
    retry_backoff_base REAL,
    retry_max_delay REAL,
    retry_jitter INTEGER,
    rendering_evidence TEXT,
    outbox_id TEXT,
    created_at TEXT NOT NULL
);

-- delivery_status view: one row per unique (delivery_plan_id, target_adapter,
-- target_channel) tuple, projecting the latest receipt via MAX(sequence).
-- COALESCE(target_channel, '') in GROUP BY ensures that NULL and '' channels
-- are treated as the same group, avoiding duplicate rows when some receipts
-- have NULL and others have '' for target_channel.
-- Drop and recreate to ensure column shape stays current (e.g. when
-- rendering_evidence is added).
DROP VIEW IF EXISTS delivery_status;
CREATE VIEW delivery_status AS
SELECT dr.sequence, dr.receipt_id, dr.event_id, dr.delivery_plan_id,
       dr.target_adapter, dr.target_channel, dr.route_id, dr.status, dr.error,
       dr.failure_kind,
       dr.adapter_message_id, dr.next_retry_at, dr.attempt_number,
       dr.parent_receipt_id, dr.source, dr.replay_run_id,
       dr.retry_max_attempts, dr.retry_backoff_base,
       dr.retry_max_delay, dr.retry_jitter, dr.rendering_evidence,
       dr.outbox_id, dr.created_at
FROM delivery_receipts dr
JOIN (
    SELECT delivery_plan_id, target_adapter, target_channel, MAX(sequence) AS max_seq
    FROM delivery_receipts GROUP BY delivery_plan_id, target_adapter, COALESCE(target_channel, '')
) latest ON dr.sequence = latest.max_seq;

CREATE TABLE IF NOT EXISTS delivery_outbox (
    outbox_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    route_id TEXT NOT NULL DEFAULT '',
    delivery_plan_id TEXT NOT NULL,
    target_adapter TEXT NOT NULL,
    target_channel TEXT,
    target_address TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    failure_kind TEXT,
    failure_kind_detail TEXT,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_attempt_at TEXT,
    locked_at TEXT,
    lease_until TEXT,
    worker_id TEXT,
    payload_hash TEXT,
    receipt_id TEXT,
    parent_receipt_id TEXT,
    error_summary TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    UNIQUE(delivery_plan_id, target_adapter, target_channel, attempt_number)
);

CREATE TABLE IF NOT EXISTS plugin_state (
    plugin_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(plugin_id, key)
);

CREATE TABLE IF NOT EXISTS _medre_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Targeted indexes matching actual query patterns.
# Run AFTER shape validation so that old-shape DBs fail with a clear
# StorageInitializationError before index creation is attempted.
# NOTE: native_message_refs(adapter, native_channel_id, native_message_id) is
# already covered by the UNIQUE constraint autoindex; no manual duplicate needed.
# NOTE: idx_nrefs_event_created replaces the older idx_nrefs_event_id.  The
# composite (event_id, created_at) covers the WHERE + ORDER BY of
# _SELECT_NREFS_FOR_EVENT and is a strict superset of the single-column index.
# The DROP IF EXISTS handles old prerelease databases that still have the
# previous single-column index.  This is index-shape cleanup, not a
# schema-versioned migration.
_INDEXES: str = """
CREATE INDEX IF NOT EXISTS idx_events_timestamp
    ON canonical_events(timestamp, event_id);
CREATE INDEX IF NOT EXISTS idx_relations_event_id
    ON event_relations(event_id, id);
DROP INDEX IF EXISTS idx_nrefs_event_id;
CREATE INDEX IF NOT EXISTS idx_nrefs_event_created
    ON native_message_refs(event_id, created_at);
CREATE INDEX IF NOT EXISTS idx_receipts_plan
    ON delivery_receipts(delivery_plan_id, target_adapter, target_channel, attempt_number, sequence);
CREATE INDEX IF NOT EXISTS idx_receipts_event
    ON delivery_receipts(event_id, sequence);
CREATE INDEX IF NOT EXISTS idx_receipts_replay_run
    ON delivery_receipts(replay_run_id);
CREATE INDEX IF NOT EXISTS idx_receipts_source
    ON delivery_receipts(source, replay_run_id);
CREATE INDEX IF NOT EXISTS idx_receipts_retry_due
    ON delivery_receipts(status, failure_kind, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_receipts_parent_retry
    ON delivery_receipts(parent_receipt_id, source);
CREATE INDEX IF NOT EXISTS idx_outbox_due
    ON delivery_outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_outbox_plan_target
    ON delivery_outbox(delivery_plan_id, target_adapter, target_channel);
CREATE INDEX IF NOT EXISTS idx_outbox_event
    ON delivery_outbox(event_id);
CREATE INDEX IF NOT EXISTS idx_outbox_event_created
    ON delivery_outbox(event_id, created_at, outbox_id);
-- SQLite treats NULL != NULL in UNIQUE constraints.  This partial unique
-- index closes the gap: no two outbox items with NULL target_channel can
-- share the same (delivery_plan_id, target_adapter, attempt_number) tuple.
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_null_channel_unique
    ON delivery_outbox(delivery_plan_id, target_adapter, attempt_number)
    WHERE target_channel IS NULL;
"""

# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

_EXPECTED_SCHEMA_VERSION: int = 1
"""Frozen schema version — stays at **1** until the project curator declares a
release compatibility boundary.  DDL shape changes during pre-release do **not**
require a version bump; the expected version is only incremented once the
storage contract is formally release-tracked.

That said, the version **is** checked on every
:meth:`SQLiteStorage.initialize` call and a mismatch will raise an error.
This strictness is intentional: it catches databases that were manually made
incompatible (e.g. by an older pre-release build) even before any migration
machinery exists.
"""

# ---------------------------------------------------------------------------
# Required column inventory  (derived from _SCHEMA DDL above)
# ---------------------------------------------------------------------------

_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "canonical_events": frozenset(
        {
            "event_id",
            "event_kind",
            "schema_version",
            "timestamp",
            "source_adapter",
            "source_transport_id",
            "source_channel_id",
            "parent_event_id",
            "lineage",
            "payload",
            "metadata",
            "depth",
            "trace_id",
            "root_event_id",
            "conversation_id",
            "source_native_adapter",
            "source_native_channel_id",
            "source_native_message_id",
            "source_native_thread_id",
            "created_at",
        }
    ),
    "event_relations": frozenset(
        {
            "id",
            "event_id",
            "relation_type",
            "target_event_id",
            "target_native_adapter",
            "target_native_channel_id",
            "target_native_message_id",
            "target_native_thread_id",
            "key",
            "fallback_text",
            "metadata",
            "created_at",
        }
    ),
    "native_message_refs": frozenset(
        {
            "id",
            "event_id",
            "adapter",
            "native_channel_id",
            "native_message_id",
            "native_thread_id",
            "native_relation_id",
            "direction",
            "metadata",
            "created_at",
        }
    ),
    "delivery_receipts": frozenset(
        {
            "sequence",
            "receipt_id",
            "event_id",
            "delivery_plan_id",
            "target_adapter",
            "target_channel",
            "route_id",
            "status",
            "error",
            "failure_kind",
            "adapter_message_id",
            "next_retry_at",
            "attempt_number",
            "parent_receipt_id",
            "source",
            "replay_run_id",
            "retry_max_attempts",
            "retry_backoff_base",
            "retry_max_delay",
            "retry_jitter",
            "rendering_evidence",
            "outbox_id",
            "created_at",
        }
    ),
    "delivery_outbox": frozenset(
        {
            "outbox_id",
            "event_id",
            "route_id",
            "delivery_plan_id",
            "target_adapter",
            "target_channel",
            "target_address",
            "attempt_number",
            "status",
            "failure_kind",
            "failure_kind_detail",
            "next_attempt_at",
            "created_at",
            "updated_at",
            "last_attempt_at",
            "locked_at",
            "lease_until",
            "worker_id",
            "payload_hash",
            "receipt_id",
            "parent_receipt_id",
            "error_summary",
            "metadata",
        }
    ),
    "plugin_state": frozenset(
        {
            "plugin_id",
            "key",
            "value",
            "updated_at",
        }
    ),
    "_medre_schema_meta": frozenset(
        {
            "key",
            "value",
        }
    ),
}

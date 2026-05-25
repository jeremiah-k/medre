"""SQLite-backed storage backend for the medre.

Uses *aiosqlite* when available for native async database access; otherwise
falls back to synchronous ``sqlite3`` wrapped in ``asyncio.to_thread``.
The database runs in WAL mode for safe concurrent reads.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator

import msgspec

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    DuplicateEventError,
    EventFilter,
    StorageError,
    StorageInitializationError,
)

try:
    import aiosqlite  # type: ignore[import-untyped]

    _HAS_AIOSQLITE: bool = True
except ImportError:
    aiosqlite = None  # type: ignore[assignment]
    _HAS_AIOSQLITE = False

logger = logging.getLogger(__name__)


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
    created_at TEXT NOT NULL
);

-- delivery_status view: one row per unique (delivery_plan_id, target_adapter,
-- target_channel) tuple, projecting the latest receipt via MAX(sequence).
-- COALESCE(target_channel, '') in GROUP BY ensures that NULL and '' channels
-- are treated as the same group, avoiding duplicate rows when some receipts
-- have NULL and others have '' for target_channel.
CREATE VIEW IF NOT EXISTS delivery_status AS
SELECT dr.sequence, dr.receipt_id, dr.event_id, dr.delivery_plan_id,
       dr.target_adapter, dr.target_channel, dr.route_id, dr.status, dr.error,
       dr.failure_kind,
       dr.adapter_message_id, dr.next_retry_at, dr.attempt_number,
       dr.parent_receipt_id, dr.source, dr.replay_run_id,
       dr.retry_max_attempts, dr.retry_backoff_base,
       dr.retry_max_delay, dr.retry_jitter, dr.created_at
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
# The DROP IF EXISTS handles databases created before this migration.
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
"""Current storage schema version.

During pre-release development this value stays at **1** — it is only bumped
when the project curator explicitly declares a public compatibility boundary.
Even so, the version is checked on every :meth:`SQLiteStorage.initialize` call
to catch databases that were manually made incompatible (e.g. by an older
pre-release build).  DDL shape changes during pre-release are handled by
updating docs and tests directly; no automatic migrations are provided.
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

# ---------------------------------------------------------------------------
# Prepared statements
# ---------------------------------------------------------------------------

_INSERT_EVENT = """
INSERT INTO canonical_events
    (event_id, event_kind, schema_version, timestamp,
     source_adapter, source_transport_id, source_channel_id,
     parent_event_id, lineage, payload, metadata, depth,
     trace_id, source_native_adapter, source_native_channel_id,
     source_native_message_id, source_native_thread_id,
     created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_RELATION = """
INSERT INTO event_relations
    (event_id, relation_type, target_event_id,
     target_native_adapter, target_native_channel_id,
     target_native_message_id, target_native_thread_id,
     key, fallback_text,
     metadata, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_NATIVE_REF = """
INSERT OR IGNORE INTO native_message_refs
    (id, event_id, adapter, native_channel_id,
     native_message_id, native_thread_id, native_relation_id,
     direction, metadata, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_RECEIPT = """
INSERT INTO delivery_receipts
    (receipt_id, event_id, delivery_plan_id, target_adapter,
     target_channel, route_id, status, error, failure_kind, adapter_message_id,
     next_retry_at, attempt_number, parent_receipt_id, source,
     replay_run_id, retry_max_attempts, retry_backoff_base,
     retry_max_delay, retry_jitter, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_EVENT = "SELECT * FROM canonical_events WHERE event_id = ?"

_SELECT_RELATIONS = "SELECT * FROM event_relations WHERE event_id = ? ORDER BY id ASC"

_RESOLVE_NATIVE_REF = """
SELECT event_id FROM native_message_refs
WHERE adapter = ? AND native_channel_id IS ? AND native_message_id = ?
"""

_DELIVERY_RECEIPT_LATEST_BY_CHANNEL = """
SELECT * FROM delivery_receipts
WHERE delivery_plan_id = ? AND target_adapter = ?
  AND target_channel IS ?
ORDER BY attempt_number DESC, sequence DESC
LIMIT 1
"""

_SELECT_RECEIPTS_FOR_PLAN = """
SELECT * FROM delivery_receipts
WHERE delivery_plan_id = ? AND target_adapter = ?
ORDER BY attempt_number ASC, sequence ASC
"""

_SELECT_RECEIPTS_BY_REPLAY_RUN = """
SELECT * FROM delivery_receipts
WHERE replay_run_id = ?
ORDER BY sequence ASC
"""

_SELECT_RECEIPTS_FOR_EVENT = """
SELECT * FROM delivery_receipts
WHERE event_id = ?
ORDER BY sequence ASC
"""

_SELECT_NREFS_FOR_EVENT = """
SELECT * FROM native_message_refs
WHERE event_id = ?
ORDER BY created_at ASC, id ASC
"""


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


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
        source_native_ref=source_native_ref,
    )


def _row_to_relation(row: dict[str, Any]) -> EventRelation:
    """Map an ``event_relations`` row to an :class:`EventRelation`."""
    target_native_ref: NativeRef | None = None
    if row["target_native_adapter"]:
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
    except Exception:
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


def _add_seconds_iso(iso_str: str, seconds: int) -> str:
    """Add *seconds* to an ISO-8601 string and return the new ISO string."""
    try:
        dt = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return iso_str
    return (dt + timedelta(seconds=seconds)).isoformat()


def _build_query_sql(filt: EventFilter) -> tuple[str, tuple[Any, ...]]:
    """Build a parameterised ``SELECT`` for ``canonical_events``."""
    clauses: list[str] = []
    params: list[Any] = []

    if filt.event_kinds:
        holders = ",".join("?" for _ in filt.event_kinds)
        clauses.append(f"event_kind IN ({holders})")
        params.extend(filt.event_kinds)

    if filt.source_adapters:
        holders = ",".join("?" for _ in filt.source_adapters)
        clauses.append(f"source_adapter IN ({holders})")
        params.extend(filt.source_adapters)

    if filt.time_start:
        clauses.append("timestamp >= ?")
        params.append(filt.time_start.isoformat())

    if filt.time_end:
        clauses.append("timestamp <= ?")
        params.append(filt.time_end.isoformat())

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM canonical_events{where} ORDER BY timestamp ASC, event_id ASC LIMIT ?"  # nosec: clauses are hardcoded field names, values via ? parameters
    params.append(filt.limit)
    return sql, tuple(params)


# ---------------------------------------------------------------------------
# SQLiteStorage
# ---------------------------------------------------------------------------


class SQLiteStorage:
    """Thread-safe, WAL-mode SQLite storage.

    Implements the :class:`~medre.core.storage.backend.StorageBackend`
    protocol.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite database file.  Use ``":memory:"``
        for an in-memory database (useful for testing).

    Notes
    -----
    * Prefers *aiosqlite* for native async database access.  When *aiosqlite*
      is not installed the implementation falls back to synchronous
      ``sqlite3`` wrapped in ``asyncio.to_thread``.
    * The database is opened in WAL mode for safe concurrent reads.
    * All public methods are async and require ``initialize()`` to have been
      called first.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        # The underlying connection is either an aiosqlite.Connection or a
        # plain sqlite3.Connection, depending on whether aiosqlite is
        # available.  We keep the type as ``Any`` because the two
        # connection types are not related and every public method
        # dispatches on ``_use_aiosqlite`` before calling async/sync APIs.
        self._db: Any = None
        self._lock = threading.Lock()
        self._outbox_create_lock = asyncio.Lock()
        self._use_aiosqlite = _HAS_AIOSQLITE

    # -- Internal helpers ---------------------------------------------------

    def _require_db(self) -> Any:
        """Return the active connection or raise if not initialised."""
        if self._db is None:
            raise StorageInitializationError(
                "Storage backend has not been initialised. " "Call initialize() first."
            )
        return self._db

    # -- Lifecycle ----------------------------------------------------------

    async def initialize(self) -> None:
        """Open the database, enable WAL mode, create schema, and verify version.

        Raises
        ------
        StorageInitializationError
            If the database schema version does not match the expected
            version.  The operator must resolve the mismatch manually
            (no silent migration or reset).
        """
        if self._use_aiosqlite:
            db = await aiosqlite.connect(self._db_path)  # type: ignore[union-attr]
            db.row_factory = sqlite3.Row
            await db.executescript(_SCHEMA)
            await db.execute("PRAGMA journal_mode=WAL")
            await db.commit()
            self._db = db
        else:
            self._db = await asyncio.to_thread(self._sync_open)

        try:
            # Verify schema version after DDL.
            await self._verify_schema_version()

            # Verify column shape — catches old pre-release DBs that claim
            # schema_version=1 but predate current columns.
            await self._validate_schema_shape()

            # Create targeted indexes AFTER shape validation so that old-shape
            # databases fail with a clear StorageInitializationError before
            # index creation references missing columns.
            await self._create_indexes()
        except BaseException:
            try:
                await self.close()
            except BaseException:
                logger.debug(
                    "error while closing SQLite storage after initialization failure",
                    exc_info=True,
                )
            raise

    async def _verify_schema_version(self) -> None:
        """Check that the stored schema version matches the expected version.

        On a fresh database the version row does not exist, so we insert it.
        If it exists but mismatches, raise immediately.
        """
        row = await self._read_one(
            "SELECT value FROM _medre_schema_meta WHERE key = 'schema_version'"
        )
        if row is None:
            # Fresh database — stamp the current version.
            await self._write(
                "INSERT INTO _medre_schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(_EXPECTED_SCHEMA_VERSION),),
            )
            return

        stored_version = row["value"]
        try:
            stored_int = int(stored_version)
        except (ValueError, TypeError):
            raise StorageInitializationError(
                f"Storage schema version is not an integer: {stored_version!r}. "
                f"Expected {_EXPECTED_SCHEMA_VERSION}. "
                f"Resolve the mismatch manually — no auto-migration is performed."
            ) from None

        if stored_int != _EXPECTED_SCHEMA_VERSION:
            raise StorageInitializationError(
                f"Storage schema version mismatch: database has version "
                f"{stored_int}, but this version of medre expects version "
                f"{_EXPECTED_SCHEMA_VERSION}. "
                f"Resolve the mismatch manually — no auto-migration or "
                f"silent reset is performed.  Options: export data, delete "
                f"the database file, and restart; or downgrade medre to "
                f"match the database version."
            )

    async def _validate_schema_shape(self) -> None:
        """Verify that every required table has all expected columns.

        This catches old pre-release databases whose ``schema_version`` still
        reads ``1`` but whose column shape predates the current DDL.  The
        check is intentionally lightweight — it inspects ``PRAGMA
        table_info`` for each required table and compares column names
        against :data:`_REQUIRED_COLUMNS`.

        Raises
        ------
        StorageInitializationError
            If any required table or column is missing.  No automatic
            migration is performed; the operator must recreate the DB.
        """
        for table, required in _REQUIRED_COLUMNS.items():
            rows = await self._read_all(f"PRAGMA table_info({table})")
            existing = {row["name"] for row in rows}
            missing = required - existing
            if missing:
                raise StorageInitializationError(
                    f"Pre-release schema shape mismatch: table '{table}' is "
                    f"missing required columns {sorted(missing)}. "
                    f"The database was likely created by an older pre-release "
                    f"build.  Please recreate the database — no automatic "
                    f"migration is provided."
                )

    async def _create_indexes(self) -> None:
        """Create targeted indexes for current query patterns.

        Called after :meth:`_validate_schema_shape` so that old-shape
        databases raise :class:`StorageInitializationError` *before*
        any index DDL references columns that may not exist.
        """
        if self._use_aiosqlite:
            await self._db.executescript(_INDEXES)  # type: ignore[union-attr]
            await self._db.commit()  # type: ignore[union-attr]
        else:
            await asyncio.to_thread(self._sync_create_indexes)

    def _sync_create_indexes(self) -> None:
        """Synchronous counterpart of :meth:`_create_indexes`."""
        db = self._require_db()
        db.executescript(_INDEXES)
        db.commit()

    def _sync_open(self) -> sqlite3.Connection:
        """Synchronous counterpart of :meth:`initialize` for the fallback path."""
        db = sqlite3.connect(self._db_path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        db.execute("PRAGMA journal_mode=WAL")
        db.commit()
        return db

    @classmethod
    async def open_readonly(cls, db_path: str) -> SQLiteStorage:
        """Open an existing database in strict read-only mode.

        Does **not** create the database file, tables, indexes, or metadata
        rows.  Suitable for ``medre inspect`` commands that must never
        mutate storage.

        Raises
        ------
        StorageInitializationError
            If the database file does not exist, has no schema version
            metadata (uninitialised), or has an incompatible schema shape.
        """
        if db_path != ":memory:" and not os.path.exists(db_path):
            raise StorageInitializationError(
                f"Database file does not exist: {db_path}. "
                f"Cannot open in read-only mode — no file was created."
            )

        instance = cls(db_path)

        if instance._use_aiosqlite:
            db = await aiosqlite.connect(  # type: ignore[union-attr]
                f"file:{db_path}?mode=ro",
                uri=True,
            )
            db.row_factory = sqlite3.Row
            instance._db = db
        else:
            instance._db = await asyncio.to_thread(instance._sync_open_readonly)

        try:
            # Validate metadata and shape without writing anything.
            await instance._verify_schema_version_readonly()
            await instance._validate_schema_shape()
        except BaseException:
            try:
                await instance.close()
            except BaseException:
                logger.debug(
                    "error while closing read-only SQLite connection after initialization failure",
                    exc_info=True,
                )
            raise

        return instance

    def _sync_open_readonly(self) -> sqlite3.Connection:
        """Synchronous counterpart of :meth:`open_readonly`."""
        db = sqlite3.connect(
            f"file:{self._db_path}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        db.row_factory = sqlite3.Row
        return db

    async def _verify_schema_version_readonly(self) -> None:
        """Check schema version without writing.

        Unlike :meth:`_verify_schema_version`, this raises immediately when
        the version row is absent (uninitialised database) rather than
        inserting it.
        """
        try:
            row = await self._read_one(
                "SELECT value FROM _medre_schema_meta WHERE key = 'schema_version'"
            )
        except StorageError as exc:
            # Table doesn't exist — database not initialised.
            raise StorageInitializationError(
                "Database has no schema version metadata — likely "
                "uninitialised.  Cannot open in read-only mode."
            ) from exc

        if row is None:
            raise StorageInitializationError(
                "Database has no schema version metadata — likely "
                "uninitialised.  Cannot open in read-only mode."
            )

        stored_version = row["value"]
        try:
            stored_int = int(stored_version)
        except (ValueError, TypeError):
            raise StorageInitializationError(
                f"Storage schema version is not an integer: {stored_version!r}. "
                f"Expected {_EXPECTED_SCHEMA_VERSION}. "
                f"Resolve the mismatch manually — no auto-migration is performed."
            ) from None

        if stored_int != _EXPECTED_SCHEMA_VERSION:
            raise StorageInitializationError(
                f"Storage schema version mismatch: database has version "
                f"{stored_int}, but this version of medre expects version "
                f"{_EXPECTED_SCHEMA_VERSION}. "
                f"Resolve the mismatch manually — no auto-migration or "
                f"silent reset is performed.  Options: export data, delete "
                f"the database file, and restart; or downgrade medre to "
                f"match the database version."
            )

    async def close(self) -> None:
        """Close the underlying database connection and release resources."""
        db = self._db
        if db is None:
            return
        self._db = None
        if self._use_aiosqlite:
            await db.close()
        else:
            await asyncio.to_thread(self._sync_close, db)

    async def count_events(self) -> int:
        """Return the total number of persisted canonical events.

        Returns
        -------
        int
            Count of rows in ``canonical_events``.
        """
        row = await self._read_one("SELECT COUNT(*) AS cnt FROM canonical_events")
        if row is not None:
            return int(row["cnt"])
        return 0

    async def count_receipts(self) -> int:
        """Return the total number of delivery receipt rows.

        Returns
        -------
        int
            Count of rows in ``delivery_receipts``.
        """
        row = await self._read_one("SELECT COUNT(*) AS cnt FROM delivery_receipts")
        if row is not None:
            return int(row["cnt"])
        return 0

    async def count_native_refs(self) -> int:
        """Return the total number of native message ref records.

        Returns
        -------
        int
            Count of rows in ``native_message_refs``.
        """
        row = await self._read_one("SELECT COUNT(*) AS cnt FROM native_message_refs")
        return row["cnt"] if row else 0

    async def count_receipts_by_source(self, source: str) -> int:
        """Return the number of delivery receipts matching *source*.

        Parameters
        ----------
        source:
            The ``source`` column value to match (e.g. ``"live"`` or
            ``"replay"``).

        Returns
        -------
        int
            Count of rows in ``delivery_receipts`` with the given source.
        """
        row = await self._read_one(
            "SELECT COUNT(*) AS cnt FROM delivery_receipts WHERE source = ?",
            (source,),
        )
        return row["cnt"] if row else 0

    async def count_replay_runs(self) -> int:
        """Return the number of distinct ``replay_run_id`` values.

        Counts only non-null ``replay_run_id`` values in
        ``delivery_receipts``.

        Returns
        -------
        int
            Count of distinct replay run IDs.
        """
        row = await self._read_one(
            "SELECT COUNT(DISTINCT replay_run_id) AS cnt FROM delivery_receipts "
            "WHERE replay_run_id IS NOT NULL",
        )
        return row["cnt"] if row else 0

    def _sync_close(self, db: sqlite3.Connection) -> None:
        """Close a synchronous connection (called from a worker thread).

        Acquires ``self._lock`` to avoid closing the connection while a
        synchronous write or read is in progress on the same connection
        object.  Without the lock, ``db.close()`` can race with
        ``db.execute()`` in another thread, producing a use-after-free
        crash in SQLite's C extension.
        """
        with self._lock:
            db.close()

    # -- Read / write primitives --------------------------------------------

    async def _write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Execute a single write statement and commit."""
        db = self._require_db()
        try:
            if self._use_aiosqlite:
                await db.execute(sql, params)
                await db.commit()
            else:
                await asyncio.to_thread(self._sync_write, db, sql, params)
        except sqlite3.Error as exc:
            raise StorageError(f"Database write failed: {exc}") from exc

    async def _write_batch(self, ops: list[tuple[str, tuple[Any, ...]]]) -> None:
        """Execute multiple write statements in one transaction and commit."""
        db = self._require_db()
        try:
            if self._use_aiosqlite:
                for sql, params in ops:
                    await db.execute(sql, params)
                await db.commit()
            else:
                await asyncio.to_thread(self._sync_write_batch, db, ops)
        except sqlite3.IntegrityError as exc:
            msg = str(exc)
            # Only raise DuplicateEventError for canonical_events PK/UNIQUE
            # violations.  Other IntegrityErrors (FK violations, etc.) are
            # raised as generic StorageError.
            if "canonical_events" in msg and (
                "UNIQUE constraint failed" in msg or "PRIMARY KEY" in msg.upper()
            ):
                raise DuplicateEventError(f"Duplicate event: {exc}") from exc
            raise StorageError(f"Batch write failed: {exc}") from exc
        except sqlite3.Error as exc:
            raise StorageError(f"Batch write failed: {exc}") from exc

    async def _read_one(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        """Execute a read and return the first row as a dict, or ``None``."""
        db = self._require_db()
        try:
            if self._use_aiosqlite:
                async with db.execute(sql, params) as cur:
                    row = await cur.fetchone()
                return dict(row) if row else None
            else:
                return await asyncio.to_thread(self._sync_read_one, db, sql, params)
        except sqlite3.Error as exc:
            raise StorageError(f"Database read failed: {exc}") from exc

    async def _read_all(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        """Execute a read and return all rows as dicts."""
        db = self._require_db()
        try:
            if self._use_aiosqlite:
                async with db.execute(sql, params) as cur:
                    rows = await cur.fetchall()
                return [dict(r) for r in rows]
            else:
                return await asyncio.to_thread(self._sync_read_all, db, sql, params)
        except sqlite3.Error as exc:
            raise StorageError(f"Database read failed: {exc}") from exc

    # -- Synchronous I/O helpers (called inside worker threads) -------------

    def _sync_write(
        self, db: sqlite3.Connection, sql: str, params: tuple[Any, ...]
    ) -> None:
        with self._lock:
            db.execute(sql, params)
            db.commit()

    def _sync_write_batch(
        self,
        db: sqlite3.Connection,
        ops: list[tuple[str, tuple[Any, ...]]],
    ) -> None:
        with self._lock:
            for sql, params in ops:
                db.execute(sql, params)
            db.commit()

    def _sync_read_one(
        self, db: sqlite3.Connection, sql: str, params: tuple[Any, ...]
    ) -> dict[str, Any] | None:
        with self._lock:
            row = db.execute(sql, params).fetchone()
            return dict(row) if row else None

    def _sync_read_all(
        self, db: sqlite3.Connection, sql: str, params: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    # -- Event CRUD ---------------------------------------------------------

    async def append(self, event: CanonicalEvent) -> None:
        """Persist a canonical event together with its inline relations."""
        snr = event.source_native_ref
        ops: list[tuple[str, tuple[Any, ...]]] = [
            (
                _INSERT_EVENT,
                (
                    event.event_id,
                    event.event_kind,
                    event.schema_version,
                    event.timestamp.isoformat(),
                    event.source_adapter,
                    event.source_transport_id,
                    event.source_channel_id,
                    event.parent_event_id,
                    _encode_json(event.lineage),
                    _encode_json(event.payload),
                    _serialize_metadata(event.metadata),
                    event.depth,
                    event.trace_id,
                    snr.adapter if snr else None,
                    snr.native_channel_id if snr else None,
                    snr.native_message_id if snr else None,
                    snr.native_thread_id if snr else None,
                    _now_iso(),
                ),
            )
        ]
        for rel in event.relations:
            ops.append(self._relation_op(event.event_id, rel))
        await self._write_batch(ops)

    async def get(self, event_id: str) -> CanonicalEvent | None:
        """Retrieve a single event by ID, including its relations."""
        row = await self._read_one(_SELECT_EVENT, (event_id,))
        if row is None:
            return None
        rel_rows = await self._read_all(_SELECT_RELATIONS, (event_id,))
        return _row_to_event(row, [_row_to_relation(r) for r in rel_rows])

    async def query(self, filter: EventFilter) -> AsyncGenerator[CanonicalEvent, None]:
        """Yield events matching *filter*, ordered by timestamp ascending."""
        sql, params = _build_query_sql(filter)
        rows = await self._read_all(sql, params)
        if not rows:
            return

        # Fetch relations for all matched events in one round-trip.
        event_ids = [r["event_id"] for r in rows]
        placeholders = ",".join("?" for _ in event_ids)
        rel_sql = (
            "SELECT * FROM event_relations WHERE event_id "
            f"IN ({placeholders})"  # nosec: placeholders are only ? markers, values passed as params
        )
        rel_rows = await self._read_all(rel_sql, tuple(event_ids))

        rel_map: dict[str, list[EventRelation]] = {}
        for rr in rel_rows:
            rel_map.setdefault(rr["event_id"], []).append(_row_to_relation(rr))

        for row in rows:
            yield _row_to_event(row, rel_map.get(row["event_id"], []))

    # -- Native ref correlation ---------------------------------------------

    async def store_native_ref(self, ref: NativeMessageRef) -> None:
        """Persist a native-to-canonical message mapping.

        Duplicate ``(adapter, native_channel_id, native_message_id)`` triples
        are silently ignored (idempotent).  When *native_channel_id* is
        ``None``, SQLite's UNIQUE constraint cannot detect duplicates
        because ``NULL != NULL``.  This method therefore performs an
        explicit resolve-before-insert check so that NULL-channel refs
        also dedupe deterministically.

        Use :meth:`resolve_native_ref` to retrieve the canonical
        ``event_id`` for an existing mapping.
        """
        # Resolve-before-insert: handles NULL native_channel_id which
        # SQLite UNIQUE treats as distinct per SQL standard.
        existing = await self._read_one(
            _RESOLVE_NATIVE_REF,
            (ref.adapter, ref.native_channel_id, ref.native_message_id),
        )
        if existing is not None:
            return

        await self._write(
            _INSERT_NATIVE_REF,
            (
                ref.id,
                ref.event_id,
                ref.adapter,
                ref.native_channel_id,
                ref.native_message_id,
                ref.native_thread_id,
                ref.native_relation_id,
                ref.direction,
                _encode_json(ref.metadata),
                ref.created_at.isoformat(),
            ),
        )

    async def resolve_native_ref(
        self,
        adapter: str,
        native_channel_id: str | None,
        native_message_id: str,
    ) -> str | None:
        """Look up the canonical event ID for a native message reference."""
        row = await self._read_one(
            _RESOLVE_NATIVE_REF,
            (adapter, native_channel_id, native_message_id),
        )
        return row["event_id"] if row else None

    # -- Relations ----------------------------------------------------------

    @staticmethod
    def _relation_op(
        event_id: str, relation: EventRelation
    ) -> tuple[str, tuple[Any, ...]]:
        """Build an ``(sql, params)`` pair for inserting a single relation."""
        nref = relation.target_native_ref
        return (
            _INSERT_RELATION,
            (
                event_id,
                relation.relation_type,
                relation.target_event_id,
                nref.adapter if nref else None,
                nref.native_channel_id if nref else None,
                nref.native_message_id if nref else None,
                nref.native_thread_id if nref else None,
                relation.key,
                relation.fallback_text,
                _encode_json(relation.metadata),
                _now_iso(),
            ),
        )

    async def store_relation(self, event_id: str, relation: EventRelation) -> None:
        """Persist a single relation for an existing event."""
        sql, params = self._relation_op(event_id, relation)
        await self._write(sql, params)

    async def list_relations(self, event_id: str) -> list[EventRelation]:
        """Return all relations belonging to *event_id*."""
        rows = await self._read_all(_SELECT_RELATIONS, (event_id,))
        return [_row_to_relation(r) for r in rows]

    # -- Receipts -----------------------------------------------------------

    async def append_receipt(self, receipt: DeliveryReceipt) -> None:
        """Append a delivery receipt record.

        Receipts are append-only: every call creates a new row.  Existing
        receipt rows are never updated or deleted.  The ``delivery_status``
        view projects the latest receipt as a ``MAX(sequence)`` aggregation.

        Empty-string ``target_channel`` values are normalised to ``None``
        before storage so that NULL and ``""`` are never stored as distinct
        values — the ``delivery_status`` view groups them together via
        ``COALESCE(target_channel, '')`` and normalising at write time
        keeps queries unambiguous.
        """
        # Normalise empty-string target_channel to NULL.
        channel = receipt.target_channel or None
        await self._write(
            _INSERT_RECEIPT,
            (
                receipt.receipt_id,
                receipt.event_id,
                receipt.delivery_plan_id,
                receipt.target_adapter,
                channel,
                receipt.route_id,
                receipt.status,
                receipt.error,
                receipt.failure_kind,
                receipt.adapter_message_id,
                receipt.next_retry_at.isoformat() if receipt.next_retry_at else None,
                receipt.attempt_number,
                receipt.parent_receipt_id,
                receipt.source,
                receipt.replay_run_id,
                receipt.retry_max_attempts,
                receipt.retry_backoff_base,
                receipt.retry_max_delay,
                (
                    1
                    if receipt.retry_jitter is True
                    else (0 if receipt.retry_jitter is False else None)
                ),
                receipt.created_at.isoformat(),
            ),
        )

    async def delivery_status(
        self,
        delivery_plan_id: str,
        target_adapter: str,
        target_channel: str | None = None,
    ) -> DeliveryReceipt | None:
        """Return the latest receipt for a delivery plan / adapter / channel triple.

        Queries the ``delivery_receipts`` base table directly (rather than
        the ``delivery_status`` view) so that NULL and empty-string channel
        values are handled robustly without relying on the view's
        ``COALESCE(target_channel, '')`` grouping.

        Parameters
        ----------
        delivery_plan_id:
            The delivery plan to look up.
        target_adapter:
            The target adapter to filter on.
        target_channel:
            Channel name to match.  When a named channel is passed, only
            receipts with that exact channel value are returned.  When
            ``None`` (default), only receipts with a NULL (no-channel)
            target are returned.  Passing ``None`` does **not** query
            across all channels.

        Returns
        -------
        DeliveryReceipt | None
            The latest-matching receipt, or ``None`` when no receipt exists
            for the given combination.
        """
        row = await self._read_one(
            _DELIVERY_RECEIPT_LATEST_BY_CHANNEL,
            (delivery_plan_id, target_adapter, target_channel or None),
        )
        return _row_to_receipt(row) if row else None

    async def list_receipts_for_plan(
        self,
        delivery_plan_id: str,
        target_adapter: str,
    ) -> list[DeliveryReceipt]:
        """Return all receipts for a delivery plan / adapter pair in
        attempt order.

        Receipts are ordered by ``attempt_number`` ascending (then
        ``sequence`` as tiebreaker) so callers can walk the full
        receipt lineage from first attempt to last.
        """
        rows = await self._read_all(
            _SELECT_RECEIPTS_FOR_PLAN,
            (delivery_plan_id, target_adapter),
        )
        return [_row_to_receipt(r) for r in rows]

    async def list_receipts_by_replay_run(
        self,
        run_id: str,
    ) -> list[DeliveryReceipt]:
        """Return all receipts produced by a specific replay run.

        Receipts are ordered by ``sequence`` ascending.  Only receipts
        with the given ``replay_run_id`` are returned.  Returns an
        empty list when no receipts match.
        """
        rows = await self._read_all(
            _SELECT_RECEIPTS_BY_REPLAY_RUN,
            (run_id,),
        )
        return [_row_to_receipt(r) for r in rows]

    async def list_receipts_for_event(
        self,
        event_id: str,
    ) -> list[DeliveryReceipt]:
        """Return all delivery receipts for a specific event.

        Receipts are ordered by ``sequence`` ascending, which reflects
        the chronological append order across all delivery plans and
        adapters for this event.
        """
        rows = await self._read_all(
            _SELECT_RECEIPTS_FOR_EVENT,
            (event_id,),
        )
        return [_row_to_receipt(r) for r in rows]

    async def list_due_retry_receipts(
        self, now: datetime, limit: int = 50, max_attempts: int = 3
    ) -> list[DeliveryReceipt]:
        """Return transient-failure receipts whose next_retry_at <= now,
        ordered by next_retry_at ASC, sequence ASC, limited to *limit*.
        Excludes receipts that have reached *max_attempts* or are dead_lettered."""
        rows = await self._read_all(
            """SELECT * FROM delivery_receipts r
             WHERE r.status = 'failed'
               AND r.failure_kind = 'adapter_transient'
               AND r.next_retry_at IS NOT NULL
               AND r.next_retry_at <= ?
               AND r.attempt_number < ?
               AND NOT EXISTS (
                   SELECT 1 FROM delivery_receipts child
                   WHERE child.parent_receipt_id = r.receipt_id
                     AND child.source = 'retry'
               )
             ORDER BY r.next_retry_at ASC, r.sequence ASC
             LIMIT ?""",
            (now.isoformat(), max_attempts, limit),
        )
        return [_row_to_receipt(r) for r in rows]

    async def count_pending_retry(self, now: datetime, max_attempts: int = 3) -> int:
        """Count transient-failure receipts due for retry."""
        row = await self._read_one(
            """SELECT COUNT(*) AS cnt FROM delivery_receipts r
             WHERE r.status = 'failed'
               AND r.failure_kind = 'adapter_transient'
               AND r.next_retry_at IS NOT NULL
               AND r.next_retry_at <= ?
               AND r.attempt_number < ?
               AND NOT EXISTS (
                   SELECT 1 FROM delivery_receipts child
                   WHERE child.parent_receipt_id = r.receipt_id
                     AND child.source = 'retry'
               )""",
            (now.isoformat(), max_attempts),
        )
        return row["cnt"] if row else 0

    async def update_retry_due(
        self,
        receipt_id: str,
        next_retry_at: datetime,
    ) -> None:
        """Update next_retry_at on a receipt (for capacity rejection backoff)."""
        await self._write(
            "UPDATE delivery_receipts SET next_retry_at = ? WHERE receipt_id = ?",
            (next_retry_at.isoformat(), receipt_id),
        )

    # -------------------------------------------------------------------
    # Outbox
    # -------------------------------------------------------------------

    async def create_outbox_item(self, item: DeliveryOutboxItem) -> DeliveryOutboxItem:
        """Create a new outbox item.

        Checks for an existing item with the same key tuple
        ``(delivery_plan_id, target_adapter, target_channel, attempt_number)``
        before inserting.  If a non-terminal item already exists it is
        returned unchanged (idempotent create).  If the existing item is
        terminal it is deleted first so a new row for re-delivery can
        be inserted without violating the UNIQUE constraint.

        The entire SELECT + conditional DELETE + INSERT runs inside a
        single ``BEGIN IMMEDIATE`` transaction so that two concurrent
        callers cannot both pass the existence check and race on INSERT.
        If the INSERT still fails with a UNIQUE constraint violation
        (extreme edge case), the existing row is re-read and returned.
        """
        _terminal = frozenset({"sent", "dead_lettered", "cancelled", "abandoned"})
        now = _now_iso()
        meta_json = _encode_json(item.metadata or {})

        select_sql = (
            "SELECT outbox_id, status FROM delivery_outbox"
            " WHERE delivery_plan_id = ? AND target_adapter = ?"
            " AND target_channel IS ? AND attempt_number = ?"
        )
        select_params = (
            item.delivery_plan_id,
            item.target_adapter,
            item.target_channel or None,
            item.attempt_number,
        )
        delete_sql = "DELETE FROM delivery_outbox WHERE outbox_id = ?"
        insert_sql = (
            "INSERT INTO delivery_outbox"
            " (outbox_id, event_id, route_id, delivery_plan_id,"
            "  target_adapter, target_channel, target_address,"
            "  attempt_number, status, failure_kind, failure_kind_detail,"
            "  next_attempt_at, created_at, updated_at, last_attempt_at,"
            "  locked_at, lease_until, worker_id, payload_hash,"
            "  receipt_id, parent_receipt_id, error_summary, metadata)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        insert_params = (
            item.outbox_id,
            item.event_id,
            item.route_id,
            item.delivery_plan_id,
            item.target_adapter,
            item.target_channel or None,
            item.target_address,
            item.attempt_number,
            item.status or "pending",
            item.failure_kind,
            item.failure_kind_detail,
            item.next_attempt_at,
            item.created_at or now,
            item.updated_at or now,
            item.last_attempt_at,
            item.locked_at,
            item.lease_until,
            item.worker_id,
            item.payload_hash,
            item.receipt_id,
            item.parent_receipt_id,
            item.error_summary,
            meta_json,
        )

        if self._use_aiosqlite:
            async with self._outbox_create_lock:
                db = self._require_db()
                try:
                    await db.execute("BEGIN IMMEDIATE")  # type: ignore[union-attr]
                    # SELECT existing
                    async with db.execute(select_sql, select_params) as cur:  # type: ignore[union-attr]
                        row = await cur.fetchone()
                    if row is not None:
                        existing = dict(row)
                        if existing["status"] not in _terminal:
                            await db.execute("COMMIT")  # type: ignore[union-attr]
                            return (
                                await self.get_outbox_item(existing["outbox_id"])
                                or item
                            )
                        # Terminal — delete so re-insertion can proceed.
                        await db.execute(delete_sql, (existing["outbox_id"],))  # type: ignore[union-attr]
                    await db.execute(insert_sql, insert_params)  # type: ignore[union-attr]
                    await db.execute("COMMIT")  # type: ignore[union-attr]
                except sqlite3.IntegrityError:
                    # UNIQUE race: another writer inserted between our SELECT
                    # and INSERT.  Rollback if the transaction is still active,
                    # then re-read the winning row.
                    try:
                        await db.execute("ROLLBACK")  # type: ignore[union-attr]
                    except Exception:
                        pass
                    existing = await self._read_one(select_sql, select_params)
                    if existing is not None:
                        return await self.get_outbox_item(existing["outbox_id"]) or item
                    raise
                except BaseException:
                    # Ensure the transaction is rolled back on any other failure
                    # (e.g. operational error between BEGIN and COMMIT) so the
                    # connection is not left with an open transaction.
                    try:
                        await db.execute("ROLLBACK")  # type: ignore[union-attr]
                    except Exception:
                        pass
                    raise
        else:
            try:
                existing_id = await asyncio.to_thread(
                    self._sync_atomic_create_outbox,
                    self._require_db(),
                    select_sql,
                    select_params,
                    delete_sql,
                    insert_sql,
                    insert_params,
                    _terminal,
                )
                if existing_id is not None:
                    return await self.get_outbox_item(existing_id) or item
            except sqlite3.IntegrityError:
                existing = await self._read_one(select_sql, select_params)
                if existing is not None:
                    return await self.get_outbox_item(existing["outbox_id"]) or item
                raise

        return await self.get_outbox_item(item.outbox_id) or item

    def _sync_atomic_create_outbox(
        self,
        db: sqlite3.Connection,
        select_sql: str,
        select_params: tuple[Any, ...],
        delete_sql: str,
        insert_sql: str,
        insert_params: tuple[Any, ...],
        terminal: frozenset[str],
    ) -> str | None:
        """Synchronous helper: BEGIN IMMEDIATE, SELECT, optional DELETE, INSERT, COMMIT.

        Returns the existing outbox_id when a non-terminal row was found
        (idempotent), or None when a new row was inserted.
        """
        with self._lock:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(select_sql, select_params).fetchone()
                if row is not None:
                    existing = dict(row)
                    if existing["status"] not in terminal:
                        db.execute("COMMIT")
                        return existing["outbox_id"]
                    db.execute(delete_sql, (existing["outbox_id"],))
                db.execute(insert_sql, insert_params)
                db.execute("COMMIT")
                return None
            except BaseException:
                db.execute("ROLLBACK")
                raise

    async def get_outbox_item(self, outbox_id: str) -> DeliveryOutboxItem | None:
        """Retrieve a single outbox item by its ID."""
        row = await self._read_one(
            "SELECT * FROM delivery_outbox WHERE outbox_id = ?",
            (outbox_id,),
        )
        if row is None:
            return None
        return _row_to_outbox_item(row)

    async def get_outbox_item_for_delivery(
        self,
        event_id: str,
        delivery_plan_id: str,
        target_adapter: str,
        target_channel: str | None,
        status: str | None = None,
    ) -> DeliveryOutboxItem | None:
        """Retrieve an outbox item by its delivery target key.

        Performs a targeted SELECT matching *event_id*,
        *delivery_plan_id*, *target_adapter*, *target_channel*
        (using ``IS`` for proper ``NULL`` handling) and optionally
        *status*.  Returns the first match or ``None``.
        """
        clauses = [
            "event_id = ?",
            "delivery_plan_id = ?",
            "target_adapter = ?",
            "target_channel IS ?",
        ]
        channel = target_channel or None
        params: list[Any] = [event_id, delivery_plan_id, target_adapter, channel]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = " AND ".join(clauses)
        row = await self._read_one(
            f"SELECT * FROM delivery_outbox WHERE {where} LIMIT 1",  # nosec: where clause built from hardcoded identifiers only, values via ? params
            tuple(params),
        )
        if row is None:
            return None
        return _row_to_outbox_item(row)

    async def list_outbox_items(
        self,
        status_filter: list[str] | None = None,
        due_before: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DeliveryOutboxItem]:
        """List outbox items matching optional status and due filters."""
        clauses: list[str] = []
        params: list[Any] = []

        if status_filter:
            holders = ",".join("?" for _ in status_filter)
            clauses.append(f"status IN ({holders})")
            params.extend(status_filter)

        if due_before is not None:
            clauses.append("(next_attempt_at IS NOT NULL AND next_attempt_at <= ?)")
            params.append(due_before)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM delivery_outbox{where} ORDER BY next_attempt_at ASC, created_at ASC LIMIT ? OFFSET ?"  # nosec
        params.append(limit)
        params.append(offset)

        rows = await self._read_all(sql, tuple(params))
        return [_row_to_outbox_item(r) for r in rows]

    async def claim_due_outbox_items(
        self,
        now: str,
        worker_id: str,
        lease_seconds: int = 30,
        limit: int = 20,
    ) -> list[DeliveryOutboxItem]:
        """Atomically claim due outbox items for processing.

        Uses a transaction to SELECT FOR UPDATE equivalent (rowid-based)
        and updates in one step.  Claims items that are:
        - status IN ('pending', 'retry_wait')
          OR (status = 'in_progress' AND lease_until <= now) — expired leases
        - (next_attempt_at IS NULL OR next_attempt_at <= now)
        - (lease_until IS NULL OR lease_until <= now)
        """
        lease_until = _add_seconds_iso(now, lease_seconds)
        # Use a two-step approach: SELECT candidates, then UPDATE matching.
        # SQLite doesn't support RETURNING with ORIGIN in all configurations,
        # so we select first, then update by outbox_id.
        rows = await self._read_all(
            """SELECT * FROM delivery_outbox
               WHERE (status IN ('pending', 'retry_wait')
                      OR (status = 'in_progress' AND lease_until <= ?))
                 AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                 AND (lease_until IS NULL OR lease_until <= ?)
               ORDER BY next_attempt_at ASC, created_at ASC
               LIMIT ?""",
            (now, now, now, limit),
        )
        if not rows:
            return []

        outbox_ids = [r["outbox_id"] for r in rows]
        placeholders = ",".join("?" for _ in outbox_ids)
        await self._write(
            f"""UPDATE delivery_outbox
                SET status = 'in_progress',
                    locked_at = ?,
                    lease_until = ?,
                    worker_id = ?,
                    updated_at = ?
                WHERE outbox_id IN ({placeholders})
                  AND (status IN ('pending', 'retry_wait')
                       OR (status = 'in_progress' AND lease_until <= ?))
                  AND (lease_until IS NULL OR lease_until <= ?)""",  # nosec: placeholders are only ? markers, values passed as params
            (now, lease_until, worker_id, now, *outbox_ids, now, now),
        )

        # Re-read to get the updated rows (some may have been claimed by
        # another worker if the SELECT/UPDATE window was contested).
        final_rows = await self._read_all(
            f"SELECT * FROM delivery_outbox WHERE outbox_id IN ({','.join('?' for _ in outbox_ids)}) AND worker_id = ? AND status = 'in_progress'",  # nosec: placeholders are only ? markers, values passed as params
            (*outbox_ids, worker_id),
        )
        return [_row_to_outbox_item(r) for r in final_rows]

    async def _update_outbox_status(
        self,
        outbox_id: str,
        new_status: str,
        *,
        allowed_from: tuple[str, ...] | None = None,
        receipt_id: str | None = None,
        attempt_number: int | None = None,
        failure_kind: str | None = None,
        failure_kind_detail: str | None = None,
        error_summary: str | None = None,
        next_attempt_at: str | None = None,
    ) -> None:
        """Shared helper for status transitions.

        Only updates non-terminal items.  The ``WHERE status NOT IN``
        clause prevents regression once a terminal status is set.
        If *allowed_from* is provided, an additional ``AND status IN
        (...)`` guard is added so the transition is only valid from
        the listed source statuses.
        """
        now = _now_iso()
        sets = ["status = ?", "updated_at = ?"]
        params: list[Any] = [new_status, now]

        if receipt_id is not None:
            sets.append("receipt_id = ?")
            params.append(receipt_id)
        if attempt_number is not None:
            sets.append("attempt_number = ?")
            params.append(attempt_number)
        if failure_kind is not None:
            sets.append("failure_kind = ?")
            params.append(failure_kind)
        if failure_kind_detail is not None:
            sets.append("failure_kind_detail = ?")
            params.append(failure_kind_detail)
        if error_summary is not None:
            sets.append("error_summary = ?")
            params.append(error_summary)
        if next_attempt_at is not None:
            sets.append("next_attempt_at = ?")
            params.append(next_attempt_at)
        elif new_status == "retry_wait":
            # retry_wait MUST have next_attempt_at — defensive guard.
            raise ValueError(
                "next_attempt_at is required when transitioning to retry_wait"
            )
        else:
            sets.append("next_attempt_at = NULL")

        if new_status in ("queued", "sent"):
            sets.extend(
                [
                    "failure_kind = NULL",
                    "failure_kind_detail = NULL",
                    "error_summary = NULL",
                ]
            )
        elif new_status in ("dead_lettered", "cancelled", "abandoned"):
            # Clear next_attempt_at is handled above; also clear
            # failure_kind_detail which is not caller-specified for
            # these terminal transitions.  Keep failure_kind and
            # error_summary as callers pass meaningful values.
            if failure_kind_detail is None:
                sets.append("failure_kind_detail = NULL")
        if new_status in ("queued", "sent", "retry_wait"):
            sets.append("last_attempt_at = ?")
            params.append(now)
        if new_status in (
            "sent",
            "dead_lettered",
            "cancelled",
            "abandoned",
            "retry_wait",
            "queued",
        ):
            sets.append("locked_at = NULL")
            sets.append("lease_until = NULL")
            sets.append("worker_id = NULL")

        where_clauses = [
            "outbox_id = ?",
            "status NOT IN ('sent', 'dead_lettered', 'cancelled', 'abandoned')",
        ]
        params.append(outbox_id)
        if allowed_from is not None:
            holders = ",".join("?" for _ in allowed_from)
            where_clauses.append(f"status IN ({holders})")
            params.extend(allowed_from)

        set_clause = ", ".join(sets)
        where_sql = " AND ".join(where_clauses)
        await self._write(
            f"UPDATE delivery_outbox SET {set_clause} WHERE {where_sql}",  # nosec: set_clause contains only hardcoded column names, values via ? params
            tuple(params),
        )

    async def mark_outbox_sent(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        attempt_number: int | None = None,
    ) -> None:
        """Mark an outbox item as ``sent`` (terminal).

        Only transitions from ``in_progress`` or ``queued``.
        """
        await self._update_outbox_status(
            outbox_id,
            "sent",
            allowed_from=("in_progress", "queued"),
            receipt_id=receipt_id,
            attempt_number=attempt_number,
        )

    async def mark_outbox_queued(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        attempt_number: int | None = None,
    ) -> None:
        """Mark an outbox item as ``queued`` (adapter-local queue acceptance).

        Only transitions from ``in_progress``.
        """
        await self._update_outbox_status(
            outbox_id,
            "queued",
            allowed_from=("in_progress",),
            receipt_id=receipt_id,
            attempt_number=attempt_number,
        )

    async def mark_outbox_retry_wait(
        self,
        outbox_id: str,
        next_attempt_at: str,
        receipt_id: str | None = None,
        failure_kind: str | None = None,
        failure_kind_detail: str | None = None,
        error_summary: str | None = None,
        attempt_number: int | None = None,
    ) -> None:
        """Mark an outbox item as ``retry_wait`` (transient failure).

        Sets ``next_attempt_at`` for the next scheduled attempt.
        Only transitions from ``in_progress``.
        """
        await self._update_outbox_status(
            outbox_id,
            "retry_wait",
            allowed_from=("in_progress",),
            receipt_id=receipt_id,
            attempt_number=attempt_number,
            failure_kind=failure_kind,
            failure_kind_detail=failure_kind_detail,
            error_summary=error_summary,
            next_attempt_at=next_attempt_at,
        )

    async def mark_outbox_dead_lettered(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        failure_kind: str | None = None,
        failure_kind_detail: str | None = None,
        error_summary: str | None = None,
    ) -> None:
        """Mark an outbox item as ``dead_lettered`` (terminal failure).

        Only transitions from ``in_progress`` or ``retry_wait``.
        """
        await self._update_outbox_status(
            outbox_id,
            "dead_lettered",
            allowed_from=("in_progress", "retry_wait"),
            receipt_id=receipt_id,
            failure_kind=failure_kind,
            failure_kind_detail=failure_kind_detail,
            error_summary=error_summary,
        )

    async def mark_outbox_cancelled(
        self,
        outbox_id: str,
        error_summary: str | None = None,
    ) -> None:
        """Mark an outbox item as ``cancelled`` (terminal).

        May be called from ``pending``, ``in_progress``, ``retry_wait``,
        or ``queued``.
        """
        await self._update_outbox_status(
            outbox_id,
            "cancelled",
            allowed_from=("pending", "in_progress", "retry_wait", "queued"),
            error_summary=error_summary,
        )

    async def mark_outbox_abandoned(
        self,
        outbox_id: str,
        error_summary: str | None = None,
    ) -> None:
        """Mark an outbox item as ``abandoned`` (terminal).

        May be called from ``pending``, ``in_progress``, ``retry_wait``,
        or ``queued``.
        """
        await self._update_outbox_status(
            outbox_id,
            "abandoned",
            allowed_from=("pending", "in_progress", "retry_wait", "queued"),
            error_summary=error_summary,
        )

    async def renew_outbox_lease(
        self,
        outbox_id: str,
        worker_id: str,
        lease_until: str,
    ) -> bool:
        """Renew the lease on an in_progress outbox item.

        Returns True if the lease was renewed, False if the item is no
        longer owned by this worker or is not in_progress.
        """
        now = _now_iso()
        await self._write(
            """UPDATE delivery_outbox
               SET lease_until = ?, updated_at = ?
               WHERE outbox_id = ?
                 AND worker_id = ?
                 AND status = 'in_progress'""",
            (lease_until, now, outbox_id, worker_id),
        )
        # Verify the update matched a row.
        row = await self._read_one(
            "SELECT outbox_id FROM delivery_outbox WHERE outbox_id = ? AND worker_id = ? AND status = 'in_progress'",
            (outbox_id, worker_id),
        )
        return row is not None

    async def release_outbox_claim(
        self,
        outbox_id: str,
        worker_id: str,
        *,
        release_status: str = "pending",
    ) -> None:
        """Release a claim on an outbox item, restoring the caller-specified status.

        Clears locked_at, lease_until, worker_id and sets status to
        *release_status*.  Only succeeds when the current worker_id matches.
        """
        _allowed_release_statuses = {
            "pending",
            "retry_wait",
        }
        if release_status not in _allowed_release_statuses:
            raise ValueError(f"Invalid release_status: {release_status!r}")

        await self._write(
            """UPDATE delivery_outbox
               SET locked_at = NULL, lease_until = NULL, worker_id = NULL,
                   status = ?, updated_at = ?
               WHERE outbox_id = ? AND worker_id = ?
                 AND status NOT IN ('sent', 'dead_lettered', 'cancelled', 'abandoned')""",
            (release_status, _now_iso(), outbox_id, worker_id),
        )

    async def count_outbox_by_status(self) -> dict[str, int]:
        """Return counts of outbox items grouped by status."""
        rows = await self._read_all(
            "SELECT status, COUNT(*) AS cnt FROM delivery_outbox GROUP BY status"
        )
        return {r["status"]: r["cnt"] for r in rows}

    async def list_native_refs_for_event(
        self,
        event_id: str,
    ) -> list[NativeMessageRef]:
        """Return all native message refs for a specific event.

        Native refs are ordered by ``created_at`` ascending, which reflects
        the chronological order in which adapters materialised the event
        into their native namespaces.
        """
        rows = await self._read_all(
            _SELECT_NREFS_FOR_EVENT,
            (event_id,),
        )
        return [_row_to_native_ref(r) for r in rows]

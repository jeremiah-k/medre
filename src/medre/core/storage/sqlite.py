"""SQLite-backed storage backend for the medre.

Uses *aiosqlite* when available for native async database access; otherwise
falls back to synchronous ``sqlite3`` wrapped in ``asyncio.to_thread``.
The database runs in WAL mode for safe concurrent reads.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, AsyncIterator

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
    created_at TEXT NOT NULL
);

CREATE VIEW IF NOT EXISTS delivery_status AS
SELECT dr.sequence, dr.receipt_id, dr.event_id, dr.delivery_plan_id,
       dr.target_adapter, dr.target_channel, dr.route_id, dr.status, dr.error,
       dr.adapter_message_id, dr.next_retry_at, dr.attempt_number,
       dr.parent_receipt_id, dr.source, dr.replay_run_id, dr.created_at
FROM delivery_receipts dr
JOIN (
    SELECT delivery_plan_id, target_adapter, MAX(sequence) AS max_seq
    FROM delivery_receipts GROUP BY delivery_plan_id, target_adapter
) latest ON dr.sequence = latest.max_seq;

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
    ON delivery_receipts(delivery_plan_id, target_adapter, attempt_number, sequence);
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
    "canonical_events": frozenset({
        "event_id", "event_kind", "schema_version", "timestamp",
        "source_adapter", "source_transport_id", "source_channel_id",
        "parent_event_id", "lineage", "payload", "metadata", "depth",
        "trace_id", "source_native_adapter", "source_native_channel_id",
        "source_native_message_id", "source_native_thread_id",
        "created_at",
    }),
    "event_relations": frozenset({
        "id", "event_id", "relation_type", "target_event_id",
        "target_native_adapter", "target_native_channel_id",
        "target_native_message_id", "target_native_thread_id",
        "key", "fallback_text", "metadata", "created_at",
    }),
    "native_message_refs": frozenset({
        "id", "event_id", "adapter", "native_channel_id",
        "native_message_id", "native_thread_id", "native_relation_id",
        "direction", "metadata", "created_at",
    }),
    "delivery_receipts": frozenset({
        "sequence", "receipt_id", "event_id", "delivery_plan_id",
        "target_adapter", "target_channel", "route_id", "status", "error", "failure_kind",
        "adapter_message_id", "next_retry_at", "attempt_number",
        "parent_receipt_id", "source", "replay_run_id", "created_at",
    }),
    "plugin_state": frozenset({
        "plugin_id", "key", "value", "updated_at",
    }),
    "_medre_schema_meta": frozenset({
        "key", "value",
    }),
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
     replay_run_id, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_EVENT = "SELECT * FROM canonical_events WHERE event_id = ?"

_SELECT_RELATIONS = "SELECT * FROM event_relations WHERE event_id = ? ORDER BY id ASC"

_RESOLVE_NATIVE_REF = """
SELECT event_id FROM native_message_refs
WHERE adapter = ? AND native_channel_id IS ? AND native_message_id = ?
"""

_DELIVERY_STATUS_VIEW = """
SELECT * FROM delivery_status
WHERE delivery_plan_id = ? AND target_adapter = ?
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
    sql = f"SELECT * FROM canonical_events{where} ORDER BY timestamp ASC, event_id ASC LIMIT ?"
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
        self._use_aiosqlite = _HAS_AIOSQLITE

    # -- Internal helpers ---------------------------------------------------

    def _require_db(self) -> Any:
        """Return the active connection or raise if not initialised."""
        if self._db is None:
            raise StorageInitializationError(
                "Storage backend has not been initialised. "
                "Call initialize() first."
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

        # Verify schema version after DDL.
        await self._verify_schema_version()

        # Verify column shape — catches old pre-release DBs that claim
        # schema_version=1 but predate current columns.
        await self._validate_schema_shape()

        # Create targeted indexes AFTER shape validation so that old-shape
        # databases fail with a clear StorageInitializationError before
        # index creation references missing columns.
        await self._create_indexes()

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
            )

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
                f"file:{db_path}?mode=ro", uri=True,
            )
            db.row_factory = sqlite3.Row
            instance._db = db
        else:
            instance._db = await asyncio.to_thread(instance._sync_open_readonly)

        # Validate metadata and shape without writing anything.
        await instance._verify_schema_version_readonly()
        await instance._validate_schema_shape()

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
            )

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

    @staticmethod
    def _sync_close(db: sqlite3.Connection) -> None:
        """Close a synchronous connection (called from a worker thread)."""
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

    async def _write_batch(
        self, ops: list[tuple[str, tuple[Any, ...]]]
    ) -> None:
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
                "UNIQUE constraint failed" in msg
                or "PRIMARY KEY" in msg.upper()
            ):
                raise DuplicateEventError(
                    f"Duplicate event: {exc}"
                ) from exc
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

    async def query(self, filter: EventFilter) -> AsyncIterator[CanonicalEvent]:
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
            f"IN ({placeholders})"
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

    async def store_relation(
        self, event_id: str, relation: EventRelation
    ) -> None:
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
        """
        await self._write(
            _INSERT_RECEIPT,
            (
                receipt.receipt_id,
                receipt.event_id,
                receipt.delivery_plan_id,
                receipt.target_adapter,
                receipt.target_channel,
                receipt.route_id,
                receipt.status,
                receipt.error,
                receipt.failure_kind,
                receipt.adapter_message_id,
                receipt.next_retry_at.isoformat()
                if receipt.next_retry_at
                else None,
                receipt.attempt_number,
                receipt.parent_receipt_id,
                receipt.source,
                receipt.replay_run_id,
                receipt.created_at.isoformat(),
            ),
        )

    async def delivery_status(
        self, delivery_plan_id: str, target_adapter: str
    ) -> DeliveryReceipt | None:
        """Return the latest receipt for a delivery plan / adapter pair."""
        row = await self._read_one(
            _DELIVERY_STATUS_VIEW,
            (delivery_plan_id, target_adapter),
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
        self, receipt_id: str, next_retry_at: datetime,
    ) -> None:
        """Update next_retry_at on a receipt (for capacity rejection backoff)."""
        await self._write(
            "UPDATE delivery_receipts SET next_retry_at = ? WHERE receipt_id = ?",
            (next_retry_at.isoformat(), receipt_id),
        )

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

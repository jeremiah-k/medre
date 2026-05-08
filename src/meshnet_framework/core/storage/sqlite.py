"""SQLite-backed storage backend for the meshnet framework.

Uses *aiosqlite* when available for native async database access; otherwise
falls back to synchronous ``sqlite3`` wrapped in ``asyncio.to_thread``.
The database runs in WAL mode for safe concurrent reads.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import asdict, fields
from datetime import datetime, timezone
from typing import Any, AsyncIterator, TypeVar

from meshnet_framework.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeMetadata,
    NativeRef,
    RadioMetadata,
    RoutingMetadata,
    TelemetryMetadata,
    TransportMetadata,
)
from meshnet_framework.core.storage.backend import (
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

_T = TypeVar("_T")


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
    status TEXT NOT NULL,
    error TEXT,
    adapter_message_id TEXT,
    next_retry_at TEXT,
    created_at TEXT NOT NULL
);

CREATE VIEW IF NOT EXISTS delivery_status AS
SELECT dr.* FROM delivery_receipts dr
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
"""

# ---------------------------------------------------------------------------
# Prepared statements
# ---------------------------------------------------------------------------

_INSERT_EVENT = """
INSERT INTO canonical_events
    (event_id, event_kind, schema_version, timestamp,
     source_adapter, source_transport_id, source_channel_id,
     parent_event_id, lineage, payload, metadata, depth,
     trace_id, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_RELATION = """
INSERT INTO event_relations
    (event_id, relation_type, target_event_id,
     target_native_adapter, target_native_channel_id,
     target_native_message_id, key, fallback_text,
     metadata, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_NATIVE_REF = """
INSERT INTO native_message_refs
    (id, event_id, adapter, native_channel_id,
     native_message_id, native_thread_id, native_relation_id,
     direction, metadata, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_RECEIPT = """
INSERT INTO delivery_receipts
    (receipt_id, event_id, delivery_plan_id, target_adapter,
     status, error, adapter_message_id, next_retry_at, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_EVENT = "SELECT * FROM canonical_events WHERE event_id = ?"

_SELECT_RELATIONS = "SELECT * FROM event_relations WHERE event_id = ?"

_RESOLVE_NATIVE_REF = """
SELECT event_id FROM native_message_refs
WHERE adapter = ? AND native_channel_id IS ? AND native_message_id = ?
"""

_DELIVERY_STATUS_VIEW = """
SELECT * FROM delivery_status
WHERE delivery_plan_id = ? AND target_adapter = ?
"""


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _dict_to_dataclass(cls: type[_T], data: dict[str, Any]) -> _T:
    """Construct a dataclass from a dict, silently ignoring unknown keys.

    This provides forward-compatibility when new fields are added to
    metadata dataclasses in a newer version of the framework but the
    stored JSON was produced by that newer version.
    """
    valid_keys = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    return cls(**{k: v for k, v in data.items() if k in valid_keys})


def _serialize_metadata(metadata: EventMetadata) -> str:
    """Serialise an :class:`EventMetadata` instance to a JSON string."""
    return json.dumps(asdict(metadata))


def _deserialize_metadata(raw: str) -> EventMetadata:
    """Reconstruct an :class:`EventMetadata` from its JSON representation."""
    data: dict[str, Any] = json.loads(raw)

    transport = (
        _dict_to_dataclass(TransportMetadata, data["transport"])
        if data.get("transport")
        else None
    )

    routing_data = data.get("routing")
    routing = (
        RoutingMetadata(
            matched_routes=tuple(routing_data.get("matched_routes", ())),
            fanout_group=routing_data.get("fanout_group"),
        )
        if routing_data
        else None
    )

    radio = (
        _dict_to_dataclass(RadioMetadata, data["radio"])
        if data.get("radio")
        else None
    )
    telemetry = (
        _dict_to_dataclass(TelemetryMetadata, data["telemetry"])
        if data.get("telemetry")
        else None
    )
    native = (
        _dict_to_dataclass(NativeMetadata, data["native"])
        if data.get("native")
        else None
    )

    return EventMetadata(
        transport=transport,
        routing=routing,
        radio=radio,
        telemetry=telemetry,
        native=native,
        custom=data.get("custom", {}),
    )


def _row_to_event(
    row: dict[str, Any],
    relations: list[EventRelation],
) -> CanonicalEvent:
    """Map a database row (plus pre-fetched relations) to a :class:`CanonicalEvent`."""
    return CanonicalEvent(
        event_id=row["event_id"],
        event_kind=row["event_kind"],
        schema_version=row["schema_version"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        source_adapter=row["source_adapter"],
        source_transport_id=row["source_transport_id"],
        source_channel_id=row["source_channel_id"],
        parent_event_id=row["parent_event_id"],
        lineage=json.loads(row["lineage"]),
        relations=relations,
        payload=json.loads(row["payload"]),
        metadata=_deserialize_metadata(row["metadata"]),
        depth=row["depth"],
        trace_id=row["trace_id"],
    )


def _row_to_relation(row: dict[str, Any]) -> EventRelation:
    """Map an ``event_relations`` row to an :class:`EventRelation`."""
    target_native_ref: NativeRef | None = None
    if row["target_native_adapter"]:
        target_native_ref = NativeRef(
            adapter=row["target_native_adapter"],
            native_channel_id=row["target_native_channel_id"],
            native_message_id=row["target_native_message_id"],
        )
    return EventRelation(
        relation_type=row["relation_type"],  # type: ignore[arg-type]
        target_event_id=row["target_event_id"],
        target_native_ref=target_native_ref,
        key=row["key"],
        fallback_text=row["fallback_text"],
        metadata=json.loads(row["metadata"]),
    )


def _row_to_receipt(row: dict[str, Any]) -> DeliveryReceipt:
    """Map a ``delivery_receipts`` row to a :class:`DeliveryReceipt`."""
    return DeliveryReceipt(
        sequence=row["sequence"],
        receipt_id=row["receipt_id"],
        event_id=row["event_id"],
        delivery_plan_id=row["delivery_plan_id"],
        target_adapter=row["target_adapter"],
        status=row["status"],  # type: ignore[arg-type]
        error=row["error"],
        adapter_message_id=row["adapter_message_id"],
        next_retry_at=(
            datetime.fromisoformat(row["next_retry_at"])
            if row["next_retry_at"]
            else None
        ),
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
    sql = f"SELECT * FROM canonical_events{where} ORDER BY timestamp DESC LIMIT ?"
    params.append(filt.limit)
    return sql, tuple(params)


# ---------------------------------------------------------------------------
# SQLiteStorage
# ---------------------------------------------------------------------------


class SQLiteStorage:
    """Thread-safe, WAL-mode SQLite storage.

    Implements the :class:`~meshnet_framework.core.storage.backend.StorageBackend`
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
        """Open the database, enable WAL mode, and create the schema."""
        if self._use_aiosqlite:
            db = await aiosqlite.connect(self._db_path)  # type: ignore[union-attr]
            db.row_factory = sqlite3.Row
            await db.executescript(_SCHEMA)
            await db.execute("PRAGMA journal_mode=WAL")
            await db.commit()
            self._db = db
        else:
            self._db = await asyncio.to_thread(self._sync_open)

    def _sync_open(self) -> sqlite3.Connection:
        """Synchronous counterpart of :meth:`initialize` for the fallback path."""
        db = sqlite3.connect(self._db_path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        db.execute("PRAGMA journal_mode=WAL")
        db.commit()
        return db

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
                    json.dumps(event.lineage),
                    json.dumps(event.payload),
                    _serialize_metadata(event.metadata),
                    event.depth,
                    event.trace_id,
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
        """Yield events matching *filter*, newest-first."""
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
        """Persist a native-to-canonical message mapping."""
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
                json.dumps(ref.metadata),
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
                relation.key,
                relation.fallback_text,
                json.dumps(relation.metadata),
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
        """Append a delivery receipt record."""
        await self._write(
            _INSERT_RECEIPT,
            (
                receipt.receipt_id,
                receipt.event_id,
                receipt.delivery_plan_id,
                receipt.target_adapter,
                receipt.status,
                receipt.error,
                receipt.adapter_message_id,
                receipt.next_retry_at.isoformat()
                if receipt.next_retry_at
                else None,
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

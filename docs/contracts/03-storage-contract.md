# Storage Contract

> Extracted from: Modular Event Communications Runtime Specification, Sections 12, 18, 19
> Version: 0.1.0 (Draft)
> Last updated: 2026-05-07

## 1. Overview

The storage layer is the single source of truth for the runtime. It persists canonical events, native message references, delivery receipts, event relations, identity data, plugin state, and raw native archives. The initial backend is SQLite.

Design constraints:

- Events are append-only. No row in `canonical_events` is ever updated or deleted.
- Delivery receipts are append-only. Multiple attempts for the same delivery plan produce separate receipt rows. Current delivery status is a projection from the latest receipt.
- All cross-adapter correlation goes through storage, not through external platform metadata.

## 2. StorageBackend Protocol

Every storage implementation must satisfy this interface:

```python
class StorageBackend(Protocol):
    """Protocol defining the interface all storage backends must implement.

    Contractual guarantees: Events are append-only.  Delivery receipts are
    append-only.  Native refs are idempotent.  Relations queryable by
    event_id.  Query results ordered by timestamp ascending.
    """

    # -- Event CRUD ---------------------------------------------------------

    async def append(self, event: CanonicalEvent) -> None:
        """Persist a canonical event together with its inline relations."""
        ...

    async def get(self, event_id: str) -> CanonicalEvent | None:
        """Retrieve a single event by its unique identifier."""
        ...

    async def query(self, filter: EventFilter) -> AsyncIterator[CanonicalEvent]:
        """Yield events matching filter, ordered by timestamp."""
        ...

    # -- Native ref correlation ---------------------------------------------

    async def store_native_ref(self, ref: NativeMessageRef) -> None:
        """Persist a native-to-canonical message mapping."""
        ...

    async def resolve_native_ref(
        self, adapter: str, native_channel_id: str | None, native_message_id: str
    ) -> str | None:
        """Look up the canonical event ID for a native message reference.
        Returns None if no mapping exists."""
        ...

    # -- Relations ----------------------------------------------------------

    async def store_relation(self, event_id: str, relation: EventRelation) -> None:
        """Persist a single relation for an existing event."""
        ...

    async def list_relations(self, event_id: str) -> list[EventRelation]:
        """Return all relations belonging to event_id."""
        ...

    # -- Receipts -----------------------------------------------------------

    async def append_receipt(self, receipt: DeliveryReceipt) -> None:
        """Append a delivery receipt record. Never updates existing rows."""
        ...

    async def delivery_status(
        self, delivery_plan_id: str, target_adapter: str
    ) -> DeliveryReceipt | None:
        """Return the latest receipt for a delivery plan / adapter pair.
        Returns None when no receipt exists."""
        ...

    async def list_receipts_for_plan(
        self, delivery_plan_id: str, target_adapter: str
    ) -> list[DeliveryReceipt]:
        """Return all receipts for a delivery plan / adapter in attempt order."""
        ...

    # -- Lifecycle ----------------------------------------------------------

    async def initialize(self) -> None:
        """Prepare the backend for use (open connections, create schema)."""
        ...

    async def close(self) -> None:
        """Release all resources held by the backend."""
        ...
```

> **Note on `archive_raw` and `resolve_native_relation`:** These methods appear in the master spec (Section 12.4) but are not part of the Phase 1 `StorageBackend` protocol. Raw archiving is a future capability. Native relation resolution is handled through `resolve_native_ref` with the `native_relation_id` column index on `native_message_refs`.

`resolve_native_ref` takes `native_channel_id` as an optional parameter (`str | None`) because the uniqueness constraint on native refs is `(adapter, native_channel_id, native_message_id)`. A message ID may not be unique within an adapter alone (e.g., the same packet ID on different MeshCore channel slots).

## 3. SQLite Schema

The initial backend. File location is configurable, defaulting to `$XDG_DATA_DIR/<project>/events.db` on Linux or `%APPDATA%\<project>\events.db` on Windows. SQLite must run in WAL mode for concurrent reads.

### 3.1 canonical_events

```sql
CREATE TABLE canonical_events (
    event_id TEXT PRIMARY KEY,
    event_kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    timestamp TEXT NOT NULL,         -- ISO 8601 with nanoseconds
    source_adapter TEXT NOT NULL,
    source_transport_id TEXT NOT NULL,  -- Native actor/source identity (not native message ID)
    source_channel_id TEXT,          -- Native channel/room/topic on source adapter
    source_native_adapter TEXT,      -- Source NativeRef.adapter for inbound native reference
    source_native_channel_id TEXT,   -- Source NativeRef.native_channel_id
    source_native_message_id TEXT,   -- Source NativeRef.native_message_id
    source_native_thread_id TEXT,    -- Source NativeRef.native_thread_id
    parent_event_id TEXT,
    lineage TEXT,                    -- JSON array of event IDs
    payload TEXT NOT NULL,           -- JSON
    metadata TEXT NOT NULL,          -- JSON
    depth INTEGER NOT NULL DEFAULT 0,
    trace_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_events_kind ON canonical_events(event_kind);
CREATE INDEX idx_events_timestamp ON canonical_events(timestamp);
CREATE INDEX idx_events_source ON canonical_events(source_adapter, source_transport_id);
CREATE INDEX idx_events_parent ON canonical_events(parent_event_id);
```

`source_transport_id` identifies the native actor (who produced the event), not the native message. Native message IDs belong in `native_message_refs`.

`source_channel_id` is the native channel/room/topic where the event originated. `NULL` if the transport has no channel concept.

The `source_native_*` columns persist the optional `CanonicalEvent.source_native_ref` for inbound events as split nullable fields. They carry the native message reference from the adapter codec; the pipeline persists the same values as an inbound `NativeMessageRef` after canonical event storage. All four fields are `NULL` for outbound events or events created internally.

`relations` on the in-memory `CanonicalEvent` are not stored in the `payload` or `metadata` columns. They are reconstructed at load time from `event_relations`.

### 3.2 event_relations

```sql
CREATE TABLE event_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    relation_type TEXT NOT NULL CHECK(relation_type IN ('reply', 'reaction', 'edit', 'delete', 'thread')),
    target_event_id TEXT,                -- Canonical event ID of the target, once resolved
    target_native_ref TEXT,              -- JSON: NativeRef dict when canonical ID not yet known
    key TEXT,                            -- Relation-specific key (e.g., emoji for reactions)
    fallback_text TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_relations_event ON event_relations(event_id);
CREATE INDEX idx_relations_target ON event_relations(target_event_id);
CREATE INDEX idx_relations_type ON event_relations(relation_type);
```

`target_native_ref` holds a JSON-serialized `NativeRef` dict when the canonical event ID hasn't been resolved yet. The relation resolution stage resolves it to `target_event_id` by querying `native_message_refs`.

`key` carries type-specific data: emoji for reactions, reason/label for other types.

`fallback_text` is the inline text representation used when the target adapter doesn't support the relation type natively.

### 3.3 native_message_refs

```sql
CREATE TABLE native_message_refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    adapter TEXT NOT NULL,
    native_channel_id TEXT NOT NULL,
    native_message_id TEXT NOT NULL,
    native_thread_id TEXT,
    native_relation_id TEXT,
    direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
    metadata TEXT NOT NULL DEFAULT '{}',  -- JSON
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(adapter, native_channel_id, native_message_id)
);

CREATE INDEX idx_native_refs_event ON native_message_refs(event_id);
CREATE INDEX idx_native_refs_adapter_native ON native_message_refs(adapter, native_message_id);
CREATE INDEX idx_native_refs_relation ON native_message_refs(adapter, native_relation_id);
```

The `UNIQUE(adapter, native_channel_id, native_message_id)` constraint is the foundation of idempotent correlation. Two calls to `store_native_ref` with the same adapter, channel, and message ID must not create duplicate rows.

Transport-specific examples:

| Transport | native_channel_id | native_message_id |
|---|---|---|
| Matrix | Room ID (e.g., `!abc:server.org`) | Matrix event ID (e.g., `$abc123`) |
| Meshtastic | Channel index | Packet ID |
| MeshCore | Channel slot index | MeshCore message reference |
| LXMF | Source hash (16-byte hex) | LXMF message ID |

### 3.4 delivery_receipts

```sql
CREATE TABLE delivery_receipts (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT UNIQUE NOT NULL,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    delivery_plan_id TEXT NOT NULL,
    target_adapter TEXT NOT NULL,
    status TEXT NOT NULL,             -- "accepted", "queued", "sent", "confirmed", "failed", "dead_lettered"
    error TEXT,
    adapter_message_id TEXT,
    next_retry_at TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    parent_receipt_id TEXT,
    created_at TEXT NOT NULL
);
```

`sequence` provides a strictly monotonic append order. Used by the `delivery_status` view to deterministically find the latest receipt, avoiding timestamp collisions.

Every delivery attempt produces a new row. Existing rows are never updated or deleted. A delivery that retried three times produces four rows.

Status values are `accepted`, `queued`, `sent`, `confirmed`, `failed`, `dead_lettered`. Note: `confirmed` (not `acknowledged`) is the status for transport-level acknowledgement.

`attempt_number` is the 1-indexed attempt number for this receipt. The first delivery attempt is `1`; retries increment from there. Enables receipt lineage ordering without relying on timestamps.

`parent_receipt_id` is the receipt ID of the preceding attempt in this delivery chain. `NULL` for the first attempt. Together with `attempt_number` this provides explicit receipt lineage.

Receipts are **append-only records**. The "current status" of a delivery is a **projection**: the latest receipt for a given `(delivery_plan_id, target_adapter)` tuple, provided by the `delivery_status` view (Section 3.5). No code path writes to the view directly. To change the "current status", append a new receipt row.

### 3.5 delivery_status View

The current delivery status for any plan is a projection: the latest receipt row for a given `(delivery_plan_id, target_adapter)` tuple.

```sql
CREATE VIEW delivery_status AS
SELECT dr.* FROM delivery_receipts dr
JOIN (
    SELECT delivery_plan_id, target_adapter, MAX(sequence) AS max_seq
    FROM delivery_receipts GROUP BY delivery_plan_id, target_adapter
) latest ON dr.sequence = latest.max_seq;
```

Uses `MAX(sequence)` for deterministic ordering rather than timestamps, which may collide.

### 3.6 plugin_state

```sql
CREATE TABLE plugin_state (
    plugin_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '{}',  -- JSON
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (plugin_id, key)
);
```

Scoped key-value storage for plugins. Keys are scoped to `plugin_id`. Plugins cannot read or write state belonging to other plugins.

### 3.7 Identity Tables

These tables support identity resolution and actor management.

#### actors

```sql
CREATE TABLE actors (
    actor_id TEXT PRIMARY KEY,           -- Runtime-unique actor ID (UUIDv7)
    display_name TEXT NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK(verification_status IN ('verified', 'manual', 'auto', 'unverified')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
```

#### native_identities

```sql
CREATE TABLE native_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    adapter TEXT NOT NULL,               -- Adapter instance name
    native_id TEXT NOT NULL,             -- Transport-specific ID
    native_name TEXT,                    -- Display name on the transport
    native_metadata TEXT NOT NULL DEFAULT '{}',  -- JSON
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(adapter, native_id)
);
```

#### actor_identity_links

```sql
CREATE TABLE actor_identity_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    native_identity_id INTEGER NOT NULL REFERENCES native_identities(id),
    link_method TEXT NOT NULL DEFAULT 'auto'
        CHECK(link_method IN ('verified', 'manual', 'auto')),
    linked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(actor_id, native_identity_id)
);
```

#### actor_permissions

```sql
CREATE TABLE actor_permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    permission TEXT NOT NULL,            -- e.g., 'admin', 'post_cross_channel'
    granted_by TEXT,                     -- 'operator' or 'auto_rule'
    granted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(actor_id, permission)
);
```

### 3.8 native_archive

```sql
CREATE TABLE native_archive (
    archive_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    adapter TEXT NOT NULL,
    raw_data BLOB NOT NULL,          -- Compressed (zstd or gzip)
    compression TEXT NOT NULL DEFAULT 'gzip',
    archived_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
```

Opt-in per adapter. Raw data is compressed and stored in storage only, never embedded in presentation events. Retention is configurable (time-based or count-based pruning).

Configuration example:

```yaml
storage:
  native_archive:
    enabled: true
    compression: gzip
    retention:
      max_age_days: 30
      max_count: 100000
    adapters:
      meshcore-radio-1: true
      mqtt-bridge: true
      matrix-home: false
```

## 4. Required Guarantees

| Guarantee | Required | Details |
|---|---|---|
| Atomic writes | **Required** | Event append, native ref storage, and receipt append must be atomic. A partial write must not leave the database in an inconsistent state. |
| Idempotent correlation | **Required** | Storing the same `(adapter, native_channel_id, native_message_id)` tuple twice must not create duplicate rows. The `UNIQUE` constraint enforces this. |
| Ordered append | **Required** | `canonical_events` rows are ordered by `timestamp`. `delivery_receipts` rows are ordered by `sequence` (monotonic auto-increment). |
| Replay | **Required** | The event log must support querying by time range, event kind, source adapter, and other filter criteria for replay and reprocessing. |
| Relation lookup | **Required** | Given a `target_native_ref` (adapter, native_channel_id, native_message_id), the resolver must find the canonical `event_id` via `native_message_refs`. |
| Receipt immutability | **Required** | Receipt rows are append-only. No `UPDATE` or `DELETE` on `delivery_receipts`. Current status is always a projection. |
| Concurrent reads | **Required** | WAL mode must be enabled to allow concurrent reads while writes are in progress. |
| Raw archival | Optional | Per-adapter opt-in. Not required for core pipeline operation. |
| Future backend swap | Optional | The `StorageBackend` protocol abstracts over implementation. PostgreSQL, NATS JetStream, Redis Streams, and Kafka are future possibilities. |

## 5. Storage Method Semantics

### 5.1 append(event)

- Writes a `CanonicalEvent` as a single row into `canonical_events`.
- The `relations` list is not stored inline. Call `store_relation` separately for each relation.
- Must be atomic. If the write fails, the database state must be unchanged.

### 5.2 get(event_id)

- Returns the `CanonicalEvent` for the given ID, or `None` if not found.
- The in-memory `relations` list must be reconstructed by querying `event_relations` for this event's ID.

### 5.3 store_native_ref(ref)

- Inserts a row into `native_message_refs`.
- If `(adapter, native_channel_id, native_message_id)` already exists, the insert is a no-op (idempotent).
- The `direction` field records whether this ref was created on ingress (`inbound`) or delivery (`outbound`).
- For inbound events, the pipeline calls `store_native_ref` with `direction="inbound"` after canonical event storage, using the native message ID carried on `CanonicalEvent.source_native_ref`.
- For outbound events, the pipeline calls `store_native_ref` with `direction="outbound"` after successful delivery, using the native event ID returned by the adapter.

### 5.4 resolve_native_ref(adapter, native_channel_id, native_message_id)

- Queries `native_message_refs` for the given `(adapter, native_channel_id, native_message_id)`.
- Returns the `event_id` if found, `None` otherwise.
- This is the primary lookup used by relation resolution to map `target_native_ref` from an `EventRelation` back to a canonical event ID.

### 5.5 store_relation(event_id, relation)

- Inserts a row into `event_relations` for the given event and relation.
- Serializes `target_native_ref` as JSON if present.
- Called separately from `append`. The caller is responsible for storing all relations after appending the event.

### 5.6 list_relations(event_id)

- Queries `event_relations` for the given event ID.
- Returns a `list[EventRelation]`, reconstructing each from the stored row.
- Used by `get` to rebuild the in-memory `relations` list on a `CanonicalEvent`.

### 5.7 append_receipt(receipt)

- Inserts a new row into `delivery_receipts`.
- Never updates an existing row. Every call creates a new row with a new `sequence` value.
- The current status of a delivery is read from the `delivery_status` view, not from any single row.

### 5.8 archive_raw(event_id, adapter, data) (Future)

> **Note:** `archive_raw` is not part of the Phase 1 `StorageBackend` protocol. It appears in the master spec as a future capability. The `native_archive` table schema is defined in Section 3.8 for reference but is not created or used in Phase 1.

## 6. Replay Interface

The canonical event log supports replaying events through the pipeline. The replay interface is defined in the dedicated replay contract (`docs/contracts/07-replay-event-log-contract.md`), which specifies `ReplayRequest`, `ReplayMode`, stage guarantees, and constraints.

### 6.1 Key Constraints (Summary)

- Replay never modifies existing events. It creates new derived events and new receipts.
- Replay can target specific stages (e.g., re-run transforms only, skip policy).
- Phase 1 does not implement replay rate limiting, progress tracking, or resumption.
- Phase 1 does not implement receipt deduplication during replay.
- Phase 1 does not preserve historical renderer or adapter versions during replay.

### 6.2 Use Cases

| Scenario | Mode | target_stages |
|---|---|---|
| New plugin wants historical events | `RE_RENDER` or `BEST_EFFORT` | render, deliver |
| New adapter added, needs past events | `BEST_EFFORT` | deliver |
| Routing rules changed, re-evaluate | `RE_ROUTE` | route, plan |
| Debug current config against past events | `DRY_RUN` | all |

### 6.3 Future Backend Compatibility

The replay interface works against the `StorageBackend.query` method and remains the same regardless of backend. Future streaming backends (NATS JetStream, Redis Streams, Kafka) replace the storage query with a stream consumer, but the `ReplayRequest` model and constraints stay identical.

## 7. Implementation Reference

Package location: `core/storage/`

```
core/storage/
    __init__.py
    backend.py     # StorageBackend protocol definition
    sqlite.py      # SQLite implementation
    replay.py      # Replay engine
    archive.py     # Raw native archive management
```

The SQLite implementation must:

1. Enable WAL mode on connection.
2. Use transactions for atomic multi-table writes (e.g., appending an event and its native refs together).
3. Handle the `UNIQUE` constraint on `native_message_refs` with `INSERT OR IGNORE` or equivalent idempotent behavior.
4. Reconstruct `CanonicalEvent.relations` from `event_relations` on every `get` and `query` call.

Cross-reference: Canonical event model is defined in `core/events/canonical.py` (spec Section 5). Delivery receipt model is in spec Section 10. Identity model is in spec Section 11. Adapter context provides a `StorageBackend` instance (spec Section 9.4).

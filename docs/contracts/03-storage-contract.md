# Storage Contract

> Extracted from: Modular Event Communications Runtime Specification, Sections 12, 18, 19
> Version: 0.1.0 (Draft)
> Last updated: 2026-05-13

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
        """Yield events matching filter, ordered by timestamp ascending.

        Ordering is ``ORDER BY timestamp ASC`` only. There is no secondary
        sort on event_id. Events with identical timestamps may be yielded
        in any order.
        """
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

    async def list_receipts_by_replay_run(
        self, run_id: str
    ) -> list[DeliveryReceipt]:
        """Return all receipts produced by a specific replay run, ordered by
        sequence ascending. Returns empty list if no receipts match."""
        ...

    async def list_receipts_for_event(
        self, event_id: str
    ) -> list[DeliveryReceipt]:
        """Return all delivery receipts for a specific event, ordered by
        sequence ascending."""
        ...

    # -- Lifecycle ----------------------------------------------------------

    async def initialize(self) -> None:
        """Prepare the backend for use (open connections, create schema)."""
        ...

    async def close(self) -> None:
        """Release all resources held by the backend."""
        ...

    # -- Retry --------------------------------------------------------------

    async def list_due_retry_receipts(
        self, now: datetime, limit: int = 50, max_attempts: int = 3
    ) -> list[Any]:
        """Return transient-failure receipts whose next_retry_at <= now.

        Ordered by next_retry_at ASC, sequence ASC, limited to *limit*.
        Excludes receipts that have reached *max_attempts* or are dead_lettered.

        The returned receipts carry retry policy metadata
        (retry_max_attempts, retry_backoff_base, retry_max_delay,
        retry_jitter) from the original failure, allowing the RetryWorker
        to continue the same policy without re-reading route configuration.
        """
        ...

    async def count_pending_retry(self, now: datetime, max_attempts: int = 3) -> int:
        """Count transient-failure receipts due for retry."""
        ...

    async def update_retry_due(
        self, receipt_id: str, next_retry_at: datetime,
    ) -> None:
        """Update next_retry_at on a receipt (for capacity rejection backoff).

        This is the only mutation allowed on existing receipt rows -- all
        other receipt updates are append-only. Used by the RetryWorker when
        delivery capacity is unavailable: the existing failed receipt's
        next_retry_at is advanced to the next cycle without creating a new
        receipt row.
        """
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
    timestamp TEXT NOT NULL,            -- ISO 8601, set by application code
    source_adapter TEXT NOT NULL,
    source_transport_id TEXT NOT NULL,  -- Native actor/source identity (not native message ID)
    source_channel_id TEXT,             -- Native channel/room/topic on source adapter
    parent_event_id TEXT,
    lineage TEXT NOT NULL DEFAULT '[]', -- JSON array of event IDs
    payload TEXT NOT NULL DEFAULT '{}', -- JSON
    metadata TEXT NOT NULL DEFAULT '{}',-- JSON (serialised EventMetadata)
    depth INTEGER NOT NULL DEFAULT 0,
    trace_id TEXT,
    source_native_adapter TEXT,         -- Source NativeRef.adapter for inbound native reference
    source_native_channel_id TEXT,      -- Source NativeRef.native_channel_id
    source_native_message_id TEXT,      -- Source NativeRef.native_message_id
    source_native_thread_id TEXT,       -- Source NativeRef.native_thread_id
    created_at TEXT NOT NULL            -- Set by application code, no SQL DEFAULT
);
```

**Indexes:**

| Index                           | Columns                 | Type                  | Purpose                                                                      |
| ------------------------------- | ----------------------- | --------------------- | ---------------------------------------------------------------------------- |
| `idx_events_timestamp_event_id` | `(timestamp, event_id)` | Manual `CREATE INDEX` | Supports `query()` ORDER BY timestamp ascending with tiebreaker on event_id. |

> **Additional query patterns:** Lookups by `event_kind`, `(source_adapter, source_transport_id)`, and `parent_event_id` are documented as candidates for future indexes but are not yet created. The PRIMARY KEY on `event_id` provides direct lookups by event ID.

`source_transport_id` identifies the native actor (who produced the event), not the native message. Native message IDs belong in `native_message_refs`.

`source_channel_id` is the native channel/room/topic where the event originated. `NULL` if the transport has no channel concept.

The `source_native_*` columns persist the optional `CanonicalEvent.source_native_ref` for inbound events as split nullable fields. They carry the native message reference from the adapter codec; the pipeline persists the same values as an inbound `NativeMessageRef` after canonical event storage. All four fields are `NULL` for outbound events or events created internally.

`relations` on the in-memory `CanonicalEvent` are not stored in the `payload` or `metadata` columns. They are reconstructed at load time from `event_relations`.

### 3.2 event_relations

```sql
CREATE TABLE event_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    relation_type TEXT NOT NULL,
    target_event_id TEXT,                -- Canonical event ID of the target, once resolved
    target_native_adapter TEXT,          -- Split from NativeRef when canonical ID not yet known
    target_native_channel_id TEXT,
    target_native_message_id TEXT,
    target_native_thread_id TEXT,
    key TEXT,                            -- Relation-specific key (e.g., emoji for reactions)
    fallback_text TEXT,
    metadata TEXT NOT NULL DEFAULT '{}', -- JSON
    created_at TEXT NOT NULL             -- Set by application code, no SQL DEFAULT
);
```

**Indexes:**

| Index                    | Columns          | Type                  | Purpose                                                                               |
| ------------------------ | ---------------- | --------------------- | ------------------------------------------------------------------------------------- |
| `idx_relations_event_id` | `(event_id, id)` | Manual `CREATE INDEX` | Supports `list_relations(event_id)` lookups with deterministic row ordering via `id`. |

> **Additional query patterns:** Lookups by `target_event_id` and `relation_type` are candidates for future indexes but are not yet created.

The `target_native_*` split columns store the `NativeRef` fields when the canonical event ID for the relation target is not yet known. When a relation is unresolved, `target_event_id` is `NULL` and the four `target_native_*` columns carry the native reference. The relation resolution stage resolves these to `target_event_id` by calling `resolve_native_ref(adapter, native_channel_id, native_message_id)` against `native_message_refs`. At load time, `_row_to_relation` reconstructs the in-memory `EventRelation.target_native_ref` from the four split columns.

`key` carries type-specific data: emoji for reactions, reason/label for other types.

`fallback_text` is the inline text representation used when the target adapter doesn't support the relation type natively.

### 3.3 native_message_refs

```sql
CREATE TABLE native_message_refs (
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
```

The `UNIQUE(adapter, native_channel_id, native_message_id)` constraint is the foundation of idempotent correlation. Two calls to `store_native_ref` with the same adapter, channel, and message ID must not create duplicate rows.

**NULL-channel idempotency:** SQLite treats each `NULL` as distinct for `UNIQUE` constraints (`NULL != NULL`). When `native_channel_id` is `NULL`, the UNIQUE constraint alone cannot prevent duplicate rows. `SQLiteStorage.store_native_ref` performs an explicit resolve-before-insert check: it queries for an existing row with the same `(adapter, NULL, native_message_id)` before inserting. If a match is found, the insert is skipped. This ensures NULL-channel native refs are idempotent despite the SQL standard's NULL handling.

Transport-specific examples:

| Transport  | native_channel_id                 | native_message_id                 |
| ---------- | --------------------------------- | --------------------------------- |
| Matrix     | Room ID (e.g., `!abc:server.org`) | Matrix event ID (e.g., `$abc123`) |
| Meshtastic | Channel index                     | Packet ID                         |
| MeshCore   | Channel slot index                | MeshCore message reference        |
| LXMF       | Source hash (16-byte hex)         | LXMF message ID                   |

**Indexes:**

| Index                      | Columns                                           | Type                                      | Purpose                                                                                                                                     |
| -------------------------- | ------------------------------------------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `idx_native_refs_event_id` | `(event_id)`                                      | Manual `CREATE INDEX`                     | Reverse lookup from a canonical event to all its native message references.                                                                 |
| _(autoindex)_              | `(adapter, native_channel_id, native_message_id)` | SQLite autoindex from `UNIQUE` constraint | No manual `CREATE INDEX` is needed — the `UNIQUE` constraint already produces an SQLite autoindex that covers `resolve_native_ref` lookups. |

### 3.4 delivery_receipts

```sql
CREATE TABLE delivery_receipts (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT UNIQUE NOT NULL,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    delivery_plan_id TEXT NOT NULL,
    target_adapter TEXT NOT NULL,
    target_channel TEXT,
    route_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,             -- "accepted", "queued", "sent", "confirmed", "failed", "dead_lettered"
    error TEXT,
    failure_kind TEXT,
    adapter_message_id TEXT,
    next_retry_at TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    parent_receipt_id TEXT,
    source TEXT NOT NULL DEFAULT 'live',  -- "live" for normal deliveries, "retry" for RetryWorker-attempted deliveries, "replay" for replay deliveries
    replay_run_id TEXT,                   -- Set to the replay run_id when source='replay'; NULL for live
    retry_max_attempts INTEGER,           -- RetryPolicy.max_attempts snapshot at first failure; NULL when no retry policy
    retry_backoff_base REAL,              -- RetryPolicy.backoff_base snapshot at first failure; NULL when no retry policy
    retry_max_delay REAL,                 -- RetryPolicy.backoff_max snapshot at first failure; NULL when no retry policy
    retry_jitter INTEGER,                 -- RetryPolicy.jitter enabled flag snapshot at first failure; NULL when no retry policy
    created_at TEXT NOT NULL
);
```

`sequence` provides a strictly monotonic append order. Used by the `delivery_status` view to deterministically find the latest receipt, avoiding timestamp collisions.

Every delivery attempt produces a new row. Existing rows are never updated or deleted. A delivery that retried three times produces four rows.

Status values are `accepted`, `queued`, `sent`, `confirmed`, `failed`, `dead_lettered`. Note: `confirmed` (not `acknowledged`) is the status for transport-level acknowledgement.

`target_channel` carries the target channel/room/topic from the `RouteTarget`. This is the logical channel name resolved at delivery planning time. `NULL` if the route target does not specify a channel.

`failure_kind` carries the `DeliveryFailureKind` value (e.g., `"adapter_transient"`, `"adapter_permanent"`, `"planner_failure"`, `"renderer_failure"`, `"deadline_exceeded"`, `"capacity_rejection"`, `"shutdown_rejection"`) when `status` is `failed`. `NULL` on successful deliveries. This field drives retry decisions and operator diagnosis.

`attempt_number` is the 1-indexed attempt number for this receipt. The first delivery attempt is `1`; retries increment from there. Enables receipt lineage ordering without relying on timestamps.

`parent_receipt_id` is the receipt ID of the preceding attempt in this delivery chain. `NULL` for the first attempt. Together with `attempt_number` this provides explicit receipt lineage.

`source` indicates the origin of the delivery: `"live"` for normal pipeline deliveries, `"retry"` for RetryWorker-attempted deliveries, `"replay"` for deliveries produced during replay. Every receipt has this field set; it defaults to `"live"` so receipts from normal pipeline operation are always explicitly tagged.

`replay_run_id` is `NULL` for live deliveries. When `source='replay'`, this field carries the `run_id` of the replay that produced the delivery. This allows operators to trace which receipts came from a specific replay run. It is for traceability only — it does not prevent duplicate sends.

`retry_max_attempts`, `retry_backoff_base`, `retry_max_delay`, and `retry_jitter` are snapshots of the `RetryPolicy` parameters at the time of the first failure receipt. They are persisted so that the `RetryWorker` can continue the same retry policy after process restart without re-reading route configuration. These fields are `NULL` on receipts that have no retry policy (e.g., successful first-attempt deliveries, deliveries without a configured `RetryPolicy`, or replay receipts). Once set on the first failure receipt, subsequent retry receipts in the same lineage inherit the same policy values. This ensures retry policy is frozen at first failure: route config changes after the original failure do not affect in-flight retry behavior.

**Native message refs are NOT source-tagged.** `NativeMessageRef` rows do not carry `source` or `replay_run_id` fields. This is intentional: native refs created during replay can be correlated to their replay origin through the associated `DeliveryReceipt` (which carries `source` and `replay_run_id`), then via the receipt's `delivery_plan_id` / `event_id` flow. Adding source tagging to native refs would increase schema complexity without proportional benefit, since the receipt → native ref linkage already provides full traceability.

Receipts are **append-only records**. The "current status" of a delivery is a **projection**: the latest receipt for a given `(delivery_plan_id, target_adapter)` tuple, provided by the `delivery_status` view (Section 3.5). No code path writes to the view directly. To change the "current status", append a new receipt row.

**Indexes:**

| Index                 | Columns                                                        | Type                  | Purpose                                                                                                                                                                                                                                                                                                                                                           |
| --------------------- | -------------------------------------------------------------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `idx_receipts_plan`   | `(delivery_plan_id, target_adapter, attempt_number, sequence)` | Manual `CREATE INDEX` | Supports both `delivery_status` view's `GROUP BY (delivery_plan_id, target_adapter)` + `MAX(sequence)` projection and `list_receipts_for_plan()` `ORDER BY attempt_number, sequence` lineage walk. The four-column composite covers the `delivery_status` subquery prefix `(delivery_plan_id, target_adapter)` and the full ordering of `list_receipts_for_plan`. |
| `idx_receipts_event`  | `(event_id, sequence)`                                         | Manual `CREATE INDEX` | Supports receipt lookups by event (e.g., finding all delivery attempts for a given event).                                                                                                                                                                                                                                                                        |
| `idx_receipts_source` | `(source, replay_run_id)`                                      | Manual `CREATE INDEX` | Supports filtering receipts by replay run — traceability queries for `source='replay'` with a specific `replay_run_id`.                                                                                                                                                                                                                                           |

### 3.5 delivery_status View

The current delivery status for any plan is a projection: the latest receipt row for a given `(delivery_plan_id, target_adapter)` tuple.

```sql
CREATE VIEW IF NOT EXISTS delivery_status AS
SELECT dr.sequence, dr.receipt_id, dr.event_id, dr.delivery_plan_id,
       dr.target_adapter, dr.target_channel, dr.route_id, dr.status, dr.error,
       dr.failure_kind, dr.adapter_message_id, dr.next_retry_at, dr.attempt_number,
       dr.parent_receipt_id, dr.source, dr.replay_run_id,
       dr.retry_max_attempts, dr.retry_backoff_base, dr.retry_max_delay, dr.retry_jitter,
       dr.created_at
FROM delivery_receipts dr
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
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (plugin_id, key)
);
```

Scoped key-value storage for plugins. Keys are scoped to `plugin_id`. Plugins cannot read or write state belonging to other plugins.

### 3.7 \_medre_schema_meta

```sql
CREATE TABLE _medre_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

Internal metadata table. Used to track the storage schema version. On initialization, `SQLiteStorage` checks that the stored `schema_version` matches `_EXPECTED_SCHEMA_VERSION`. On a fresh database, the version row is inserted. If the version mismatches, `StorageInitializationError` is raised (see Section 5.10).

### 3.8 Identity Tables

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

### 3.9 native_archive

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

### 3.10 Index Policy

All indexes are created via `CREATE INDEX IF NOT EXISTS` during `initialize()`, alongside table DDL. They are part of the pre-release schema shape but are **not** individually versioned.

Key points:

- **No automatic migration.** Adding or changing an index does not bump `_EXPECTED_SCHEMA_VERSION`. Indexes are created idempotently (`IF NOT EXISTS`) on every `initialize()` call.
- **Performance only.** Indexes affect query performance, not correctness. A database that lacks an index will return the same results, just more slowly.
- **Recreation guidance.** Existing old pre-release databases that predate an index addition will gain the index on the next `initialize()` call. No manual intervention is required.
- **Column-shape validation remains the hard compatibility check.** The `_validate_schema_shape()` check (Section 5.10) catches structural incompatibilities. Missing indexes are never a compatibility failure.
- **SQLite autoindexes are not duplicated.** Tables with `UNIQUE` constraints (e.g., `native_message_refs(adapter, native_channel_id, native_message_id)`) already have an SQLite autoindex. No manual `CREATE INDEX` is created for those column sets.

## 4. Required Guarantees

| Guarantee              | Required     | Details                                                                                                                                                                                                                                                         |
| ---------------------- | ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Atomic writes          | **Required** | Event append, native ref storage, and receipt append must be atomic. A partial write must not leave the database in an inconsistent state.                                                                                                                      |
| Idempotent correlation | **Required** | Storing the same `(adapter, native_channel_id, native_message_id)` tuple twice must not create duplicate rows. The `UNIQUE` constraint enforces this.                                                                                                           |
| Ordered append         | **Required** | `canonical_events` rows are ordered by `timestamp` (ascending, no secondary sort). `delivery_receipts` rows are ordered by `sequence` (monotonic auto-increment).                                                                                               |
| Replay                 | **Required** | The event log must support querying by time range, event kind, source adapter, and other filter criteria for replay and reprocessing.                                                                                                                           |
| Relation lookup        | **Required** | Given a `target_native_ref` (adapter, native*channel_id, native_message_id), the resolver must find the canonical `event_id` via `native_message_refs`. Unresolved relations store the native reference as split `target_native*\*`columns in`event_relations`. |
| Receipt immutability   | **Required** | Receipt rows are append-only. No `UPDATE` or `DELETE` on `delivery_receipts`. Current status is always a projection.                                                                                                                                            |
| Concurrent reads       | **Required** | WAL mode must be enabled to allow concurrent reads while writes are in progress.                                                                                                                                                                                |
| Raw archival           | Optional     | Per-adapter opt-in. Not required for core pipeline operation.                                                                                                                                                                                                   |
| Future backend swap    | Optional     | The `StorageBackend` protocol abstracts over implementation. PostgreSQL, NATS JetStream, Redis Streams, and Kafka are future possibilities.                                                                                                                     |

## 5. Storage Method Semantics

### 5.1 append(event)

- Writes a `CanonicalEvent` as a single row into `canonical_events`, together with all inline relations as rows in `event_relations`, in a single atomic batch.
- Raises `DuplicateEventError` (a subclass of `StorageError`) if `event_id` already exists in `canonical_events`. Events are append-only; callers that need idempotent semantics should check with `get` first or catch this exception.
- The `relations` tuple on the in-memory `CanonicalEvent` is persisted as separate rows in `event_relations` within the same batch write. No separate `store_relation` call is needed for inline relations.
- Must be atomic. If the write fails, the database state must be unchanged.

### 5.2 get(event_id)

- Returns the `CanonicalEvent` for the given ID, or `None` if not found.
- The in-memory `relations` list must be reconstructed by querying `event_relations` for this event's ID.

### 5.3 store_native_ref(ref)

- Inserts a row into `native_message_refs`.
- If `(adapter, native_channel_id, native_message_id)` already exists, the insert is a no-op (idempotent).
- **NULL-channel handling:** When `native_channel_id` is `NULL`, SQLite's `UNIQUE` constraint cannot detect duplicates (SQL standard: `NULL != NULL`). The implementation performs an explicit resolve-before-insert: it queries for an existing row with the same `(adapter, NULL, native_message_id)` before inserting. If a match is found, the insert is skipped. This ensures NULL-channel native refs are idempotent.
- The `direction` field records whether this ref was created on ingress (`inbound`) or delivery (`outbound`).
- For inbound events, the pipeline calls `store_native_ref` with `direction="inbound"` after canonical event storage, using the native message ID carried on `CanonicalEvent.source_native_ref`.
- For outbound events, the pipeline calls `store_native_ref` with `direction="outbound"` after successful delivery, using the native event ID returned by the adapter.

### 5.4 resolve_native_ref(adapter, native_channel_id, native_message_id)

- Queries `native_message_refs` for the given `(adapter, native_channel_id, native_message_id)`.
- Returns the `event_id` if found, `None` otherwise.
- This is the primary lookup used by relation resolution to map `target_native_ref` from an `EventRelation` back to a canonical event ID.

### 5.5 store_relation(event_id, relation)

- Inserts a row into `event_relations` for the given event and relation.
- Stores `target_native_ref` as four split nullable columns (`target_native_adapter`, `target_native_channel_id`, `target_native_message_id`, `target_native_thread_id`). At load time, `_row_to_relation` reconstructs the in-memory `NativeRef` from these columns.
- Called separately from `append`. The caller is responsible for storing all relations after appending the event.

### 5.6 list_relations(event_id)

- Queries `event_relations` for the given event ID.
- Returns a `list[EventRelation]`, reconstructing each from the stored row.
- Used by `get` to rebuild the in-memory `relations` list on a `CanonicalEvent`.

### 5.7 append_receipt(receipt)

- Inserts a new row into `delivery_receipts`.
- Never updates an existing row. Every call creates a new row with a new `sequence` value.
- The current status of a delivery is read from the `delivery_status` view, not from any single row.
- The `source` field defaults to `"live"` for normal pipeline deliveries. Retry deliveries set `source='retry'`. Replay deliveries set `source='replay'` and populate `replay_run_id` with the replay run ID.

### 5.7a list_receipts_by_replay_run(run_id)

- Returns all `DeliveryReceipt` rows whose `replay_run_id` matches _run_id_, ordered by `sequence` ascending.
- Used for operator traceability: identifying all receipts produced by a specific replay run.
- Returns an empty list when no receipts match.
- This is a focused query helper for replay investigation. It does not provide deduplication or prevent duplicate sends.

### 5.7b list_receipts_for_event(event_id)

- Returns all `DeliveryReceipt` rows whose `event_id` matches _event_id_, ordered by `sequence` ascending.
- Used to inspect all delivery attempts (across all plans and adapters) for a given event.
- Returns an empty list when no receipts match.

### 5.7c list_due_retry_receipts(now, limit, max_attempts)

- Returns failed `DeliveryReceipt` rows where `next_retry_at <= now`, `status = 'failed'`, and `failure_kind = 'adapter_transient'`.
- Excludes receipts where `attempt_number >= max_attempts` or `status = 'dead_lettered'`.
- Ordered by `next_retry_at ASC, sequence ASC`. Limited to `limit` rows.
- Used by the `RetryWorker` to load receipts that are due for retry on each cycle.

### 5.7d count_pending_retry(now, max_attempts)

- Returns the count of transient-failure receipts due for retry.
- Same filter criteria as `list_due_retry_receipts` but returns a count instead of rows.

### 5.7e update_retry_due(receipt_id, next_retry_at)

- Updates `next_retry_at` on an existing receipt row identified by `receipt_id`.
- This is the **only** mutation allowed on an existing receipt row. All other receipt operations are append-only.
- Used by the `RetryWorker` when delivery capacity is unavailable: instead of creating a new receipt, the worker advances `next_retry_at` on the existing failed receipt to the next cycle. This avoids creating spurious receipt rows for capacity rejection.
- Capacity rejection does not advance `attempt_number` or count toward `RetryPolicy` exhaustion.

### 5.8 archive_raw(event_id, adapter, data) (Future)

> **Note:** `archive_raw` is not part of the Phase 1 `StorageBackend` protocol. It appears in the master spec as a future capability. The `native_archive` table schema is defined in Section 3.9 for reference but is not created or used in Phase 1.

### 5.9 Delivery Plan Methods

The delivery pipeline creates delivery receipts through the `build_retry_receipt` helper, which is used by `RetryExecutor` and the replay engine to construct a `DeliveryReceipt` for each delivery attempt.

**`build_retry_receipt`** accepts the following parameters beyond the standard receipt fields:

- `source` (default `"live"`) — Matches the `DeliveryReceipt.source` field. Set to `"retry"` when the receipt is produced by the RetryWorker, `"replay"` when the receipt is produced during a replay run. Normal pipeline deliveries leave this as `"live"`.
- `replay_run_id` (default `None`) — Matches the `DeliveryReceipt.replay_run_id` field. When `source="replay"`, this carries the `run_id` of the replay that produced the delivery. `None` for live deliveries.

These parameters ensure that receipts created by `RetryExecutor` carry the same traceability fields as receipts from the normal delivery pipeline, enabling operators to distinguish live deliveries from replay deliveries without inspecting the call site.

### 5.10 Pre-release Database Policy

MEDRE has not yet made its first release. There is no automatic migration support. Existing databases from prior development builds are not guaranteed to be compatible after schema-affecting changes. The `initialize()` method performs two validation checks:

1. **Schema version check** against `_medre_schema_meta`: On a fresh database, the version row is inserted automatically. If the stored version mismatches `_EXPECTED_SCHEMA_VERSION`, `StorageInitializationError` is raised with guidance to resolve the mismatch manually (export data, delete the database file, and restart; or downgrade medre to match the database version).

2. **Column-shape validation** via `_validate_schema_shape()`: After DDL execution, `initialize()` inspects `PRAGMA table_info` for each required table and compares column names against `_REQUIRED_COLUMNS` (which includes `source` and `replay_run_id` on `delivery_receipts`). If any required column is missing, `StorageInitializationError` is raised with a message identifying the affected table and missing columns, advising the operator to recreate the database. This catches old pre-release databases whose `schema_version` still reads `1` but whose column shape predates the current DDL.

## 6. Replay Interface

The canonical event log supports replaying events through the pipeline. The replay interface is defined in the dedicated replay contract (`docs/contracts/07-replay-event-log-contract.md`), which specifies `ReplayRequest`, `ReplayMode`, stage guarantees, and constraints.

### 6.1 Key Constraints (Summary)

- Replay never modifies existing events. It creates new derived events and new receipts.
- Replay can target specific stages (e.g., re-run transforms only, skip policy).
- Phase 1 does not implement replay rate limiting, progress tracking, or resumption.
- Phase 1 does not implement receipt deduplication during replay.
- Phase 1 does not preserve historical renderer or adapter versions during replay.

### 6.2 Use Cases

| Scenario                                 | Mode                         | target_stages   |
| ---------------------------------------- | ---------------------------- | --------------- |
| New plugin wants historical events       | `RE_RENDER` or `BEST_EFFORT` | render, deliver |
| New adapter added, needs past events     | `BEST_EFFORT`                | deliver         |
| Routing rules changed, re-evaluate       | `RE_ROUTE`                   | route, plan     |
| Debug current config against past events | `DRY_RUN`                    | all             |

### 6.3 Future Backend Compatibility

The replay interface works against the `StorageBackend.query` method and remains the same regardless of backend. Future streaming backends (NATS JetStream, Redis Streams, Kafka) replace the storage query with a stream consumer, but the `ReplayRequest` model and constraints stay identical.

## 7. Implementation Reference

Package location: `core/storage/`

```text
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

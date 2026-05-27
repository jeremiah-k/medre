# Storage Specification

> **Classification:** Normative
> **Authority:** Authoritative specification for the MEDRE storage layer: SQLite schema, StorageBackend protocol, path resolution, durability guarantees, and replay/recovery interface.
> **Audience:** Runtime builders, adapter authors, operators.

## Conformance

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHALL**, **SHALL NOT**, **SHOULD**, **SHOULD NOT**, **RECOMMENDED**, **MAY**, and **OPTIONAL** in this document are to be interpreted as described in RFC 2119.

## 1. Scope

This document specifies the MEDRE storage layer: the single source of truth for canonical events, native message references, delivery receipts, event relations, identity data, plugin state, raw native archives, operational delivery outbox state, and filesystem path resolution.

The initial and current backend is SQLite. The `StorageBackend` protocol (Section 3) abstracts over implementation so that future backends (PostgreSQL, NATS JetStream, Redis Streams, Kafka) MAY be substituted without changing callers.

## 2. Path Resolution

### 2.1 XDG Mode (Default)

When `MEDRE_HOME` is not set, the runtime **MUST** follow the XDG Base Directory Specification. Each category resolves independently:

| Category | Default Path            | XDG Override              |
| -------- | ----------------------- | ------------------------- |
| Config   | `~/.config/medre/`      | `$XDG_CONFIG_HOME/medre/` |
| State    | `~/.local/state/medre/` | `$XDG_STATE_HOME/medre/`  |
| Data     | `~/.local/share/medre/` | `$XDG_DATA_HOME/medre/`   |
| Cache    | `~/.cache/medre/`       | `$XDG_CACHE_HOME/medre/`  |

### 2.2 MEDRE_HOME Mode

When the `MEDRE_HOME` environment variable is set to a non-empty value, all categories **MUST** resolve under that single root:

| Category | Path                      |
| -------- | ------------------------- |
| Config   | `$MEDRE_HOME/config.toml` |
| State    | `$MEDRE_HOME/state/`      |
| Data     | `$MEDRE_HOME/data/`       |
| Cache    | `$MEDRE_HOME/cache/`      |
| Logs     | `$MEDRE_HOME/logs/`       |

This mode is intended for container, Docker, Kubernetes, and portable deployments.

### 2.3 Database Location

The SQLite database file **MUST** reside at `{state}/medre.sqlite`. When `StorageConfig.path` is `None`, the runtime resolves the database path from `MedrePaths.database_path`. When `StorageConfig.path` is set explicitly, the provided path **MUST** be used unchanged.

There **MUST NOT** be per-adapter databases. All persisted state resides in the single global database.

### 2.4 Path Resolution is No-I/O

Path resolution is a pure computation. No filesystem I/O **SHALL** occur during config loading or path resolution. Directories are created only at runtime startup.

### 2.5 Per-Adapter State Directories

Every adapter receives a state root at `{state}/adapters/{adapter_id}/`. Transport-specific subdirectories follow the pattern `{state}/adapters/{adapter_id}/{transport}/`. These directories hold transport-owned state (e.g., Matrix crypto store) and **MUST NOT** contain databases.

### 2.6 Summary

```text
{state}/medre.sqlite                                   — Global database (single backend)
{log_dir}/medre.log                                    — Global log file
{state}/adapters/{adapter_id}/                         — Per-adapter state root
{state}/adapters/{adapter_id}/matrix/store/            — Matrix E2EE crypto store
{state}/adapters/{adapter_id}/meshtastic/              — Meshtastic state
{state}/adapters/{adapter_id}/meshcore/                — MeshCore state
{state}/adapters/{adapter_id}/lxmf/                    — LXMF state
```

In XDG mode, `{log_dir}` resolves to `{state}/logs`. In MEDRE_HOME mode, `{log_dir}` resolves to `$MEDRE_HOME/logs`.

## 3. StorageBackend Protocol

Every storage implementation **MUST** satisfy this interface:

```python
class StorageBackend(Protocol):
    """Protocol defining the interface all storage backends MUST implement.

    Guarantees: events are append-only, delivery receipts are append-only,
    native refs are idempotent, relations are queryable by event_id, and
    query results are ordered by timestamp ascending.
    """

    # -- Event CRUD --------------------------------------------------------

    async def append(self, event: CanonicalEvent) -> None:
        """Persist a canonical event together with its inline relations.

        MUST be atomic.  Raises DuplicateEventError if event_id already
        exists.
        """
        ...

    async def get(self, event_id: str) -> CanonicalEvent | None:
        """Retrieve a single event by unique identifier.

        MUST reconstruct the in-memory relations list from event_relations.
        Returns None if not found.
        """
        ...

    async def query(self, filter: EventFilter) -> AsyncIterator[CanonicalEvent]:
        """Yield events matching filter, ordered by timestamp ascending.

        Ordering is ORDER BY timestamp ASC only.  There is no secondary
        sort on event_id.
        """
        ...

    # -- Native ref correlation ---------------------------------------------

    async def store_native_ref(self, ref: NativeMessageRef) -> None:
        """Persist a native-to-canonical message mapping.

        MUST be idempotent: storing the same (adapter, native_channel_id,
        native_message_id) tuple twice MUST NOT create duplicate rows.
        """
        ...

    async def resolve_native_ref(
        self, adapter: str, native_channel_id: str | None, native_message_id: str
    ) -> str | None:
        """Look up the canonical event ID for a native message reference.

        Returns None if no mapping exists.
        """
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
        """Append a delivery receipt record.  MUST NOT update existing rows."""
        ...

    async def delivery_status(
        self, delivery_plan_id: str, target_adapter: str,
        target_channel: str | None = None,
    ) -> DeliveryReceipt | None:
        """Return the latest receipt for a delivery plan / adapter / channel.

        When target_channel is None, only receipts with a NULL target
        channel are considered.  Returns None when no receipt exists.
        """
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
        sequence ascending.  Returns empty list if no receipts match."""
        ...

    async def list_receipts_for_event(
        self, event_id: str
    ) -> list[DeliveryReceipt]:
        """Return all delivery receipts for a specific event, ordered by
        sequence ascending."""
        ...

    # -- Lifecycle ----------------------------------------------------------

    async def initialize(self) -> None:
        """Prepare the backend for use (open connections, create schema).

        MUST enable WAL mode.  MUST validate schema version and column
        shape (see Section 10).
        """
        ...

    async def close(self) -> None:
        """Release all resources held by the backend."""
        ...

    # -- Retry --------------------------------------------------------------

    async def list_due_retry_receipts(
        self, now: datetime, limit: int = 50, max_attempts: int = 3
    ) -> list[Any]:
        """Return transient-failure receipts whose next_retry_at <= now.

        Ordered by next_retry_at ASC, sequence ASC, limited to limit.
        Excludes receipts that have reached max_attempts or are
        dead_lettered.  Returned receipts carry retry policy metadata.
        """
        ...

    async def count_pending_retry(self, now: datetime, max_attempts: int = 3) -> int:
        """Count transient-failure receipts due for retry."""
        ...

    async def update_retry_due(
        self, receipt_id: str, next_retry_at: datetime,
    ) -> None:
        """Update next_retry_at on a receipt (capacity rejection backoff).

        This is the only mutation allowed on existing receipt rows -- all
        other receipt updates are append-only.
        """
        ...

    # -- Outbox -------------------------------------------------------------

    async def create_outbox_item(self, item: OutboxItem) -> OutboxItem:
        """Create an outbox item.  Idempotent create with reclaim semantics
        (see Section 9.3)."""
        ...

    async def get_outbox_item(self, outbox_id: str) -> OutboxItem | None:
        """Retrieve an outbox item by ID."""
        ...

    async def list_outbox_items(
        self, status: str | None = None, limit: int = 100
    ) -> list[OutboxItem]:
        """List outbox items, optionally filtered by status."""
        ...

    async def claim_due_outbox_items(
        self, now: datetime, worker_id: str, limit: int = 50
    ) -> list[OutboxItem]:
        """Claim due outbox items for processing.  Items with pending,
        retry_wait, expired in_progress leases, or stale queued status
        are eligible."""
        ...

    async def mark_outbox_sent(self, outbox_id: str, receipt_id: str) -> None:
        """Mark an outbox item as sent (terminal)."""
        ...

    async def mark_outbox_failed(
        self, outbox_id: str, failure_kind: str, error_summary: str,
        next_attempt_at: datetime | None = None,
    ) -> None:
        """Mark an outbox item as retry_wait or dead_lettered."""
        ...

    async def mark_outbox_dead_lettered(
        self, outbox_id: str, failure_kind: str, error_summary: str,
    ) -> None:
        """Mark an outbox item as dead_lettered (terminal)."""
        ...

    async def release_outbox_claim(self, outbox_id: str) -> None:
        """Release a claimed outbox item back to pending."""
        ...

    async def count_outbox_by_status(self) -> dict[str, int]:
        """Return counts of outbox items grouped by status."""
        ...
```

## 4. SQLite Schema

### 4.1 canonical_events

```sql
CREATE TABLE canonical_events (
    event_id TEXT PRIMARY KEY,
    event_kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    timestamp TEXT NOT NULL,            -- ISO 8601, set by application code
    source_adapter TEXT NOT NULL,
    source_transport_id TEXT NOT NULL,  -- Native actor/source identity
    source_channel_id TEXT,             -- Native channel/room/topic
    parent_event_id TEXT,
    lineage TEXT NOT NULL DEFAULT '[]', -- JSON array of event IDs
    payload TEXT NOT NULL DEFAULT '{}', -- JSON
    metadata TEXT NOT NULL DEFAULT '{}',-- JSON (serialised EventMetadata)
    depth INTEGER NOT NULL DEFAULT 0,
    trace_id TEXT,
    source_native_adapter TEXT,         -- Source NativeRef.adapter
    source_native_channel_id TEXT,      -- Source NativeRef.native_channel_id
    source_native_message_id TEXT,      -- Source NativeRef.native_message_id
    source_native_thread_id TEXT,       -- Source NativeRef.native_thread_id
    created_at TEXT NOT NULL            -- Set by application code
);
```

**Indexes:**

| Index                           | Columns                 | Purpose                      |
| ------------------------------- | ----------------------- | ---------------------------- |
| `idx_events_timestamp_event_id` | `(timestamp, event_id)` | ORDER BY timestamp ascending |

`source_transport_id` identifies the native actor (who produced the event), not the native message. `source_channel_id` is the native channel/room/topic where the event originated; `NULL` if the transport has no channel concept.

The `source_native_*` columns persist the optional `CanonicalEvent.source_native_ref` for inbound events as split nullable fields. All four fields are `NULL` for outbound events or events created internally.

`relations` on the in-memory `CanonicalEvent` are not stored in `payload` or `metadata`. They are reconstructed at load time from `event_relations`.

**Append-only guarantee:** No row in `canonical_events` **MUST** ever be updated or deleted.

### 4.2 event_relations

```sql
CREATE TABLE event_relations (
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
```

**Indexes:**

| Index                    | Columns          | Purpose                                                  |
| ------------------------ | ---------------- | -------------------------------------------------------- |
| `idx_relations_event_id` | `(event_id, id)` | list_relations(event_id) with deterministic row ordering |

The `target_native_*` split columns store the `NativeRef` fields when the canonical event ID for the relation target is not yet known. When a relation is unresolved, `target_event_id` is `NULL` and the four `target_native_*` columns carry the native reference. The relation resolution stage resolves these by calling `resolve_native_ref`.

### 4.3 native_message_refs

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

**Indexes:**

| Index                      | Columns                                           | Purpose                                            |
| -------------------------- | ------------------------------------------------- | -------------------------------------------------- |
| `idx_native_refs_event_id` | `(event_id)`                                      | Reverse lookup from canonical event to native refs |
| _(autoindex)_              | `(adapter, native_channel_id, native_message_id)` | SQLite autoindex from UNIQUE constraint            |

The `UNIQUE(adapter, native_channel_id, native_message_id)` constraint is the foundation of idempotent correlation. Two calls to `store_native_ref` with the same adapter, channel, and message ID **MUST NOT** create duplicate rows.

**NULL-channel idempotency:** SQLite treats each `NULL` as distinct for `UNIQUE` constraints. When `native_channel_id` is `NULL`, implementations **MUST** perform an explicit resolve-before-insert check: query for an existing row with the same `(adapter, NULL, native_message_id)` before inserting. If a match is found, the insert **MUST** be skipped.

Transport-specific examples:

| Transport  | native_channel_id                 | native_message_id                 |
| ---------- | --------------------------------- | --------------------------------- |
| Matrix     | Room ID (e.g., `!abc:server.org`) | Matrix event ID (e.g., `$abc123`) |
| Meshtastic | Channel index                     | Packet ID                         |
| MeshCore   | Channel slot index                | MeshCore message reference        |
| LXMF       | `NULL`                            | LXMF message ID                   |

### 4.4 delivery_receipts

```sql
CREATE TABLE delivery_receipts (
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
```

`sequence` provides a strictly monotonic append order. It is used by the `delivery_status` view to deterministically find the latest receipt, avoiding timestamp collisions.

**Status values:** `accepted`, `queued`, `sent`, `confirmed`, `suppressed`, `failed`, `dead_lettered`.

`confirmed` (not `acknowledged`) is the status for transport-level acknowledgement. `suppressed` covers loop/capacity/shutdown rejection receipts.

`target_channel` carries the target channel/room/topic from the `RouteTarget`. `NULL` if the route target does not specify a channel.

`failure_kind` carries the `DeliveryFailureKind` value (e.g., `"adapter_transient"`, `"adapter_permanent"`, `"planner_failure"`, `"renderer_failure"`, `"deadline_exceeded"`, `"capacity_rejection"`, `"shutdown_rejection"`) when `status` is `failed`. `NULL` on successful deliveries.

`attempt_number` is the 1-indexed attempt number. The first delivery attempt is `1`; retries increment from there.

`parent_receipt_id` is the receipt ID of the preceding attempt in this delivery chain. `NULL` for the first attempt.

`source` indicates the origin: `"live"` for normal pipeline deliveries, `"retry"` for RetryWorker-attempted deliveries, `"replay"` for deliveries produced during replay. Defaults to `"live"`.

`replay_run_id` is `NULL` for live deliveries. When `source='replay'`, it carries the `run_id` of the replay that produced the delivery.

`retry_max_attempts`, `retry_backoff_base`, `retry_max_delay`, and `retry_jitter` are snapshots of the `RetryPolicy` parameters at the time of the first failure receipt. They are `NULL` on receipts with no retry policy. Once set on the first failure receipt, subsequent retry receipts in the same lineage inherit the same values. Retry policy is frozen at first failure.

**Append-only guarantee:** Every delivery attempt produces a new row. Existing rows **MUST NOT** be updated or deleted (except `next_retry_at` via `update_retry_due`). Current delivery status is a projection (Section 4.5).

**Indexes:**

| Index                 | Columns                                                                        | Purpose                                         |
| --------------------- | ------------------------------------------------------------------------------ | ----------------------------------------------- |
| `idx_receipts_plan`   | `(delivery_plan_id, target_adapter, target_channel, attempt_number, sequence)` | delivery_status view and list_receipts_for_plan |
| `idx_receipts_event`  | `(event_id, sequence)`                                                         | Receipt lookups by event                        |
| `idx_receipts_source` | `(source, replay_run_id)`                                                      | Filtering receipts by replay run                |

### 4.5 delivery_status View

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
    SELECT delivery_plan_id, target_adapter, target_channel, MAX(sequence) AS max_seq
    FROM delivery_receipts GROUP BY delivery_plan_id, target_adapter, COALESCE(target_channel, '')
) latest ON dr.sequence = latest.max_seq;
```

The current delivery status for any plan is a projection: the latest receipt row for a given `(delivery_plan_id, target_adapter, target_channel)` tuple. Uses `MAX(sequence)` for deterministic ordering. No code path **SHALL** write to this view directly. To change the current status, append a new receipt row.

The grouping uses `COALESCE(target_channel, '')` so that `NULL` and empty-string channels are treated as the same group.

### 4.6 plugin_state

```sql
CREATE TABLE plugin_state (
    plugin_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (plugin_id, key)
);
```

Scoped key-value storage for plugins. Keys are scoped to `plugin_id`. Plugins **MUST NOT** read or write state belonging to other plugins.

### 4.7 \_medre_schema_meta

```sql
CREATE TABLE _medre_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

Internal metadata table. Tracks the storage schema version. On initialization, the implementation **MUST** check that the stored `schema_version` matches `_EXPECTED_SCHEMA_VERSION`. On a fresh database, the version row is inserted. If the version mismatches, `StorageInitializationError` **MUST** be raised.

### 4.8 Identity Tables

#### actors

```sql
CREATE TABLE actors (
    actor_id TEXT PRIMARY KEY,
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
    adapter TEXT NOT NULL,
    native_id TEXT NOT NULL,
    native_name TEXT,
    native_metadata TEXT NOT NULL DEFAULT '{}',
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
    permission TEXT NOT NULL,
    granted_by TEXT,
    granted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(actor_id, permission)
);
```

### 4.9 native_archive

```sql
CREATE TABLE native_archive (
    archive_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    adapter TEXT NOT NULL,
    raw_data BLOB NOT NULL,
    compression TEXT NOT NULL DEFAULT 'gzip',
    archived_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
```

Opt-in per adapter. Raw data is compressed and stored in storage only, never embedded in presentation events. Retention is configurable (time-based or count-based pruning).

### 4.10 delivery_outbox

The `delivery_outbox` table persists operational delivery work state (distinct from the evidence/audit `delivery_receipts` log). Where receipts record what did happen, the outbox records what still needs to happen.

```sql
CREATE TABLE delivery_outbox (
    outbox_id       TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL,
    route_id        TEXT NOT NULL DEFAULT '',
    delivery_plan_id TEXT NOT NULL,
    target_adapter   TEXT NOT NULL,
    target_channel   TEXT,
    target_address   TEXT,
    attempt_number   INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'pending',
    failure_kind    TEXT,
    failure_kind_detail TEXT,
    next_attempt_at TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_attempt_at TEXT,
    locked_at       TEXT,
    lease_until     TEXT,
    worker_id       TEXT,
    payload_hash    TEXT,
    receipt_id      TEXT,
    parent_receipt_id TEXT,
    error_summary   TEXT,
    metadata        TEXT NOT NULL DEFAULT '{}',
    UNIQUE(delivery_plan_id, target_adapter, target_channel, attempt_number)
);
```

**Statuses:**

| Status          | Meaning                                          | Terminal |
| --------------- | ------------------------------------------------ | -------- |
| `pending`       | Work exists but has not started                  | No       |
| `in_progress`   | Claimed by a worker for processing               | No       |
| `queued`        | Handed to adapter-local queue (e.g., Meshtastic) | No       |
| `sent`          | Local SDK/client send returned success           | Yes      |
| `retry_wait`    | Transient failure, awaiting next attempt         | No       |
| `dead_lettered` | Retries exhausted or terminal failure            | Yes      |
| `cancelled`     | Operator or shutdown cancelled                   | Yes      |
| `abandoned`     | Drain timeout or ambiguous loss                  | Yes      |

**NULL-channel uniqueness:** The `UNIQUE` constraint on `(delivery_plan_id, target_adapter, target_channel, attempt_number)` is supplemented by a partial unique index:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_null_channel_unique
    ON delivery_outbox (delivery_plan_id, target_adapter, attempt_number)
    WHERE target_channel IS NULL;
```

This closes the SQLite `NULL != NULL` gap for outbox uniqueness.

### 4.11 Index Policy

All indexes are created via `CREATE INDEX IF NOT EXISTS` during `initialize()`, alongside table DDL. They are part of the pre-release schema shape but are not individually versioned.

- Indexes affect query performance, not correctness. A database that lacks an index **SHALL** return the same results, just more slowly.
- Adding or changing an index **MUST NOT** bump `_EXPECTED_SCHEMA_VERSION`.
- SQLite autoindexes from `UNIQUE` constraints **MUST NOT** be duplicated with manual `CREATE INDEX`.

## 5. Append-Only Guarantees

### 5.1 Events

No row in `canonical_events` **MUST** ever be updated or deleted. The `append` method **MUST** raise `DuplicateEventError` if `event_id` already exists.

### 5.2 Delivery Receipts

Every delivery attempt produces a new row in `delivery_receipts`. Existing rows **MUST NOT** be updated or deleted, with one exception: `update_retry_due` **MAY** modify `next_retry_at` on an existing receipt row for capacity rejection backoff. This is the sole permitted mutation.

The current status of a delivery is a projection from the `delivery_status` view (latest receipt by `MAX(sequence)`). No code path **SHALL** write to the view directly.

### 5.3 Native Message References

Storing the same `(adapter, native_channel_id, native_message_id)` tuple twice **MUST NOT** create duplicate rows. Implementations **MUST** use `INSERT OR IGNORE` or equivalent idempotent behavior for non-NULL channels. For NULL channels, an explicit resolve-before-insert check is **REQUIRED**.

## 6. WAL Mode

The SQLite implementation **MUST** enable WAL (Write-Ahead Logging) journal mode on every connection. WAL mode **MUST** be active for the lifetime of the connection.

WAL mode provides:

- Concurrent reads while writes are in progress.
- Crash consistency: committed transactions survive hard crash (`kill -9`, OOM, power loss).
- SQLite's crash recovery mechanism handles incomplete WAL frames on the next open.

## 7. Atomic Writes

Multi-table operations **MUST** be atomic:

- Event append (row in `canonical_events` plus rows in `event_relations`) **MUST** be a single transaction.
- Native ref storage alongside receipt writing **MUST** be a single transaction.
- If any write in a batch fails, the database state **MUST** remain unchanged.

SQLite transactions are atomic. An event write either completes fully or not at all. A receipt write is a separate transaction from the event write, which means:

- An event **MAY** exist in the database with zero receipts (delivery never attempted).
- A receipt **MUST NOT** exist without its corresponding event (receipts reference events by `event_id`).

## 8. Storage Method Semantics

### 8.1 append(event)

- Writes a `CanonicalEvent` as a single row into `canonical_events`, together with all inline relations as rows in `event_relations`, in a single atomic batch.
- Raises `DuplicateEventError` if `event_id` already exists.
- **MUST** be atomic. If the write fails, the database state **MUST** be unchanged.

### 8.2 get(event_id)

- Returns the `CanonicalEvent` for the given ID, or `None` if not found.
- The in-memory `relations` list **MUST** be reconstructed by querying `event_relations`.

### 8.3 query(filter)

- Yields events matching filter, ordered by `timestamp ASC`. No secondary sort.
- Events with identical timestamps **MAY** be yielded in any order.

### 8.4 store_native_ref(ref)

- Inserts a row into `native_message_refs`.
- Idempotent: duplicate `(adapter, native_channel_id, native_message_id)` tuples **MUST NOT** create new rows.
- NULL-channel handling: explicit resolve-before-insert is **REQUIRED**.
- `direction` records whether this ref was created on ingress (`inbound`) or delivery (`outbound`).

### 8.5 resolve_native_ref(adapter, native_channel_id, native_message_id)

- Queries `native_message_refs` for the given tuple.
- Returns the `event_id` if found, `None` otherwise.
- This is the primary lookup used by relation resolution.

### 8.6 store_relation(event_id, relation)

- Inserts a row into `event_relations`.
- Stores `target_native_ref` as four split nullable columns.
- Called separately from `append`.

### 8.7 list_relations(event_id)

- Returns `list[EventRelation]` from `event_relations`, reconstructing each from the stored row.

### 8.8 append_receipt(receipt)

- Inserts a new row into `delivery_receipts`.
- **MUST NOT** update an existing row. Every call creates a new row.
- `source` defaults to `"live"`. Retry deliveries set `"retry"`. Replay deliveries set `"replay"` and populate `replay_run_id`.

### 8.9 delivery_status(delivery_plan_id, target_adapter, target_channel)

- Returns the latest receipt for the given triple.
- `target_channel` is **REQUIRED** for precise lookup. When `None`, only NULL-channel receipts are considered.
- Returns `None` when no receipt exists.

### 8.10 list_receipts_for_plan(delivery_plan_id, target_adapter)

- Returns all receipts for a delivery plan / adapter in attempt order.

### 8.11 list_receipts_by_replay_run(run_id)

- Returns all receipts whose `replay_run_id` matches, ordered by `sequence` ascending.
- Returns empty list when no receipts match.

### 8.12 list_receipts_for_event(event_id)

- Returns all delivery receipts for a specific event, ordered by `sequence` ascending.

### 8.13 list_due_retry_receipts(now, limit, max_attempts)

- Returns failed receipts where `next_retry_at <= now`, `status = 'failed'`, and `failure_kind = 'adapter_transient'`.
- Excludes receipts where `attempt_number >= max_attempts` or `status = 'dead_lettered'`.
- Ordered by `next_retry_at ASC, sequence ASC`.

### 8.14 count_pending_retry(now, max_attempts)

- Returns count of transient-failure receipts due for retry.
- Same filter as `list_due_retry_receipts`.

### 8.15 update_retry_due(receipt_id, next_retry_at)

- Updates `next_retry_at` on an existing receipt row.
- This is the **only** mutation allowed on an existing receipt row.
- Capacity rejection does not advance `attempt_number` or count toward retry exhaustion.

### 8.16 Outbox Methods

- `create_outbox_item`: Creates or reclaims an outbox item (Section 9.3).
- `get_outbox_item`: Retrieves an item by `outbox_id`.
- `list_outbox_items`: Lists items, optionally filtered by status.
- `claim_due_outbox_items`: Claims eligible items for a worker.
- `mark_outbox_sent`: Terminal transition to `sent`.
- `mark_outbox_failed`: Transitions to `retry_wait` or `dead_lettered`.
- `mark_outbox_dead_lettered`: Terminal transition to `dead_lettered`.
- `release_outbox_claim`: Releases a claimed item back to `pending`.
- `count_outbox_by_status`: Returns counts grouped by status.

## 9. Delivery Outbox Semantics

### 9.1 Purpose

The outbox persists operational delivery work state. Outbox items are created after route/policy/loop/capacity acceptance and before the adapter delivery attempt, so pending work survives a crash between acceptance and receipt commit.

### 9.2 Status Transitions

**Terminal** (no further state changes; **MAY** be replaced on re-delivery):

- `sent`, `dead_lettered`, `cancelled`, `abandoned`

**Non-terminal** (may transition to other states):

- `pending`, `in_progress`, `queued`, `retry_wait`

### 9.3 Idempotent Create with Reclaim

Creating an item with the same key tuple `(delivery_plan_id, target_adapter, target_channel, attempt_number)` when a non-terminal row already exists does **not** return the existing row unchanged. The existing row is **reclaimed**: its `status`, `worker_id`, `locked_at`, `lease_until`, and `updated_at` are updated to match the new item's values. This ensures the caller always receives a properly-claimed operational row suitable for finalization.

When the existing row is terminal, it is deleted and a new row is inserted (re-delivery).

## 10. Pre-Release Database Policy

MEDRE has not yet made its first release. There is no automatic migration support. The `initialize()` method **MUST** perform two validation checks:

### 10.1 Schema Version Check

On a fresh database, the version row is inserted automatically into `_medre_schema_meta`. If the stored version mismatches `_EXPECTED_SCHEMA_VERSION`, `StorageInitializationError` **MUST** be raised with guidance to resolve the mismatch manually (export data, delete the database file, and restart; or downgrade to match the database version).

### 10.2 Column-Shape Validation

After DDL execution, `initialize()` **MUST** inspect `PRAGMA table_info` for each required table and compare column names against `_REQUIRED_COLUMNS`. If any required column is missing, `StorageInitializationError` **MUST** be raised with a message identifying the affected table and missing columns. This catches old pre-release databases whose `schema_version` still reads `1` but whose column shape predates the current DDL.

## 11. Durability Guarantees

### 11.1 Committed Transactions Survive Hard Crash

SQLite WAL mode ensures that committed transactions are durable even after `kill -9`, OOM, or power loss. SQLite's crash recovery mechanism handles incomplete WAL frames on the next open.

### 11.2 Events Stored Before Delivery

Every normalized event that enters the pipeline **MUST** be written to durable storage before delivery begins. If the runtime crashes after storing but before delivering, the event is preserved with no delivery receipt.

### 11.3 Receipts Written After Completion

A delivery receipt **MUST** be written after each delivery attempt completes (success or failure). If the runtime crashes during a delivery, no receipt is written for that attempt. An `in_progress` outbox row **MAY** survive the crash and can be reclaimed after lease expiry.

### 11.4 Single-Machine Persistence

MEDRE persists state to a local SQLite database and local filesystem. There is no replication, no remote backup, and no distributed coordination. Operators are **RESPONSIBLE** for database backup, log rotation, and monitoring disk space.

### 11.5 Shutdown Flush

During the Persist phase of shutdown, the runtime **MUST** flush pending SQLite WAL buffers. After this phase completes, all receipts and events produced before shutdown began are durable on disk. This flush does not happen on hard crash.

## 12. Process-Local State (Not Persisted)

The following runtime state is held in memory only and is never written to SQLite or disk:

| State                                | Nature                       |
| ------------------------------------ | ---------------------------- |
| In-flight deliveries                 | Semaphore-tracked coroutines |
| Active replay runs                   | Async generator iterations   |
| `CapacityController` internal gauges | In-memory counters           |
| `RouteStats` per-route counters      | In-memory counters           |
| `RuntimeAccounting` counters         | In-memory counters           |
| Adapter health / connection state    | In-memory                    |
| Pipeline runner state                | Ephemeral                    |

All of these reset to zero or initial state on every startup. No history is retained across restarts.

## 13. Replay/Recovery Interface

### 13.1 Replay Semantics

The canonical event log supports replaying events through the pipeline. Replay is an ephemeral runtime operation, not a durable job system.

| Property                   | Value                                                                            |
| -------------------------- | -------------------------------------------------------------------------------- |
| Replay request durability  | Not persisted. Replay runs are initiated in-memory and lost on crash.            |
| Replay queue               | Does not exist.                                                                  |
| Replay resume after crash  | Not supported. Must be re-initiated manually.                                    |
| Replay deduplication       | Not provided. Re-running replay **MAY** produce duplicate deliveries.            |
| Replay receipt persistence | Yes. Receipts produced by replay are persisted to SQLite like any other receipt. |

### 13.2 Replay Modes

| Mode          | target_stages   | Description                                               |
| ------------- | --------------- | --------------------------------------------------------- |
| `DRY_RUN`     | all             | Debug current config against past events; no side effects |
| `RE_RENDER`   | render, deliver | Re-run rendering for existing events                      |
| `RE_ROUTE`    | route, plan     | Re-evaluate routing rules against past events             |
| `BEST_EFFORT` | deliver         | Re-deliver events, producing new receipts                 |

### 13.3 Replay Receipt Traceability

Replay receipts carry `source='replay'` and a `replay_run_id` for run-level grouping. These fields support post-incident investigation and manual mitigation only; they do not prevent or detect duplicate sends at delivery time.

Native message refs created during replay are not tagged with `source` or `replay_run_id`. Replay-produced native refs **MAY** be correlated to their replay origin through the associated `DeliveryReceipt` (which carries `source` and `replay_run_id`), then via the receipt's `delivery_plan_id` / `event_id` linkage.

### 13.4 Replay Constraints

- Replay **MUST NOT** modify existing events. It creates new derived events and new receipts.
- Replay **MAY** target specific stages (e.g., re-run transforms only, skip policy).
- Traceability is not deduplication. Replaying an event that was previously delivered **WILL** produce a second delivery attempt.

### 13.5 Crash Recovery

On hard crash:

1. No graceful shutdown. No drain phase.
2. SQLite database is preserved. WAL mode provides crash consistency.
3. In-flight deliveries with outbox items are re-claimable after lease expiry.
4. All process-local state is lost.
5. Restart with the same config. Adapters reconnect autonomously.

To identify events that were stored but never delivered:

```sql
SELECT e.event_id, e.source_adapter, e.created_at
FROM canonical_events e
LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
WHERE r.event_id IS NULL
ORDER BY e.created_at DESC;
```

Events returned by this query are not necessarily unrecoverable. Check the `delivery_outbox` table for matching rows:

```sql
SELECT outbox_id, event_id, delivery_plan_id, target_adapter,
       target_channel, status, lease_until, attempt_number
FROM delivery_outbox
WHERE event_id = ?;
```

An `in_progress` row with an expired lease is re-claimable by the RetryWorker on restart. A `queued` row is ambiguous; stale rows past `STALE_QUEUED_GRACE_SECONDS` (default 300 s) are automatically reclaimed. A `pending` or `retry_wait` row is eligible for automatic retry. Rows with no match indicate the event was stored before outbox creation and cannot be automatically retried.

### 13.6 Database Integrity Verification

Operators **MAY** verify database integrity with:

```bash
sqlite3 {state}/medre.sqlite "PRAGMA integrity_check;"
```

If corrupted, recovery:

```bash
sqlite3 {state}/medre.sqlite ".recover" | sqlite3 {state}/medre-recovered.sqlite
mv {state}/medre.sqlite {state}/medre.sqlite.corrupted
mv {state}/medre-recovered.sqlite {state}/medre.sqlite
```

## 14. Storage Configuration

```python
@dataclass(frozen=True)
class StorageConfig:
    backend: str = "sqlite"           # Backend identifier
    path: str | None = None           # None → {state}/medre.sqlite
```

`backend` identifies the storage backend to use. Only `"sqlite"` is currently supported. `path` overrides the database file location; when `None`, the path is derived from `MedrePaths.database_path`.

## 15. Required Guarantees Summary

| Guarantee              | Requirement                                                                                                                    |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Atomic writes          | Events, native refs, and receipts **MUST** be written atomically. Partial writes **MUST NOT** leave the database inconsistent. |
| Idempotent correlation | Duplicate `(adapter, native_channel_id, native_message_id)` tuples **MUST NOT** create duplicate rows.                         |
| Ordered append         | `canonical_events` ordered by `timestamp ASC`. `delivery_receipts` ordered by `sequence` (monotonic).                          |
| Receipt immutability   | Receipt rows are append-only. No `UPDATE` or `DELETE` on `delivery_receipts` (except `next_retry_at` via `update_retry_due`).  |
| Concurrent reads       | WAL mode **MUST** be enabled for concurrent reads during writes.                                                               |
| Replay support         | Event log **MUST** support querying by time range, event kind, source adapter, and other filter criteria.                      |
| Schema validation      | `initialize()` **MUST** validate schema version and column shape.                                                              |
| Single database        | There **MUST NOT** be per-adapter databases.                                                                                   |

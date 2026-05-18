# Replay and Event Log Contract

> Extracted from: Modular Event Communications Runtime Specification (Sections 5, 12, 18, 19)
> Version: 0.2.0
> Last updated: 2026-05-08

This document defines the canonical event log semantics, event record taxonomy, replay interface, replay constraints, raw native archive mode, and future backend considerations. An implementer should be able to build the replay engine (`core/storage/replay.py`) and archive module (`core/storage/archive.py`) from this contract alone.

## 1. Canonical Event Log Semantics

The canonical event log is the single source of truth for all event history. Three invariants hold:

**Append-only.** Events are written once. No code path deletes or overwrites a row in `canonical_events`. New events derived from existing ones are inserted as separate rows with `parent_event_id` linking back.

**Immutable.** Once written, no field of a `CanonicalEvent` changes. Enrichment, transformation, and policy evaluation all create new derived events. The original event is always recoverable by following the `lineage` chain backward. Event IDs are UUIDv7 for natural time ordering and uniqueness.

**Ordered.** Events carry nanosecond-precision UTC timestamps. UUIDv7 IDs also encode a timestamp component, giving a secondary ordering mechanism. The `timestamp` column is indexed for range queries.

Storage is authoritative over any metadata embedded in external platforms (Matrix custom content fields, Discord embeds, etc.). That embedded data may be lost due to redaction, pruning, or platform API changes. Any feature that needs reliable metadata (replay, correlation, identity resolution) must read from storage.

The Phase 1 backend is SQLite with WAL mode for concurrent reads. The storage interface is abstracted so backends can be swapped without changing replay logic.

## 2. Event Record Taxonomy

Not every record in the pipeline has the same semantic weight. The runtime distinguishes four classes:

| Record Class          | Purpose                                                                                                                                                  | Storage Location                                                                                                                                                                                                      |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Source Event**      | Initial canonical event produced by an adapter codec from raw native data. Primary record of what happened on a transport.                               | Always stored in `canonical_events`.                                                                                                                                                                                  |
| **Derived Event**     | Produced by enrichment, transform, or policy stages. References parent via `parent_event_id`, carries a full `lineage` tuple.                            | Stored in `canonical_events` if semantically meaningful (e.g., a telemetry-to-message transform that downstream systems act on). Transient intermediates may be stored or discarded based on configuration.           |
| **Delivery Artifact** | Target-specific rendering of an event for a particular adapter (Matrix HTML with embedded metadata, MeshCore 160-byte truncated text, LXMF fields dict). | Stored as a rendered payload record attached to the delivery plan. Not a canonical event. Adapter-specific, not semantically independent.                                                                             |
| **Receipt Event**     | Records the outcome of a delivery attempt.                                                                                                               | Phase 1: rows in `delivery_receipts` table (not canonical events). Future: may optionally mirror as canonical `delivery.receipt` events for audit. Receipts are semantically meaningful but live in a separate table. |

**Storage guidance for replay:** The canonical event log holds source events and semantically meaningful derived events. Delivery receipts live in `delivery_receipts`. Target-specific renderings live as payload records on delivery plans. If a rendering is semantically meaningful (e.g., an edited message produces a new canonical event with edit semantics), it is a derived event, not a rendering artifact.

**Receipt append-only rule:** Receipts are append-only. Every delivery attempt produces a new `DeliveryReceipt` row. Existing rows are never updated or deleted. A delivery that retried three times produces four receipt rows. The "current status" of a delivery is a projection: the latest receipt for a given `(event_id, delivery_plan_id, target_adapter)` tuple, provided by the `delivery_status` view.

## 3. ReplayMode Enum and Stage Guarantees

The replay engine uses a `ReplayMode` enum to control which pipeline stages execute and whether side effects are permitted. Each mode has explicit, testable guarantees.

```python
class ReplayMode(Enum):
    STRICT = "strict"
    RE_RENDER = "re_render"
    RE_ROUTE = "re_route"
    BEST_EFFORT = "best_effort"
    DRY_RUN = "dry_run"
```

### 3.1 Mode Stage Matrix

| Mode            | Store  | Route | Plan | Render  | Deliver     | Side Effects     |
| --------------- | ------ | ----- | ---- | ------- | ----------- | ---------------- |
| **STRICT**      | verify | --    | --   | --      | --          | None (read-only) |
| **RE_RENDER**   | verify | --    | --   | capture | --          | None (read-only) |
| **RE_ROUTE**    | verify | route | plan | --      | --          | None (read-only) |
| **BEST_EFFORT** | verify | route | plan | render  | **deliver** | Adapter delivery |
| **DRY_RUN**     | verify | route | plan | capture | skip        | None (read-only) |

### 3.2 Per-Mode Guarantees

**STRICT**

- Verifies event existence and integrity (event_id non-empty, event_kind registered).
- No pipeline stages invoked. No side effects.
- Re-raises unexpected exceptions.
- Use for: integrity checks, migration validation, audit verification.

**RE_RENDER**

- Re-runs transforms and rendering pipeline. Captures rendering output in `ReplayResult.output`.
- Does **not** invoke routing, planning, or delivery.
- No side effects. No storage mutations.
- Re-raises unexpected exceptions.
- Use for: testing new renderers, metadata evolution, rendering preview.

**RE_ROUTE**

- Re-runs routing and planning with current route configuration.
- Does **not** invoke rendering or delivery.
- No side effects. No storage mutations.
- Re-raises unexpected exceptions.
- Use for: testing route changes, planning changes, route coverage analysis.

**BEST_EFFORT**

- Full re-processing including adapter delivery. Only mode with side effects.
- Individual event failures are captured as `"error"` results without crashing the replay.
- Failures are recorded via the `Diagnostician` (adapter_failures, planner_failures, renderer_failures).
- Results are yielded in storage query order for deterministic iteration.
- Use for: migration, adapter testing with real data, retry of failed deliveries.

**DRY_RUN**

- Executes all pipeline stages through rendering but **skips delivery**.
- Delivery stage result is always `"skipped"` with reason `"dry_run: delivery suppressed"`.
- No side effects. No storage mutations.
- Re-raises unexpected exceptions.
- Use for: previewing BEST_EFFORT replay, debugging, dry-run validation.

### 3.3 Cross-Mode Invariants

1. **Immutability:** Replay never mutates historical `CanonicalEvent` instances. The `frozen=True` struct prevents in-place mutation. All modes pass events read-only through pipeline stages.
2. **No storage writes (non-BEST_EFFORT):** STRICT, RE_RENDER, RE_ROUTE, and DRY_RUN modes produce no storage side effects. Event count before and after replay is identical.
3. **Deterministic ordering:** For a given stored dataset and pipeline configuration, the sequence of `(event_id, stage, status)` tuples is deterministic. Events are processed in storage query order (timestamp ascending) or correlation_id list order.
4. **Lineage preservation:** Every `ReplayResult` carries the `lineage` tuple from the source event, enabling derivation ancestry tracking across replay.

## 4. ReplayRequest Interface

```python
@dataclass
class ReplayRequest:
    """Filter and targeting specification for a replay operation."""

    time_start: datetime | None = None
    time_end: datetime | None = None
    event_kinds: list[str] | None = None
    source_adapters: list[str] | None = None
    target_stages: list[str] | None = None
    correlation_ids: list[str] | None = None
    mode: ReplayMode = ReplayMode.STRICT
    limit: int = 1000
    target_adapters: list[str] | None = None
```

**Fields explained:**

| Field             | Type                | Description                                                                                                                                                                                                |
| ----------------- | ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `time_start`      | `datetime \| None`  | Earliest event timestamp to include (inclusive). `None` = no lower bound.                                                                                                                                  |
| `time_end`        | `datetime \| None`  | Latest event timestamp to include (inclusive). `None` = no upper bound.                                                                                                                                    |
| `event_kinds`     | `list[str] \| None` | Restrict to these event kind strings. `None` = all kinds.                                                                                                                                                  |
| `source_adapters` | `list[str] \| None` | Restrict to events from these adapters. `None` = all adapters.                                                                                                                                             |
| `target_stages`   | `list[str] \| None` | Pipeline stages to replay (subset of mode-allowed stages). `None` = all stages for the mode. Valid values: `"store"`, `"route"`, `"plan"`, `"render"`, `"deliver"`.                                        |
| `correlation_ids` | `list[str] \| None` | Restrict to events whose `event_id` appears in this list. When set, events are fetched by individual ID; remaining filters applied as post-filters.                                                        |
| `mode`            | `ReplayMode`        | The replay behavioural mode. Default: `STRICT`.                                                                                                                                                            |
| `limit`           | `int`               | Maximum number of events to replay. Default: 1000.                                                                                                                                                         |
| `target_adapters` | `list[str] \| None` | Restrict delivery to these adapter names. Only meaningful for BEST_EFFORT and DRY_RUN modes. Plans targeting adapters not in this list have their deliver stage set to `"skipped"`. `None` = all adapters. |

**EventFilter conversion:** `time_start`, `time_end`, `event_kinds`, `source_adapters`, and `limit` are converted to an `EventFilter` for storage queries. `correlation_ids`, `target_stages`, and `target_adapters` are handled directly by the replay engine.

### 4.1 target_adapters Filtering

When `target_adapters` is set, delivery plans are filtered before the deliver stage. Plans whose `target.adapter` attribute matches an entry in `target_adapters` are included; others are excluded. If no plans remain after filtering, the deliver stage result is `"skipped"` with reason `"No delivery plans matched target_adapters filter"`.

Plans with opaque structure (no `target.adapter` attribute) are included conservatively.

## 5. Replay Constraints

These constraints protect the system from unintended side effects during replay:

1. **Replay does not modify existing events.** Historical `CanonicalEvent` instances are frozen structs. The replay engine never writes to storage except via adapter delivery in BEST_EFFORT mode.

2. **Live adapters are not targeted by default.** When `target_adapters` is `None`, delivery targets are resolved by the current routing configuration. An operator must explicitly choose which adapters to replay into.

3. **No side effects in transforms during replay.** Transforms that write to external systems must check for a replay context and skip side effects. Transforms may produce different output events, but must not trigger external actions when running in replay mode.

4. **Replay can target specific stages.** The `target_stages` field allows re-running only specific pipeline stages. The result is the intersection of requested stages and mode-allowed stages, preserving the mode's ordering.

5. **Diagnostician wiring.** When a `Diagnostician` is provided to the `ReplayEngine`, notable replay conditions are recorded:
   - `record_replay_skip`: Missing events, no routes matched, target_adapters filter excluded all plans.
   - `record_replay_downgrade`: Unregistered event_kind detected during store verification.
   - `record_renderer_failure`: Rendering pipeline raised an exception.
   - `record_adapter_failure`: BEST_EFFORT delivery failed or unexpected exception caught.
   - `record_planner_failure`: Routing or planning raised an exception.

## 6. Retry Semantics (Dead-Letter / Failed Delivery Replay)

Phase 1 does not implement a separate `RETRY` replay mode. Retry semantics are achieved through **BEST_EFFORT replay scoped to events with failed delivery receipts**. This is a selection pattern, not a distinct mode:

1. Query `delivery_receipts` for events with `status = "failed"` or `status = "dead_lettered"`.
2. Collect the `event_id` values from those receipts.
3. Use those IDs as `correlation_ids` in a `ReplayRequest(mode=BEST_EFFORT)`.
4. Optionally set `target_adapters` to limit retry to specific adapters.

This approach is honest about what Phase 1 provides: replay with delivery, scoped to specific events. A future `RETRY` mode could add receipt-awareness (skipping already-succeeded deliveries) and dead-letter integration, but the current model is sufficient for operational retry use cases.

**Phase 1 Replay Caveats:**

- **No automatic retry scheduling.** The pipeline records `next_retry_at` on failed receipts but does not automatically re-attempt delivery. Manual replay via BEST_EFFORT mode is required.
- **No receipt deduplication.** Replaying events that already have successful delivery receipts will produce duplicate receipts.
- **No preserved historical renderer or adapter versions.** Replay uses the current pipeline configuration, current renderers, and current adapter capabilities. If a renderer or adapter has changed since the original delivery, replay output will differ.
- **No replay rate limiting.** Phase 1 does not rate-limit replay operations per adapter.
- **No replay progress tracking or resumption.** Replay runs to completion or failure without intermediate checkpoints.

**Why not a RETRY mode?** A true retry mode requires:

- Receipt deduplication (skip deliveries that already succeeded).
- Dead-letter queue integration (select events from dead-letter state).
- Retry budget/rate limiting per adapter.
- These belong in Track 3 (delivery failure executor), not Track 1 (replay determinism).

## 7. Replay Use Cases

| Use Case                     | Mode          | target_stages         | Notes                                                                            |
| ---------------------------- | ------------- | --------------------- | -------------------------------------------------------------------------------- |
| **Integrity check**          | `STRICT`      | default               | Verify events exist and event_kinds are registered. No pipeline stages.          |
| **Renderer testing**         | `RE_RENDER`   | default               | Test new renderers against historical events. Captures output.                   |
| **Route change preview**     | `RE_ROUTE`    | default               | Evaluate current routing against historical events. Plans captured, no delivery. |
| **Migration delivery**       | `BEST_EFFORT` | default               | Full re-processing with adapter delivery. Use for adapter migration.             |
| **Dry-run preview**          | `DRY_RUN`     | default               | Preview what BEST_EFFORT would do without side effects.                          |
| **Failed delivery retry**    | `BEST_EFFORT` | default               | Scope to failed events via `correlation_ids` from `delivery_receipts`.           |
| **Targeted adapter replay**  | `BEST_EFFORT` | default               | Set `target_adapters` to limit delivery to specific adapters.                    |
| **Stage-specific debugging** | any           | `["store", "render"]` | Re-run only specific stages within the mode's allowed set.                       |

**Correlation ID replay:** When `correlation_ids` is set, only events matching those IDs are replayed. This is useful for debugging a specific event chain or reprocessing a failed delivery.

## 8. Raw Native Archive Mode

> **Phase 1 Note:** Raw archiving is not implemented in Phase 1. The `native_archive` table schema is defined for future use but is not created or populated. The `archive_raw` method is not part of the Phase 1 `StorageBackend` protocol.

Raw archiving stores the original native packets received from transports, separate from the canonical event. This is for debugging, compliance, and advanced analysis.

### 8.1 Behavior

- **Opt-in** per adapter via configuration. Not enabled by default.
- Raw data is **compressed** (gzip by default, zstd if available) and stored in the `native_archive` table, linked to the canonical event by `event_id`.
- Raw data is **never embedded** into Matrix or other presentation events. Storage only.
- Archive retention is configurable: time-based pruning (`max_age_days`), count-based pruning (`max_count`), or both.
- Archived raw data access is a future capability (management interface and CLI).

### 8.2 Storage Schema

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

### 8.3 Configuration Schema

```yaml
storage:
  native_archive:
    enabled: true
    compression: gzip # "gzip" or "zstd"
    retention:
      max_age_days: 30
      max_count: 100000
    adapters:
      meshcore-radio-1: true
      mqtt-bridge: true
      matrix-home: false # Don't archive Matrix raw data
```

### 8.4 StorageBackend Archive Method (Future)

> **Not implemented in Phase 1.** The following interface is defined for future implementation:

```python
async def archive_raw(self, event_id: str, adapter: str, data: bytes) -> None:
    """Compress and store raw native data linked to a canonical event."""
    ...
```

The archive would be written at ingress time, before the event enters the pipeline. Only adapters with `native_archive.adapters.<name>: true` would produce archive entries.

## 9. Storage Backend Protocol

The replay engine depends on this protocol. Phase 1 implements it with SQLite. Future backends implement the same interface.

```python
class StorageBackend(Protocol):
    """Protocol defining the interface all storage backends must implement.

    Contractual guarantees: Events are append-only.  Delivery receipts are
    append-only.  Native refs are idempotent.  Relations queryable by
    event_id.  Query results ordered by timestamp ascending.
    """

    async def append(self, event: CanonicalEvent) -> None: ...
    async def query(self, filter: EventFilter) -> AsyncIterator[CanonicalEvent]: ...
    async def get(self, event_id: str) -> CanonicalEvent | None: ...
    async def append_receipt(self, receipt: DeliveryReceipt) -> None: ...
    async def store_native_ref(self, ref: NativeMessageRef) -> None: ...
    async def resolve_native_ref(self, adapter: str, native_channel_id: str | None, native_message_id: str) -> str | None: ...
    async def store_relation(self, event_id: str, relation: EventRelation) -> None: ...
    async def list_relations(self, event_id: str) -> list[EventRelation]: ...
    async def delivery_status(self, delivery_plan_id: str, target_adapter: str) -> DeliveryReceipt | None: ...
    async def list_receipts_for_plan(self, delivery_plan_id: str, target_adapter: str) -> list[DeliveryReceipt]: ...
    async def initialize(self) -> None: ...
    async def close(self) -> None: ...
```

> **Not in Phase 1 protocol:** `archive_raw` and `resolve_native_relation` appear in the master spec but are not part of the current `StorageBackend` protocol. Raw archiving is a future capability. Native relation resolution is handled through `resolve_native_ref` with the `native_relation_id` column index.

The `query` method is the primary entry point for replay. It returns an `AsyncIterator[CanonicalEvent]` matching the filter, ordered by timestamp. The replay engine consumes this iterator and feeds events into the requested pipeline stages.

## 10. Future Backend Considerations

The storage abstraction is designed so the replay interface stays the same regardless of backend.

| Backend            | Use Case                                                                                                               | Replay Mechanism                                                                                                     |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| **PostgreSQL**     | High-volume deployments with concurrent writers. Better concurrency for multi-writer scenarios.                        | SQL query with time-range and kind predicates. Same `StorageBackend` interface.                                      |
| **NATS JetStream** | Real-time event streaming with built-in replay. Subscribe to a subject, replay from a specific sequence number.        | NATS consumer starting at the sequence corresponding to `time_start`.                                                |
| **Redis Streams**  | Low-latency short-window replay. Events expire from Redis after a configurable retention period, so replay is bounded. | `XREAD` from a consumer group starting at a specific stream ID. Suitable for recent-event replay (minutes to hours). |
| **Apache Kafka**   | Large-scale distributed deployments. Partitions provide parallelism. Consumer groups track offsets.                    | Consume from topic partitions starting at an offset derived from `time_start`.                                       |

**Backend swap considerations:**

- The `StorageBackend` protocol hides backend-specific query details behind `query(filter)`.
- The `append` method is idempotent in the sense that writing the same `event_id` twice must not duplicate data (upsert semantics or primary key enforcement).
- Receipt append semantics (`delivery_receipts` table) must be preserved across backends: every attempt produces a new row, current status is a projection.
- The `native_archive` table schema is backend-agnostic. A streaming backend may store raw blobs in object storage (S3, local filesystem) rather than a database column, but the `archive_raw` interface stays the same.

## 11. Key Storage Schema for Replay

Relevant tables and indexes from the Phase 1 SQLite schema:

```sql
-- Canonical event log (source and derived events)
CREATE TABLE canonical_events (
    event_id TEXT PRIMARY KEY,
    event_kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    timestamp TEXT NOT NULL,         -- ISO 8601 with nanoseconds
    source_adapter TEXT NOT NULL,
    source_transport_id TEXT NOT NULL,
    source_channel_id TEXT,
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

-- Delivery receipts (append-only, never updated)
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

-- Current delivery status projection
CREATE VIEW delivery_status AS
SELECT dr.* FROM delivery_receipts dr
JOIN (
    SELECT delivery_plan_id, target_adapter, MAX(sequence) AS max_seq
    FROM delivery_receipts GROUP BY delivery_plan_id, target_adapter
) latest ON dr.sequence = latest.max_seq;

-- Event relations (first-class, not in metadata)
CREATE TABLE event_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    relation_type TEXT NOT NULL CHECK(relation_type IN ('reply', 'reaction', 'edit', 'delete', 'thread')),
    target_event_id TEXT,
    target_native_adapter TEXT,
    target_native_channel_id TEXT,
    target_native_message_id TEXT,
    key TEXT,
    fallback_text TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Native message references (cross-adapter correlation)
CREATE TABLE native_message_refs (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    adapter TEXT NOT NULL,
    native_channel_id TEXT,
    native_message_id TEXT NOT NULL,
    native_thread_id TEXT,
    native_relation_id TEXT,
    direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(adapter, native_channel_id, native_message_id)
);
```

Full schema including identity tables and plugin state is in the master spec, Section 12.3.

## 12. References

- Master spec: `docs/spec/modular-event-engine-spec.md`
  - Section 5: Canonical Event Model (event record taxonomy, immutability rules)
  - Section 12: Storage and Canonical Event Log (schema, backends, protocol)
  - Section 18: Raw Native Archive Mode
  - Section 19: Replay and Reprocessing
- Related contracts:
  - `docs/contracts/01-canonical-event-contract.md` (CanonicalEvent dataclass, event kinds)
  - `docs/contracts/03-storage-contract.md` (full storage schema, SQLite implementation)
  - `docs/contracts/phase-1-limitations.md` (Phase 1 constraints and taxonomy)

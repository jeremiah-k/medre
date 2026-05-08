# Replay and Event Log Contract

> Extracted from: Modular Event Communications Runtime Specification (Sections 5, 12, 18, 19)
> Version: 0.1.0
> Last updated: 2026-05-07

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

| Record Class | Purpose | Storage Location |
|---|---|---|
| **Source Event** | Initial canonical event produced by an adapter codec from raw native data. Primary record of what happened on a transport. | Always stored in `canonical_events`. |
| **Derived Event** | Produced by enrichment, transform, or policy stages. References parent via `parent_event_id`, carries a full `lineage` list. | Stored in `canonical_events` if semantically meaningful (e.g., a telemetry-to-message transform that downstream systems act on). Transient intermediates may be stored or discarded based on configuration. |
| **Delivery Artifact** | Target-specific rendering of an event for a particular adapter (Matrix HTML with embedded metadata, MeshCore 160-byte truncated text, LXMF fields dict). | Stored as a rendered payload record attached to the delivery plan. Not a canonical event. Adapter-specific, not semantically independent. |
| **Receipt Event** | Records the outcome of a delivery attempt. | Phase 1: rows in `delivery_receipts` table (not canonical events). Future: may optionally mirror as canonical `delivery.receipt` events for audit. Receipts are semantically meaningful but live in a separate table. |

**Storage guidance for replay:** The canonical event log holds source events and semantically meaningful derived events. Delivery receipts live in `delivery_receipts`. Target-specific renderings live as payload records on delivery plans. If a rendering is semantically meaningful (e.g., an edited message produces a new canonical event with edit semantics), it is a derived event, not a rendering artifact.

**Receipt append-only rule:** Receipts are append-only. Every delivery attempt produces a new `DeliveryReceipt` row. Existing rows are never updated or deleted. A delivery that retried three times produces four receipt rows. The "current status" of a delivery is a projection: the latest receipt for a given `(event_id, delivery_plan_id, target_adapter)` tuple, provided by the `delivery_status` view.


## 3. ReplayRequest Interface

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

@dataclass
class ReplayRequest:
    """Request to replay events from the canonical event log through the pipeline."""

    source: Literal["storage", "file", "stream"]
    filter: EventFilter                # Time range, event kinds, source adapter, etc.
    target_stages: list[str]           # Which pipeline stages to replay through
    target_adapters: list[str] | None  # None = replay to all current adapters
    dry_run: bool                      # True = log results, don't deliver
    replay_mode: Literal["reprocess", "replay_only"]
    # "reprocess": create new derived events and new receipts
    # "replay_only": deliver existing derived events to new targets
```

**Fields explained:**

| Field | Description |
|---|---|
| `source` | Where to read events from. `storage` queries the canonical event log. `file` reads from a dump. `stream` reads from a streaming backend (future). |
| `filter` | An `EventFilter` constraining which events to replay. Supports time range (`time_range`), event kinds (`event_kinds`), source adapters (`source_adapters`), and correlation IDs. |
| `target_stages` | Pipeline stages to replay through. Examples: `["transform"]` to re-run transforms only, `["routing", "delivery"]` to re-evaluate routing with current rules. |
| `target_adapters` | Specific adapters to deliver replayed events to. `None` means all currently registered adapters. |
| `dry_run` | When `True`, the replay engine logs what would happen but does not execute deliveries or write new receipts. Used for debugging and preview. |
| `replay_mode` | `reprocess` runs events through transforms and policy again, creating new derived events and receipts. `replay_only` takes existing derived events and delivers them to new targets without re-running transforms. |

**EventFilter fields relevant to replay:**

```python
@dataclass
class EventFilter:
    time_range: tuple[datetime, datetime] | None
    event_kinds: list[str] | None
    source_adapters: list[str] | None
    correlation_ids: list[str] | None
    # Additional filter criteria as needed
```


## 4. Replay Constraints

These constraints protect the system from unintended side effects during replay:

1. **Replay does not modify existing events.** It creates new derived events and new receipt rows. The original canonical event log entries remain untouched.

2. **Live adapters are not targeted by default.** Replay targets adapters explicitly listed in `target_adapters`, or all registered adapters if `None`. An operator must explicitly choose to replay into live adapters. This prevents accidental duplicate delivery to production channels.

3. **Receipts are deduplicated during replay.** If a replay would produce a receipt for an `(event_id, delivery_plan_id, target_adapter)` combination that already has a successful delivery receipt, the replay engine skips that delivery. This prevents duplicate messages to the same target for the same event.

4. **No side effects in transforms during replay.** Transforms that write to external systems (e.g., a transform that calls an HTTP API) must check for a replay context and skip side effects. Transforms may produce different output events, but must not trigger external actions when running in replay mode.

5. **Replay is rate-limited.** The replay engine respects adapter rate limits to avoid overwhelming targets with historical traffic.

6. **Replay can target specific stages.** Operators can re-run only transforms, only routing, or only delivery, without reprocessing the entire pipeline for each event.

7. **Replay progress is tracked and resumable.** The replay engine records its position so it can resume after interruption without reprocessing already-completed events.


## 5. Replay Use Cases

The replay engine supports these scenarios, each with distinct replay mode and stage configuration:

| Use Case | replay_mode | target_stages | Notes |
|---|---|---|---|
| **Plugin changes** | `reprocess` | `["transform", "policy", "routing", "delivery"]` | A new plugin wants to process historical events. All pipeline stages run so the plugin sees events as if they arrived live. |
| **Route changes** | `replay_only` | `["routing", "delivery"]` | Routing rules changed. Existing derived events are re-evaluated against new routes without re-running transforms. |
| **Adapter development** | `replay_only` or `reprocess` | `["delivery"]` | A new adapter was added. Historical events are delivered to it. Use `replay_only` if existing derived events are sufficient, `reprocess` if the new adapter needs transform outputs that weren't generated before. |
| **Debugging** | `reprocess` | configurable | `dry_run=True` lets operators inspect how events would be processed with current configuration without delivering anything. Any stage combination is valid. |

**Correlation ID replay:** When `filter.correlation_ids` is set, only events matching those IDs are replayed. This is useful for debugging a specific event chain or reprocessing a failed delivery.


## 6. Raw Native Archive Mode

Raw archiving stores the original native packets received from transports, separate from the canonical event. This is for debugging, compliance, and advanced analysis.

### 6.1 Behavior

- **Opt-in** per adapter via configuration. Not enabled by default.
- Raw data is **compressed** (gzip by default, zstd if available) and stored in the `native_archive` table, linked to the canonical event by `event_id`.
- Raw data is **never embedded** into Matrix or other presentation events. Storage only.
- Archive retention is configurable: time-based pruning (`max_age_days`), count-based pruning (`max_count`), or both.
- Archived raw data is accessible via the API and CLI for debugging.

### 6.2 Storage Schema

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

### 6.3 Configuration Schema

```yaml
storage:
  native_archive:
    enabled: true
    compression: gzip          # "gzip" or "zstd"
    retention:
      max_age_days: 30
      max_count: 100000
    adapters:
      meshcore-radio-1: true
      mqtt-bridge: true
      matrix-home: false       # Don't archive Matrix raw data
```

### 6.4 StorageBackend Archive Method

```python
async def archive_raw(self, event_id: str, adapter: str, data: bytes) -> None:
    """Compress and store raw native data linked to a canonical event."""
    ...
```

The archive is written at ingress time, before the event enters the pipeline. Only adapters with `native_archive.adapters.<name>: true` produce archive entries.


## 7. Storage Backend Protocol

The replay engine depends on this protocol. Phase 1 implements it with SQLite. Future backends implement the same interface.

```python
class StorageBackend(Protocol):
    async def append(self, event: CanonicalEvent) -> None: ...
    async def query(self, filter: EventFilter) -> AsyncIterator[CanonicalEvent]: ...
    async def get(self, event_id: str) -> CanonicalEvent | None: ...
    async def append_receipt(self, receipt: DeliveryReceipt) -> None: ...
    async def archive_raw(self, event_id: str, adapter: str, data: bytes) -> None: ...
    async def store_native_ref(self, ref: NativeMessageRef) -> None: ...
    async def resolve_native_ref(self, adapter: str, native_channel_id: str, native_message_id: str) -> str | None: ...
    async def resolve_native_relation(self, adapter: str, native_relation_id: str) -> str | None: ...
    async def store_relation(self, event_id: str, relation: EventRelation) -> None: ...
    async def list_relations(self, event_id: str) -> list[EventRelation]: ...
```

The `query` method is the primary entry point for replay. It returns an `AsyncIterator[CanonicalEvent]` matching the filter, ordered by timestamp. The replay engine consumes this iterator and feeds events into the requested pipeline stages.


## 8. Future Backend Considerations

The storage abstraction is designed so the replay interface stays the same regardless of backend. The `ReplayRequest.source` field already anticipates non-SQLITE backends.

| Backend | Use Case | Replay Mechanism |
|---|---|---|
| **PostgreSQL** | High-volume deployments with concurrent writers. Better concurrency for multi-writer scenarios. | SQL query with time-range and kind predicates. Same `StorageBackend` interface. |
| **NATS JetStream** | Real-time event streaming with built-in replay. Subscribe to a subject, replay from a specific sequence number. | `ReplayRequest.source="stream"`. The NATS consumer starts at the sequence corresponding to `filter.time_range[0]`. |
| **Redis Streams** | Low-latency short-window replay. Events expire from Redis after a configurable retention period, so replay is bounded. | `XREAD` from a consumer group starting at a specific stream ID. Suitable for recent-event replay (minutes to hours). |
| **Apache Kafka** | Large-scale distributed deployments. Partitions provide parallelism. Consumer groups track offsets. | `ReplayRequest.source="stream"`. Consume from topic partitions starting at an offset derived from `filter.time_range`. |

**Backend swap considerations:**

- The `StorageBackend` protocol hides backend-specific query details behind `query(filter)`.
- The `append` method is idempotent in the sense that writing the same `event_id` twice must not duplicate data (upsert semantics or primary key enforcement).
- Receipt append semantics (`delivery_receipts` table) must be preserved across backends: every attempt produces a new row, current status is a projection.
- The `native_archive` table schema is backend-agnostic. A streaming backend may store raw blobs in object storage (S3, local filesystem) rather than a database column, but the `archive_raw` interface stays the same.


## 9. Key Storage Schema for Replay

Relevant tables and indexes from the Phase 1 SQLite schema:

```sql
-- Canonical event log (source and derived events)
CREATE TABLE canonical_events (
    event_id TEXT PRIMARY KEY,
    event_kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    timestamp TEXT NOT NULL,         -- ISO 8601 with nanoseconds
    source_adapter TEXT NOT NULL,
    source_transport_id TEXT,
    source_channel_id TEXT,
    parent_event_id TEXT,
    lineage TEXT,                    -- JSON array of event IDs
    payload TEXT NOT NULL,           -- JSON
    metadata TEXT NOT NULL,          -- JSON
    tags TEXT,                       -- JSON array
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
    status TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    adapter_message_id TEXT,
    error TEXT,
    retry_count INTEGER DEFAULT 0,
    next_retry_at TEXT
);

-- Current delivery status projection
CREATE VIEW delivery_status AS
SELECT
    dr.event_id,
    dr.delivery_plan_id,
    dr.target_adapter,
    dr.status AS current_status,
    dr.timestamp AS last_updated,
    dr.retry_count,
    dr.next_retry_at,
    dr.adapter_message_id,
    dr.error
FROM delivery_receipts dr
INNER JOIN (
    SELECT delivery_plan_id, target_adapter, MAX(sequence) AS max_seq
    FROM delivery_receipts
    GROUP BY delivery_plan_id, target_adapter
) latest ON dr.sequence = latest.max_seq;

-- Event relations (first-class, not in metadata)
CREATE TABLE event_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    relation_type TEXT NOT NULL CHECK(relation_type IN ('reply', 'reaction', 'edit', 'delete', 'thread')),
    target_event_id TEXT,
    target_native_ref TEXT,              -- JSON: NativeRef dict
    key TEXT,
    fallback_text TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Native message references (cross-adapter correlation)
CREATE TABLE native_message_refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    adapter TEXT NOT NULL,
    native_channel_id TEXT NOT NULL,
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


## 10. References

- Master spec: `docs/spec/modular-event-engine-spec.md`
  - Section 5: Canonical Event Model (event record taxonomy, immutability rules)
  - Section 12: Storage and Canonical Event Log (schema, backends, protocol)
  - Section 18: Raw Native Archive Mode
  - Section 19: Replay and Reprocessing
- Related contracts:
  - `docs/contracts/01-canonical-event-contract.md` (CanonicalEvent dataclass, event kinds)
  - `docs/contracts/03-storage-contract.md` (full storage schema, SQLite implementation)

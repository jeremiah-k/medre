# Canonical Event Contract

> Extracted from: Modular Event Communications Runtime Specification v0.1.0, Sections 3, 5, 6, 12, 13, 14
> Contract version: 1 (matches `CURRENT_SCHEMA_VERSION`)
> Last updated: 2026-05-08
> This document is a standalone implementation reference. Read this, build the event model, done.

---

## 1. CanonicalEvent

The core immutable record. Everything in the pipeline is a `CanonicalEvent`.

```python
import msgspec
from datetime import datetime
from typing import Literal


class CanonicalEvent(msgspec.Struct, frozen=True):
    event_id: str               # UUIDv7 for time-ordering
    event_kind: str             # "message.text", "telemetry.received", etc.
    schema_version: int         # Schema version this event conforms to (>= 1)
    timestamp: datetime         # UTC, timezone-aware
    source_adapter: str         # Adapter that created this event
    source_transport_id: str    # Native actor/source identity (not native message ID)
    source_channel_id: str | None  # Native channel/room/topic on source adapter
    source_native_ref: NativeRef | None  # Inbound native message reference carried from codec
    parent_event_id: str | None # For derived events, points to origin
    lineage: tuple[str, ...]    # Chain of event_ids from origin to current (immutable)
    relations: tuple[EventRelation, ...]  # First-class relations (replies, reactions, etc.)
    payload: dict[str, object]  # Kind-specific payload (validated per event_kind)
    metadata: EventMetadata     # Structured metadata
    depth: int = 0              # Depth in derivation tree (0 for source events)
    trace_id: str | None = None # Distributed tracing correlation ID
```

### Field Notes

**`schema_version`** must be `>= 1`. The current schema contract is `v1` (`CURRENT_SCHEMA_VERSION = 1`). Future versions append fields with defaults; existing fields are never removed.

**`source_transport_id`** identifies the native actor, not the native message. Native message IDs belong in `native_message_refs` (Section 12.2 of the master spec).

| Transport | source_transport_id | Example |
|-----------|-------------------|---------|
| Matrix | Sender MXID | `@user:server.org` |
| LXMF | Source hash (16-byte hex) | `a1b2c3d4e5f6a7b8` |
| Meshtastic | Node number (string) | `1234` |
| MeshCore | Node number (string) | `1234` |

**`source_channel_id`** is the native channel, room, or topic where the event originated. A core field so route matching can evaluate source channels without pulling from metadata.

**`source_native_ref`** carries the inbound native message reference from the adapter codec. Set by `MatrixCodec.decode()` (and analogous codecs) during inbound processing. For Matrix, this is a `NativeRef` wrapping the Matrix event ID. `None` for outbound events or events created internally. The pipeline persists this as an inbound `NativeMessageRef` after canonical event storage.

| Transport | source_channel_id | Example |
|-----------|-------------------|---------|
| Matrix | Room ID | `!abc123:server.org` |
| MeshCore | Channel slot index | `0` |
| LXMF | Destination hash (for inbound) | `e5f6a7b8c9d0e1f2` |
| (no channel concept) | `None` | |

**`lineage`** is stored as an immutable `tuple[str, ...]`. Every element must be a non-empty string (event ID). The constructor validates this invariant and raises `ValueError` on violation.

**`depth`** defaults to `0` and must be `>= 0`.

**`trace_id`** is an optional distributed tracing correlation ID, reserved for future protocol-neutral use.


---

## 2. EventRelation

Relations between events are first-class. Every reply, reaction, edit, delete, or thread association is an `EventRelation` attached to the event.

```python
class EventRelation(msgspec.Struct, frozen=True):
    relation_type: Literal["reply", "reaction", "edit", "delete", "thread"]
    target_event_id: str | None        # Canonical event ID of the target event
    target_native_ref: NativeRef | None  # Structured native reference when canonical ID not yet known
    key: str | None                    # Relation-specific key (e.g., emoji for reactions)
    fallback_text: str | None          # Inline text when target adapter lacks native support
    metadata: dict[str, object] = {}   # Arbitrary key-value metadata attached to this relation
```

The `relation_type` is validated at construction time. Values outside the five known types (`"reply"`, `"reaction"`, `"edit"`, `"delete"`, `"thread"`) raise `ValueError`. The set of valid types is exported as `VALID_RELATION_TYPES`.

| Field | Purpose |
|-------|---------|
| `relation_type` | Semantic type of the relation |
| `target_event_id` | Canonical event ID this relation points to. Set after correlation |
| `target_native_ref` | Structured `NativeRef` identifying the native reference when the canonical event ID has not been resolved yet. The relation resolution stage resolves this to `target_event_id` via the `native_message_refs` table |
| `key` | Type-specific data. For `reaction`, this is the emoji or reaction identifier |
| `fallback_text` | Inline text used when the target adapter does not support this relation type natively (e.g., `[Alice] re: original msg > reply text`) |
| `metadata` | Arbitrary key-value metadata frozen via `_FrozenDict` |

Relations are canonical. They are stored with the event and used by the relation resolution and delivery planning stages. Adapters and plugins read and write relations through the event model, not through ad-hoc metadata fields.


---

## 3. NativeRef

Structured native reference for cross-adapter relation resolution.

```python
class NativeRef(msgspec.Struct, frozen=True):
    adapter: str                    # Adapter instance name (e.g., "matrix-home")
    native_channel_id: str | None   # Native channel/room/topic on the adapter
    native_message_id: str          # Native message ID on the adapter
    native_thread_id: str | None = None  # Native thread/conversation ID if applicable
```

When a `CanonicalEvent` carries an `EventRelation` with `target_native_ref` set (and `target_event_id` is `None`), the relation resolution stage queries `native_message_refs` by `(adapter, native_channel_id, native_message_id)` to find the canonical `event_id`.


---

## 4. EventMetadata

Structured metadata organized into well-defined namespaces. Not a flat bag of strings.

```python
class EventMetadata(msgspec.Struct, frozen=True):
    transport: TransportMetadata | None = None     # How the event arrived
    routing: RoutingMetadata | None = None         # Routing decisions applied
    radio: RadioMetadata | None = None             # Radio-specific data
    telemetry: TelemetryMetadata | None = None     # Device telemetry at time of event
    native: NativeMetadata | None = None           # Transport-native fields not yet normalized
    custom: dict[str, object] = {}                 # Plugin/extension metadata (frozen)
```

### Namespace Definitions

| Namespace | Purpose | Example Fields |
|-----------|---------|---------------|
| `transport` | Transport layer details | `protocol` (`"meshcore-tcp"`, `"lxmf"`, `"mqtt"`), `gateway_id`, `delivery_method`, `delivery_confirmed`, `transport_encrypted`, `signature_valid`, `propagation_state` |
| `routing` | Routing context | `matched_routes` (tuple), `fanout_group` |
| `radio` | Radio-specific data | `frequency`, `snr`, `rssi`, `channel_index` |
| `telemetry` | Device state at event time | `metrics` dict (frozen) |
| `native` | Unnormalized native fields | `data` dict (frozen) |
| `custom` | Plugin/extension data | Key-value pairs from plugins, reverse-DNS namespaced (frozen) |

The `native` namespace is a temporary holding area. The enrichment stage normalizes `native` fields into their proper namespaces when possible.

All `dict` fields in metadata are frozen via `_FrozenDict` — a `dict` subclass that raises `TypeError` on all mutation methods. Nested dicts and lists are recursively frozen. This provides deep immutability compatible with msgspec serialization.


---

## 5. Event Kind Registry

The canonical event kind registry. Every constant is a plain `str` following `<domain>.<action>` naming. Extensible by plugins via `plugin.custom`.

| Kind | Description | Domain |
|------|-------------|--------|
| `message.created` | A new message has entered the system | message |
| `message.text` | Plain text message payload | message |
| `message.reacted` | A reaction was attached to a message | message |
| `message.edited` | An existing message body was edited | message |
| `message.deleted` | A message was soft- or hard-deleted | message |
| `message.file` | A file attachment message | message |
| `telemetry.received` | Raw telemetry data received from a node | telemetry |
| `telemetry.position` | Geographic-position telemetry report | telemetry |
| `presence.changed` | A node or user's presence state changed | presence |
| `identity.updated` | Identity material (keys, profile) was updated | identity |
| `delivery.accepted` | Delivery plan accepted by target adapter | delivery |
| `delivery.queued` | Message enqueued for delivery | delivery |
| `delivery.sent` | Message handed off to transport layer | delivery |
| `delivery.confirmed` | Transport-level acknowledgement received | delivery |
| `delivery.failed` | Delivery attempt failed | delivery |
| `system.audit` | Audit-log entry produced by the framework | system |
| `system.lifecycle` | Lifecycle event (start, stop, reload) | system |
| `plugin.custom` | Reserved for plugin-defined custom events | plugin |

**Registration pattern**: The schema registry maps `(event_kind, schema_version)` to a validation function. New kinds are registered at adapter/plugin load time.

**Non-routeable kinds**: `delivery.*`, `system.audit`, and `system.lifecycle` are system/audit events that do not flow through normal user routes unless explicitly enabled by the operator.


---

## 6. Schema Versioning

### Version Format

- Monotonically increasing integers: 1, 2, 3, ...
- No sub-versioning. Every change increments the integer by one.
- Stored in the event's `schema_version` field.
- The schema registry maps `(event_kind, schema_version)` to a validation function.
- `CURRENT_SCHEMA_VERSION = 1` is the baseline schema contract.

### Migration Contract

1. **v1 is current.** All events with `schema_version == 1` conform to the contract documented here.

2. **New fields append with defaults.** When a new schema version adds fields, those fields carry sensible defaults so that `v1` consumers can read `v2` payloads without error.

3. **Existing fields are never removed.** A field may be superseded by a new field with a different name, but the original field continues to be populated. When a public stability guarantee is in effect, superseded fields carry a `superseded_by` annotation in the schema registry.

4. **`schema_version >= 1`** is enforced at construction. Values `< 1` raise `ValueError`.

5. **Unknown fields are preserved, not stripped.** If a payload contains a field the current schema version doesn't define, that field is kept in the payload and ignored by core logic. msgspec's default behavior skips unknown struct fields during decode (forward-looking tolerance of future fields).

6. **Known fields keep their meaning.** A field named `voltage_mv` always means voltage in millivolts. Renaming requires a new field alongside the original.

7. **Migration registry.** `MIGRATION_REGISTRY` provides a minimal registry-only hook for future migration functions. No migrations are executed in Phase 1. The registry maps `(event_kind, from_version, to_version)` to a `Callable[[dict], dict]` that transforms a payload.

### Handling Unknown Versions

| Scenario | Behavior |
|----------|----------|
| Consumer sees higher version (future) | Treat all known fields normally, ignore unknown fields |
| Consumer sees lower version (old) | Populate any new fields with defaults if possible, otherwise leave unset |
| Consumer understands version N | Can read any version <= N |


---

## 7. Immutability Rules

These rules are non-negotiable. No code path may violate them.

1. Once written to the canonical event log, **no field of a `CanonicalEvent` changes**. The `frozen=True` struct and `_FrozenDict` dict containers enforce this at runtime.

2. Enrichment creates a **new event** with `parent_event_id` set to the original's `event_id`. The `lineage` tuple is appended with the parent's ID.

3. Transforms create **new events** referencing their input event as parent.

4. The original event is always recoverable by following the `lineage` chain backward.

5. Event IDs are **UUIDv7** for natural time ordering and uniqueness.

6. **Deep immutability**: `payload`, `metadata.custom`, `EventRelation.metadata`, `TelemetryMetadata.metrics`, and `NativeMetadata.data` are all wrapped in `_FrozenDict`, which recursively freezes nested dicts and lists.

7. **Constructor input isolation**: Mutable inputs (lists, dicts) passed to the constructor are defensively copied. Mutating the original after construction does not affect the event.


---

## 8. Event Record Taxonomy

Not every record in the pipeline has the same semantic weight. Four classes:

| Record Class | `EventRecordKind` Value | Purpose | Storage |
|-------------|---------|---------|---------|
| **Source Event** | `source_event` | Initial canonical event produced by an adapter codec. | Always stored in the canonical event log. |
| **Derived Event** | `derived_event` | Produced by enrichment, transform, or policy stages. | Stored if semantically meaningful. |
| **Delivery Artifact** | `delivery_artifact` | Target-specific rendering for a particular adapter. | Stored as a `rendered_payload` record, **not** as a canonical event. |
| **Receipt Event** | `receipt_event` | Records the outcome of a delivery attempt. | Phase 1: rows in `delivery_receipts` table. |


---

## 9. SQL Schema

### canonical_events

```sql
CREATE TABLE canonical_events (
    event_id TEXT PRIMARY KEY,
    event_kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
    timestamp TEXT NOT NULL,         -- ISO 8601 with nanoseconds
    source_adapter TEXT NOT NULL,
    source_transport_id TEXT,        -- Native actor/source identity (not native message ID)
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

### event_relations

```sql
CREATE TABLE event_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    relation_type TEXT NOT NULL CHECK(relation_type IN ('reply', 'reaction', 'edit', 'delete', 'thread')),
    target_event_id TEXT,
    target_native_ref TEXT,
    key TEXT,
    fallback_text TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_relations_event ON event_relations(event_id);
CREATE INDEX idx_relations_target ON event_relations(target_event_id);
CREATE INDEX idx_relations_type ON event_relations(relation_type);
```

### native_message_refs

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
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(adapter, native_channel_id, native_message_id)
);

CREATE INDEX idx_native_refs_event ON native_message_refs(event_id);
CREATE INDEX idx_native_refs_adapter_native ON native_message_refs(adapter, native_message_id);
CREATE INDEX idx_native_refs_relation ON native_message_refs(adapter, native_relation_id);
```


---

## 10. Relation Persistence Rules

The `relations` tuple on a `CanonicalEvent` is **not** duplicated inside the `canonical_events` payload or metadata columns.

**Storage**: Relations are persisted as rows in the `event_relations` table. Each relation becomes its own row keyed by `event_id`.

**Loading**: When a `CanonicalEvent` is loaded from storage, its in-memory `relations` tuple is reconstructed by querying `event_relations` for that event's ID.

**Resolution flow**:

1. Adapter codec creates a `CanonicalEvent` with an `EventRelation` where `target_native_ref` is set and `target_event_id` is `None`.
2. The pipeline invokes `RelationResolver` during ingress, after decode and before storage, to resolve `target_native_ref` to `target_event_id` by querying `native_message_refs`.
3. If found, the relation is updated: `target_event_id` is set, `target_native_ref` is cleared.
4. If not found, the relation remains unresolved. The native ref is preserved. Routing and rendering continue without error. `fallback_text` may be used by the delivery stage.


---

## 11. Package Location

Per the package tree, the event model lives in:

```
core/events/
    __init__.py
    canonical.py          # CanonicalEvent, EventRelation, NativeRef, etc.
    kinds.py              # EventKind constants and registry
    schema.py             # SchemaRegistry, SchemaVersion, migration registry
    metadata.py           # EventMetadata and sub-namespaces
    bus.py                # Event bus (not part of this contract)
```


---

## 12. Protocol-Neutral Readiness (Future)

The canonical event model is designed to be transport-agnostic. The following extensions are reserved for future protocol-neutral integration (e.g., webhooks, request/response adapters) but are **not** implemented in Phase 1:

| Concept | Reserved Location | Notes |
|---------|-------------------|-------|
| **Correlation IDs** | `trace_id` field | Already present; supports distributed tracing across transports |
| **Idempotency keys** | `metadata.custom["idempotency_key"]` | Plugins/adapters may set this for deduplication |
| **Principal/auth context** | `metadata.custom["principal"]` | Reserved for future auth context; not populated in Phase 1 |
| **Request/response lineage** | `lineage` + `parent_event_id` | Existing mechanism supports request-response correlation |
| **Inbound provenance** | `source_adapter` + `source_transport_id` | Already present; extensible for new transports |
| **Webhook readiness** | docs/contracts only | No HTTP/webhook server implemented; schema and field contracts are transport-neutral, ready for future adapter implementation |

These are documented here so that future protocol adapters can rely on existing fields rather than requiring schema changes.

# Canonical Event Contract

> Extracted from: Modular Event Communications Runtime Specification v0.1.0, Sections 3, 5, 6, 12, 13, 14
> This document is a standalone implementation reference. Read this, build the event model, done.

---

## 1. CanonicalEvent

The core immutable record. Everything in the pipeline is a `CanonicalEvent`.

```python
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class CanonicalEvent:
    event_id: str               # UUIDv7 for time-ordering
    event_kind: str             # "message.text", "telemetry", "presence", etc.
    schema_version: int         # Schema version this event conforms to
    timestamp: datetime         # UTC, nanosecond precision if available
    source_adapter: str         # Adapter that created this event
    source_transport_id: str    # Native actor/source identity (not native message ID)
    source_channel_id: str | None  # Native channel/room/topic on source adapter
    parent_event_id: str | None # For derived events, points to origin
    lineage: list[str]          # Chain of event_ids from origin to current
    relations: list[EventRelation]  # First-class relations (replies, reactions, etc.)
    payload: dict               # Kind-specific payload (validated per event_kind)
    metadata: EventMetadata     # Structured metadata
    tags: set[str]              # Freeform tags for filtering/routing
```

### Field Notes

**`source_transport_id`** identifies the native actor, not the native message. Native message IDs belong in `native_message_refs` (Section 12.2 of the master spec).

| Transport | source_transport_id | Example |
|-----------|-------------------|---------|
| Matrix | Sender MXID | `@user:server.org` |
| LXMF | Source hash (16-byte hex) | `a1b2c3d4e5f6a7b8` |
| Meshtastic | Node number (string) | `1234` |
| MeshCore | Node number (string) | `1234` |

**`source_channel_id`** is the native channel, room, or topic where the event originated. A core field so route matching can evaluate source channels without pulling from metadata.

| Transport | source_channel_id | Example |
|-----------|-------------------|---------|
| Matrix | Room ID | `!abc123:server.org` |
| MeshCore | Channel slot index | `0` |
| LXMF | Destination hash (for inbound) | `e5f6a7b8c9d0e1f2` |
| (no channel concept) | `None` | |


---

## 2. EventRelation

Relations between events are first-class. Every reply, reaction, edit, delete, or thread association is an `EventRelation` attached to the event.

```python
from typing import Literal


@dataclass(frozen=True)
class EventRelation:
    relation_type: Literal["reply", "reaction", "edit", "delete", "thread"]
    target_event_id: str | None        # Canonical event ID of the target event
    target_native_ref: NativeRef | None  # Structured native reference when canonical ID not yet known
    key: str | None                    # Relation-specific key (e.g., emoji for reactions)
    fallback_text: str | None          # Inline text when target adapter lacks native support
```

| Field | Purpose |
|-------|---------|
| `relation_type` | Semantic type of the relation |
| `target_event_id` | Canonical event ID this relation points to. Set after correlation |
| `target_native_ref` | Structured `NativeRef` identifying the native reference when the canonical event ID has not been resolved yet. The relation resolution stage resolves this to `target_event_id` via the `native_message_refs` table |
| `key` | Type-specific data. For `reaction`, this is the emoji or reaction identifier |
| `fallback_text` | Inline text used when the target adapter does not support this relation type natively (e.g., `[Alice] re: original msg > reply text`) |

Relations are canonical. They are stored with the event and used by the relation resolution and delivery planning stages. Adapters and plugins read and write relations through the event model, not through ad-hoc metadata fields.

See [Section 5.2 rationale](../spec/modular-event-engine-spec.md#52-event-relations).


---

## 3. NativeRef

Structured native reference for cross-adapter relation resolution.

```python
@dataclass(frozen=True)
class NativeRef:
    """Structured native reference for cross-adapter relation resolution."""
    adapter: str                    # Adapter instance name (e.g., "matrix-home")
    native_channel_id: str | None   # Native channel/room/topic on the adapter
    native_message_id: str          # Native message ID on the adapter
    native_thread_id: str | None    # Native thread/conversation ID if applicable
```

When a `CanonicalEvent` carries an `EventRelation` with `target_native_ref` set (and `target_event_id` is `None`), the relation resolution stage queries `native_message_refs` by `(adapter, native_channel_id, native_message_id)` to find the canonical `event_id`.


---

## 4. EventMetadata

Structured metadata organized into well-defined namespaces. Not a flat bag of strings.

```python
@dataclass
class EventMetadata:
    transport: TransportMetadata | None     # How the event arrived
    routing: RoutingMetadata | None         # Routing decisions applied
    radio: RadioMetadata | None             # Radio-specific data
    telemetry: TelemetryMetadata | None     # Device telemetry at time of event
    native: NativeMetadata | None           # Transport-native fields not yet normalized
    custom: dict                            # Plugin/extension metadata
```

### Namespace Definitions

| Namespace | Purpose | Example Fields |
|-----------|---------|---------------|
| `transport` | Transport layer details | `protocol` (`"meshcore-tcp"`, `"lxmf"`, `"mqtt"`), `gateway_id`, `received_at`, `encoding` |
| `routing` | Routing context | `matched_routes`, `fanout_group`, `bridge_id` |
| `radio` | Radio-specific data | `frequency`, `modulation`, `snr`, `rssi`, `hop_limit`, `channel_index` |
| `telemetry` | Device state at event time | `battery_percent`, `voltage_mv`, `uptime_seconds`, `air_util_tx` |
| `native` | Unnormalized native fields | Adapter-specific raw fields not yet mapped to canonical fields |
| `custom` | Plugin/extension data | Key-value pairs from plugins, reverse-DNS namespaced |

The `native` namespace is a temporary holding area. The enrichment stage normalizes `native` fields into their proper namespaces when possible.

See [Section 14 rationale](../spec/modular-event-engine-spec.md#14-metadata-boundaries).


---

## 5. Event Kind Registry

Initial registry of event kinds. Extensible by plugins and future adapters.

| Kind | Description | Primary Producers | Routeable |
|------|-------------|-------------------|-----------|
| `message.text` | Plain text message from a user or node | Transport adapters | Yes |
| `message.file` | File, image, or attachment | Transport adapters | Yes |
| `telemetry` | Device telemetry (battery, voltage, position, environment) | Transport adapters | Yes (via transform) |
| `position` | Location update | Transport adapters | Yes (via transform) |
| `presence` | Online/offline/away status change | Transport adapters, system | Yes |
| `metrics.update` | Internal metric (queue depth, uptime, message count) | System, plugins | Yes |
| `channel.announcement` | Channel metadata change | Transport adapters | Yes |
| `system.lifecycle` | Adapter start/stop, connection state change | System | No (system event) |
| `plugin.event` | Plugin-generated signal (custom kind in payload) | Plugins | Yes |
| `delivery.receipt` | Result of a delivery attempt | Delivery system | No (system/audit) |
| `transform.output` | Output of a transform stage | Transform pipeline | No (audit/system) |
| `policy.action` | Policy decision (drop, flag, rate-limit) | Policy pipeline | No (audit/system) |

**Non-routeable kinds** (`delivery.receipt`, `transform.output`, `policy.action`, `system.lifecycle`) are system/audit events that do not flow through normal user routes unless explicitly enabled by the operator.

**Registration pattern**: The schema registry maps `(event_kind, schema_version)` to a validation function. New kinds are registered at adapter/plugin load time.

See [Section 5.3 rationale](../spec/modular-event-engine-spec.md#53-event-kinds-initial-registry).


---

## 6. Schema Versioning

### Version Format

- Monotonically increasing integers: 1, 2, 3, ...
- No sub-versioning. Every change increments the integer by one.
- Stored in the event's `schema_version` field.
- The schema registry maps `(event_kind, schema_version)` to a validation function.

### Rules

1. **Unknown fields are preserved, not stripped.** If an event contains a field the current schema version doesn't define, that field is kept in the payload and ignored by core logic.

2. **Known fields keep their meaning.** A field named `voltage_mv` always means voltage in millivolts. Renaming requires a new field and a deprecation window.

3. **Adapters declare schema versions.** Each adapter states the maximum schema version it supports. The runtime handles downgrade if needed.

4. **Deprecation windows.** When a field is deprecated, it remains populated for at least one major version cycle alongside its replacement. Both fields are present during the transition.

5. **Schema negotiation.** On startup, adapters and the runtime exchange supported schema versions. The runtime uses the highest mutually supported version.

### Handling Unknown Versions

| Scenario | Behavior |
|----------|----------|
| Consumer sees higher version (future) | Treat all known fields normally, ignore unknown fields |
| Consumer sees lower version (old) | Populate any new fields with defaults if possible, otherwise leave unset |
| Consumer understands version N | Can read any version <= N |

See [Section 13 rationale](../spec/modular-event-engine-spec.md#13-schema-versioning).


---

## 7. Immutability Rules

These rules are non-negotiable. No code path may violate them.

1. Once written to the canonical event log, **no field of a `CanonicalEvent` changes**.

2. Enrichment creates a **new event** with `parent_event_id` set to the original's `event_id`. The `lineage` list is appended with the parent's ID.

3. Transforms create **new events** referencing their input event as parent.

4. The original event is always recoverable by following the `lineage` chain backward.

5. Event IDs are **UUIDv7** for natural time ordering and uniqueness.

Design rationale: [Section 3.2](../spec/modular-event-engine-spec.md#32-immutability).


---

## 8. Event Record Taxonomy

Not every record in the pipeline has the same semantic weight. Four classes:

| Record Class | Purpose | Storage |
|-------------|---------|---------|
| **Source Event** | Initial canonical event produced by an adapter codec from raw native data. Primary record of what happened on a transport. | Always stored in the canonical event log. |
| **Derived Event** | Produced by enrichment, transform, or policy stages. References parent via `parent_event_id` with full `lineage`. | Stored in the canonical event log if semantically meaningful. Transient intermediates may be discarded based on configuration. |
| **Delivery Artifact** | Target-specific rendering for a particular adapter (e.g., Matrix HTML, MeshCore 160-byte text, LXMF fields dict). | Stored as a `rendered_payload` record on the delivery plan, **not** as a canonical event. |
| **Receipt Event** | `delivery.receipt` recording the outcome of a delivery attempt. | Phase 1: rows in `delivery_receipts` table (not canonical events). Future: may optionally mirror as canonical events for audit. |

**Distinction rule**: If a rendering is semantically meaningful (e.g., a message edit produces a new canonical event with edit semantics), it is a derived event, not a delivery artifact.

**Storage guidance**: The canonical event log holds source events and semantically meaningful derived events. Receipts live in `delivery_receipts`. Renderings live as payload records on delivery plans.

See [Section 5.5 rationale](../spec/modular-event-engine-spec.md#55-event-record-taxonomy).


---

## 9. SQL Schema

### canonical_events

```sql
CREATE TABLE canonical_events (
    event_id TEXT PRIMARY KEY,
    event_kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    timestamp TEXT NOT NULL,         -- ISO 8601 with nanoseconds
    source_adapter TEXT NOT NULL,
    source_transport_id TEXT,        -- Native actor/source identity (not native message ID)
    source_channel_id TEXT,          -- Native channel/room/topic on source adapter
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
```

### event_relations

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

### native_message_refs

Required for cross-adapter relation resolution. Maps native adapter message IDs to canonical event IDs.

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

### native_message_refs Transport Examples

| Transport | native_message_id | native_channel_id |
|-----------|------------------|-------------------|
| Matrix | Matrix event ID (e.g., `$abc123`) | Room ID |
| Meshtastic | Packet ID | Channel index |
| MeshCore | MeshCore message reference | Channel slot |
| LXMF | LXMF message ID | Source hash |

See [Section 12.2](../spec/modular-event-engine-spec.md#122-native-message-references).


---

## 10. Relation Persistence Rules

The `relations` list on a `CanonicalEvent` is **not** duplicated inside the `canonical_events` payload or metadata columns.

**Storage**: Relations are persisted as rows in the `event_relations` table. Each relation becomes its own row keyed by `event_id`.

**Loading**: When a `CanonicalEvent` is loaded from storage, its in-memory `relations` list is reconstructed by querying `event_relations` for that event's ID.

**API boundary**: `StorageBackend.store_relation(event_id, relation)` and `StorageBackend.list_relations(event_id)` manage this. The storage layer handles the split between the event row and its relation rows.

```python
class StorageBackend(Protocol):
    # ... other methods ...
    async def store_relation(self, event_id: str, relation: EventRelation) -> None: ...
    async def list_relations(self, event_id: str) -> list[EventRelation]: ...
```

**Resolution flow**:

1. Adapter creates a `CanonicalEvent` with an `EventRelation` where `target_native_ref` is set and `target_event_id` is `None`.
2. The relation resolution stage queries `native_message_refs` using the `NativeRef` fields: `SELECT event_id FROM native_message_refs WHERE adapter = ? AND native_channel_id = ? AND native_message_id = ?`.
3. If found, the relation is updated: `target_event_id` is set to the canonical event ID, `target_native_ref` is cleared.
4. If not found, the relation remains unresolved. The `fallback_text` field may be used by the delivery stage.

See [Section 5.2 persistence rule](../spec/modular-event-engine-spec.md#52-event-relations) and [Section 12.4](../spec/modular-event-engine-spec.md#124-future-backends).


---

## 11. Package Location

Per the proposed package tree, the event model lives in:

```
core/events/
    __init__.py
    canonical.py          # CanonicalEvent dataclass
    kinds.py              # Event kind registry
    schema.py             # Schema registry and validation
```

Storage layer:

```
core/storage/
    __init__.py
    backend.py            # StorageBackend protocol
    sqlite.py             # SQLite implementation
```

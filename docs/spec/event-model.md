# Event Model

> Normative specification for MEDRE's canonical event model.

This document defines the data structures, invariants, and storage schema for
every event in the MEDRE pipeline. All implementations MUST conform to the
requirements stated here.

## Conformance Language

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHALL**, **SHALL NOT**,
**SHOULD**, **SHOULD NOT**, **RECOMMENDED**, **MAY**, and **OPTIONAL** in this
document are to be interpreted as described in RFC 2119.

---

## 1. CanonicalEvent

`CanonicalEvent` is the universal, immutable event envelope. Every event —
whether sourced from an external adapter, synthesised by the framework, or
produced as a delivery artifact — is represented as a `CanonicalEvent`.

### 1.1 Definition

```python
class CanonicalEvent(msgspec.Struct, frozen=True):
    event_id: str                          # UUIDv7
    event_kind: str                        # "<domain>.<action>"
    schema_version: int                    # >= 1
    timestamp: datetime                    # UTC, timezone-aware
    source_adapter: str                    # Adapter instance that created this event
    source_transport_id: str               # Native actor/source identity
    source_channel_id: str | None          # Native channel/room/topic on source adapter
    source_native_ref: NativeRef | None    # Inbound native message reference from codec
    parent_event_id: str | None            # Parent in derivation chain (None for source events)
    lineage: tuple[str, ...]               # Ordered chain of ancestor event IDs
    relations: tuple[EventRelation, ...]   # First-class relations (replies, reactions, etc.)
    payload: dict[str, object]             # Kind-specific data (validated per event_kind)
    metadata: EventMetadata                # Structured metadata namespaces
    depth: int = 0                         # Depth in derivation tree (0 for source events)
    trace_id: str | None = None            # Distributed tracing correlation ID
```

### 1.2 Field Reference

| Field                 | Type                        | Default | Description                                                                                                                                                                                   |
| --------------------- | --------------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `event_id`            | `str`                       | —       | Globally unique identifier. MUST be UUIDv7 for time-ordering and uniqueness. MUST be a non-empty string.                                                                                      |
| `event_kind`          | `str`                       | —       | Kind string from the event kind registry (Section 5). MUST be a non-empty string.                                                                                                             |
| `schema_version`      | `int`                       | —       | Schema version this event conforms to. MUST be `>= 1`. Current version: `1`.                                                                                                                  |
| `timestamp`           | `datetime`                  | —       | When the event occurred. MUST be timezone-aware (UTC).                                                                                                                                        |
| `source_adapter`      | `str`                       | —       | Name of the adapter instance that produced this event.                                                                                                                                        |
| `source_transport_id` | `str`                       | —       | Native actor/source identity on the transport. This identifies _who_ sent the event, not the message ID.                                                                                      |
| `source_channel_id`   | `str \| None`               | —       | Native channel, room, or topic where the event originated. `None` when the transport has no channel concept.                                                                                  |
| `source_native_ref`   | `NativeRef \| None`         | `None`  | Structured native message reference carried from the adapter codec. Set during inbound processing by codecs (e.g., `MatrixCodec.decode()`). `None` for outbound or internally created events. |
| `parent_event_id`     | `str \| None`               | —       | ID of the parent event in the derivation chain. `None` for source events.                                                                                                                     |
| `lineage`             | `tuple[str, ...]`           | —       | Ordered chain of event IDs from origin to current. Every element MUST be a non-empty string.                                                                                                  |
| `relations`           | `tuple[EventRelation, ...]` | —       | First-class typed relations to other events.                                                                                                                                                  |
| `payload`             | `dict[str, object]`         | —       | Kind-specific data payload, validated per `event_kind` via the schema registry.                                                                                                               |
| `metadata`            | `EventMetadata`             | —       | Structured metadata organized into namespaces (Section 4).                                                                                                                                    |
| `depth`               | `int`                       | `0`     | Depth in the derivation tree. MUST be `>= 0`. `0` for source events.                                                                                                                          |
| `trace_id`            | `str \| None`               | `None`  | Distributed tracing correlation ID, reserved for future protocol-neutral use.                                                                                                                 |

### 1.3 Source Identity Examples

`source_transport_id` identifies the native actor, not the native message:

| Transport  | `source_transport_id`     | Example            |
| ---------- | ------------------------- | ------------------ |
| Matrix     | Sender MXID               | `@user:server.org` |
| LXMF       | Source hash (16-byte hex) | `a1b2c3d4e5f6a7b8` |
| Meshtastic | Node number (string)      | `1234`             |
| MeshCore   | Node number (string)      | `1234`             |

`source_channel_id` identifies the native channel:

| Transport          | `source_channel_id` | Example              |
| ------------------ | ------------------- | -------------------- |
| Matrix             | Room ID             | `!abc123:server.org` |
| Meshtastic         | Channel index       | `0`                  |
| MeshCore           | Channel slot index  | `0`                  |
| LXMF               | `None`              | —                    |
| No channel concept | `None`              | —                    |

### 1.4 Constructor Invariants

The constructor enforces the following invariants. Violations raise `ValueError`:

- `event_id` MUST be a non-empty string.
- `event_kind` MUST be a non-empty string.
- `schema_version` MUST be `>= 1`.
- `timestamp` MUST be timezone-aware (UTC).
- `depth` MUST be `>= 0`.
- `lineage` MUST NOT be `None`. Every element MUST be a non-empty string.
- `relations` MUST NOT be `None`.
- Mutable inputs (`list`, `dict`) are defensively copied to immutable
  storage (`tuple`, `_FrozenDict`). Mutating the original after construction
  MUST NOT affect the event.

---

## 2. EventRelation

Relations between events are first-class. Every reply, reaction, edit, delete,
or thread association is an `EventRelation` attached to the event.

### 2.1 Definition

```python
class EventRelation(msgspec.Struct, frozen=True):
    relation_type: Literal["reply", "reaction", "edit", "delete", "thread"]
    target_event_id: str | None           # Canonical event ID of the target
    target_native_ref: NativeRef | None    # Native reference when canonical ID not yet resolved
    key: str | None                       # Relation-specific key (e.g., emoji for reactions)
    fallback_text: str | None             # Degraded text representation for relation types the target cannot render natively
    metadata: dict[str, object] = {}      # Arbitrary key-value metadata (frozen)
```

### 2.2 Field Reference

| Field               | Type                                                       | Default | Description                                                                                                                                                                                                          |
| ------------------- | ---------------------------------------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `relation_type`     | `Literal["reply", "reaction", "edit", "delete", "thread"]` | —       | Semantic type of the relation. MUST be one of the five known types. Invalid values raise `ValueError` at construction.                                                                                               |
| `target_event_id`   | `str \| None`                                              | —       | Canonical event ID this relation points to. Set after correlation by the relation resolution stage.                                                                                                                  |
| `target_native_ref` | `NativeRef \| None`                                        | —       | Structured `NativeRef` identifying the native reference when the canonical event ID has not yet been resolved. The relation resolution stage resolves this to `target_event_id` via the `native_message_refs` table. |
| `key`               | `str \| None`                                              | —       | Type-specific data. For `reaction`, this is the emoji or reaction identifier.                                                                                                                                        |
| `fallback_text`     | `str \| None`                                              | —       | Human-readable text carrying the semantic content of this relation when the target adapter's capability level is `"fallback"`. Used by the target-native renderer to produce degraded text output within its native format (e.g., inline `[Alice] re: original msg > reply text` inside a Matrix message body). Not a generic text payload. Not used when capability is `"native"` or `"unsupported"`. |
| `metadata`          | `dict[str, object]`                                        | `{}`    | Arbitrary key-value metadata. Frozen via `_FrozenDict` at construction.                                                                                                                                              |

### 2.3 Valid Relation Types

The set of valid `relation_type` values is exported as `VALID_RELATION_TYPES`:

```python
VALID_RELATION_TYPES: frozenset[str] = frozenset(
    {"reply", "reaction", "edit", "delete", "thread"}
)
```

The constructor validates `relation_type` at construction time. Values outside
the five known types raise `ValueError`.

### 2.4 Resolution Flow

1. An adapter codec creates a `CanonicalEvent` with an `EventRelation` where
   `target_native_ref` is set and `target_event_id` is `None`.
2. The pipeline invokes `RelationResolver` during ingress (after decode, before
   storage) to resolve `target_native_ref` to `target_event_id` by querying
   `native_message_refs`.
3. If found, the relation is updated: `target_event_id` is set, `target_native_ref`
   is cleared.
4. If not found, the relation remains unresolved. The native ref is preserved.
   Routing and rendering continue without error. `fallback_text` MAY be used by
   the delivery stage.

### 2.5 Fallback Text and Relation Degradation

`fallback_text` on `EventRelation` carries a human-readable representation of
the relation's semantic content. Its purpose is to preserve relation meaning
when the target transport cannot express the relation type through its native
mechanism.

**fallback_text is not a generic text payload.** It does not bypass the
target-native renderer. When the target adapter's capability for a relation
type is at level `"fallback"`, the target-native renderer (e.g. MatrixRenderer
for Matrix, MeshtasticRenderer for Meshtastic) consumes `fallback_text` and
produces degraded text output within its own native format. The rendered
payload is still adapter-native; only the relation representation degrades to
inline text.

**Capability-driven degradation rules:**

| Target capability level | Delivery strategy | Rendering behaviour                                           |
| ----------------------- | ----------------- | ------------------------------------------------------------- |
| `"native"`              | `direct`          | Native relation rendering (e.g. `m.in_reply_to` on Matrix)   |
| `"fallback"`            | `fallback_text`   | Target-native renderer produces its format with inline text   |
| `"unsupported"`         | `skip`            | No rendering; delivery suppressed before adapter invocation   |

A transport that declares `"unsupported"` for a relation type receives no
delivery for events carrying that relation. A transport that declares
`"native"` never uses `fallback_text`. Only the `"fallback"` level triggers
degraded text rendering via the target-native renderer.

**Example:** A `reply` relation with `fallback_text = "original msg text"`
routed to a Matrix adapter (replies = `"native"`) is rendered as a native
`m.in_reply_to` relation. The same event routed to a MeshCore adapter
(replies = `"unsupported"`) is skipped entirely. If a hypothetical adapter
declared replies = `"fallback"`, the MeshCore renderer would produce its
native channel message format with the reply context embedded as inline text
drawn from `fallback_text`.

---

## 3. NativeRef

Structured native reference for cross-adapter relation resolution.

### 3.1 Definition

```python
class NativeRef(msgspec.Struct, frozen=True):
    adapter: str                          # Adapter instance name (e.g., "matrix-home")
    native_channel_id: str | None         # Native channel/room/topic on the adapter
    native_message_id: str                # Native message ID on the adapter
    native_thread_id: str | None = None   # Native thread/conversation ID if applicable
```

### 3.2 Field Reference

| Field               | Type          | Default | Description                                                                                                       |
| ------------------- | ------------- | ------- | ----------------------------------------------------------------------------------------------------------------- |
| `adapter`           | `str`         | —       | Name of the adapter that owns the native namespace.                                                               |
| `native_channel_id` | `str \| None` | —       | Channel or conversation ID in the adapter's native format.                                                        |
| `native_message_id` | `str`         | —       | Message ID in the adapter's native format.                                                                        |
| `native_thread_id`  | `str \| None` | `None`  | Thread or parent message ID in the adapter's native format. Reserved — no adapter currently populates this field. |

### 3.3 Usage

When a `CanonicalEvent` carries an `EventRelation` with `target_native_ref` set
(and `target_event_id` is `None`), the relation resolution stage queries
`native_message_refs` by `(adapter, native_channel_id, native_message_id)` to
find the canonical `event_id`.

---

## 4. EventMetadata

Structured metadata organized into well-defined namespaces. Not a flat bag of
strings.

### 4.1 Definition

```python
class EventMetadata(msgspec.Struct, frozen=True):
    transport: TransportMetadata | None = None     # How the event arrived
    routing: RoutingMetadata | None = None         # Routing decisions applied
    radio: RadioMetadata | None = None             # Radio-specific data
    telemetry: TelemetryMetadata | None = None     # Device telemetry at time of event
    native: NativeMetadata | None = None           # Transport-native fields not yet normalized
    custom: dict[str, object] = {}                 # Plugin/extension metadata (frozen)
```

### 4.2 Namespace Definitions

| Namespace   | Struct              | Purpose                    | Example Fields                                                                                                                                |
| ----------- | ------------------- | -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `transport` | `TransportMetadata` | Transport layer details    | `protocol`, `substrate`, `gateway_id`, `delivery_method`, `delivery_confirmed`, `transport_encrypted`, `signature_valid`, `propagation_state` |
| `routing`   | `RoutingMetadata`   | Routing context            | `matched_routes` (tuple), `fanout_group`, `route_trace` (tuple)                                                                               |
| `radio`     | `RadioMetadata`     | Radio-specific data        | `frequency`, `snr`, `rssi`, `channel_index`                                                                                                   |
| `telemetry` | `TelemetryMetadata` | Device state at event time | `metrics` dict (frozen)                                                                                                                       |
| `native`    | `NativeMetadata`    | Unnormalized native fields | `data` dict (frozen)                                                                                                                          |
| `custom`    | `dict[str, object]` | Plugin/extension data      | Key-value pairs, reverse-DNS namespaced (frozen)                                                                                              |

### 4.3 Namespace Rules

1. Adapters MUST normalize their native fields into the appropriate namespace.
   Fields that do not map cleanly to a canonical namespace MUST go into `native`.

2. The `native` namespace is a temporary holding area. The enrichment stage
   SHOULD normalize `native` fields into their proper namespaces when possible.

3. The `custom` namespace is reserved for plugins and extensions. Keys in
   `custom` SHOULD use reverse-DNS namespacing (e.g., `com.example.plugin.field`).

4. All `dict` fields (`custom`, `TelemetryMetadata.metrics`,
   `NativeMetadata.data`, `EventRelation.metadata`) are wrapped in `_FrozenDict`
   at construction, providing deep immutability while remaining `dict`-compatible
   for msgspec serialization.

5. Nested dicts and lists are recursively frozen.

### 4.4 Sub-Struct Definitions

#### TransportMetadata

```python
class TransportMetadata(msgspec.Struct, frozen=True):
    protocol: str | None = None             # e.g., "meshcore-tcp", "lxmf", "mqtt"
    substrate: str | None = None            # e.g., "lorawan", "wifi"
    gateway_id: str | None = None           # Gateway node identifier
    delivery_method: str | None = None      # e.g., "direct", "store_forward"
    delivery_confirmed: bool | None = None  # Tri-state: True, False, None
    transport_encrypted: bool | None = None # Tri-state: True, False, None
    signature_valid: bool | None = None     # Tri-state: True, False, None
    propagation_state: str | None = None    # Transport-specific state string
```

Nullable boolean fields (`delivery_confirmed`, `transport_encrypted`,
`signature_valid`) are tri-state: `True`, `False`, or `None`. Consumers MUST
treat `None` as "unknown", not as `False` or `True`.

#### RoutingMetadata

```python
class RoutingMetadata(msgspec.Struct, frozen=True):
    matched_routes: tuple[str, ...] = ()    # Route IDs that matched this event
    fanout_group: str | None = None         # Fanout group name
    route_trace: tuple[str, ...] = ()       # Ordered route IDs per delivery
```

#### RadioMetadata

```python
class RadioMetadata(msgspec.Struct, frozen=True):
    snr: float | None = None               # Signal-to-noise ratio (dB)
    rssi: float | None = None              # Received signal strength (dBm)
    channel_index: int | None = None       # Radio channel index
    frequency: float | None = None         # Operating frequency (MHz)
```

#### TelemetryMetadata

```python
class TelemetryMetadata(msgspec.Struct, frozen=True):
    metrics: dict[str, float | int | str | bool] = {}   # Frozen at construction
```

#### NativeMetadata

```python
class NativeMetadata(msgspec.Struct, frozen=True):
    data: dict[str, object] = {}           # Frozen at construction
```

### 4.5 Deep Immutability

All `dict` fields in metadata are frozen via `_FrozenDict` — a `dict` subclass
that raises `TypeError` on all mutation methods (`__setitem__`, `__delitem__`,
`clear`, `pop`, `popitem`, `setdefault`, `update`, `__ior__`). Nested dicts
and lists are recursively frozen. This provides deep immutability compatible
with msgspec serialization (`isinstance(_FrozenDict(), dict)` is `True`).

---

## 5. Event Kind Registry

Every `event_kind` is a plain `str` following `<domain>.<action>` naming.
The registry is extensible by plugins via `plugin.custom`.

### 5.1 Built-in Kinds

| Kind                 | Description                                   | Domain    |
| -------------------- | --------------------------------------------- | --------- |
| `message.created`    | A new message has entered the system          | message   |
| `message.text`       | Plain text message payload                    | message   |
| `message.reacted`    | A reaction was attached to a message          | message   |
| `message.edited`     | An existing message body was edited           | message   |
| `message.deleted`    | A message was soft- or hard-deleted           | message   |
| `message.file`       | A file attachment message                     | message   |
| `telemetry.received` | Raw telemetry data received from a node       | telemetry |
| `telemetry.position` | Geographic-position telemetry report          | telemetry |
| `presence.changed`   | A node or user's presence state changed       | presence  |
| `identity.updated`   | Identity material (keys, profile) was updated | identity  |
| `delivery.queued`    | Message enqueued for delivery                 | delivery  |
| `delivery.sent`      | Message handed off to transport layer         | delivery  |
| `delivery.failed`    | Delivery attempt failed                       | delivery  |
| `system.audit`       | Audit-log entry produced by the framework     | system    |
| `system.lifecycle`   | Lifecycle event (start, stop, reload)         | system    |
| `plugin.custom`      | Reserved for plugin-defined custom events     | plugin    |

### 5.2 Registration

The schema registry maps `(event_kind, schema_version)` to a validation
function. New kinds are registered at adapter/plugin load time.

```python
class SchemaRegistry:
    def register(self, event_kind: str, schema_version: int,
                 validator: Callable[[dict], list[str]]) -> None: ...
```

A validator receives the event payload dict and returns a list of error strings.
An empty list means the payload is valid.

### 5.3 Non-Routeable Kinds

`delivery.*`, `system.audit`, and `system.lifecycle` are system/audit events
that MUST NOT flow through normal user routes unless explicitly enabled by the
operator.

### 5.4 Known Kinds Set

The immutable set of all built-in kinds is exported as:

```python
KNOWN_KINDS: frozenset[str] = frozenset([
    "message.created", "message.text", "message.reacted", "message.edited",
    "message.deleted", "message.file",
    "telemetry.received", "telemetry.position",
    "presence.changed",
    "identity.updated",
    "delivery.queued", "delivery.sent",
    "delivery.failed",
    "system.audit", "system.lifecycle",
    "plugin.custom",
])
```

---

## 6. EventRecordKind

Not every record in the pipeline has the same semantic weight. Four classes are
distinguished:

### 6.1 Definition

```python
class EventRecordKind(Enum):
    SOURCE_EVENT = "source_event"           # Initial canonical event from adapter codec
    DERIVED_EVENT = "derived_event"         # Produced by enrichment, transform, or policy
    DELIVERY_ARTIFACT = "delivery_artifact" # Target-specific rendering for an adapter
    RECEIPT_EVENT = "receipt_event"         # Records the outcome of a delivery attempt
```

### 6.2 Storage Rules

| Record Class      | Storage                                                                      |
| ----------------- | ---------------------------------------------------------------------------- |
| Source Event      | MUST be stored in the canonical event log.                                   |
| Derived Event     | Stored if semantically meaningful. Transient intermediates MAY be discarded. |
| Delivery Artifact | Stored as a `rendered_payload` record, NOT as a canonical event.             |
| Receipt Event     | Stored as rows in the `delivery_receipts` table.                             |

---

## 7. Immutability Rules

These rules are non-negotiable. No code path MAY violate them.

1. **No field mutation after creation.** Once written to the canonical event
   log, no field of a `CanonicalEvent` changes. The `frozen=True` struct and
   `_FrozenDict` containers enforce this at runtime.

2. **Enrichment produces new events.** Enrichment MUST create a new event with
   `parent_event_id` set to the original's `event_id`. The `lineage` tuple is
   appended with the parent's ID.

3. **Transforms produce new events.** Transforms MUST create new events
   referencing their input event as parent.

4. **Lineage is recoverable.** The original event MUST always be recoverable
   by following the `lineage` chain backward.

5. **UUIDv7 identifiers.** Event IDs MUST be UUIDv7 for natural time ordering
   and uniqueness.

6. **Deep immutability.** `payload`, `metadata.custom`, `EventRelation.metadata`,
   `TelemetryMetadata.metrics`, and `NativeMetadata.data` are all wrapped in
   `_FrozenDict`, which recursively freezes nested dicts and lists.

7. **Constructor input isolation.** Mutable inputs (`list`, `dict`) passed to
   the constructor MUST be defensively copied. Mutating the original after
   construction MUST NOT affect the event.

---

## 8. Schema Versioning

### 8.1 Version Format

- Schema versions are monotonically increasing integers: `1`, `2`, `3`, ...
- No sub-versioning. Every change increments the integer by one.
- Stored in the event's `schema_version` field.
- The current baseline is `CURRENT_SCHEMA_VERSION = 1`.
- The schema registry maps `(event_kind, schema_version)` to a validation
  function.

```python
CURRENT_SCHEMA_VERSION: int = 1
```

### 8.2 SchemaVersion

```python
class SchemaVersion(msgspec.Struct, frozen=True):
    event_kind: str       # The event kind string
    version: int          # Monotonically increasing version number
```

### 8.3 Migration Policy

**Pre-release (current).** Schemas MAY change directly — fields renamed, types
changed, structures reorganised — without migration paths. Breaking changes are
applied by updating tests and documentation in the same commit. `schema_version`
remains `1` throughout pre-release.

**Post-release stability guarantee.** Once a stable release ships:

1. New fields MUST append with defaults so that consumers of older versions can
   read newer payloads without error.
2. Existing fields MUST NOT be removed. A field MAY be superseded by a new
   field, but the original continues to be populated. Superseded fields carry a
   `superseded_by` annotation in the schema registry.
3. `schema_version >= 1` MUST be enforced at construction. Values `< 1` raise
   `ValueError`.
4. Unknown fields MUST be preserved, not stripped. If a payload contains a field
   the current schema version does not define, that field is kept and ignored by
   core logic. msgspec's default behavior skips unknown struct fields during
   decode, providing forward-looking tolerance.
5. Known fields MUST keep their meaning. A field named `voltage_mv` always means
   voltage in millivolts. Renaming requires a new field alongside the original.
6. `MIGRATION_REGISTRY` provides a registry-only hook for future migration
   functions. No migrations are executed until post-release stability is in
   effect. Migrations map `(event_kind, from_version, to_version)` to a
   `Callable[[dict], dict]`.

### 8.4 Handling Unknown Versions

| Scenario                              | Behavior                                                                 |
| ------------------------------------- | ------------------------------------------------------------------------ |
| Consumer sees higher version (future) | Treat all known fields normally, ignore unknown fields                   |
| Consumer sees lower version (old)     | Populate any new fields with defaults if possible, otherwise leave unset |
| Consumer understands version N        | Can read any version `<= N`                                              |

---

## 9. SQL Schema

### 9.1 canonical_events

```sql
CREATE TABLE canonical_events (
    event_id TEXT PRIMARY KEY,
    event_kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
    timestamp TEXT NOT NULL,            -- ISO 8601 with nanoseconds
    source_adapter TEXT NOT NULL,
    source_transport_id TEXT NOT NULL,  -- Native actor/source identity
    source_channel_id TEXT,             -- Native channel/room/topic on source adapter
    source_native_adapter TEXT,         -- Source NativeRef.adapter
    source_native_channel_id TEXT,      -- Source NativeRef.native_channel_id
    source_native_message_id TEXT,      -- Source NativeRef.native_message_id
    source_native_thread_id TEXT,       -- Source NativeRef.native_thread_id
    parent_event_id TEXT,
    lineage TEXT,                       -- JSON array of event IDs
    payload TEXT NOT NULL,              -- JSON
    metadata TEXT NOT NULL,             -- JSON
    depth INTEGER NOT NULL DEFAULT 0,
    trace_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_events_kind ON canonical_events(event_kind);
CREATE INDEX idx_events_timestamp ON canonical_events(timestamp);
CREATE INDEX idx_events_source ON canonical_events(source_adapter, source_transport_id);
CREATE INDEX idx_events_parent ON canonical_events(parent_event_id);
```

### 9.2 event_relations

```sql
CREATE TABLE event_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    relation_type TEXT NOT NULL CHECK(relation_type IN ('reply', 'reaction', 'edit', 'delete', 'thread')),
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

CREATE INDEX idx_relations_event ON event_relations(event_id);
CREATE INDEX idx_relations_target ON event_relations(target_event_id);
CREATE INDEX idx_relations_type ON event_relations(relation_type);
```

### 9.3 native_message_refs

```sql
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
    created_at TEXT NOT NULL,
    UNIQUE(adapter, native_channel_id, native_message_id)
);

CREATE INDEX idx_native_refs_event ON native_message_refs(event_id);
CREATE INDEX idx_native_refs_adapter_native ON native_message_refs(adapter, native_message_id);
CREATE INDEX idx_native_refs_relation ON native_message_refs(adapter, native_relation_id);
```

### 9.4 Relation Persistence Rules

The `relations` tuple on a `CanonicalEvent` is NOT duplicated inside the
`canonical_events` payload or metadata columns.

- **Storage:** Relations are persisted as rows in the `event_relations` table.
  Each relation becomes its own row keyed by `event_id`.
- **Loading:** When a `CanonicalEvent` is loaded from storage, its in-memory
  `relations` tuple is reconstructed by querying `event_relations` for that
  event's ID.

---

## 10. Package Location

The event model lives in:

```text
core/events/
    __init__.py
    canonical.py     # CanonicalEvent, EventRelation, NativeRef, NativeMessageRef, DeliveryReceipt, EventRecordKind
    kinds.py         # EventKind constants, KNOWN_KINDS, is_registered()
    schema.py        # SchemaRegistry, SchemaVersion, CURRENT_SCHEMA_VERSION, MIGRATION_REGISTRY
    metadata.py      # EventMetadata, TransportMetadata, RoutingMetadata, RadioMetadata, TelemetryMetadata, NativeMetadata
    bus.py           # Event bus (not part of this specification)
```

# Modular Event Communications Runtime Specification

> Status: Draft
> Version: 0.1.0
> Last updated: 2026-05-07

## Table of Contents

1. [Overview](#1-overview)
2. [Project Naming](#2-project-naming)
3. [Design Principles](#3-design-principles)
4. [Architecture](#4-architecture)
5. [Canonical Event Model](#5-canonical-event-model)
6. [Event Transforms](#6-event-transforms)
7. [Policy Pipeline](#7-policy-pipeline)
8. [Routing and Delivery Planning](#8-routing-and-delivery-planning)
9. [Adapter Contract](#9-adapter-contract)
10. [Delivery Results and Receipts](#10-delivery-results-and-receipts)
11. [Identity Model](#11-identity-model)
12. [Storage and Canonical Event Log](#12-storage-and-canonical-event-log)
13. [Schema Versioning](#13-schema-versioning)
14. [Metadata Boundaries](#14-metadata-boundaries)
15. [MeshCore State Machine](#15-meshcore-state-machine)
16. [Matrix Metadata and Embedding](#16-matrix-metadata-and-embedding)
17. [LXMF and Reticulum Adapter Notes](#17-lxmf-and-reticulum-adapter-notes)
18. [Raw Native Archive Mode](#18-raw-native-archive-mode)
19. [Replay and Reprocessing](#19-replay-and-reprocessing)
20. [Plugin System](#20-plugin-system)
21. [Observability](#21-observability)
22. [Proposed Package Tree](#22-proposed-package-tree)
23. [Acceptance Criteria](#23-acceptance-criteria)
24. [Phased Implementation Plan](#24-phased-implementation-plan)
25. [Future Document Split](#25-future-document-split)
26. [Behavioral Lessons from MMRelay](#26-behavioral-lessons-from-mmrelay)
27. [Out of Scope](#27-out-of-scope)
28. [Appendix: Illustrative Snippets](#28-appendix-illustrative-snippets)


## 1. Overview

This specification describes a platform-neutral event communications runtime. The system ingests events from heterogeneous transport networks (mesh radio, LXMF over Reticulum, MQTT, TCP bridges), transforms and routes them through a canonical pipeline, and delivers them to presentation platforms (Matrix, Discord, Telegram, web dashboards) or other transports.

The architecture is event-first, not message-first. A text message is one event kind among many. Telemetry readings, metrics updates, presence changes, channel announcements, and plugin-generated signals are all first-class events that flow through the same pipeline.

Key goals:

- **Transport agnostic.** No single radio protocol or chat platform is central to the design.
- **Event immutable.** Events are never mutated in place. Enrichment and transformation produce derived events with traceable lineage.
- **Pluggable adapters.** Transports and presentation layers implement a common adapter contract with explicit lifecycle management.
- **Replayable.** The canonical event log supports reprocessing for plugin changes, routing changes, and debugging.
- **Observable.** Every stage of the pipeline emits structured telemetry.

This project is not an MMRelay rewrite, compatibility layer, or migration tool. It is a new runtime informed by operational experience with MMRelay and the MeshCore plugin ecosystem.


## 2. Project Naming

Naming is deferred. The codebase and documentation use the working title "Modular Event Communications Runtime" or the shorthand "the runtime." Several name candidates exist for reference, but naming decisions are not a blocker for architecture or implementation.

Example candidates:

- **Nexus Relay** (emphasizes interconnection)
- **Eventway** (emphasizes the pipeline metaphor)
- **Meshbridge** (retains the mesh networking context)
- **Conduit** (neutral, pipe metaphor)
- **Crossnet** (cross-network bridging)

Configuration, data, and state paths will use the final project name at release time. No paths are hardcoded to any legacy tool's conventions.


## 3. Design Principles

### 3.1 Event-First, Not Message-First

Messages (text, images, files) are one `event_kind` among many. The pipeline handles telemetry, metrics, presence, system signals, and plugin events identically. Adapters decide how to render each event kind for their platform.

### 3.2 Immutability

Canonical events are append-only records. Enrichment, transformation, and policy evaluation create new derived events that reference their parent via `parent_event_id`. No code path modifies an event after creation.

### 3.3 Transport Agnostic

No adapter is special-cased in core. Meshtastic is not the "source of truth." Matrix is not the "primary interface." Both are adapters with defined roles.

### 3.4 Pipeline over Callback

Events flow through explicit stages: ingress, canonical log, enrichment, transform, policy, routing, delivery planning, adapter execution, receipt. Each stage is inspectable, testable, and replaceable. No adapter directly calls another adapter.

### 3.5 Schema Evolution over Schema Lock

Unknown fields in events are preserved, not stripped. Known fields keep their meaning. Deprecation follows time-bounded windows. Adapters declare the schema version they understand.

### 3.6 Storage Authoritative

The canonical event log in storage is the single source of truth. Metadata embedded in external platforms (Matrix custom content fields, Discord embeds) is secondary and may be lost due to redaction, pruning, or platform API changes.


## 4. Architecture

### 4.1 Pipeline Overview

```
[Adapters] --> ingress --> canonical event log
                              |
                         enrichment
                              |
                       transform pipeline
                              |
                        policy pipeline
                              |
                     routing / fanout
                              |
                      delivery planning
                              |
                   adapter queues / execution
                              |
                    receipts / correlation
```

### 4.2 Stage Descriptions

| Stage | Responsibility |
|---|---|
| **Ingress** | Adapters receive raw data from their native transport/protocol and emit a preliminary canonical event. No transformation happens here. The adapter wraps what it received. |
| **Canonical Event Log** | The event is persisted to storage with a unique ID, timestamp, and initial schema version. This is the immutable record. |
| **Enrichment** | Supplementary data is attached: identity resolution, geo lookups, radio metadata normalization, source adapter state. Produces a derived event. |
| **Transform Pipeline** | Derived events are converted into target event kinds. A telemetry event becomes a presentation-ready message event, a metrics update event, a database-only event, etc. Each transform declares input/output event kinds. |
| **Policy Pipeline** | Transformed events pass through rate limiting, content filtering, permission checks, and user-configurable rules. Events may be dropped, modified (producing new derived events), or flagged. |
| **Routing / Fanout** | The routing engine determines which adapters should receive this event. Fanout logic handles one-to-many delivery, channel mapping, and bridge group resolution. |
| **Delivery Planning** | For each target adapter, a delivery plan is constructed: primary delivery method, fallback chain, retry strategy, ordering constraints, deduplication scope. |
| **Adapter Queues / Execution** | Each adapter has an inbound/outbound queue. Delivery plans are dequeued and executed respecting adapter rate limits, connection state, and priority. |
| **Receipts / Correlation** | Delivery results are recorded and correlated back to the originating event. Failed deliveries trigger fallback plans or dead-letter processing. |

### 4.3 Data Flow Constraints

- Events flow in one direction through the pipeline. No cycles.
- Adapters never call other adapters directly.
- All inter-adapter communication goes through the pipeline.
- The canonical event log is the only persistent record of event history.
- Adapter state (connection status, queue depth) is tracked separately from events.


## 5. Canonical Event Model

### 5.1 Core Fields

```python
@dataclass(frozen=True)
class CanonicalEvent:
    event_id: str               # UUIDv7 for time-ordering
    event_kind: str             # "message.text", "telemetry", "presence", etc.
    schema_version: int         # Schema version this event conforms to
    timestamp: datetime         # UTC, nanosecond precision if available
    source_adapter: str         # Adapter that created this event
    source_transport_id: str    # Opaque ID from the source transport (node num, user id)
    parent_event_id: str | None # For derived events, points to origin
    lineage: list[str]          # Chain of event_ids from origin to current
    payload: dict               # Kind-specific payload (validated per event_kind)
    metadata: EventMetadata     # Structured metadata (see Section 14)
    tags: set[str]              # Freeform tags for filtering/routing
```

### 5.2 Event Kinds (Initial Registry)

| Kind | Description | Primary Producers |
|---|---|---|
| `message.text` | Plain text message from a user or node | Transport adapters |
| `message.file` | File, image, or attachment | Transport adapters |
| `telemetry` | Device telemetry (battery, voltage, position, environment) | Transport adapters |
| `position` | Location update | Transport adapters |
| `presence` | Online/offline/away status change | Transport adapters, system |
| `metrics.update` | Internal metric (queue depth, uptime, message count) | System, plugins |
| `channel.announcement` | Channel metadata change | Transport adapters |
| `system.lifecycle` | Adapter start/stop, connection state change | System |
| `plugin.event` | Plugin-generated signal (custom kind in payload) | Plugins |
| `delivery.receipt` | Result of a delivery attempt | Delivery system |
| `transform.output` | Output of a transform stage | Transform pipeline |
| `policy.action` | Policy decision (drop, flag, rate-limit) | Policy pipeline |

### 5.3 Immutability Rules

1. Once written to the canonical event log, no field of a `CanonicalEvent` changes.
2. Enrichment creates a new event with `parent_event_id` set to the original's `event_id`. The `lineage` list is appended with the parent's ID.
3. Transforms create new events referencing their input event as parent.
4. The original event is always recoverable by following the lineage chain backward.
5. Event IDs are UUIDv7 for natural time ordering and uniqueness.


## 6. Event Transforms

### 6.1 Purpose

Transforms convert events from one representation to another. They are the glue between the raw event world (what came off the radio wire) and the presentation world (what shows up in a chat room).

Transforms are not plugins. Plugins observe or emit events through the plugin API. Transforms are pipeline stages that modify event representations: enrichment adds context, normalization maps native fields into canonical namespaces, policy transforms apply rules, and delivery transforms adapt events for a specific target adapter. The transform pipeline runs between enrichment and delivery planning, producing the derived events that routing and planning operate on.

### 6.2 Transform Interface

```python
class EventTransform(Protocol):
    input_kinds: set[str]       # What event kinds this transform accepts
    output_kinds: set[str]      # What event kinds this transform produces

    async def transform(self, event: CanonicalEvent) -> list[CanonicalEvent]:
        """Produce zero or more derived events from the input."""
        ...
```

### 6.3 Built-in Transform Examples

| Transform | Input | Output | Notes |
|---|---|---|---|
| **TelemetryToMessage** | `telemetry` | `message.text` | Formats telemetry as a human-readable summary for presentation adapters. Configurable template. |
| **TelemetryToMetrics** | `telemetry` | `metrics.update` | Extracts numeric values for internal observability storage. |
| **TelemetryToDatabaseOnly** | `telemetry` | (no output, tagged `storage-only`) | Marks telemetry for storage but not delivery to any presentation adapter. |
| **PositionToMapUpdate** | `position` | `plugin.event` (kind: map) | Feeds position data to map visualization plugins. |
| **MessageToDiscordEmbed** | `message.text` + routing metadata | `message.text` with Discord-specific formatting | Adds embed structure, webhook payload hints. |
| **MessageToMatrixFormatted** | `message.text` + routing metadata | `message.text` with Matrix HTML hints | Adds org namespace custom content fields for Matrix. |
| **MatrixToMeshCoreText** | `message.text` with Matrix HTML | `message.text` plain | Strips Matrix HTML formatting for MeshCore 160-byte text delivery. |
| **ReplyFallbackRendering** | `message.text` with relation metadata | `message.text` with fallback prefix | Renders reply context as inline text prefix when target adapter lacks native reply support (e.g., `[Alice] re: original msg > reply text`). |
| **MeshCoreTruncation** | `message.text` (long) | `message.text` (truncated) | Truncates or splits messages exceeding adapter byte limits (e.g., 160 bytes for MeshCore). |
| **LXMFFieldEmbedding** | `message.text` + relation metadata | `message.text` with LXMF fields dict | Embeds canonical event ID, relation, and schema metadata into LXMF `fields` dict for framework-aware LXMF peers. |
| **PluginEventRouter** | `plugin.event` | Varies | Routes plugin outputs to the correct event kind based on plugin type. |

### 6.4 Transform Chain Ordering

Transforms are ordered by a dependency graph. If Transform A produces an event kind that Transform B consumes, A runs before B. Cycles are detected at configuration load time and rejected.

### 6.5 Fan-out in Transforms

A single input event may produce multiple output events of different kinds. For example, a `telemetry` event can fan out to:
- A `message.text` event (for presentation adapters)
- A `metrics.update` event (for storage)
- A `plugin.event` event (for trigger plugins)

All outputs share the same `parent_event_id` and include the input in their `lineage`.


## 7. Policy Pipeline

### 7.1 Purpose

Policies are rules that govern whether, how, and where events are delivered. They run after transforms, operating on the final derived events.

### 7.2 Policy Interface

```python
class Policy(Protocol):
    priority: int               # Execution order (lower runs first)

    async def evaluate(self, event: CanonicalEvent, context: PolicyContext) -> PolicyResult:
        """Return a policy decision for this event."""
        ...
```

### 7.3 Policy Result

```python
@dataclass
class PolicyResult:
    action: Literal["pass", "drop", "flag", "rate_limit", "transform"]
    reason: str
    modified_event: CanonicalEvent | None  # If action is "transform"
    rate_limit_key: str | None             # If action is "rate_limit"
    cooldown_seconds: float | None         # If action is "rate_limit"
```

### 7.4 Built-in Policies

- **RateLimitPolicy**: Per-source, per-channel, per-event-kind rate limiting with configurable windows.
- **ContentFilterPolicy**: Regex or keyword-based content filtering with allow/deny lists.
- **PermissionPolicy**: Checks if the source identity has permission to send to the target route.
- **DeduplicationPolicy**: Suppresses duplicate events within a configurable time window based on content hashing.
- **QuietHoursPolicy**: Suppresses non-urgent deliveries during configured quiet hours per route.
- **MaxLengthPolicy**: Truncates or splits messages that exceed adapter limits (e.g., MeshCore 160 bytes).


## 8. Routing and Delivery Planning

### 8.1 Routing

The routing engine maps events to target adapters and channels/routes.

```python
@dataclass
class Route:
    route_id: str
    source_pattern: str        # Glob pattern matching source adapter or event kind
    target_adapter: str        # Adapter instance name
    target_channel: str        # Adapter-specific channel/room/topic
    priority: int              # Delivery priority (lower = higher priority)
    enabled: bool
    filters: dict              # Additional filter criteria (tags, metadata values)
```

Routes are configured by the operator. The routing engine evaluates all matching routes for each event, producing a list of target deliveries.

### 8.2 Fanout

The `fanout.py` module handles one-to-many delivery:

- A single event may match multiple routes (e.g., a message bridged to both Matrix and Discord).
- Each target gets its own delivery plan.
- Fanout is configurable: broadcast (all matches), round-robin, weighted, or first-available.

### 8.3 Delivery Planning

The `delivery_plan.py` module constructs a plan for each target. The planner receives candidate destinations from the router and determines rendering, relation fallback, truncation, metadata embedding, capability downgrade, multi-destination fanout rendering, and protocol-specific transformations for each destination.

Core planning modules:

| Module | Responsibility |
|---|---|
| `delivery_plan.py` | Constructs `DeliveryPlan` instances with primary strategy, fallback chain, retry policy, ordering, and dedup scope |
| `relation_resolution.py` | Maps reply threading, reactions, and edit correlation across adapters. Falls back to inline text when the target lacks native support |
| `capability_fallback.py` | Degrades event features based on target adapter capabilities (e.g., drops reactions when adapter reports `reactions: false`, renders edits as new messages when `edits: metadata_native_or_fallback`) |
| `rendering.py` | Produces the final rendered payload for each adapter from the planned event, applying formatting, truncation, and metadata embedding |
| `fanout.py` | Handles one-to-many delivery (see Section 8.2), ensuring each destination gets its own delivery plan |
| `transforms.py` | Delivery transforms that adapt event representations for specific protocol requirements (e.g., LXMF field embedding, Matrix HTML stripping, MeshCore truncation) |

```python
@dataclass
class DeliveryPlan:
    plan_id: str
    event_id: str              # Event being delivered
    target_adapter: str
    target_channel: str
    primary_strategy: DeliveryStrategy
    fallback_chain: list[DeliveryStrategy]  # Ordered fallback attempts
    retry_policy: RetryPolicy
    ordering_key: str | None   # For in-order delivery within a group
    deduplication_scope: str   # Scope for delivery dedup
    deadline: datetime | None  # Maximum time to keep attempting delivery
```

### 8.4 Fallback Resolution

`fallback_resolution.py` handles what happens when primary delivery fails:

1. Try primary strategy.
2. If it fails, consult the fallback chain.
3. If all fallbacks fail, mark the event as `dead_lettered`.
4. Fallbacks may include: retry with delay, deliver to alternative channel, convert to lower-fidelity format, store for later delivery.

### 8.5 Relation Resolution

`relation_resolution.py` handles reply threading, reactions, and edit correlation across adapters:

- A reply on Matrix needs to be correlated back to the originating mesh message.
- A reaction on Discord may need to be represented differently on Matrix (or dropped if the target transport has no reaction support).
- Relations are tracked in storage as a mapping between canonical event IDs and adapter-specific message IDs.


## 9. Adapter Contract

### 9.1 Adapter Roles

Adapters are categorized by their primary function:

| Role | Description | Examples |
|---|---|---|
| **TRANSPORT** | Moves data to/from a physical or logical transport layer. Handles protocol specifics, connection management, and raw data encoding/decoding. | Meshtastic, MeshCore, LXMF, MQTT, TCP serial bridge, AX.25 |
| **PRESENTATION** | Presents events to human users. Handles formatting, rich content, threading, reactions, and user interaction. | Matrix, Discord, Telegram, Slack, Web UI |
| **HYBRID** | Both transports and presents. Can act as a message source and a display target simultaneously. | IRC, XMPP |

### 9.2 Adapter Interface

```python
class Adapter(Protocol):
    name: str                   # Unique adapter instance name
    adapter_role: AdapterRole   # TRANSPORT, PRESENTATION, or HYBRID
    supported_event_kinds: set[str]  # Event kinds this adapter can handle
    rate_limits: RateLimitConfig     # Adapter-specific rate limit configuration

    async def start(self, context: AdapterContext) -> None:
        """Initialize the adapter. Given context provides event bus access, storage, config."""
        ...

    async def stop(self, timeout: float) -> None:
        """Gracefully shut down. Drain queues within timeout."""
        ...

    async def receive(self, raw_data: bytes | dict, metadata: dict) -> CanonicalEvent:
        """Convert raw transport data into a preliminary canonical event. Called by ingress."""
        ...

    async def deliver(self, event: CanonicalEvent, plan: DeliveryPlan) -> DeliveryReceipt:
        """Deliver a transformed event to this adapter's target. Return a receipt."""
        ...

    async def health_check(self) -> AdapterHealth:
        """Return current health status."""
        ...
```

### 9.3 Adapter Lifecycle

Adapters are managed by a lifecycle controller that handles:

1. **Initialization**: Load config, establish connections, register with the event bus.
2. **Running**: Process ingress and deliver events. Report health.
3. **Degraded**: Connection lost or partial failure. Queue events for later delivery.
4. **Draining**: Graceful shutdown in progress. Complete in-flight deliveries, reject new ones.
5. **Stopped**: Fully shut down.

### 9.4 Adapter Context

Each adapter receives an `AdapterContext` providing controlled access to runtime services:

```python
@dataclass
class AdapterContext:
    config: dict                      # Adapter-specific configuration
    event_bus: EventBus                # For emitting events into the pipeline
    storage: StorageBackend            # For querying historical events
    identity_resolver: IdentityResolver # For resolving native IDs to canonical actors
    logger: BoundLogger                # Structured logger with adapter context
    schema_registry: SchemaRegistry    # For looking up event schemas
```

Adapters do not get direct access to other adapters. All communication goes through the event pipeline.


## 10. Delivery Results and Receipts

### 10.1 Receipt Model

Every delivery attempt produces a receipt:

```python
class DeliveryStatus(str, Enum):
    ACCEPTED = "accepted"         # Adapter accepted the event for delivery
    QUEUED = "queued"             # Event is queued, delivery pending
    SENT = "sent"                 # Event was sent to the external platform
    ACKNOWLEDGED = "acknowledged" # External platform confirmed receipt
    FAILED = "failed"             # Delivery failed, will retry per plan
    DEAD_LETTERED = "dead_lettered" # All delivery attempts exhausted
```

### 10.2 Receipt Record

```python
@dataclass(frozen=True)
class DeliveryReceipt:
    receipt_id: str
    event_id: str
    delivery_plan_id: str
    target_adapter: str
    status: DeliveryStatus
    timestamp: datetime
    adapter_message_id: str | None   # Platform-specific message ID (e.g., Matrix event ID)
    error: str | None                # Error details if failed
    retry_count: int
    next_retry_at: datetime | None
```

### 10.3 Receipt Processing

- Receipts are written to storage as `delivery.receipt` events.
- The correlation engine links receipts back to originating events via the delivery plan.
- Dead-lettered events trigger alerts and are available for manual reprocessing.
- Receipt metrics feed into observability (delivery latency, success rates, retry counts).


## 11. Identity Model

### 11.1 Identity Concepts

The identity model bridges between native transport identities and canonical actors within the runtime. Each transport has its own identity scheme: Matrix MXIDs, Meshtastic node numbers, MeshCore public keys, Discord user IDs, and LXMF hashes are distinct native references. The identity layer reconciles these into canonical actors without assuming any two native IDs represent the same entity.

The `core/identity/` package handles actor reconciliation, identity linking, aliasing, canonical actor resolution, trust and verification states, and per-platform native identity mappings. No native ID from any transport is treated as a universal identifier. All reconciliation is explicit and operator-auditable.

```python
@dataclass
class NativeIdentity:
    """Identity as it exists on a specific transport."""
    adapter: str                # Adapter name (e.g., "meshcore-radio-1", "lxmf-node-a")
    native_id: str              # Transport-specific ID (node number, MXID, source hash)
    native_name: str | None     # Display name on the transport
    native_metadata: dict       # Transport-specific identity data

@dataclass
class CanonicalActor:
    """Resolved identity within the runtime. May link multiple native identities."""
    actor_id: str               # Runtime-unique actor ID
    display_name: str
    linked_identities: list[NativeIdentity]
    verification_status: str    # "verified", "manual", "auto", "unverified"
    permissions: set[str]       # Granted permissions
    created_at: datetime
    last_seen_at: datetime
```

Native identity examples by transport:

| Transport | native_id | native_metadata keys |
|---|---|---|
| Meshtastic | Node number (string) | `node_num`, `short_name`, `long_name`, `hw_model` |
| MeshCore | Public key (hex string) | `pubkey`, `short_name`, `role` |
| Matrix | MXID (e.g., `@user:server.org`) | `displayname`, `avatar_url` |
| Discord | User ID (string) | `username`, `discriminator` |
| LXMF | Source hash (16-byte hex) | `source_hash`, `destination_hash`, `reticulum_identity_hash` |

### 11.2 Identity Resolution Flow

1. An event arrives with a `source_transport_id`.
2. The identity resolver looks up any existing `CanonicalActor` linked to this native identity.
3. If found, the actor ID is attached to the event during enrichment.
4. If not found, a new actor is created with `verification_status: "unverified"` and the native identity is linked.
5. Operators can manually merge native identities into a single canonical actor.
6. Auto-linking rules can be configured (e.g., match by callsign across transports).

### 11.3 Identity States

| State | Meaning |
|---|---|
| **Verified** | Operator has confirmed this identity mapping. |
| **Manual** | Operator created or edited this mapping. |
| **Auto** | System auto-linked based on configurable rules. Subject to operator review. |
| **Unverified** | No mapping exists. The native identity is treated as a standalone actor. |

### 11.4 Permission Implications

- Unverified identities may be restricted to certain channels or rate limited more aggressively.
- Verified identities can be granted elevated permissions (e.g., admin commands, cross-channel posting).
- Permission policies in Section 7 evaluate against the canonical actor, not the native identity.
- Each linked native identity can have transport-specific permission overrides.


## 12. Storage and Canonical Event Log

### 12.1 Initial Backend: SQLite

The initial storage backend is SQLite. File location is configurable, defaulting to a platform-appropriate data directory (e.g., `$XDG_DATA_DIR/<project>/events.db` on Linux, `%APPDATA%\<project>\events.db` on Windows).

SQLite is chosen for:
- Zero-configuration deployment.
- Single-file portability.
- Sufficient performance for typical mesh network event volumes.
- Built-in WAL mode for concurrent reads.

### 12.2 Storage Schema (Conceptual)

```sql
CREATE TABLE canonical_events (
    event_id TEXT PRIMARY KEY,
    event_kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    timestamp TEXT NOT NULL,         -- ISO 8601 with nanoseconds
    source_adapter TEXT NOT NULL,
    source_transport_id TEXT,
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

CREATE TABLE delivery_receipts (
    receipt_id TEXT PRIMARY KEY,
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

CREATE TABLE native_archive (
    archive_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    adapter TEXT NOT NULL,
    raw_data BLOB NOT NULL,          -- Compressed (zstd or gzip)
    compression TEXT NOT NULL DEFAULT 'gzip',
    archived_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
```

### 12.3 Future Backends

The storage interface is abstracted to allow future backends:

- **PostgreSQL**: For high-volume deployments with concurrent writers.
- **NATS JetStream**: For real-time event streaming with built-in replay.
- **Redis Streams**: For low-latency short-window replay.
- **Apache Kafka**: For large-scale distributed deployments.

The abstraction boundary is defined in `core/storage/` and follows a simple protocol:

```python
class StorageBackend(Protocol):
    async def append(self, event: CanonicalEvent) -> None: ...
    async def query(self, filter: EventFilter) -> AsyncIterator[CanonicalEvent]: ...
    async def get(self, event_id: str) -> CanonicalEvent | None: ...
    async def append_receipt(self, receipt: DeliveryReceipt) -> None: ...
    async def archive_raw(self, event_id: str, adapter: str, data: bytes) -> None: ...
```


## 13. Schema Versioning

### 13.1 Principles

1. **Unknown fields are preserved, not stripped.** If an event contains a field the current schema version doesn't define, that field is kept in the payload and ignored by core logic.
2. **Known fields keep their meaning.** A field named `voltage_mv` always means voltage in millivolts. Renaming requires a new field and a deprecation window.
3. **Adapters declare schema versions.** Each adapter states the maximum schema version it supports. The runtime handles downgrade if needed.
4. **Deprecation windows.** When a field is deprecated, it remains populated for at least one major version cycle alongside its replacement. Both fields are present during the transition.
5. **Schema negotiation.** On startup, adapters and the runtime exchange supported schema versions. The runtime uses the highest mutually supported version.

### 13.2 Version Strategy

- Schema versions are integers, monotonically increasing.
- The current schema version is stored in the event's `schema_version` field.
- The schema registry maps `(event_kind, schema_version)` to a validation function.
- Breaking changes increment the major version. Additive changes increment the minor version (tracked as decimal in the integer, e.g., v2 = second major, v3 = third).

### 13.3 Handling Unknown Versions

If a consumer encounters a schema version it doesn't recognize:
- If the version is higher (future), treat all known fields normally, ignore unknown fields.
- If the version is lower (old), populate any new fields with defaults if possible, otherwise leave them unset.


## 14. Metadata Boundaries

### 14.1 Structured Metadata

Event metadata is organized into well-defined namespaces rather than a flat namespace with transport-specific prefixes.

```python
@dataclass
class EventMetadata:
    transport: TransportMetadata | None     # How the event arrived (radio, TCP, MQTT)
    routing: RoutingMetadata | None         # Routing decisions applied
    radio: RadioMetadata | None             # Radio-specific data (frequency, SNR, RSSI, hop)
    telemetry: TelemetryMetadata | None     # Device telemetry at time of event
    native: NativeMetadata | None           # Transport-native fields not yet normalized
    custom: dict                            # Plugin/extension metadata
```

### 14.2 Namespace Definitions

| Namespace | Purpose | Example Fields |
|---|---|---|
| `metadata.transport` | Transport layer details | `protocol` (e.g., `"meshcore-tcp"`, `"lxmf"`, `"mqtt"`), `gateway_id`, `received_at`, `encoding` |
| `metadata.routing` | Routing context | `matched_routes`, `fanout_group`, `bridge_id` |
| `metadata.radio` | Radio-specific data | `frequency`, `modulation`, `snr`, `rssi`, `hop_limit`, `channel_index` |
| `metadata.telemetry` | Device state at event time | `battery_percent`, `voltage_mv`, `uptime_seconds`, `air_util_tx` |
| `metadata.native` | Unnormalized native fields | Adapter-specific raw fields that haven't been mapped to canonical fields yet |
| `metadata.custom` | Plugin/extension data | Key-value pairs from plugins, using reverse-DNS namespacing |

### 14.3 Migration from Flat Metadata

Older implementations used `metadata.meshtastic.*` as a catch-all. In this runtime:

- `metadata.meshtastic.snr` becomes `metadata.radio.snr`
- `metadata.meshtastic.channel` becomes `metadata.radio.channel_index`
- `metadata.meshtastic.from` becomes `metadata.transport.source_id`
- `metadata.meshtastic.telemetry.*` becomes `metadata.telemetry.*`

The `metadata.native` namespace serves as a temporary holding area for fields that haven't been categorized yet. The enrichment stage normalizes `metadata.native` fields into their proper namespaces when possible.


## 15. MeshCore State Machine

MeshCore (the Meshtastic ecosystem adapter) has a complex connection lifecycle that must be modeled explicitly. The adapter tracks its state through these stages:

```
DISCONNECTED --> CONNECTING --> AUTHENTICATING --> SYNCING --> READY
     ^              |              |                 |          |
     |              v              v                 v          v
     +--------------+--------------+----------+------+----> DEGRADED
     |                                           |             |
     +<------------------------------------------+             v
     |                                                       STOPPING
     +<----------------------------------------------------------+
```

### 15.1 State Definitions

| State | Description | Transitions From |
|---|---|---|
| **DISCONNECTED** | No active connection. Adapter is idle. | Initial, STOPPING, DEGRADED (reconnect abandoned) |
| **CONNECTING** | Establishing TCP/serial/Bluetooth connection to the radio. | DISCONNECTED |
| **AUTHENTICATING** | Connection established, performing MeshCore authentication handshake. | CONNECTING |
| **SYNCING** | Authenticated, syncing node database, channel config, and initial state. | AUTHENTICATING |
| **READY** | Fully operational. Sending and receiving events. | SYNCING |
| **DEGRADED** | Partially functional. Connection unstable, high latency, or missing features. Receiving events but delivery may be impaired. | READY, SYNCING |
| **DRAINING** | Graceful shutdown in progress. Completing in-flight operations. | READY, DEGRADED |
| **STOPPING** | Force stop. Aborting operations. | Any state |

### 15.2 State Transition Events

Each state change emits a `system.lifecycle` event:

```python
{
    "event_kind": "system.lifecycle",
    "payload": {
        "component": "adapter",
        "adapter": "meshcore-radio-1",
        "old_state": "CONNECTING",
        "new_state": "AUTHENTICATING",
        "reason": "TCP connection established"
    }
}
```

### 15.3 Delivery Behavior per State

| State | Ingress | Delivery |
|---|---|---|
| READY | Accept | Queue and deliver |
| DEGRADED | Accept | Queue, delay delivery, may fallback |
| SYNCING | Buffer | Buffer |
| CONNECTING / AUTHENTICATING | Buffer | Buffer |
| DISCONNECTED | Reject (emit error event) | Queue for later, apply deadline |
| DRAINING | Reject | Complete in-flight only |
| STOPPING | Reject | Abort |

### 15.4 MeshCore Constraints

These constraints inform adapter behavior and delivery planning:

- **160-byte text limit** on messages. Messages exceeding this must be split or truncated. The MaxLengthPolicy handles this.
- **Channel slot/index** model. Each message is associated with a channel index (0-7). Routing maps canonical channels to MeshCore channel indices.
- **Contacts** are addressable entities. Not all nodes are contacts. Identity resolution handles the distinction.
- **Telemetry** is broadcast periodically or on-demand. Telemetry events are transport-sourced, not user-sourced.
- **No native replies or reactions.** MeshCore has no reply threading or reaction mechanism. Relation resolution must handle this gracefully when bridging from platforms that do (Matrix, Discord).
- **No message editing.** Edits from presentation adapters are represented as new messages on MeshCore.


## 16. Matrix Metadata and Embedding

### 16.1 Namespace Convention

Metadata embedded in Matrix events uses a reverse-DNS namespace under `org.<project>.*`. Until the project is named, the placeholder `org.meshnet-framework` is used. This will be updated once the final name is chosen.

Example Matrix event content:

```json
{
    "msgtype": "m.text",
    "body": "Hello from node 1234",
    "org.meshnet-framework.event": {
        "event_id": "0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",
        "event_kind": "message.text",
        "source_adapter": "meshcore-radio-1",
        "source_transport_id": "1234",
        "metadata": {
            "native": {},
            "transport": {"protocol": "meshcore-tcp", "gateway_id": "radio-1"},
            "routing": {"matched_routes": ["mesh-to-matrix-general"]},
            "radio": {"snr": 5.2, "rssi": -78, "channel_index": 1},
            "telemetry": {}
        }
    }
}
```

When the source adapter is LXMF, the same structure carries LXMF-specific values in the appropriate namespaces rather than introducing adapter-specific top-level fields:

```json
{
    "msgtype": "m.text",
    "body": "Hello from LXMF peer",
    "org.meshnet-framework.event": {
        "event_id": "0190b2c3-d4e5-7f6a-8b9c-0d1e2f3a4b5c",
        "event_kind": "message.text",
        "source_adapter": "lxmf-node-a",
        "source_transport_id": "a1b2c3d4e5f6a7b8",
        "metadata": {
            "native": {"lxmf": {"source_hash": "a1b2c3d4e5f6a7b8", "destination_hash": "e5f6a7b8c9d0e1f2"}},
            "transport": {"protocol": "lxmf", "substrate": "reticulum", "gateway_id": "lxmf-node-a", "delivery_method": "propagated", "delivery_confirmed": true, "propagation_state": "delivered"},
            "routing": {"matched_routes": ["lxmf-to-matrix-general"]},
            "radio": {"rssi": -90, "snr": 3.1},
            "telemetry": {}
        }
    }
}
```

The metadata structure follows the same `native/transport/routing/telemetry` namespaces regardless of source adapter. Adapter-specific fields that have no canonical mapping live in `native`.

### 16.2 Storage is Authoritative

The canonical event log in storage is the single source of truth. Embedded Matrix metadata is secondary and may be:

- Lost due to Synapse redaction (redaction destroys message content).
- Unavailable if the Matrix homeserver is down.
- Incomplete if the Matrix adapter was offline when the event was processed.

Any feature that needs reliable metadata (replay, correlation, identity resolution) must read from storage, not from Matrix.

### 16.3 Redaction and Privacy

- **Redaction behavior**: Synapse redacts the `content` body of an event when redacted. The `org.meshnet-framework.event` field is part of `content` and will be destroyed. The canonical event in storage is unaffected.
- **Configurable privacy modes**:
  - `full`: Embed all metadata in Matrix events. Maximum context for users, but all metadata is lost on redaction.
  - `minimal`: Embed only `event_id` and `source_transport_id`. Users see limited context. Less data exposed on redaction.
  - `none`: Do not embed any runtime metadata in Matrix events. Matrix is purely a display surface. All correlation goes through storage.
- **Default**: `minimal`. Operators can choose based on their threat model.

### 16.4 Matrix-Specific Adapter Notes

- Custom content fields (the `org.*` namespace) are preserved by Synapse under normal operation. They are not pruned by the server.
- The Matrix adapter uses the `m.relates_to` field for threading and reactions, mapping them to the runtime's relation resolution system.
- The Matrix adapter handles HTML formatting for presentation of enriched events (telemetry summaries, position maps, etc.).


## 17. LXMF and Reticulum Adapter Notes

### 17.1 Overview

LXMF (Lightweight Extensible Message Format) is a delay-tolerant messaging protocol built on Reticulum. The LXMF adapter is a TRANSPORT adapter: it moves events to and from the Reticulum network via LXMF messages. Reticulum provides the underlying network layer (identity, routing, links, packets). Both are adapter internals. Core never exposes raw Reticulum primitives.

### 17.2 Reticulum Containment

All Reticulum internals remain inside the LXMF adapter package. The core runtime must not expose or depend on:

- Raw `RNS.Packet`, `RNS.Link`, or `RNS.Resource` objects
- `RNS.Destination` instances or direct destination addressing
- `RNS.Transport` path management or path request APIs
- `RNS.Request`/`RNS.Response` channel APIs
- Link state, resource transfer, or announce handling outside the adapter

The adapter boundary translates between Reticulum concepts and the runtime's canonical model. Reticulum initialization, identity loading and generation, `LXMRouter` setup, propagation node handling, announce handling, delivery callbacks, and path/link/resource internals are all adapter-private concerns.

### 17.3 LXMF Capabilities

| Capability | Value | Notes |
|---|---|---|
| `text` | true | Primary content in `LXMessage.content` |
| `title` | true | Subject line in `LXMessage.title` |
| `metadata_fields` | true | Arbitrary key-value via `LXMessage.fields` dict |
| `replies` | metadata_native | No Matrix-style native reply threading. Relation metadata carried in fields dict between framework-aware peers |
| `reactions` | metadata_native | Same as replies: no native mechanism, carried in fields |
| `edits` | metadata_native_or_fallback | Framework-aware peers can signal edits via fields; fallback renders edit as new message |
| `deletes` | metadata_native_or_fallback | Same pattern as edits |
| `delivery_receipts` | true | LXMF per-message delivery/failed callbacks map to core receipt system |
| `store_and_forward` | true | Propagation nodes store encrypted messages for later retrieval |
| `propagation_nodes` | true | Configurable outbound propagation node |
| `direct_messages` | true | Point-to-point encrypted delivery |
| `attachments` | future | LXMF defines `FIELD_FILE_ATTACHMENTS`, `FIELD_IMAGE`, `FIELD_AUDIO` constants, but attachment/resource handling is not implemented by LXMF itself. Application-level concern, not adapter-level. |

### 17.4 LXMF Relation Handling

LXMF does not define Matrix-style native replies, reactions, edits, or deletes. The adapter represents relations between framework-aware LXMF peers using structured data in the `LXMessage.fields` dict. When the remote peer is not framework-aware, relation resolution falls back to inline text rendering (e.g., `[Alice] re: original msg > reply text`).

### 17.5 LXMF Metadata Mapping

Canonical event metadata is embedded into LXMF messages using a namespaced field in the `fields` dict:

```python
# LXMessage.fields entry for framework-aware peers
"org.meshnet-framework.event": {
    "schema": 1,
    "canonical_event_id": "0190b2c3-d4e5-...",
    "relation": {"type": "reply", "parent_event_id": "0190a1b2-c3d4-..."},
    "source": "meshnet-framework-runtime"
}
```

The adapter may use LXMF field constants (`FIELD_EVENT`, `FIELD_CUSTOM_TYPE`, `FIELD_CUSTOM_DATA`, `FIELD_CUSTOM_META`, `FIELD_THREAD`) as implementation details for how this data is packed into the fields dict. These constants are adapter internals and are not exposed in the canonical event model.

### 17.6 LXMF Delivery Metadata

When events arrive from LXMF, the adapter normalizes delivery metadata into the canonical event's structured metadata namespaces. Core consumers should prefer the normalized `metadata.transport` fields. The `metadata.native.lxmf` namespace is reserved for LXMF-specific debugging and correlation only.

```python
# metadata.transport — normalized fields consumed by core pipeline
metadata.transport = {
    "protocol": "lxmf",
    "substrate": "reticulum",
    "delivery_method": "propagated",    # direct | propagated | opportunistic | paper
    "delivery_confirmed": True,
    "transport_encrypted": True,
    "signature_valid": True,
    "stamp_valid": True,
    "propagation_state": "delivered",   # Adapter tracks propagation node sync state
    "link_quality": {                   # When available from underlying transport
        "rssi": -90,
        "snr": 3.1,
        "q": 0.85
    }
}

# metadata.native — LXMF-specific fields for adapter debugging/correlation
metadata.native = {
    "lxmf": {
        "message_id": "abc123...",       # LXMessage.message_id (SHA-256 derived, not transmitted)
        "title": "...",                  # LXMessage.title (bytes decoded)
        "source_hash": "a1b2c3d4...",   # 16-byte hex
        "destination_hash": "e5f6a7b8...",
        "field_keys": ["org.meshnet-framework.event"]  # Top-level keys found in LXMessage.fields
    }
}
```

Link quality values (rssi, snr, q) are carried when the underlying Reticulum transport provides them. They are not guaranteed on every message.

### 17.7 LXMF Identity Mapping

LXMF identities map to native IDs as follows:

| LXMF Concept | native_id value | native_metadata key |
|---|---|---|
| Source hash | `LXMessage.source_hash` (16-byte hex string) | `source_hash` |
| Destination hash | `LXMessage.destination_hash` (16-byte hex string) | `destination_hash` |
| Reticulum identity | `RNS.Identity.hash` (hex string) | `reticulum_identity_hash` |

Source hash and destination hash are opaque native IDs. They are not assumed to correspond to any other transport's identity. Identity reconciliation follows the standard flow in Section 11.

### 17.8 LXMF Delivery Planning Considerations

Delivery planning for LXMF targets must account for:

- **Delivery method selection**: Direct, propagated, or opportunistic delivery depending on whether the destination is currently reachable and whether propagation nodes are configured.
- **Propagation delay**: Propagated delivery has no guaranteed latency. Delivery plans may use longer deadlines and different retry strategies.
- **Content size**: LXMF messages are conveyed as Reticulum resources when they exceed single-packet size. The adapter handles this internally, but delivery planning should be aware of size constraints for metadata embedding.
- **Receipt correlation**: LXMF per-message delivery and failed callbacks map directly to the core receipt system. Propagated messages may receive delivery confirmation much later than the send time.

### 17.9 LXMF Adapter Package

The adapter is organized as follows:

```
adapters/lxmf/
├── __init__.py          # Adapter registration
├── adapter.py           # Adapter protocol implementation, lifecycle
├── codec.py             # LXMessage <-> canonical event encoding/decoding
├── router.py            # LXMRouter setup, delivery callback registration
├── identity.py          # Reticulum identity management, hash mapping
├── delivery.py          # Outbound delivery, method selection, receipt handling
├── propagation.py       # Propagation node configuration and sync
├── formatting.py        # Content formatting for LXMF (title, content, fields)
├── fields.py            # Fields dict construction and parsing for framework metadata
└── connection.py        # Reticulum transport initialization, announce handling
```


## 18. Raw Native Archive Mode

### 18.1 Purpose

For debugging, compliance, or advanced analysis, operators may want to retain the raw native packets received from transports. This is distinct from the canonical event, which is a normalized representation.

### 18.2 Behavior

- Raw archiving is **opt-in** per adapter via configuration.
- Raw data is compressed (gzip by default, zstd if available) and stored in the `native_archive` table, linked to the canonical event by `event_id`.
- Raw data is **never embedded** into Matrix or other presentation events. It lives in storage only.
- Archive retention is configurable (time-based or count-based pruning).
- Archived raw data is accessible via the API and CLI for debugging.

### 18.3 Configuration Example

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
      matrix-home: false   # Don't archive Matrix raw data
```


## 19. Replay and Reprocessing

### 19.1 Canonical Event Log as Replay Source

The canonical event log supports replaying events through the pipeline. This enables:

- **Plugin changes**: A new plugin wants to process historical events.
- **Adapter changes**: A new adapter was added and should receive past events.
- **Route changes**: Routing rules changed and operators want to re-evaluate past events.
- **Debugging**: Inspecting how events would have been processed with current configuration.

### 19.2 Replay Interface

```python
class ReplayRequest:
    source: Literal["storage", "file", "stream"]
    filter: EventFilter               # Time range, event kinds, source adapter, etc.
    target_stages: list[str]           # Which pipeline stages to replay through
    target_adapters: list[str] | None  # If None, replay to all current adapters
    dry_run: bool                      # If true, log results but don't deliver
    replay_mode: Literal["reprocess", "replay_only"]
    # "reprocess": create new derived events, new receipts
    # "replay_only": deliver existing derived events to new targets
```

### 19.3 Replay Constraints

- Replay does not modify existing events. It creates new derived events and receipts.
- Replay is rate-limited to avoid overwhelming adapters.
- Replay can target specific stages (e.g., re-run transforms only, skip policy).
- Replay progress is tracked and resumable.

### 19.4 Future Streaming

The storage abstraction allows replacing SQLite-based replay with streaming backends:

- **NATS JetStream**: Subscribe to a subject and replay from a specific sequence.
- **Redis Streams**: XREAD from a consumer group starting at a specific ID.
- **Kafka**: Consume from a topic partition starting at a specific offset.

The replay interface remains the same regardless of backend.


## 20. Plugin System

### 20.1 Plugin Interface

```python
class Plugin(Protocol):
    name: str
    version: str
    api_version: int                    # Plugin API version the plugin targets
    capabilities: set[PluginCapability]  # What this plugin can do

    async def initialize(self, context: PluginContext) -> None: ...
    async def handle_event(self, event: CanonicalEvent) -> list[CanonicalEvent]: ...
    async def shutdown(self) -> None: ...
```

### 20.2 Plugin Capabilities

```python
class PluginCapability(str, Enum):
    READ_EVENTS = "read_events"           # Can observe events
    EMIT_EVENTS = "emit_events"           # Can produce new events
    READ_ROUTES = "read_routes"           # Can inspect routing config
    MODIFY_ROUTES = "modify_routes"       # Can add/remove routes
    READ_IDENTITY = "read_identity"       # Can resolve identities
    READ_STORAGE = "read_storage"         # Can query historical events
    ACCESS_TELEMETRY = "access_telemetry" # Can read telemetry data
```

### 20.3 Plugin Security Boundaries

Plugins operate within capability-scoped boundaries:

1. **Capability declaration**: Plugins declare required capabilities at load time. The runtime grants only what's declared.
2. **Route permissions**: Plugins that emit events can only send to routes the operator has explicitly allowed for that plugin.
3. **Rate limits**: Each plugin has configurable rate limits for event emission, storage queries, and API calls.
4. **Sandboxing (future)**: Plugins may optionally run in a restricted execution environment (subprocess, WASM, or container) with limited system access.
5. **API versioning**: Plugins declare the runtime plugin API version they target. The runtime supports plugins written for its own current and immediately prior major plugin API version so that plugins do not break across a single major runtime upgrade. This applies only to this runtime's native plugin API, not to any external or legacy system's plugin interface.
6. **Audit logging**: All plugin actions are logged with the plugin identity and capability used.

### 20.4 Plugin Context

```python
@dataclass
class PluginContext:
    config: dict
    event_bus: EventBus                 # Scoped to plugin's capabilities
    storage: StorageBackend             # Read-only unless READ_STORAGE capability
    identity_resolver: IdentityResolver # Scoped to READ_IDENTITY capability
    logger: BoundLogger
    plugin_id: str
    rate_limiter: RateLimiter           # Per-plugin rate limiter
```


## 21. Observability

### 21.1 Structured Logging

All pipeline stages emit structured logs with:
- Timestamp (UTC)
- Stage name
- Event ID (if applicable)
- Adapter name (if applicable)
- Duration (for processing stages)
- Outcome (success, failure, dropped)

### 21.2 Metrics

| Metric | Type | Labels |
|---|---|---|
| `events_ingressed_total` | Counter | `source_adapter`, `event_kind` |
| `events_transformed_total` | Counter | `transform_name`, `input_kind`, `output_kind` |
| `events_delivered_total` | Counter | `target_adapter`, `status` |
| `delivery_latency_seconds` | Histogram | `target_adapter` |
| `adapter_health` | Gauge | `adapter`, `state` |
| `pipeline_stage_duration_seconds` | Histogram | `stage` |
| `queue_depth` | Gauge | `adapter`, `direction` (ingress/egress) |
| `active_routes` | Gauge | `source_adapter`, `target_adapter` |

### 21.3 Tracing

Events carry a trace context through the pipeline. Each stage creates a span. Distributed tracing is supported via OpenTelemetry-compatible exporters.


## 22. Proposed Package Tree

```
<project>/
├── app/                          # Application entry point, CLI, configuration loading
│   ├── __init__.py
│   ├── main.py                   # Async entry point
│   ├── cli.py                    # Click/Typer CLI
│   └── config.py                 # Configuration loading and validation
├── core/
│   ├── __init__.py
│   ├── events/                   # Canonical event model, schema registry, event kinds
│   │   ├── __init__.py
│   │   ├── canonical.py          # CanonicalEvent dataclass
│   │   ├── kinds.py              # Event kind registry
│   │   └── schema.py             # Schema registry and validation
│   ├── routing/                  # Route matching, fanout
│   │   ├── __init__.py
│   │   ├── router.py             # Route evaluation engine
│   │   └── fanout.py             # One-to-many delivery fanout
│   ├── planning/                 # Delivery plan construction, fallback, relation resolution
│   │   ├── __init__.py
│   │   ├── delivery_plan.py      # DeliveryPlan construction
│   │   ├── fallback_resolution.py
│   │   ├── relation_resolution.py
│   │   ├── capability_fallback.py  # Capability downgrade per target adapter
│   │   ├── rendering.py            # Final payload rendering per adapter
│   │   ├── fanout.py               # Multi-destination fanout delivery
│   │   └── transforms.py           # Delivery transforms (protocol-specific adaptations)
│   ├── delivery/                 # Adapter queues, execution, receipt processing
│   │   ├── __init__.py
│   │   ├── executor.py           # Delivery execution engine
│   │   ├── queues.py             # Per-adapter queues
│   │   └── receipt_processor.py  # Receipt handling and correlation
│   ├── transforms/               # Event transform pipeline
│   │   ├── __init__.py
│   │   ├── pipeline.py           # Transform chain executor
│   │   ├── telemetry.py          # Telemetry transforms
│   │   ├── message.py            # Message transforms
│   │   └── presentation.py       # Presentation-specific transforms
│   ├── policies/                 # Policy pipeline
│   │   ├── __init__.py
│   │   ├── pipeline.py           # Policy evaluation chain
│   │   ├── rate_limit.py
│   │   ├── content_filter.py
│   │   ├── dedup.py
│   │   └── permissions.py
│   ├── storage/                  # Storage abstraction and implementations
│   │   ├── __init__.py
│   │   ├── backend.py            # StorageBackend protocol
│   │   ├── sqlite.py             # SQLite implementation
│   │   ├── replay.py             # Replay engine
│   │   └── archive.py            # Raw native archive
│   ├── lifecycle/                # Component lifecycle management
│   │   ├── __init__.py
│   │   ├── manager.py            # Adapter lifecycle manager
│   │   └── states.py             # Lifecycle state definitions
│   ├── observability/            # Logging, metrics, tracing
│   │   ├── __init__.py
│   │   ├── metrics.py            # Prometheus-compatible metrics
│   │   ├── tracing.py            # OpenTelemetry tracing setup
│   │   └── logging.py            # Structured logging config
│   └── identity/                 # Identity resolution and actor management
│       ├── __init__.py
│       ├── resolver.py           # IdentityResolver
│       ├── actor.py              # CanonicalActor model
│       └── mapping.py            # Identity mapping storage
├── adapters/                     # Adapter implementations
│   ├── __init__.py
│   ├── base.py                   # Adapter protocol and base classes
│   ├── meshcore/                 # MeshCore TRANSPORT adapter
│   │   ├── __init__.py
│   │   ├── adapter.py
│   │   └── state_machine.py
│   ├── meshtastic/               # Meshtastic TRANSPORT adapter
│   │   ├── __init__.py
│   │   ├── adapter.py
│   │   └── node_cache.py         # Node database cache and refresh
│   ├── lxmf/                     # LXMF TRANSPORT adapter (over Reticulum)
│   │   ├── __init__.py
│   │   ├── adapter.py            # Adapter protocol implementation, lifecycle
│   │   ├── codec.py              # LXMessage <-> canonical event encoding/decoding
│   │   ├── router.py             # LXMRouter setup, delivery callback registration
│   │   ├── identity.py           # Reticulum identity management, hash mapping
│   │   ├── delivery.py           # Outbound delivery, method selection, receipt handling
│   │   ├── propagation.py        # Propagation node configuration and sync
│   │   ├── formatting.py         # Content formatting for LXMF (title, content, fields)
│   │   ├── fields.py             # Fields dict construction and parsing for framework metadata
│   │   └── connection.py         # Reticulum transport initialization, announce handling
│   ├── matrix/                   # Matrix PRESENTATION adapter
│   │   ├── __init__.py
│   │   ├── adapter.py
│   │   └── embedding.py
│   ├── discord/                  # Discord PRESENTATION adapter
│   │   ├── __init__.py
│   │   └── adapter.py
│   ├── telegram/                 # Telegram PRESENTATION adapter
│   │   ├── __init__.py
│   │   └── adapter.py
│   └── mqtt/                     # MQTT TRANSPORT adapter
│       ├── __init__.py
│       └── adapter.py
├── plugins/                      # Built-in plugins and plugin host
│   ├── __init__.py
│   ├── host.py                   # Plugin loader and sandbox
│   ├── map_viz.py                # Example: map visualization plugin
│   └── alert_rules.py            # Example: alert rule plugin
├── api/                          # HTTP/WebSocket API for management
│   ├── __init__.py
│   ├── server.py                 # FastAPI/Starlette server
│   ├── routes_events.py          # Event query/replay endpoints
│   ├── routes_routes.py          # Route management endpoints
│   ├── routes_adapters.py        # Adapter status/endpoints
│   └── routes_plugins.py         # Plugin management endpoints
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_canonical_events.py
    ├── test_transforms.py
    ├── test_policies.py
    ├── test_routing.py
    ├── test_delivery.py
    ├── test_identity.py
    ├── test_storage.py
    ├── test_meshcore_adapter.py
    ├── test_matrix_adapter.py
    └── test_replay.py
```


## 23. Acceptance Criteria

### 23.1 Minimum Viable Runtime (Phase 1)

- [ ] Canonical event model defined with all core fields.
- [ ] SQLite storage backend writes and reads canonical events.
- [ ] Single TRANSPORT adapter (MeshCore) ingresses raw radio data as canonical events.
- [ ] Single PRESENTATION adapter (Matrix) delivers text events to a configured room.
- [ ] Event pipeline stages execute in order: ingress, store, enrich, transform, route, deliver.
- [ ] Immutability invariant holds: no event is mutated after creation.
- [ ] Delivery receipts are recorded for every delivery attempt.
- [ ] Basic identity resolution creates canonical actors for new native identities.
- [ ] Structured logging covers all pipeline stages.
- [ ] Configuration is loaded from a single YAML file.

### 23.2 Core Feature Complete (Phase 2)

- [ ] Transform pipeline with at least 3 built-in transforms (telemetry-to-message, telemetry-to-metrics, message-to-matrix).
- [ ] Policy pipeline with rate limiting and deduplication.
- [ ] Delivery planning with fallback chains and capability downgrade.
- [ ] Relation resolution handles Matrix replies to mesh messages and LXMF metadata-native relations.
- [ ] Additional adapters: Discord or Telegram.
- [ ] MeshCore state machine handles all states and transitions.
- [ ] Metadata is properly namespaced (transport, routing, radio, telemetry).
- [ ] Raw native archive mode available (opt-in).
- [ ] Replay engine can reprocess historical events.
- [ ] Plugin host loads and executes plugins within capability boundaries.
- [ ] LXMF adapter ingresses and delivers events via LXMRouter with identity mapping (source hash, destination hash).
- [ ] LXMF metadata fields (delivery method, delivery confirmation, propagation state) are normalized into canonical event metadata.
- [ ] LXMF delivery receipts are correlated to core receipt system from per-message callbacks.

### 23.3 Production Ready (Phase 3)

- [ ] All metrics exposed via Prometheus-compatible endpoint.
- [ ] OpenTelemetry tracing through all pipeline stages.
- [ ] Management API for routes, adapters, events, plugins.
- [ ] CLI for configuration validation, event querying, replay triggering.
- [ ] Graceful shutdown drains all adapter queues.
- [ ] Schema versioning handles at least one version upgrade path.
- [ ] Documentation covers configuration, adapter setup, plugin authoring.
- [ ] Integration tests cover end-to-end flows for each adapter pair.


## 24. Phased Implementation Plan

### Phase 1: Foundation (Weeks 1-4)

Focus: Core event model, storage, single adapter pair.

| Week | Deliverables |
|---|---|
| 1 | `core/events/` package: CanonicalEvent, event kinds, schema registry. `core/storage/` SQLite backend. |
| 2 | `app/` package: CLI entry point, YAML config loading. `core/lifecycle/` manager skeleton. |
| 3 | `adapters/meshcore/` adapter: ingress from MeshCore, state machine (DISCONNECTED through READY). |
| 4 | `adapters/matrix/` adapter: delivery to Matrix room, basic metadata embedding. End-to-end integration test. |

### Phase 2: Pipeline (Weeks 5-10)

Focus: Transform, policy, routing, delivery planning.
| Week | Deliverables |
|---|---|
| 5-6 | `core/transforms/` pipeline with telemetry-to-message, telemetry-to-metrics transforms. |
| 7 | `core/policies/` pipeline with rate limiting and deduplication policies. |
| 8 | `core/routing/` route evaluation and fanout. `core/planning/` delivery plans and fallback resolution. |
| 9 | `core/delivery/` adapter queues and execution engine. Receipt processing and correlation. |
| 10 | `core/identity/` resolver and actor model. Relation resolution for cross-adapter replies. |

### Phase 3: Expansion (Weeks 11-16)

Focus: Additional adapters, plugins, observability, API.

| Week | Deliverables |
|---|---|
| 11-12 | Additional adapters: Discord or Telegram. MeshCore state machine completed (all states). |
| 13 | `plugins/` host with capability scoping. Example plugins. Raw native archive mode. |
| 14 | `core/observability/` metrics and tracing. Replay engine. |
| 15 | `api/` management endpoints. Schema versioning upgrade path. |
| 16 | Documentation, configuration examples, integration test coverage, CLI polish. |

### Phase 4: Hardening (Weeks 17-20)

Focus: Production readiness, edge cases, performance.

| Week | Deliverables |
|---|---|
| 17-18 | Graceful shutdown guarantees. Error recovery paths. Dead letter processing. |
| 19 | Performance testing under load. Storage query optimization. Queue backpressure handling. |
| 20 | Security review of plugin boundaries. API authentication. Final documentation pass. |


## 25. Future Document Split

This single document serves as the initial specification. As the project matures, it should be split into focused documents:

| Document | Content |
|---|---|
| **Architecture Spec** | Sections 3-4: Design principles, pipeline architecture, stage descriptions |
| **Canonical Event Schema** | Sections 5, 13-14: Event model, schema versioning, metadata boundaries, event kind registry |
| **Adapter Contract** | Sections 9, 15-17: Adapter interface, roles, lifecycle, MeshCore state machine, Matrix embedding, LXMF/Reticulum notes |
| **Storage Schema** | Section 12: SQLite schema, future backends, raw archive, replay |
| **Plugin API** | Section 20: Plugin interface, capabilities, security, context |
| **Routing and Policy** | Sections 7-8: Routes, fanout, delivery planning, fallback resolution, policy pipeline |
| **Identity Model** | Section 11: Native identities, canonical actors, verification, permissions |
| **Observability Guide** | Section 21: Metrics reference, tracing setup, logging conventions |
| **Behavioral Lessons** | Section 26: Operational findings from MMRelay that inform this design |
| **Configuration Reference** | YAML schema for routes, adapters, policies, plugins, storage |


## 26. Behavioral Lessons from MMRelay

This section captures operational findings from the existing MMRelay project that informed this specification. These are lessons learned, not compatibility requirements.

### 26.1 Tightly Coupled Core

MMRelay is a tightly coupled Meshtastic-to-Matrix bridge. The Meshtastic client, Matrix client, message processing, and database are all intertwined. This makes it difficult to add new transports, change storage, or test components in isolation.

**Lesson**: Strict separation between adapters, pipeline stages, and storage. Adapters know nothing about each other.

### 26.2 MeshCore Plugin Limitations

The MeshCore plugin for MMRelay revealed that adapters need first-class lifecycle, event loop, storage, and Matrix access as abstractions, not direct dependencies. The plugin had to fight the architecture to get these capabilities.

**Lesson**: Adapters and plugins receive a context object with scoped access to runtime services. No direct imports of other adapters.

### 26.3 Single Point of Failure

MMRelay's Matrix connection dropping could block Meshtastic message processing, and vice versa. A bug in one adapter affected the entire system.

**Lesson**: Each adapter has independent queues and state management. One adapter failing does not block others.

### 26.4 Message-First Design

MMRelay treats everything as a message. Telemetry, position updates, and presence changes are all shoe-horned into message-like structures. This loses semantic information and makes it hard to route telemetry differently from messages.

**Lesson**: Event-first design with typed event kinds. Transforms convert between representations as needed.

### 26.5 Metadata Loss

MMRelay embeds Meshtastic metadata into Matrix message content. When Synapse redacts a message, the metadata is lost. This breaks message correlation and debugging.

**Lesson**: Storage is authoritative. Embedded metadata is secondary and configurable.

### 26.6 Configuration Complexity

MMRelay's configuration grew organically with ad-hoc fields for each feature. There is no clear separation between transport config, Matrix config, routing config, and policy config.

**Lesson**: Configuration is structured by concern: adapters, routes, policies, storage, plugins.

### 26.7 No Replay Capability

MMRelay cannot reprocess historical messages. If a plugin is added or a route is changed, only new messages benefit.

**Lesson**: Canonical event log with replay support is a core requirement.

### 26.8 Identity Ambiguity

MMRelay has a loose mapping between Meshtastic node numbers and Matrix user IDs. There is no formal identity model, no verification, and no permission system.

**Lesson**: Explicit identity model with native identities, canonical actors, verification states, and permission evaluation.


## 27. Out of Scope

The following are explicitly out of scope for this project:

- **Legacy data migration.** Importing data from MMRelay's SQLite database, configuration files, or plugin state is not required. Users start fresh.
- **Legacy plugin compatibility.** MMRelay plugins will not work in this runtime. Plugin authors write against the new API.
- **Legacy configuration compatibility.** Configuration files follow a new schema. No migration from old MMRelay config.
- **Backward compatibility with MMRelay API.** This runtime does not expose MMRelay's API surface.
- **Binary compatibility with MeshCore plugin protocol.** The MeshCore adapter in this runtime is a new implementation.
- **Running alongside MMRelay.** This is a replacement, not a companion. Both could run simultaneously targeting different channels if needed, but no coordination between them is planned.
- **LXST (LXMF Streaming Transport).** LXST is a separate Reticulum-based streaming and media protocol, not a feature of LXMF. No media session runtime, no audio abstractions, no LXST adapter sections, and no real-time media pipeline. LXST may be evaluated later as its own adapter only if the project expands into real-time media/session events.


## 28. Appendix: Illustrative Snippets

These snippets illustrate interfaces and data structures. They are not implementation code.

### 28.1 Minimal Adapter Registration

```yaml
# config.yaml
adapters:
  meshcore-radio-1:
    type: meshcore
    role: transport
    connection:
      method: tcp
      host: 192.168.1.100
      port: 4403
    channels:
      0: "general"
      1: "emergency"
      2: "telemetry"

  matrix-home:
    type: matrix
    role: presentation
    homeserver: https://matrix.example.com
    user_id: "@relay:example.com"
    access_token: ${MATRIX_TOKEN}
    rooms:
      general: "!abc123:example.com"
      emergency: "!def456:example.com"
```

### 28.2 Route Configuration

```yaml
routes:
  - id: mesh-to-matrix-general
    source_pattern: "meshcore-radio-1:channel.general"
    target_adapter: matrix-home
    target_channel: general
    filters:
      event_kinds: ["message.text"]
    priority: 10

  - id: telemetry-to-storage
    source_pattern: "meshcore-radio-1:*"
    target_adapter: storage   # Special internal "adapter"
    filters:
      event_kinds: ["telemetry", "position"]
    priority: 5

  - id: all-to-discord
    source_pattern: "*:channel.general"
    target_adapter: discord-bot
    target_channel: "#mesh-general"
    filters:
      event_kinds: ["message.text"]
    priority: 20
```

### 28.3 Transform Registration

```yaml
transforms:
  - name: telemetry_to_message
    class: core.transforms.telemetry.TelemetryToMessage
    config:
      template: "📡 {node_name}: Battery {battery_pct}%, SNR {snr}dB, {uptime}"
      max_per_hour: 6          # Don't spam with telemetry messages

  - name: telemetry_to_metrics
    class: core.transforms.telemetry.TelemetryToMetrics
    config: {}

  - name: message_to_matrix
    class: core.transforms.presentation.MessageToMatrixFormatted
    config:
      embed_metadata: minimal  # full, minimal, none
```

### 28.4 Policy Configuration

```yaml
policies:
  - name: rate_limit_per_node
    class: core.policies.rate_limit.RateLimitPolicy
    config:
      window_seconds: 60
      max_events: 10
      scope: "source_transport_id"  # Per node
      event_kinds: ["message.text"]

  - name: dedup_messages
    class: core.policies.dedup.DeduplicationPolicy
    config:
      window_seconds: 30
      scope: "content_hash"  # Hash of payload content

  - name: meshcore_length_limit
    class: core.policies.max_length.MaxLengthPolicy
    config:
      target_adapter: meshcore-radio-1
      max_bytes: 160
      split_strategy: "truncate"  # truncate, split, reject
```

### 28.5 LXMF Adapter Configuration

```yaml
adapters:
  lxmf-node-a:
    type: lxmf
    role: transport
    reticulum:
      storage_path: ~/.reticulum/storage
      identity_path: ~/.meshnet-framework/reticulum_identity  # Auto-generated if absent
      transport_config: ~/.reticulum/config  # Reticulum transport config (interfaces, etc.)
    lxmf:
      storage_path: ~/.meshnet-framework/lxmf/storage
      propagation:
        enabled: true
        outbound_node: ""           # Auto-discover if empty, or explicit destination hash
        request_interval: 3600      # Seconds between propagation node sync checks
      announce:
        enabled: true
        interval: 7200              # Seconds between identity announces
    identity_mapping:
      auto_create_actors: true      # Create canonical actors for new LXMF source hashes
      verification_default: "unverified"

routes:
  - id: lxmf-to-matrix-general
    source_pattern: "lxmf-node-a:*"
    target_adapter: matrix-home
    target_channel: general
    filters:
      event_kinds: ["message.text"]
    priority: 10

  - id: matrix-to-lxmf
    source_pattern: "matrix-home:channel.general"
    target_adapter: lxmf-node-a
    target_channel: "default"       # LXMF uses identity-based addressing, not channels
    filters:
      event_kinds: ["message.text"]
    priority: 15
```


---

*End of specification. This document will be split into focused sub-documents as implementation progresses.*

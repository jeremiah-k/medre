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
15. [MeshCore Adapter State Machine](#15-meshcore-adapter-state-machine)
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
28. [Conceptual Configuration Schema](#28-conceptual-configuration-schema)
29. [Appendix: Illustrative Snippets](#29-appendix-illustrative-snippets)


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
[Adapters] --> ingress policy --> store source event --> enrichment
                                    |
                              semantic transforms
                                    |
                              event policy
                                    |
                                 routing
                                    |
                              route policy
                                    |
                            delivery planning
                                    |
                     delivery policy / rendering
                                    |
                          adapter execution
                                    |
                     receipts / correlation
```

### 4.2 Stage Descriptions

| Stage | Responsibility |
|---|---|
| **Ingress Policy** | Validates and filters raw inbound data before it enters the pipeline. Rejects malformed, unauthorized, or rate-limited ingress at the boundary. Prevents bad data from reaching storage. |
| **Store Source Event** | The source event is persisted to storage with a unique ID, timestamp, and initial schema version. This is the immutable record. |
| **Enrichment** | Supplementary data is attached: identity resolution, geo lookups, radio metadata normalization, source adapter state. Produces a derived event. |
| **Semantic Transforms** | Derived events are converted into target event kinds. A telemetry event becomes a presentation-ready message event, a metrics update event, a database-only event, etc. Each transform declares input/output event kinds. |
| **Event Policy** | Transformed events pass through rate limiting, content filtering, permission checks, and user-configurable rules. Events may be dropped, modified (producing new derived events), or flagged. This stage governs what content is allowed to proceed. |
| **Routing** | The routing engine determines which adapters should receive this event. Route matching evaluates structured source/target criteria, channel mapping, and bridge group resolution. |
| **Route Policy** | Per-route rules evaluated after routing but before delivery planning. Controls which matched routes actually proceed, applies per-route rate limits, quiet hours, and permission checks on the route+adapter pair. |
| **Delivery Planning** | For each surviving target, a delivery plan is constructed: primary delivery method, fallback chain, retry strategy, ordering constraints, deduplication scope. Relation resolution maps cross-adapter threading. |
| **Delivery Policy / Rendering** | Final delivery-level rules: adapter-specific content filtering, size limits, and capability downgrade. The rendering stage produces the final rendered payload for each adapter from the planned event, applying formatting, truncation, and metadata embedding. |
| **Adapter Execution** | Each adapter has an inbound/outbound queue. Delivery plans are dequeued and executed respecting adapter rate limits, connection state, and priority. |
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
    source_transport_id: str    # Native actor/source that produced the event (see notes below)
    source_channel_id: str | None  # Native channel/room/topic on source adapter, for route matching
    parent_event_id: str | None # For derived events, points to origin
    lineage: list[str]          # Chain of event_ids from origin to current
    relations: list[EventRelation]  # First-class relations to other events (replies, reactions, etc.)
    payload: dict               # Kind-specific payload (validated per event_kind)
    metadata: EventMetadata     # Structured metadata (see Section 14)
    tags: set[str]              # Freeform tags for filtering/routing
```

**`source_transport_id` identifies the native actor**, not the native message. It is the transport-specific identity of whoever or whatever produced the event. Native message IDs belong in `native_message_refs` (Section 12.2). Examples:

| Transport | source_transport_id | Notes |
|---|---|---|
| Matrix | Sender MXID (e.g., `@user:server.org`) | Who sent the Matrix event |
| LXMF | Source hash (16-byte hex) | LXMF source identity |
| Meshtastic | Node number (string) | Sending node |
| MeshCore | Node number (string) | Sending node |

**`source_channel_id`** is the native channel, room, or topic on the source adapter where the event originated. It is a core field so that route matching can evaluate source channels without pulling from metadata. For example: Matrix room ID (`!abc123:server.org`), MeshCore channel slot index, LXMF destination hash (for inbound messages the destination is the "channel"). If the transport has no channel concept, this is `None`.

### 5.2 Event Relations

Relations between events are first-class, not hidden in metadata. Every reply, reaction, edit, delete, or thread association is represented as an `EventRelation` record attached to the event.

```python
@dataclass(frozen=True)
class NativeRef:
    """Structured native reference for cross-adapter relation resolution."""
    adapter: str                    # Adapter instance name (e.g., "matrix-home")
    native_channel_id: str | None   # Native channel/room/topic on the adapter
    native_message_id: str          # Native message ID on the adapter
    native_thread_id: str | None    # Native thread/conversation ID if applicable

@dataclass(frozen=True)
class EventRelation:
    relation_type: Literal["reply", "reaction", "edit", "delete", "thread"]
    target_event_id: str | None        # Canonical event ID of the target event
    target_native_ref: NativeRef | None  # Structured native reference when canonical ID is not yet known
    key: str | None                    # Relation-specific key (e.g., emoji for reactions)
    fallback_text: str | None          # Inline text representation when target adapter lacks native support
```

Relation fields:

| Field | Purpose |
|---|---|
| `relation_type` | The semantic type of the relation. |
| `target_event_id` | The canonical event ID this relation points to. Set when the target event has been correlated. |
| `target_native_ref` | A structured `NativeRef` (adapter, native_channel_id, native_message_id, native_thread_id) identifying the native reference when the canonical event ID has not been resolved yet. The relation resolution stage resolves `target_native_ref` to `target_event_id` via the `native_message_refs` table. |
| `key` | Type-specific data. For `reaction`, this is the emoji or reaction identifier. For other types, it may carry a reason or label. |
| `fallback_text` | Inline text representation used when the target adapter does not support this relation type natively (e.g., `[Alice] re: original msg > reply text`). |

Relations are canonical. They are stored with the event and used by the relation resolution and delivery planning stages. Adapters and plugins read and write relations through the event model, not through ad-hoc metadata fields.

**Persistence rule**: The `relations` list on a `CanonicalEvent` is not duplicated inside the `canonical_events` payload or metadata columns. Relations are persisted as rows in the `event_relations` table (see Section 12.3). When a `CanonicalEvent` is loaded from storage, its in-memory `relations` list is reconstructed by querying `event_relations` for that event's ID. The `StorageBackend.store_relation` and `StorageBackend.list_relations` methods manage this (see Section 12.4).

### 5.3 Event Kinds (Initial Registry)

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
| `delivery.receipt` | Result of a delivery attempt *(system/audit event, not routeable through normal user routes unless explicitly enabled. Phase 1 stores receipts in `delivery_receipts` table rows)* | Delivery system |
| `transform.output` | Output of a transform stage *(optional audit/system event, not routeable through the normal pipeline)* | Transform pipeline |
| `policy.action` | Policy decision (drop, flag, rate-limit) *(optional audit/system event, not routeable through the normal pipeline)* | Policy pipeline |

### 5.4 Immutability Rules

1. Once written to the canonical event log, no field of a `CanonicalEvent` changes.
2. Enrichment creates a new event with `parent_event_id` set to the original's `event_id`. The `lineage` list is appended with the parent's ID.
3. Transforms create new events referencing their input event as parent.
4. The original event is always recoverable by following the lineage chain backward.
5. Event IDs are UUIDv7 for natural time ordering and uniqueness.

### 5.5 Event Record Taxonomy

Not every record in the event pipeline has the same semantic weight. The runtime distinguishes four event record classes:

| Record Class | Purpose | Storage |
|---|---|---|
| **Source Event** | The initial canonical event produced by an adapter codec from raw native data. This is the primary record of what happened on a transport. | Always stored in the canonical event log. |
| **Derived Event** | An event produced by enrichment, transform, or policy stages. It references its parent via `parent_event_id` and carries a full `lineage`. | Stored in the canonical event log if it is semantically meaningful (e.g., a telemetry-to-message transform that downstream systems act on). Transient intermediate events may be stored or discarded based on configuration. |
| **Delivery Artifact (Rendered Payload)** | The target-specific rendering of an event for a particular adapter (e.g., Matrix HTML with embedded metadata, MeshCore 160-byte truncated text, LXMF fields dict). | Stored as a `delivery_plan` / `rendered_payload` record attached to the delivery plan, not as a canonical event. These are adapter-specific renderings, not semantically independent events. |
| **Receipt Event** | A `delivery.receipt` event recording the outcome of a delivery attempt. | Phase 1: stored as rows in `delivery_receipts` table (not as canonical events). Future: may optionally mirror as canonical events for audit purposes. Receipts are semantically meaningful: they record what happened in the delivery layer. |

**Storage guidance**: The canonical event log holds source events and semantically meaningful derived events. Delivery receipts live in the `delivery_receipts` table. Target-specific renderings live as payload records on delivery plans. If a rendering is itself semantically meaningful (e.g., a message that was edited produces a new canonical event with edit semantics), it is a derived event, not a rendering artifact.


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
| **TelemetryProjection** | `telemetry` | (no output, tagged `storage-only`) | Projects telemetry into the canonical event log without producing deliverable output. Storage happens at ingress for all events; this transform marks telemetry as not intended for delivery to any presentation adapter. |
| **PositionToMapUpdate** | `position` | `plugin.event` (kind: map) | Feeds position data to map visualization plugins. |
| **MeshCoreTruncation** | `message.text` (long) | `message.text` (truncated) | Truncates or splits messages exceeding adapter byte limits (e.g., 160 bytes for MeshCore). |
| **LXMFFieldEmbedding** | `message.text` + relation metadata | `message.text` with LXMF fields dict | Embeds canonical event ID, relation, and schema metadata into LXMF `fields` dict for framework-aware LXMF peers. |
| **PluginEventRouter** | `plugin.event` | Varies | Routes plugin outputs to the correct event kind based on plugin type. |

> **Rendering vs. Transform boundary.** The following transforms produce output that is specific to a single presentation adapter's formatting model (HTML, embeds, namespace-specific metadata). These are architecturally **rendering concerns** (handled by `core/rendering/`) and must not be configured as pipeline transforms in production. They are listed here as built-in transforms for backward compatibility during development. In a future revision, they will be removed from the transform registry and relocated to `core/rendering/` as dedicated renderers. **Do not register these in the `transforms:` config block.** Use the adapter's rendering pipeline instead.
>
> | Transform | Target Adapter | Rendering Concern |
> |---|---|---|
> | **MessageToDiscordEmbed** | Discord | Discord embed structure, webhook payload hints. Moves to `core/rendering/discord.py`. |
> | **MessageToMatrixFormatted** | Matrix | Matrix HTML, `org.*` custom content fields. Moves to `core/rendering/matrix.py`. |
> | **MatrixToMeshCoreText** | MeshCore | Strips Matrix HTML for plain text. Moves to `core/rendering/meshcore.py`. |
> | **ReplyFallbackRendering** | Any (fallback) | Inline text prefix for adapters without native reply. Moves to `core/rendering/` as shared fallback renderer. |

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

Policies are rules that govern whether, how, and where events are delivered. They are split into four stages that run at different points in the pipeline, each with a distinct scope:

| Policy Stage | Pipeline Position | Scope |
|---|---|---|
| **Ingress Policy** | Before storage | Raw inbound events. Rejects malformed, unauthorized, or rate-limited ingress at the adapter boundary. Prevents bad data from reaching storage. |
| **Event Policy** | After transforms | Derived events. Rate limiting, content filtering, permission checks, deduplication. Controls what content is allowed to proceed to routing. |
| **Route Policy** | After routing | Matched routes. Per-route rate limits, quiet hours, permission checks on the route+adapter pair. Controls which matched routes actually proceed. |
| **Delivery Policy** | Before adapter execution | Delivery plans. Adapter-specific size limits, capability downgrade, final content filtering. Controls how content is rendered and delivered. |

### 7.2 Policy Interface

```python
class Policy(Protocol):
    stage: Literal["ingress", "event", "route", "delivery"]
    priority: int               # Execution order within stage (lower runs first)

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

| Policy | Stage | Description |
|---|---|---|
| **IngressValidationPolicy** | ingress | Rejects malformed events, unknown adapters, or events exceeding size limits at the ingress boundary. |
| **IngressRateLimitPolicy** | ingress | Per-adapter ingress rate limiting to prevent flooding the pipeline. |
| **RateLimitPolicy** | event | Per-source, per-channel, per-event-kind rate limiting with configurable windows. |
| **ContentFilterPolicy** | event | Regex or keyword-based content filtering with allow/deny lists. |
| **PermissionPolicy** | event | Checks if the source identity has permission to send to the target route. |
| **DeduplicationPolicy** | event | Suppresses duplicate events within a configurable time window based on content hashing. |
| **QuietHoursPolicy** | route | Suppresses non-urgent deliveries during configured quiet hours per route. |
| **RouteRateLimitPolicy** | route | Per-route rate limiting controlling how often a specific route fires. |
| **RoutePermissionPolicy** | route | Checks if the source identity has permission to use this specific route. |
| **MaxLengthPolicy** | delivery | Truncates or splits messages that exceed adapter limits (e.g., MeshCore 160 bytes). |
| **CapabilityFallbackPolicy** | delivery | Degrades event features based on target adapter capabilities (drops reactions when adapter reports `reactions: false`, renders edits as new messages when `edits: metadata_native_or_fallback`). |


## 8. Routing and Delivery Planning

### 8.1 Routing

The routing engine maps events to target adapters and channels/routes using structured source and target references, not string patterns.

```python
@dataclass
class RouteSource:
    """Structured description of where a route matches events from."""
    adapter: str | None          # Adapter instance name, or None for any
    event_kinds: list[str]       # Event kinds to match (e.g., ["message.text", "telemetry"])
    channel: str | None          # Source channel/filter, or None for any

@dataclass
class RouteTarget:
    """Structured description of where a route delivers events to."""
    adapter: str                 # Target adapter instance name
    channel: str | None          # Target channel/room/topic, or None for adapter default
    destination: RouteDestination | None  # Structured destination for identity-based addressing

@dataclass
class RouteDestination:
    """Structured destination for adapters that use identity-based addressing (e.g., LXMF)."""
    kind: Literal["channel", "lxmf_destination", "meshcore_contact", "matrix_room"]
    destination_hash: str | None     # Hash or opaque ID (e.g., LXMF destination hash)
    destination_name: str | None     # Human-readable name for config readability
    metadata: dict = field(default_factory=dict)  # Extensible destination-specific parameters

@dataclass
class Route:
    route_id: str
    from_: RouteSource          # Structured source matching criteria
    to: list[RouteTarget]       # One or more structured targets
    priority: int               # Delivery priority (lower = higher priority)
    enabled: bool
    filters: dict               # Additional filter criteria (tags, metadata values)
```

Routes are configured by the operator. The routing engine evaluates all matching routes for each event, producing a list of target deliveries.

**`channel` vs `destination` precedence rules**:

- `RouteTarget.channel` is for logical framework channel names on channel-addressed adapters (e.g., Matrix rooms by name, MeshCore channel slots by name). It maps to the adapter's own channel configuration.
- `RouteTarget.destination` is for identity/hash/contact-based addressing where the target is a specific entity, not a named channel (e.g., LXMF destination hash, MeshCore contact).
- When `destination.kind` is `"channel"` or `"matrix_room"`, the `channel` field on `RouteTarget` should be omitted. The destination carries the addressing, and the adapter resolves it internally.
- Matrix room-to-canonical-channel mapping lives in the adapter's `connection.rooms` config (see Section 29.1), not in `RouteDestination`. Routes reference channels by logical name, not by Matrix room IDs.

Example route matching a MeshCore radio to Matrix and Discord:

```python
Route(
    route_id="mesh-to-matrix-general",
    from_=RouteSource(adapter="meshcore-radio-1", event_kinds=["message.text"], channel="general"),
    to=[RouteTarget(adapter="matrix-home", channel="general")],
    priority=10,
    enabled=True,
    filters={},
)
```

Example route targeting an LXMF identity destination:

```python
Route(
    route_id="matrix-to-lxmf-peer",
    from_=RouteSource(adapter="matrix-home", event_kinds=["message.text"], channel="general"),
    to=[RouteTarget(
        adapter="lxmf-node-a",
        channel=None,
        destination=RouteDestination(
            kind="lxmf_destination",
            destination_hash="e5f6a7b8c9d0e1f2",
            destination_name="mobile-peer-1",
        ),
    )],
    priority=15,
    enabled=True,
    filters={},
)
```

### 8.2 Fanout

The router handles one-to-many delivery through the `to` list in each route:

- A single event may match multiple routes (e.g., a message bridged to both Matrix and Discord).
- Each target in the `to` list gets its own delivery plan.
- **Phase 1**: `broadcast` is the only fanout strategy. All matching targets receive the event. Round-robin, weighted, and first-available strategies are deferred to a later phase.

### 8.3 Delivery Planning

The `delivery_plan.py` module constructs a plan for each target. The planner receives candidate destinations from the router and determines rendering, relation fallback, truncation, metadata embedding, capability downgrade, multi-destination fanout rendering, and protocol-specific transformations for each destination.

Core planning modules:

| Module | Responsibility |
|---|---|
| `delivery_plan.py` | Constructs `DeliveryPlan` instances with primary strategy, fallback chain, retry policy, and deadline |
| `relation_resolution.py` | Maps reply threading, reactions, and edit correlation across adapters using `native_message_refs`. Falls back to inline text when the target lacks native support |
| `capability_fallback.py` | Degrades event features based on target adapter capabilities (e.g., drops reactions when adapter reports `reactions: false`, renders edits as new messages when `edits: metadata_native_or_fallback`) |

```python
@dataclass
class DeliveryPlan:
    plan_id: str
    event_id: str              # Event being delivered
    target: RouteTarget        # Structured target (adapter, channel, destination) — see Section 8.1
    primary_strategy: DeliveryStrategy
    fallback_chain: list[DeliveryStrategy]  # Ordered fallback attempts
    retry_policy: RetryPolicy | None
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

The adapter interface is split into two concerns: the adapter lifecycle and the codec for format conversion.

**Live adapter interface** (lifecycle and delivery):

```python
class Adapter(Protocol):
    name: str                   # Unique adapter instance name
    adapter_role: AdapterRole   # TRANSPORT, PRESENTATION, or HYBRID
    supported_event_kinds: set[str]  # Event kinds this adapter can handle
    rate_limits: RateLimitConfig     # Adapter-specific rate limit configuration

    async def start(self, context: AdapterContext) -> None:
        """Initialize the adapter. Inbound events are published via context.publish_inbound()."""
        ...

    async def stop(self) -> None:
        """Gracefully shut down."""
        ...

    async def deliver(self, result: RenderingResult) -> None:
        """Deliver a pre-rendered payload. The pipeline records receipts."""
        ...

    async def health_check(self) -> AdapterHealth:
        """Return current health status."""
        ...
```

Adapters do not implement `receive(raw_data, metadata)` as a primary contract. Instead, inbound events flow through the adapter's internal listener loop: the adapter receives native data, converts it via its codec, and publishes the canonical event by calling `ctx.publish_inbound(event)`. This keeps the adapter in control of its own receive loop and event loop integration.

**Adapter codec interface** (format conversion):

```python
class AdapterCodec(Protocol):
    """Handles conversion between native protocol data and canonical events."""

    async def decode(self, native_event: NativeEvent) -> CanonicalEvent:
        """Convert a native protocol event into a preliminary canonical event.
        Called by the adapter's inbound listener after receiving raw data."""
        ...

    async def encode(self, event: CanonicalEvent, plan: DeliveryPlan) -> NativeOutbound:
        """Convert a canonical event into a native protocol payload for delivery.
        Called by the adapter's deliver() implementation."""
        ...
```

```python
@dataclass
class NativeEvent:
    """Wrapper for raw data received from a native transport."""
    raw_data: bytes | dict       # Raw protocol data
    metadata: dict               # Transport-specific metadata (headers, connection info)
    received_at: datetime        # Timestamp when the adapter received this data

@dataclass
class NativeOutbound:
    """Rendered payload ready for delivery to a native transport."""
    payload: bytes | dict        # Protocol-specific payload
    metadata: dict               # Delivery metadata (destination, headers)
    native_message_id: str | None  # Native message ID after successful send
```

Each adapter provides its own codec implementation. The codec is an adapter-private concern, not part of the public adapter protocol. Adapters may use the codec internally in their `start()` listener loop and `deliver()` implementation.

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
    adapter_id: str                   # Unique adapter instance identifier
    event_bus: Any                    # Opaque event bus reference
    publish_inbound: Callable[[CanonicalEvent], Awaitable[None]]  # Publish inbound event into pipeline
    logger: logging.Logger            # Adapter-scoped logger
    clock: Callable[[], datetime]     # Deterministic clock hook
    shutdown_event: Any               # Graceful shutdown signal placeholder
```

Adapters do not get direct access to other adapters. All communication goes through the event pipeline.


## 10. Delivery Results and Receipts

### 10.1 Receipt Model

Every delivery attempt produces a receipt:

Phase 1 does not define a `DeliveryStatus` enum in code. Receipt status is a string literal constrained to `"accepted"`, `"queued"`, `"sent"`, `"confirmed"`, `"failed"`, or `"dead_lettered"`.

### 10.2 Receipt Record

```python
@dataclass(frozen=True)
class DeliveryReceipt:
    sequence: int = 0
    receipt_id: str
    event_id: str
    delivery_plan_id: str
    target_adapter: str
    status: Literal["accepted", "queued", "sent", "confirmed", "failed", "dead_lettered"]
    error: str | None
    adapter_message_id: str | None   # Platform-specific message ID (e.g., Matrix event ID)
    next_retry_at: datetime | None
    attempt_number: int = 1
    parent_receipt_id: str | None = None
    created_at: datetime
```

### 10.3 Receipt Processing

- Receipts are **append-only records**. Every delivery attempt produces a new `DeliveryReceipt` row in storage. Existing receipt rows are never updated or deleted. A delivery that retried three times produces four receipt rows (one per attempt), each with its own `created_at` value and `status`.
- The "current status" of a delivery is a **projection**: the latest receipt for a given `(event_id, delivery_plan_id, target_adapter)` tuple determines the current state. This projection is provided by the `delivery_status` view (Section 12.3), not by mutating receipt rows.
- **Phase 1 storage model**: Delivery receipts are stored as rows in the `delivery_receipts` table, not as `delivery.receipt` canonical events. The receipt row is the authoritative record of what happened in the delivery layer.
- `delivery.receipt` canonical events are system/audit-only records, not routeable through normal user routes unless explicitly enabled by the operator. They may be added in a later phase as optional audit mirroring of receipt rows, but they are not the primary storage mechanism.
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

### 12.2 Native Message References

Adapters operate in native protocol terms (Matrix event IDs, Meshtastic packet IDs, LXMF message IDs, MeshCore message references). The runtime must correlate these native references back to canonical event IDs. This is the foundation for cross-adapter relation resolution: a reply on Matrix referencing a Matrix event ID must resolve to the canonical event that originated from a mesh radio.

The `NativeMessageRef` concept provides this mapping:

```python
@dataclass(frozen=True)
class NativeMessageRef:
    id: int                         # Auto-increment primary key
    event_id: str                   # Canonical event ID
    adapter: str                    # Adapter instance name
    native_channel_id: str          # Native channel/room/topic on the adapter
    native_message_id: str          # Native message ID on the adapter
    native_thread_id: str | None    # Native thread/conversation ID if applicable
    native_relation_id: str | None  # Native relation reference (e.g., Matrix relates_to event ID)
    direction: Literal["inbound", "outbound"]  # Whether this ref was created on ingress or delivery
    metadata: dict                  # Adapter-specific correlation metadata
    created_at: datetime
    # UNIQUE constraint on (adapter, native_channel_id, native_message_id)
```

Storage table:

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

This table is required for:

| Transport | Native Reference | Example |
|---|---|---|
| Matrix | `native_message_id` = Matrix event ID (e.g., `$abc123`) | Reply correlation across bridges |
| Meshtastic | `native_message_id` = packet ID, `native_channel_id` = channel index | Mesh message to Matrix reply |
| MeshCore | `native_message_id` = MeshCore message reference | MeshCore to Matrix correlation |
| LXMF | `native_message_id` = LXMF message ID, source hash as `native_channel_id` | LXMF to Matrix correlation |

Relation resolution queries this table: given a `target_native_ref` (a `NativeRef` with adapter, native_channel_id, native_message_id, and optional native_thread_id) from an `EventRelation`, the resolver looks up `(adapter, native_channel_id, native_message_id)` to find the canonical `event_id`.

### 12.3 Storage Schema (Conceptual)

```sql
CREATE TABLE canonical_events (
    event_id TEXT PRIMARY KEY,
    event_kind TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    timestamp TEXT NOT NULL,         -- ISO 8601 with nanoseconds
    source_adapter TEXT NOT NULL,
    source_transport_id TEXT NOT NULL, -- Native actor/source identity (not native message ID)
    source_channel_id TEXT,          -- Native channel/room/topic on source adapter
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

CREATE TABLE event_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    relation_type TEXT NOT NULL CHECK(relation_type IN ('reply', 'reaction', 'edit', 'delete', 'thread')),
    target_event_id TEXT,                -- Canonical event ID of the target, once resolved
    target_native_adapter TEXT,
    target_native_channel_id TEXT,
    target_native_message_id TEXT,
    key TEXT,                            -- Relation-specific key (e.g., emoji for reactions)
    fallback_text TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_relations_event ON event_relations(event_id);
CREATE INDEX idx_relations_target ON event_relations(target_event_id);
CREATE INDEX idx_relations_type ON event_relations(relation_type);

-- Current delivery status is a projection from the latest receipt per delivery plan.
-- Uses MAX(sequence) for deterministic ordering (avoids timestamp collisions).
CREATE VIEW delivery_status AS
SELECT dr.* FROM delivery_receipts dr
JOIN (
    SELECT delivery_plan_id, target_adapter, MAX(sequence) AS max_seq
    FROM delivery_receipts GROUP BY delivery_plan_id, target_adapter
) latest ON dr.sequence = latest.max_seq;

CREATE TABLE plugin_state (
    plugin_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '{}',  -- JSON
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (plugin_id, key)
);

CREATE TABLE native_archive (
    archive_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES canonical_events(event_id),
    adapter TEXT NOT NULL,
    raw_data BLOB NOT NULL,          -- Compressed (zstd or gzip)
    compression TEXT NOT NULL DEFAULT 'gzip',
    archived_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Conceptual identity storage tables (Section 11).

CREATE TABLE actors (
    actor_id TEXT PRIMARY KEY,           -- Runtime-unique actor ID (UUIDv7)
    display_name TEXT NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'unverified' CHECK(verification_status IN ('verified', 'manual', 'auto', 'unverified')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE native_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    adapter TEXT NOT NULL,               -- Adapter instance name
    native_id TEXT NOT NULL,             -- Transport-specific ID
    native_name TEXT,                    -- Display name on the transport
    native_metadata TEXT NOT NULL DEFAULT '{}',  -- JSON
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(adapter, native_id)
);

CREATE TABLE actor_identity_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    native_identity_id INTEGER NOT NULL REFERENCES native_identities(id),
    link_method TEXT NOT NULL DEFAULT 'auto' CHECK(link_method IN ('verified', 'manual', 'auto')),
    linked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(actor_id, native_identity_id)
);

CREATE TABLE actor_permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    permission TEXT NOT NULL,            -- Permission name (e.g., 'admin', 'post_cross_channel')
    granted_by TEXT,                     -- 'operator' or 'auto_rule'
    granted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(actor_id, permission)
);
```

### 12.4 Future Backends

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
    async def store_native_ref(self, ref: NativeMessageRef) -> None: ...
    async def resolve_native_ref(self, adapter: str, native_channel_id: str, native_message_id: str) -> str | None: ...
    async def resolve_native_relation(self, adapter: str, native_relation_id: str) -> str | None: ...
    async def store_relation(self, event_id: str, relation: EventRelation) -> None: ...
    async def list_relations(self, event_id: str) -> list[EventRelation]: ...
```


## 13. Schema Versioning

### 13.1 Principles

1. **Unknown fields are preserved, not stripped.** If an event contains a field the current schema version doesn't define, that field is kept in the payload and ignored by core logic.
2. **Known fields keep their meaning.** A field named `voltage_mv` always means voltage in millivolts. Renaming requires a new field and a deprecation window.
3. **Adapters declare schema versions.** Each adapter states the maximum schema version it supports. The runtime handles downgrade if needed.
4. **Deprecation windows.** When a field is deprecated, it remains populated for at least one major version cycle alongside its replacement. Both fields are present during the transition.
5. **Schema negotiation.** On startup, adapters and the runtime exchange supported schema versions. The runtime uses the highest mutually supported version.

### 13.2 Version Strategy

- Schema versions are **monotonically increasing integers** (1, 2, 3, ...). Each integer represents a distinct schema revision. There is no sub-versioning: every change, whether additive or breaking, increments the integer by one.
- The current schema version is stored in the event's `schema_version` field (type `int`).
- The schema registry maps `(event_kind, schema_version)` to a validation function.
- Breaking and additive changes are both represented by a new integer. Consumers distinguish between them by comparing version numbers: a consumer that understands version N can read any version <= N. Unknown fields in higher versions are preserved but not acted on.

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


## 15. MeshCore Adapter State Machine

MeshCore is a separate mesh transport adapter with a complex connection lifecycle that must be modeled explicitly. The adapter tracks its state through these stages:

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
- **Channel slot/index model.** Each message is associated with a channel slot. The available channel slots and their ranges are adapter-discovered runtime capabilities, not fixed assumptions. Routing maps canonical channels to MeshCore channel slots discovered during the SYNCING state.
- **Contacts** are addressable entities. Not all nodes are contacts. Identity resolution handles the distinction.
- **Telemetry** is broadcast periodically or on-demand. Telemetry events are transport-sourced, not user-sourced.
- **No native replies or reactions.** MeshCore has no reply threading or reaction mechanism. Relation resolution must handle this gracefully when bridging from platforms that do (Matrix, Discord).
- **No message editing.** Edits from presentation adapters are represented as new messages on MeshCore.


## 16. Matrix Metadata and Embedding

### 16.1 Namespace Convention

Metadata embedded in Matrix events uses a reverse-DNS namespace under `org.<project>.*`. Until the project is named, the placeholder `org.medre` is used. This will be updated once the final name is chosen.

Example Matrix event content:

```json
{
    "msgtype": "m.text",
    "body": "Hello from node 1234",
    "org.medre.event": {
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
    "org.medre.event": {
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

- **Redaction behavior**: Synapse redacts the `content` body of an event when redacted. The `org.medre.event` field is part of `content` and will be destroyed. The canonical event in storage is unaffected.
- **Configurable privacy modes**:
  - `off`: Do not embed any runtime metadata in Matrix events. Matrix is purely a display surface. All correlation goes through storage.
  - `minimal`: Embed only `event_id` and `source_transport_id`. Users see limited context. Less data exposed on redaction.
  - `safe`: Embed normalized metadata (event kind, source adapter, transport protocol, radio metrics, telemetry) but never secrets or raw payloads. This is the recommended mode for operators who want visible context without security exposure.
  - `full`: Embed all metadata in Matrix events. Maximum context for users, but all metadata is lost on redaction.
- **Default**: `safe`. Operators can choose based on their threat model.

**Explicit never-embed list**: Regardless of mode, the following are never embedded in Matrix events:
- Channel keys, private keys, or access tokens
- Raw encrypted blobs or raw packets
- Raw native protocol data (Meshtastic protobuf, Reticulum packets)
- Identity private keys or signing keys
- Full raw native archive data

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
"org.medre.event": {
    "schema": 1,
    "canonical_event_id": "0190b2c3-d4e5-...",
    "relation": {"type": "reply", "parent_event_id": "0190a1b2-c3d4-..."},
    "source": "medre-runtime"
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
    "delivery_confirmed": None,         # True | False | None (None: propagated, not yet confirmed)
    "transport_encrypted": True,        # True | False | None (None: status cannot be determined)
    "signature_valid": None,            # True | False | None (None: unsigned or unverified message)
    "stamp_valid": None,                # True | False | None (None: no stamp present or not checked)
    "propagation_state": "queued",      # queued | sent | delivered | failed (adapter tracks propagation sync)
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
        "field_keys": ["org.medre.event"]  # Top-level keys found in LXMessage.fields
    }
}
```

Link quality values (rssi, snr, q) are carried when the underlying Reticulum transport provides them. They are not guaranteed on every message.

**Nullable security and delivery fields**: `transport_encrypted`, `signature_valid`, `stamp_valid`, and `delivery_confirmed` are tri-state: `True`, `False`, or `None`. `None` means the adapter could not determine the status (unsigned message, stamp not present, propagation not yet confirmed). Consumers must handle `None` as "unknown", not as "false" or "true". Examples: an opportunistic LXMF message with no signature has `signature_valid=None`; a propagated message awaiting delivery confirmation has `delivery_confirmed=None`.

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

### 17.10 LXMF Test Cases

The following test cases must pass for LXMF integration to be considered complete:

| Test Case | Description |
|---|---|
| **Inbound LXMF to Canonical Message** | An inbound `LXMessage` with text content is decoded by the codec into a `CanonicalEvent` with `event_kind="message.text"`, correct `source_adapter`, `source_transport_id` set to the source hash, and payload containing the message text. |
| **LXMF org.* Metadata to Relation Resolution** | An inbound `LXMessage` with `fields["org.medre.event"].relation` set to `{"type": "reply", "parent_event_id": "..."}` produces a `CanonicalEvent` with an `EventRelation(relation_type="reply", target_event_id="...")`. The relation is first-class, not buried in metadata. |
| **Matrix Reply to LXMF Metadata-Native Relation** | A Matrix reply (`m.relates_to` with `m.in_reply_to`) is correlated to the originating LXMF message via `native_message_refs`, producing a `CanonicalEvent` with `EventRelation(relation_type="reply", target_event_id=<canonical id of the LXMF message>)`. The LXMF adapter encodes this relation into the outbound `LXMessage.fields` dict for framework-aware peers. |
| **LXMF Delivery Callback to Receipt** | The LXMF per-message delivery callback fires with `LXMessage.state=DELIVERED`. A future adapter would append a `DeliveryReceipt` row with `status=confirmed` and store a `native_message_ref` mapping the LXMF message ID to the canonical event that was delivered. |
| **Propagated LXMF Queued/Delayed Receipt** | An outbound LXMF message sent via propagation (no direct link) would initially append a `DeliveryReceipt` row with `status=queued`. When the propagation node later confirms delivery, the adapter would append a new `DeliveryReceipt` row with `status=confirmed`. The delivery plan's deadline and retry policy govern how long to wait before appending a `status=failed` row. |
| **LXMF Unknown Peer Identity Auto-Create** | An inbound `LXMessage` from an unknown source hash triggers identity auto-creation: a new `CanonicalActor` with `verification_status="unverified"` and a `NativeIdentity` with `adapter="lxmf-node-a"`, `native_id=<source_hash>`. Subsequent messages from the same source hash resolve to the same actor. |

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
- Archived raw data is accessible via the storage backend for debugging (future: management interface and CLI).

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

> **Note:** The current Phase 1 implementation uses the `ReplayRequest` and `ReplayMode` interface defined in `docs/contracts/07-replay-event-log-contract.md`. The model below is a conceptual outline from an earlier spec revision. For the implemented interface, see the replay contract.

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
- Replay can target specific stages (e.g., re-run transforms only, skip policy).
- Replay progress tracking and rate limiting are future capabilities, not Phase 1.

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
    state: PluginStateStore             # Scoped KV store backed by plugin_state table
```

`PluginStateStore` provides scoped key-value persistence for plugins:

```python
class PluginStateStore(Protocol):
    async def get(self, key: str) -> dict | None:
        """Retrieve a JSON value by key from this plugin's scoped state."""
        ...

    async def set(self, key: str, value: dict) -> None:
        """Store a JSON value under the given key in this plugin's scoped state.
        Overwrites any existing value for the same key."""
        ...
```

Keys are scoped to `plugin_id`. Plugins cannot read or write state belonging to other plugins. The backing `plugin_state` SQL table is defined in Section 12.3.

### 20.5 Plugin Convenience APIs

Plugins can emit events through the low-level `event_bus`, but the happy path should be simple. The `PluginContext` provides convenience methods for common operations:

```python
class PluginContext:
    # ... core fields from 20.4 ...

    current_event: CanonicalEvent | None  # The event currently being handled, or None

    async def reply(self, text: str) -> None:
        """Reply to the current event being handled. Sets relation_type='reply'
        and target_event_id to the current event's ID."""
        ...

    async def send(self, text: str, target: RouteTarget | str | None = None) -> None:
        """Send a message text event. If target is a RouteTarget, routes to that
        structured target. If target is a str, it is interpreted as a route_id.
        If target is None, follows default routing."""
        ...

    async def react(self, key: str) -> None:
        """React to the current event with the given key (e.g., emoji).
        Sets relation_type='reaction' and the key field."""
        ...

    async def emit(self, kind: str, payload: dict) -> None:
        """Emit a custom event of the given kind with the given payload.
        For plugin events that don't fit reply/send/react patterns."""
        ...
```

These convenience methods internally create `CanonicalEvent` instances with appropriate `EventRelation` entries and emit them through the event bus. The low-level `event_bus` remains available for plugins that need full control over event construction.


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
│   ├── routing/                  # Route matching only (no fanout, no transforms)
│   │   ├── __init__.py
│   │   └── router.py             # Route evaluation engine
│   ├── planning/                 # Delivery planning, fallback, relation resolution
│   │   ├── __init__.py
│   │   ├── delivery_plan.py      # DeliveryPlan construction
│   │   ├── fallback_resolution.py
│   │   ├── relation_resolution.py
│   │   └── capability_fallback.py  # Capability downgrade per target adapter
│   ├── rendering/                # Target-specific payload rendering
│   │   ├── __init__.py
│   │   ├── renderer.py           # Rendering engine
│   │   ├── matrix.py             # Matrix-specific rendering (HTML, metadata embedding)
│   │   ├── meshcore.py           # MeshCore-specific rendering (truncation, plain text)
│   │   └── lxmf.py               # LXMF-specific rendering (fields dict, title/content)
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
│   │   ├── codec.py              # MeshCore native <-> canonical event encoding
│   │   └── state_machine.py
│   ├── meshtastic/               # Meshtastic TRANSPORT adapter
│   │   ├── __init__.py
│   │   ├── adapter.py
│   │   ├── codec.py              # Meshtastic protobuf <-> canonical event encoding
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
│   │   ├── codec.py              # Matrix event <-> canonical event encoding
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
├── management/                   # Management interface (future, not Phase 1)
│   ├── __init__.py
│   ├── interface.py              # Future management interface boundary
│   ├── events.py                 # Event query/replay interface (future)
│   ├── routes.py                 # Route management interface (future)
│   ├── adapters.py               # Adapter status interface (future)
│   └── plugins.py                # Plugin management interface (future)
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
    ├── test_receipt_immutability.py  # Verifies receipts are append-only and never mutated
    ├── test_meshcore_adapter.py
    ├── test_matrix_adapter.py
    ├── test_lxmf_adapter.py
    ├── test_lxmf_relations.py
    ├── test_lxmf_delivery.py
    ├── test_lxmf_identity.py
    └── test_replay.py
```


## 23. Acceptance Criteria

### 23.1 Minimum Viable Runtime (Phase 1)

- [ ] Canonical event model defined with all core fields, including `relations` list, `source_channel_id`, and `source_transport_id` (actor identity, not message ID).
- [ ] `EventRelation`, `NativeMessageRef`, and event record taxonomy defined.
- [ ] SQLite storage backend writes and reads canonical events, native refs, receipts, event relations, and identity tables.
- [ ] Fake/test adapters (TRANSPORT and PRESENTATION) exercise the full pipeline without real hardware or network.
- [ ] Structured route model with `RouteSource` and `RouteTarget` evaluates matches correctly.
- [ ] Delivery planning constructs plans with fallback chains.
- [ ] Event pipeline stages execute in order: ingress policy, store, enrich, transform, event policy, route, route policy, delivery plan, delivery policy/render, deliver.
- [ ] Immutability invariant holds: no event is mutated after creation.
- [ ] Delivery receipts are recorded for every delivery attempt.
- [ ] Receipt immutability: multiple delivery attempts for the same plan produce separate receipt rows; no receipt row is ever updated or deleted. The current delivery status is derived from the latest receipt via the `delivery_status` view, not by mutating receipt records.
- [ ] Relation resolution resolves `target_native_ref` to `target_event_id` via `native_message_refs`.
- [ ] Structured logging covers all pipeline stages.
- [ ] Configuration is loaded from a single YAML file.

### 23.2 Core Feature Complete (Phase 2)

- [ ] Transform pipeline with at least 3 built-in transforms (telemetry-to-message, telemetry-to-metrics, telemetry-projection).
- [ ] Policy pipeline with rate limiting and deduplication across all four policy stages.
- [ ] Delivery planning with fallback chains and capability downgrade.
- [ ] Relation resolution handles Matrix replies to mesh messages and LXMF metadata-native relations.
- [ ] Matrix PRESENTATION adapter: delivery to configured rooms, safe metadata embedding, `m.relates_to` mapping.
- [ ] MeshCore state machine handles all states and transitions.
- [ ] Metadata is properly namespaced (transport, routing, radio, telemetry).
- [ ] Raw native archive mode available (opt-in).

### 23.3 Mesh Transport Integration (Phase 3)

- [ ] MeshCore or Meshtastic TRANSPORT adapter ingresses raw radio data as canonical events.
- [ ] Channel slot mapping is runtime-discovered during adapter SYNCING state.
- [ ] Additional presentation adapters: Discord or Telegram.
- [ ] Identity resolution creates canonical actors for new native identities.

### 23.4 LXMF Integration (Phase 4)

- [ ] LXMF adapter ingresses and delivers events via LXMRouter with identity mapping (source hash, destination hash).
- [ ] LXMF metadata fields (delivery method, delivery confirmation, propagation state) are normalized into canonical event metadata with nullable security fields.
- [ ] LXMF delivery receipts are correlated to core receipt system from per-message callbacks.
- [ ] LXMF tests pass: inbound canonical, org.* metadata relation, Matrix reply to LXMF, delivery callback receipt, propagated queued receipt, unknown peer identity auto-create.

### 23.5 Production Ready (Phase 5)

- [ ] Plugin host loads and executes plugins within capability boundaries, including convenience APIs.
- [ ] Replay engine can reprocess historical events.
- [ ] Metrics can be exported through a production observability integration.
- [ ] OpenTelemetry tracing through all pipeline stages.
- [ ] Management interface for routes, adapters, events, plugins.
- [ ] CLI for configuration validation, event querying, replay triggering.
- [ ] Graceful shutdown drains all adapter queues.
- [ ] Schema versioning handles at least one version upgrade path.
- [ ] Documentation covers configuration, adapter setup, plugin authoring.
- [ ] Integration tests cover end-to-end flows for each adapter pair.


## 24. Phased Implementation Plan

### Phase 1: Foundation (Weeks 1-4)

Focus: Core event model, storage, fake/test adapters, routing and delivery tests.

| Week | Deliverables |
|---|---|
| 1 | `core/events/` package: CanonicalEvent with relations, source_channel_id, EventRelation, event kinds, schema registry, event record taxonomy. `core/storage/` SQLite backend with canonical_events, native_message_refs, delivery_receipts, event_relations, and identity tables (actors, native_identities, actor_identity_links, actor_permissions). |
| 2 | `app/` package: CLI entry point, YAML config loading. `core/lifecycle/` manager skeleton. `core/routing/` structured route evaluation (RouteSource, RouteTarget, RouteDestination). `core/planning/` delivery plans and fallback resolution. |
| 3 | `adapters/` base: Adapter protocol with codec pattern, AdapterContext with publish_inbound. Fake/test TRANSPORT adapter and fake/test PRESENTATION adapter. Policy pipeline split (ingress/event/route/delivery stages). |
| 4 | `core/transforms/` pipeline with basic transforms. `core/rendering/` renderer skeleton. End-to-end integration test using fake adapters through full pipeline. Delivery receipt and native ref correlation tests. |

### Phase 2: Matrix Presentation Adapter (Weeks 5-8)

Focus: Matrix adapter, delivery, metadata embedding.

| Week | Deliverables |
|---|---|
| 5-6 | `adapters/matrix/` adapter: delivery to Matrix rooms, safe metadata embedding, `m.relates_to` mapping to EventRelation. `core/rendering/matrix.py` for Matrix HTML rendering. |
| 7 | `core/identity/` resolver and actor model. Relation resolution for cross-adapter replies using `native_message_refs`. |
| 8 | Transform pipeline: telemetry-to-message, telemetry-to-metrics, telemetry-projection. Policy stages: rate limiting, deduplication, content filtering across all four stages. |

### Phase 3: Mesh Transport (Weeks 9-12)

Focus: MeshCore or Meshtastic transport adapter, channel discovery.

| Week | Deliverables |
|---|---|
| 9-10 | `adapters/meshcore/` or `adapters/meshtastic/` transport adapter: ingress from radio, state machine, codec for native-to-canonical conversion. Runtime channel slot discovery. |
| 11 | MeshCore state machine completed (all states). `core/rendering/meshcore.py` for truncation and plain text rendering. Additional presentation adapter (Discord or Telegram). |
| 12 | Cross-adapter integration tests: mesh to Matrix, Matrix to mesh, relation resolution, identity mapping. |

### Phase 4: LXMF (Weeks 13-16)

Focus: LXMF transport adapter over Reticulum.

| Week | Deliverables |
|---|---|
| 13-14 | `adapters/lxmf/` adapter: LXMRouter setup, codec (LXMessage to CanonicalEvent), identity mapping (source hash, destination hash), delivery method selection, propagation node handling. |
| 15 | `core/rendering/lxmf.py` for LXMF fields dict rendering. LXMF relation handling (metadata-native relations via fields). Nullable security metadata normalization. |
| 16 | LXMF test suite: inbound canonical event, org.* metadata relation resolution, Matrix reply to LXMF relation, delivery callback receipt, propagated queued receipt behavior, unknown peer identity auto-create. |

### Phase 5: Plugins, Replay, Production Hardening (Weeks 17-20)

Focus: Plugin system, replay engine, management interface, production readiness.

| Week | Deliverables |
|---|---|
| 17 | `plugins/` host with capability scoping, convenience APIs (reply, send, react, emit). Example plugins. Raw native archive mode. |
| 18 | Replay engine. `core/observability/` metrics and tracing. Schema versioning upgrade path. |
| 19 | Management interface boundary. CLI polish. Graceful shutdown guarantees. Error recovery paths. Dead letter processing. |
| 20 | Security review of plugin boundaries. Management interface access control. Performance testing. Final documentation pass. Integration test coverage. |


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
| **Configuration Reference** | Section 28: Conceptual config schema for routes, adapters, policies, plugins, storage, runtime, observability, API |


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


## 28. Conceptual Configuration Schema

This section provides a first-pass conceptual schema for the runtime's YAML configuration file. The schema is organized by concern. Individual sections reference specific spec sections for detailed contracts.

```yaml
# runtime.yaml — Conceptual configuration schema

runtime:
  name: "medre-relay"                    # Instance name for logging/identification
  log_level: "info"                        # debug | info | warn | error
  event_loop: "uvloop"                     # asyncio | uvloop
  graceful_shutdown_timeout: 30            # Seconds to drain on shutdown

storage:
  backend: "sqlite"                        # sqlite | postgres | future
  sqlite:
    path: "~/.medre/events.db"
    wal_mode: true
    busy_timeout: 5000
  native_archive:
    enabled: false
    compression: gzip                      # gzip | zstd
    retention:
      max_age_days: 30
      max_count: 100000
    adapters: {}                           # Per-adapter opt-in

adapters:
  <adapter-name>:
    type: <adapter-type>                   # meshcore | meshtastic | matrix | lxmf | discord | telegram | mqtt
    # role: transport | presentation | hybrid  — READ-ONLY. Inferred from adapter type at load time.
    enabled: true
    connection: {}                         # Adapter-specific connection config (see adapter sections)
    channels: {}                           # Adapter-specific channel/slot mapping
    rate_limits: {}                        # Per-adapter rate limit overrides

routes:
  - id: <route-id>
    from:
      adapter: <adapter-name> | null       # null matches any adapter
      event_kinds: [<kind>, ...]           # Event kinds to match
      channel: <channel> | null            # null matches any channel
    to:
      - adapter: <adapter-name>
        channel: <channel> | null
        destination:                       # Optional structured destination
          kind: channel | lxmf_destination | meshcore_contact | matrix_room
          destination_hash: <hash> | null
          destination_name: <name> | null
    priority: <int>
    enabled: true
    filters: {}                            # Additional filter criteria

transforms:
  - name: <transform-name>
    class: <python-class-path>
    config: {}                             # Transform-specific configuration

policies:
  ingress: []                              # Ingress-stage policies (Section 7)
  event: []                                # Event-stage policies
  route: []                                # Route-stage policies
  delivery: []                             # Delivery-stage policies

  # Each policy entry:
  # - name: <policy-name>
  #   class: <python-class-path>
  #   config: {}

plugins:
  - name: <plugin-name>
    class: <python-class-path>
    enabled: true
    capabilities: []                       # Required capabilities
    config: {}                             # Plugin-specific configuration
    rate_limits: {}                        # Per-plugin rate limits

observability:
  metrics:
    enabled: true
    port: 9090                             # Prometheus-compatible metrics export (future)
  tracing:
    enabled: false
    exporter: "otlp"                       # otlp | jaeger | none
    endpoint: "http://localhost:4317"
  logging:
    structured: true
    format: "json"                         # json | text

management:
  enabled: false                    # Future management interface, not Phase 1
  # Concrete server, access-control, and network binding settings are intentionally
  # omitted from Phase 1. The current foundation does not implement an admin API.
```

## 29. Appendix: Illustrative Snippets

These snippets illustrate interfaces and data structures. They are not implementation code.

### 29.1 Minimal Adapter Registration

```yaml
# config.yaml
adapters:
  meshcore-radio-1:
    type: meshcore
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
    homeserver: https://matrix.example.com
    user_id: "@relay:example.com"
    access_token: ${MATRIX_TOKEN}
    rooms:
      general: "!abc123:example.com"
      emergency: "!def456:example.com"
```

### 29.2 Route Configuration

```yaml
routes:
  - id: mesh-to-matrix-general
    from:
      adapter: meshcore-radio-1
      event_kinds: ["message.text"]
      channel: general
    to:
      - adapter: matrix-home
        channel: general
    priority: 10

  - id: telemetry-to-storage
    from:
      adapter: meshcore-radio-1
      event_kinds: ["telemetry", "position"]
    to:
      # NOTE: Using a transform with output tagged `storage-only` (see TelemetryProjection
      # in Section 6.3) is the preferred pattern for storing events without delivering them to
      # a presentation adapter. Routing to a fictitious "storage" adapter is an anti-pattern:
      # storage happens at the "Store Source Event" pipeline stage for every event regardless of
      # routing. This route is shown only for illustration and should not be used in production.
    priority: 5

  - id: all-to-discord
    from:
      adapter: null          # Match any adapter
      event_kinds: ["message.text"]
      channel: general
    to:
      - adapter: discord-bot
        channel: "#mesh-general"
    priority: 20
```

### 29.3 Transform Registration

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

  # NOTE: MessageToMatrixFormatted is a rendering concern, not a pipeline transform.
  # Matrix HTML formatting and metadata embedding is handled by core/rendering/matrix.py
  # during the delivery stage. Do not register it here.
```

### 29.4 Policy Configuration

```yaml
policies:
  ingress: []                              # Ingress-stage policies (see Section 7)

  event:                                   # Event-stage policies (after transforms)
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

  route: []                                # Route-stage policies (after routing)

  delivery:                                # Delivery-stage policies (before adapter execution)
    - name: meshcore_length_limit
      class: core.policies.max_length.MaxLengthPolicy
      config:
        target_adapter: meshcore-radio-1
        max_bytes: 160
        split_strategy: "truncate"  # truncate, split, reject
```

### 29.5 LXMF Adapter Configuration

```yaml
adapters:
  lxmf-node-a:
    type: lxmf
    reticulum:
      storage_path: ~/.reticulum/storage
      identity_path: ~/.medre/reticulum_identity  # Auto-generated if absent
      transport_config: ~/.reticulum/config  # Reticulum transport config (interfaces, etc.)
    lxmf:
      storage_path: ~/.medre/lxmf/storage
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
    from:
      adapter: lxmf-node-a
      event_kinds: ["message.text"]
    to:
      - adapter: matrix-home
        channel: general
    priority: 10

  - id: matrix-to-lxmf
    from:
      adapter: matrix-home
      event_kinds: ["message.text"]
      channel: general
    to:
      - adapter: lxmf-node-a
        destination:
          kind: lxmf_destination
          destination_hash: "e5f6a7b8c9d0e1f2"
          destination_name: "mobile-peer-1"
    priority: 15
```


---

*End of specification. This document will be split into focused sub-documents as implementation progresses.*

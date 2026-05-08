# Routing and Delivery Planning Contract

> Extracted from: `docs/spec/modular-event-engine-spec.md` Sections 3, 4, 7, 8, 10, 12
> This document is self-contained. An implementer can build routing and delivery planning from these definitions without re-reading the full spec.


## 1. Scope

This contract covers everything between "the pipeline has a derived event ready to deliver" and "the adapter reports back with a receipt." Specifically:

- Route data model and matching semantics
- Fanout strategy
- Delivery plan construction
- Fallback resolution
- Relation resolution (cross-adapter threading)
- Capability fallback
- The four policy stages that gate routing and delivery
- Delivery receipts, append-only semantics, and status projection
- Route configuration loading and validation

What this contract does **not** cover: adapter internals, codec implementation, transform logic, identity resolution details, or storage backend implementation.


## 2. Route Data Model

### 2.1 RouteSource

```python
@dataclass
class RouteSource:
    """Structured description of where a route matches events from."""
    adapter: str | None          # Adapter instance name, or None for any
    event_kinds: list[str]       # Event kinds to match (e.g., ["message.text", "telemetry"])
    channel: str | None          # Source channel/filter, or None for any
```

Matching rules:

| Field | Match behavior |
|---|---|
| `adapter` | Exact match on adapter instance name. `None` is a wildcard matching any adapter. |
| `event_kinds` | Event must have an `event_kind` present in this list. The list must not be empty. |
| `channel` | Exact match on the event's `source_channel_id`. `None` is a wildcard matching any channel. |

All three fields are ANDed together. A source matches only when every non-None field matches the corresponding event field.

### 2.2 RouteTarget

```python
@dataclass
class RouteTarget:
    """Structured description of where a route delivers events to."""
    adapter: str                 # Target adapter instance name (required)
    channel: str | None          # Target channel/room/topic, or None for adapter default
    destination: RouteDestination | None  # Structured destination for identity-based addressing
```

`adapter` is always required. Every delivery goes to a specific adapter instance.

`channel` and `destination` are mutually exclusive addressing modes. See Section 4 for precedence rules.

### 2.3 RouteDestination

```python
@dataclass
class RouteDestination:
    """Structured destination for adapters that use identity-based addressing (e.g., LXMF)."""
    kind: Literal["channel", "lxmf_destination", "meshcore_contact", "matrix_room"]
    destination_hash: str | None     # Hash or opaque ID (e.g., LXMF destination hash)
    destination_name: str | None     # Human-readable name for config readability
    metadata: dict = field(default_factory=dict)  # Extensible destination-specific parameters
```

The `kind` field tells the delivery planner and adapter what addressing model to use:

| kind | Addressing model | Key fields |
|---|---|---|
| `"channel"` | Logical channel name | `destination_name` holds the channel name |
| `"lxmf_destination"` | LXMF destination hash | `destination_hash` holds the 16-byte hex hash |
| `"meshcore_contact"` | MeshCore contact | `destination_hash` or `destination_name` identifies the contact |
| `"matrix_room"` | Matrix room ID | Resolved via adapter's `connection.rooms` config, not stored here |

### 2.4 Route

```python
@dataclass
class Route:
    route_id: str
    from_: RouteSource          # Structured source matching criteria
    to: list[RouteTarget]       # One or more structured targets
    priority: int               # Delivery priority (lower = higher priority)
    enabled: bool
    filters: dict               # Additional filter criteria (tags, metadata values)
```

- `route_id` must be unique across all routes. Duplicate route IDs in configuration are a startup error.
- `to` is a list of one or more targets. An empty `to` list is a configuration error.
- `priority` determines delivery ordering when multiple routes match. Lower numbers deliver first.
- `filters` provides extensible matching beyond the core fields: tag matching, metadata value checks, and other criteria the routing engine evaluates after the core field match.
- `enabled: false` means the route is loaded but never matches. Disabled routes do not participate in routing at all.

### 2.5 Configuration Representation

Routes are configured in YAML as a list:

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
    enabled: true
    filters: {}

  - id: matrix-to-lxmf-peer
    from:
      adapter: matrix-home
      event_kinds: ["message.text"]
      channel: general
    to:
      - adapter: lxmf-node-a
        channel: null
        destination:
          kind: lxmf_destination
          destination_hash: "e5f6a7b8c9d0e1f2"
          destination_name: "mobile-peer-1"
    priority: 15
    enabled: true
    filters: {}
```

A wildcard route (any adapter, any channel):

```yaml
  - id: all-text-to-discord
    from:
      adapter: null
      event_kinds: ["message.text"]
      channel: null
    to:
      - adapter: discord-bot
        channel: "#mesh-general"
    priority: 20
```


## 3. Route Matching Semantics

The routing engine evaluates **all enabled routes** against each derived event. A single event may match zero, one, or many routes. There is no first-match-wins behavior.

### 3.1 Matching Algorithm

1. Filter out routes where `enabled` is `false`.
2. For each remaining route, evaluate `from_` against the event:
   - `adapter`: event's `source_adapter` must equal this value, unless `None` (wildcard).
   - `event_kinds`: event's `event_kind` must be in this list.
   - `channel`: event's `source_channel_id` must equal this value, unless `None` (wildcard).
3. If the core fields match, evaluate `filters` (tag matching, metadata values). Implementation of filter matching is extensible.
4. Collect all matching routes.

### 3.2 Non-Exclusive Matching (Default)

Routes are **non-exclusive** by default. If an event matches routes A and B, both routes fire. The event gets delivered to all targets from both routes. There is no deduplication at the route level. Deduplication, if needed, is handled by the `DeduplicationPolicy` at the event policy stage.

### 3.3 Route Ordering

Matching routes are sorted by `priority` (ascending, lower is higher priority) before delivery plan construction. This ordering influences:
- Which delivery plans are constructed first.
- The order in which the adapter execution stage dequeues and processes deliveries.
- Policy evaluation order when `RouteRateLimitPolicy` or `RoutePermissionPolicy` apply per-route limits.

### 3.4 No Match Behavior

If an event matches zero routes, it is not delivered anywhere. It remains stored in the canonical event log and is available for replay if routes are added later. No error is raised for unroutable events. This is normal: `telemetry` events with a `storage-only` tag, `delivery.receipt` events, and `policy.action` events are typically not routed to presentation adapters.


## 4. Channel vs Destination Precedence Rules

`RouteTarget` provides two addressing modes. They are not mixed.

**Rule 1: `channel` is for channel-addressed adapters.**
Use `RouteTarget.channel` for adapters where delivery targets a logical channel, room, or slot. Examples: Matrix rooms by name, MeshCore channel slots by name, Discord channels. The adapter resolves the logical channel name to its native address internally.

**Rule 2: `destination` is for identity/hash/contact-based addressing.**
Use `RouteTarget.destination` for adapters where the target is a specific entity, not a named channel. Examples: LXMF destination hash, MeshCore contact.

**Rule 3: When `destination.kind` is `"channel"` or `"matrix_room"`, omit `channel`.**
The destination carries the addressing. The adapter resolves it internally. Setting both `channel` and `destination` with `kind="channel"` is a configuration error.

**Rule 4: Matrix room mapping is not stored in `RouteDestination`.**
Routes reference channels by logical name. The Matrix adapter's `connection.rooms` config maps logical names to Matrix room IDs. Routes never contain Matrix room IDs directly.

| Scenario | `channel` | `destination` | Example |
|---|---|---|---|
| Deliver to Matrix room "general" | `"general"` | `None` | `RouteTarget(adapter="matrix-home", channel="general")` |
| Deliver to LXMF peer | `None` | `RouteDestination(kind="lxmf_destination", ...)` | See Section 2.5 config example |
| Deliver to adapter default | `None` | `None` | `RouteTarget(adapter="meshcore-radio-1")` |
| Deliver to MeshCore channel by name | `"emergency"` | `None` | `RouteTarget(adapter="meshcore-radio-1", channel="emergency")` |


## 5. Fanout Strategy

### 5.1 Per-Route Fanout

Each route's `to` list is a fanout. A single route may target multiple adapters:

```python
Route(
    route_id="mesh-to-all",
    from_=RouteSource(adapter="meshcore-radio-1", event_kinds=["message.text"], channel="general"),
    to=[
        RouteTarget(adapter="matrix-home", channel="general"),
        RouteTarget(adapter="discord-bot", channel="#mesh-general"),
    ],
    priority=10,
    enabled=True,
    filters={},
)
```

Each target in `to` produces its own `DeliveryPlan`.

### 5.2 Cross-Route Fanout

A single event matching multiple routes fans out across all matching routes' targets. An event matching route A (with 2 targets) and route B (with 1 target) produces 3 delivery plans total.

### 5.3 Phase 1 Constraint

**Broadcast is the only fanout strategy.** All matching targets receive the event. Round-robin, weighted, and first-available strategies are deferred to a later phase. The routing engine does not filter or prioritize among matched targets within a single route. Every target in the `to` list gets a delivery plan.


## 6. DeliveryPlan

### 6.1 Dataclass

```python
@dataclass
class DeliveryPlan:
    plan_id: str
    event_id: str              # Event being delivered
    target: RouteTarget        # Structured target (adapter, channel, destination)
    primary_strategy: DeliveryStrategy
    fallback_chain: list[DeliveryStrategy]  # Ordered fallback attempts
    retry_policy: RetryPolicy
    ordering_key: str | None   # For in-order delivery within a group
    deduplication_scope: str   # Scope for delivery dedup
    deadline: datetime | None  # Maximum time to keep attempting delivery
```

The planner constructs one `DeliveryPlan` per `(event, RouteTarget)` pair.

### 6.2 DeliveryStrategy

```python
@dataclass
class DeliveryStrategy:
    method: str                # Delivery method identifier (adapter-specific)
    parameters: dict           # Method-specific parameters
    timeout: float             # Per-attempt timeout in seconds
```

`DeliveryStrategy` defines how a single delivery attempt works. The `method` field is interpreted by the target adapter. The `primary_strategy` is the first attempt. If it fails, each entry in `fallback_chain` is tried in order.

### 6.3 RetryPolicy

```python
@dataclass
class RetryPolicy:
    max_retries: int
    backoff_base: float        # Base delay in seconds for exponential backoff
    backoff_max: float         # Maximum backoff delay
    backoff_multiplier: float  # Multiplier for each retry (e.g., 2.0 for doubling)
```

### 6.4 Planning Modules

| Module | File | Responsibility |
|---|---|---|
| Delivery plan construction | `core/planning/delivery_plan.py` | Constructs `DeliveryPlan` instances with primary strategy, fallback chain, retry policy, ordering, and dedup scope |
| Relation resolution | `core/planning/relation_resolution.py` | Maps reply threading, reactions, and edit correlation across adapters using `native_message_refs`. Falls back to inline text when the target lacks native support |
| Capability fallback | `core/planning/capability_fallback.py` | Degrades event features based on target adapter capabilities |


## 7. Fallback Resolution

When primary delivery fails, the fallback resolution chain executes in order:

1. Try `primary_strategy`.
2. If it fails, try each `DeliveryStrategy` in `fallback_chain` in sequence.
3. If all fallbacks fail, mark the event as `dead_lettered`.
4. Fallback types may include:
   - Retry with delay (same adapter, same strategy, after backoff)
   - Deliver to alternative channel (same adapter, different channel)
   - Convert to lower-fidelity format (e.g., strip rich content, send plain text)
   - Store for later delivery (queue until adapter recovers)

The fallback chain is part of the `DeliveryPlan`. It is constructed at planning time, not at execution time. The executor walks the chain and reports receipts for each attempt.


## 8. Relation Resolution

### 8.1 Purpose

Cross-adapter relations require mapping native message IDs to canonical event IDs. A reply on Matrix referencing a Matrix event ID must resolve to the canonical event that originated from a mesh radio. A reaction on Discord may need different representation on Matrix.

### 8.2 Resolution via native_message_refs

The `native_message_refs` storage table provides the mapping:

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

Given a `target_native_ref` from an `EventRelation`:

```python
@dataclass(frozen=True)
class NativeRef:
    adapter: str
    native_channel_id: str | None
    native_message_id: str
    native_thread_id: str | None
```

The resolver queries `native_message_refs` by `(adapter, native_channel_id, native_message_id)` to find the canonical `event_id`.

### 8.3 Resolution Flow

1. An `EventRelation` arrives with either `target_event_id` (already resolved) or `target_native_ref` (not yet resolved).
2. If `target_event_id` is set, no resolution needed. The relation is ready.
3. If `target_native_ref` is set, the resolver queries storage: `resolve_native_ref(adapter, native_channel_id, native_message_id)`.
4. If a match is found, `target_event_id` is set on a derived relation. The event proceeds.
5. If no match is found (the referenced event hasn't been seen yet), the relation retains `target_native_ref`. Delivery falls back to inline text rendering using `fallback_text` from the `EventRelation`.
6. The relation may be reprocessed later when the referenced event arrives.

### 8.4 Fallback Rendering

When the target adapter lacks native support for a relation type, or when the canonical event ID cannot be resolved, the `fallback_text` field provides an inline text representation. Example: `[Alice] re: original msg > reply text`.


## 9. Capability Fallback

The capability fallback module degrades event features based on what the target adapter supports.

### 9.1 How It Works

Each adapter declares capabilities (e.g., `reactions: true`, `edits: "metadata_native_or_fallback"`). During delivery planning, the capability fallback module checks the event's relations and payload against the target adapter's capabilities:

| Adapter capability | Fallback when unsupported |
|---|---|
| `reactions: false` | Drop reaction relations entirely, or render as inline text |
| `edits: "metadata_native_or_fallback"` | Render edit as a new message (no native edit support) |
| `replies: false` | Render reply as plain text with `fallback_text` prefix |
| Text length limit exceeded | Truncate or split per `MaxLengthPolicy` |

### 9.2 Fallback as Policy

The `CapabilityFallbackPolicy` (delivery stage) applies these downgrade rules. It is the last policy check before the adapter executes the delivery. This means capability fallback is a delivery policy concern, not a routing concern. The router does not filter routes based on adapter capabilities. If a route matches, the delivery plan is constructed. Capability fallback happens during delivery policy evaluation.


## 10. Policy Pipeline Split

Policies are split into four stages that run at distinct pipeline positions. Each stage has a different scope and evaluates different context.

### 10.1 Stage Definitions

| Stage | Pipeline Position | Scope | What It Controls |
|---|---|---|---|
| **ingress** | Before storage | Raw inbound events | Rejects malformed, unauthorized, or rate-limited ingress at the adapter boundary. Prevents bad data from reaching storage. |
| **event** | After transforms | Derived events | Rate limiting, content filtering, permission checks, deduplication. Controls what content is allowed to proceed to routing. |
| **route** | After routing, before delivery planning | Matched routes | Per-route rate limits, quiet hours, permission checks on the route+adapter pair. Controls which matched routes actually proceed. |
| **delivery** | Before adapter execution, after delivery planning | Delivery plans | Adapter-specific size limits, capability downgrade, final content filtering. Controls how content is rendered and delivered. |

### 10.2 Policy Interface

```python
class Policy(Protocol):
    stage: Literal["ingress", "event", "route", "delivery"]
    priority: int               # Execution order within stage (lower runs first)

    async def evaluate(self, event: CanonicalEvent, context: PolicyContext) -> PolicyResult:
        """Return a policy decision for this event."""
        ...
```

### 10.3 Policy Result

```python
@dataclass
class PolicyResult:
    action: Literal["pass", "drop", "flag", "rate_limit", "transform"]
    reason: str
    modified_event: CanonicalEvent | None  # If action is "transform"
    rate_limit_key: str | None             # If action is "rate_limit"
    cooldown_seconds: float | None         # If action is "rate_limit"
```

### 10.4 Routing-Relevant Policies

These policies directly affect routing and delivery:

| Policy | Stage | Effect on Routing/Delivery |
|---|---|---|
| `RouteRateLimitPolicy` | route | Skips a matched route if the rate limit for that route+adapter pair is exceeded |
| `RoutePermissionPolicy` | route | Drops a matched route if the source identity lacks permission to use this specific route |
| `QuietHoursPolicy` | route | Suppresses non-urgent deliveries during configured quiet hours per route |
| `MaxLengthPolicy` | delivery | Truncates or splits messages exceeding adapter limits before delivery |
| `CapabilityFallbackPolicy` | delivery | Degrades event features based on target adapter capabilities |

### 10.5 Pipeline Flow for Routing

The full routing and delivery pipeline flow:

```
derived event
    |
    v
[event policy stage]  (rate limit, dedup, content filter, permission)
    |
    v
[router]  (evaluate all enabled routes, collect matches)
    |
    v
[route policy stage]  (per-route rate limit, quiet hours, route permission)
    |   Routes that survive become candidate deliveries
    v
[delivery planner]  (construct DeliveryPlan per surviving target)
    |
    v
[delivery policy / rendering]  (size limits, capability fallback, final rendering)
    |
    v
[adapter execution]  (dequeued and delivered per adapter rate limits and state)
    |
    v
[receipt]  (append-only receipt row)
```


## 11. DeliveryReceipt

### 11.1 Receipt Dataclass

```python
class DeliveryStatus(str, Enum):
    ACCEPTED = "accepted"         # Adapter accepted the event for delivery
    QUEUED = "queued"             # Event is queued, delivery pending
    SENT = "sent"                 # Event was sent to the external platform
    ACKNOWLEDGED = "acknowledged" # External platform confirmed receipt
    FAILED = "failed"             # Delivery failed, will retry per plan
    DEAD_LETTERED = "dead_lettered" # All delivery attempts exhausted

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

### 11.2 Append-Only Semantics

Receipts are **append-only records**. Every delivery attempt produces a new `DeliveryReceipt` row in storage.

- Existing receipt rows are **never updated or deleted**.
- A delivery that retried three times produces **four receipt rows** (one per attempt), each with its own `timestamp` and `status`.
- The storage table uses an auto-increment `sequence` column for deterministic ordering:

```sql
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
```

### 11.3 Phase 1 Storage Model

Delivery receipts are stored as rows in the `delivery_receipts` table. They are **not** stored as `delivery.receipt` canonical events. The receipt row is the authoritative record of what happened in the delivery layer.

The `delivery.receipt` event kind exists in the event kind registry but is system/audit-only. It is not routeable through normal user routes unless explicitly enabled by the operator. It may be added in a later phase as optional audit mirroring.

### 11.4 Receipt Processing

- The correlation engine links receipts back to originating events via the delivery plan.
- Dead-lettered events trigger alerts and are available for manual reprocessing.
- Receipt metrics feed into observability: delivery latency, success rates, retry counts.


## 12. delivery_status Projection

### 12.1 View Definition

The "current status" of a delivery is a **projection**, not a stored value. The `delivery_status` view derives current state from the latest receipt per delivery plan:

```sql
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
```

### 12.2 How It Works

- The view groups receipts by `(delivery_plan_id, target_adapter)`.
- It selects the row with the highest `sequence` for each group.
- `MAX(sequence)` is used instead of `MAX(timestamp)` to avoid timestamp collision ambiguity.
- `current_status` is the status from the latest receipt row. It is never written directly.
- Querying this view is the correct way to check the current delivery state of any event.

### 12.3 Key Invariant

The delivery_status view is read-only. No code path writes to it. It reflects the append-only receipt table. If you need to change the "current status" of a delivery, you append a new receipt row with the new status. The view picks it up automatically.


## 13. Route Startup and Dynamic Reload Rules

### 13.1 Startup Validation

When the runtime loads route configuration:

1. All route IDs must be unique. Duplicate `route_id` values are a startup error.
2. Every `to[].adapter` must reference an adapter that exists in the `adapters` configuration. Referencing a non-existent adapter is a startup error.
3. Every `from_.adapter` that is not `None` must reference an existing adapter. Referencing a non-existent adapter is a startup error.
4. `from_.event_kinds` must not be empty.
5. `to` list must not be empty.
6. `channel` and `destination` on the same `RouteTarget` must not both be set when `destination.kind` is `"channel"` or `"matrix_room"`.
7. Routes referencing disabled adapters may be loaded but will never match (the adapter won't be running to deliver to). This is a warning, not an error.

### 13.2 Dynamic Reload

When route configuration is reloaded at runtime:

- New routes are added to the active set immediately. They begin matching new events.
- Removed routes are removed from the active set. In-flight delivery plans for removed routes continue to completion. The route removal does not abort in-progress deliveries.
- Modified routes are replaced atomically. Events currently being routed that already matched the old version continue with the old match. New events see the updated route.
- Configuration validation runs before applying changes. If validation fails, the old configuration remains active and an error is logged.

### 13.3 Conflict Behavior

- Routes are non-exclusive. Multiple routes can deliver the same event to the same adapter. This produces multiple delivery plans and multiple deliveries. The `DeduplicationPolicy` (event stage) can suppress duplicates if needed.
- The runtime does not prevent overlapping routes. Overlap (e.g., two routes matching the same source and event kind) is allowed. The operator is responsible for configuring non-overlapping routes when dedup is not desired.
- Route ordering is deterministic: priority ascending, then route_id lexicographic for ties.


## 14. Route Ownership Semantics

Routes are operator-owned configuration. There is no per-user or per-identity route ownership in Phase 1. All routes apply globally to all events that match their source criteria.

- **Exclusive routes**: Not supported in Phase 1. All matching routes fire for every matching event. There is no mechanism to mark a route as "only route for this source."
- **Non-exclusive routes** (default): The standard behavior. Multiple routes may match the same event. All matched routes produce delivery plans.
- **Route priority** is the only control over delivery ordering, not delivery exclusivity.

Plugins with the `MODIFY_ROUTES` capability can add or remove routes programmatically. These runtime routes follow the same matching and validation rules as configuration-defined routes. Runtime routes should not conflict with configuration routes on `route_id` (plugin routes should use a namespaced prefix).


## 15. Storage Queries for Routing

### 15.1 StorageBackend Methods Used by Routing/Planning

```python
class StorageBackend(Protocol):
    async def append_receipt(self, receipt: DeliveryReceipt) -> None: ...
    async def store_native_ref(self, ref: NativeMessageRef) -> None: ...
    async def resolve_native_ref(self, adapter: str, native_channel_id: str, native_message_id: str) -> str | None: ...
    async def resolve_native_relation(self, adapter: str, native_relation_id: str) -> str | None: ...
```

### 15.2 Relation Resolution Queries

- `resolve_native_ref(adapter, native_channel_id, native_message_id)` returns the canonical `event_id` or `None`.
- `resolve_native_relation(adapter, native_relation_id)` returns the canonical `event_id` for a native relation reference (e.g., Matrix `relates_to` event ID) or `None`.

Both queries use the `native_message_refs` table indexes:

```sql
CREATE INDEX idx_native_refs_event ON native_message_refs(event_id);
CREATE INDEX idx_native_refs_adapter_native ON native_message_refs(adapter, native_message_id);
CREATE INDEX idx_native_refs_relation ON native_message_refs(adapter, native_relation_id);
```

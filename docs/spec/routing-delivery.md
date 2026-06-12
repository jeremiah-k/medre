# Routing and Delivery Specification

> **Status:** Active
> **Classification:** Normative
> **Authority:** Authoritative specification for MEDRE route model, fanout, loop suppression, delivery planning, retry/outbox semantics, failure taxonomy, local acceptance vs remote delivery, and non-goals.
> **Last reviewed:** 2026-05-27

This document is the single normative reference for everything between "the pipeline has a derived event ready to deliver" and "the adapter reports back with a receipt." An implementer MUST be able to build routing and delivery from these definitions without consulting any other document.

## 1. Conformance

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHALL**, **SHALL NOT**, **SHOULD**, **SHOULD NOT**, **RECOMMENDED**, **MAY**, and **OPTIONAL** in this document are to be interpreted as described in RFC 2119.

## 2. Route Model

### 2.1 RouteSource

A `RouteSource` describes where a route matches events from.

```python
@dataclass
class RouteSource:
    adapter: str | None          # Adapter instance name, or None for any
    event_kinds: list[str]       # Event kinds to match (e.g., ["message.text", "telemetry"])
    channel: str | None          # Source channel/filter, or None for any
```

Matching rules:

| Field         | Match behavior                                                                             |
| ------------- | ------------------------------------------------------------------------------------------ |
| `adapter`     | Exact match on adapter instance name. `None` is a wildcard matching any adapter.           |
| `event_kinds` | Event MUST have an `event_kind` present in this list. The list MUST NOT be empty.          |
| `channel`     | Exact match on the event's `source_channel_id`. `None` is a wildcard matching any channel. |

All three fields are ANDed together. A source matches only when every non-None field matches the corresponding event field.

### 2.2 RouteTarget

A `RouteTarget` describes where a route delivers events to.

```python
@dataclass
class RouteTarget:
    adapter: str                              # Target adapter instance name (REQUIRED)
    channel: str | None                       # Target channel/room/topic, or None for adapter default
    destination: RouteDestination | None      # Structured destination for identity-based addressing
```

`adapter` is always REQUIRED. Every delivery goes to a specific adapter instance.

`channel` and `destination` are mutually exclusive addressing modes. See Section 2.4 for precedence rules.

### 2.3 RouteDestination

A `RouteDestination` provides structured addressing for adapters that use identity-based or hash-based delivery.

```python
@dataclass
class RouteDestination:
    kind: Literal["channel", "lxmf_destination", "meshcore_contact", "matrix_room"]
    destination_hash: str | None     # Hash or opaque ID (e.g., LXMF destination hash)
    destination_name: str | None     # Human-readable name for config readability
    metadata: dict = field(default_factory=dict)  # Extensible destination-specific parameters
```

The `kind` field determines the addressing model:

| kind                 | Addressing model      | Key fields                                                        |
| -------------------- | --------------------- | ----------------------------------------------------------------- |
| `"channel"`          | Logical channel name  | `destination_name` holds the channel name                         |
| `"lxmf_destination"` | LXMF destination hash | `destination_hash` holds the 16-byte hex hash                     |
| `"meshcore_contact"` | MeshCore contact      | `destination_hash` or `destination_name` identifies the contact   |
| `"matrix_room"`      | Matrix room ID        | Resolved via adapter's `connection.rooms` config, not stored here |

### 2.4 Channel vs Destination Precedence

**Rule 1:** `channel` is for channel-addressed adapters. Use `RouteTarget.channel` for adapters where delivery targets a logical channel, room, or slot. The adapter resolves the logical channel name to its native address internally.

**Rule 2:** `destination` is for identity/hash/contact-based addressing. Use `RouteTarget.destination` for adapters where the target is a specific entity, not a named channel.

**Rule 3:** When `destination.kind` is `"channel"` or `"matrix_room"`, `channel` MUST be `None`. Setting both `channel` and `destination` with `kind="channel"` is a configuration error.

**Rule 4:** Matrix room mapping is not stored in `RouteDestination`. Routes reference channels by logical name. The Matrix adapter's `connection.rooms` config maps logical names to Matrix room IDs. Routes MUST NOT contain Matrix room IDs directly.

| Scenario                            | `channel`     | `destination`                                    |
| ----------------------------------- | ------------- | ------------------------------------------------ |
| Deliver to Matrix room "general"    | `"general"`   | `None`                                           |
| Deliver to LXMF peer                | `None`        | `RouteDestination(kind="lxmf_destination", ...)` |
| Deliver to adapter default          | `None`        | `None`                                           |
| Deliver to MeshCore channel by name | `"emergency"` | `None`                                           |

### 2.5 Route

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

- `route_id` MUST be unique across all routes. Duplicate route IDs in configuration are a startup error.
- `to` is a list of one or more targets. An empty `to` list is a configuration error.
- `priority` determines delivery ordering when multiple routes match. Lower numbers deliver first.
- `filters` provides extensible matching beyond the core fields.
- `enabled: false` means the route is loaded but never matches. Disabled routes do not participate in routing.

### 2.6 Configuration Representation

Routes are configured in YAML or TOML. Example in YAML:

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

### 2.7 Bridge Directionality

Each route declares a directionality:

```python
class RouteDirectionality(Enum):
    SOURCE_TO_DEST = "source_to_dest"   # events flow from source → dest only
    DEST_TO_SOURCE = "dest_to_source"   # events flow from dest → source only
    BIDIRECTIONAL = "bidirectional"     # events flow in both directions
```

Default: `SOURCE_TO_DEST`.

For bidirectional routes, the runtime engine registers **two** internal `Route` objects (one per direction), both sharing the same `route_id` prefix.

### 2.8 Bridge Policy

A `BridgePolicy` is an optional frozen dataclass attached to a route. All fields default to empty tuples (meaning "no restriction"):

| Field                     | Meaning                                              |
| ------------------------- | ---------------------------------------------------- |
| `allowed_event_types`     | Event kinds that MAY traverse this bridge            |
| `allowed_source_adapters` | Source adapters that MAY originate bridged events    |
| `allowed_dest_adapters`   | Destination adapters that MAY receive bridged events |
| `room_allowlist`          | Room IDs allowed to bridge                           |
| `channel_allowlist`       | Channel/conversation IDs allowed to bridge           |
| `sender_allowlist`        | Sender identifiers allowed to bridge                 |

An empty tuple means "allow all" for that dimension. `BridgePolicy` is frozen after construction. It MUST be set at configuration load time and MUST NOT be mutated at runtime.

## 3. Route Matching Semantics

The routing engine evaluates **all enabled routes** against each derived event. A single event MAY match zero, one, or many routes. There is no first-match-wins behavior.

### 3.1 Matching Algorithm

1. Filter out routes where `enabled` is `false`.
2. For each remaining route, evaluate `from_` against the event:
   - `adapter`: event's `source_adapter` MUST equal this value, unless `None` (wildcard).
   - `event_kinds`: event's `event_kind` MUST be present in this list.
   - `channel`: event's `source_channel_id` MUST equal this value, unless `None` (wildcard).
3. If the core fields match, evaluate `filters` (tag matching, metadata values). Implementation of filter matching is extensible.
4. Collect all matching routes.

### 3.2 Non-Exclusive Matching

Routes are **non-exclusive** by default. If an event matches routes A and B, both routes fire. The event gets delivered to all targets from both routes. There is no deduplication at the route level. Deduplication, if needed, is handled by the `DeduplicationPolicy` at the event policy stage.

### 3.3 Route Ordering

Matching routes are sorted by `priority` (ascending, lower is higher priority) before delivery plan construction. This ordering influences:

- Which delivery plans are constructed first.
- The order in which the adapter execution stage dequeues and processes deliveries.
- Policy evaluation order when per-route limits apply.

Route ordering is deterministic: priority ascending, then `route_id` lexicographic for ties.

### 3.4 No Match Behavior

If an event matches zero routes, it is not delivered anywhere. It remains stored in the canonical event log and is available for replay if routes are added later. No error is raised for unroutable events.

## 4. Fanout Strategy

### 4.1 Per-Route Fanout

Each route's `to` list is a fanout. A single route MAY target multiple adapters. Each target in `to` produces its own `DeliveryPlan`.

### 4.2 Cross-Route Fanout

A single event matching multiple routes fans out across all matching routes' targets. An event matching route A (with 2 targets) and route B (with 1 target) produces 3 delivery plans total.

### 4.3 Broadcast Only

Broadcast is the only fanout strategy. All matching targets receive the event. Round-robin, weighted, and first-available strategies are not supported. The routing engine does not filter or prioritize among matched targets within a single route. Every target in the `to` list gets a delivery plan.

## 5. Loop Suppression

### 5.1 Runtime Pipeline Guards

Three runtime pipeline guards prevent loop propagation during live delivery:

1. **Native-ref duplicate suppression** — The pipeline checks inbound events against stored native message references. If the event's `source_native_ref` matches a previously seen ref (same `(adapter, native_channel_id, native_message_id)` triple), the event is dropped. This prevents echo from re-delivered or duplicate packets at the pipeline boundary. Native refs are persisted to storage and are used consistently during replay — :func:`resolve_native_ref` returns the original `event_id` for previously seen triples, enabling replay to detect already-processed events.

2. **Self-loop guard** — The pipeline checks whether `target_adapter == event.source_adapter` for each delivery target. If true, the delivery is suppressed with `DeliveryOutcome(status="skipped")` and `failure_kind=LOOP_SUPPRESSED`. A `DeliveryReceipt(status="suppressed")` is persisted with `event_id`, `route_id`, `target_adapter`, `failure_kind="loop_suppressed"`, and a reason string. The adapter's `send()` method is NOT called. The suppressed receipt does NOT enter the retry queue (`next_retry_at` is `None`).

3. **Route-trace guard** — The pipeline checks the `route_trace` counter on the event's `RoutingMetadata`. If a route ID appears more than once in the trace (indicating a cycle), the delivery is skipped.

In a fan-out route with multiple targets, the self-loop guard is evaluated independently per target. A self-loop on one target does not prevent delivery to other targets.

### 5.2 Configuration-Level Loop Detection

At startup, the route engine detects routing loops via DFS on the directed adapter adjacency graph. Both direct two-adapter loops (A↔B) and multi-hop cycles are detected. These are logged as informational messages — bidirectional bridges are an intentional topology, not a misconfiguration. Route configuration validation MAY warn about obvious loops at config-load time, but runtime enforcement operates per-delivery, not per-route-topology.

### 5.3 Replay Loop Prevention

Replay loop prevention is owned by the replay module. It detects self-loops (a route would deliver an event back to its own `source_adapter`) and previously-routed events (the event's `RoutingMetadata.matched_routes` overlaps with a matched route ID). Looping routes are skipped, not errored. A `loop_warnings` tuple is attached to the replay attribution.

### 5.4 No Distributed Loop Prevention

All loop-prevention mechanisms operate within a single MEDRE process only. If two MEDRE instances bridge the same transports in opposite directions, neither detects the cross-instance loop. There is no shared loop-prevention state between instances.

### 5.5 Native ID Stability for Dedup

Native-ref duplicate suppression depends on adapters providing a stable, unique `native_message_id` via `source_native_ref`. Adapters that return `None` or an empty string for `native_message_id` bypass dedup entirely — every inbound event from that adapter is treated as novel.

| Adapter    | Native ID field    | Stability         |
| ---------- | ------------------ | ----------------- |
| Matrix     | `event_id`         | Stable            |
| Meshtastic | `packet_id`        | Stable per node   |
| MeshCore   | `sender_timestamp` | Stable per sender |
| LXMF       | `message_id` hex   | Stable            |

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
    retry_policy: RetryPolicy | None
    deadline: datetime | None  # Maximum time to keep attempting delivery
    route_id: str | None       # Route attribution when available
    target_identity: str       # Stable JSON target identity
    capability_level: str | None
    capability_field: str | None
    capability_reason: str | None
```

The planner constructs one `DeliveryPlan` per `(event, RouteTarget)` pair.

`plan_id` is produced by :func:`stable_delivery_plan_id`, which hashes `event_id`, `route_id` (or `"unrouted"`), `target_index`, and a SHA-256 digest of :func:`delivery_target_identity` (the stable JSON representation of the :class:`RouteTarget`). The format is `plan:{event_id}:{route_part}:{index_part}:{target_hash}`. It MUST NOT depend on Python object identity (`id()`). When route context is unavailable the plan uses `"unrouted"` attribution, but routed live and replay paths MUST include the matched route ID and target index so repeated equivalent targets in the same route remain distinct and delivery-plan identity is reproducible. Repeated replay runs over the same event and configuration produce the same `plan_id` values.

`target_identity` is a stable JSON representation produced by :func:`delivery_target_identity`, which serialises the :class:`RouteTarget` `adapter`, `channel`, and `destination` fields with sorted keys and compact separators. It is the same value used inside :func:`stable_delivery_plan_id` and is populated for every plan, not only for manually constructed ones.

`capability_level`, `capability_field`, and `capability_reason` mirror the :class:`CapabilityDecision` used to choose `primary_strategy`. The :class:`FallbackResolver` populates these fields from the resolver's decision on every plan it produces. They are `None` only for manually constructed plans, passthrough event kinds that have no capability candidate, or outbox rows with missing or corrupt prerelease metadata that predate route-decision metadata persistence. Retry reconstruction recovers these fields from the outbox `metadata` dict when present; see § 6.4.

Delivery plans are operational artifacts, not canonical events. They exist during pipeline execution to coordinate delivery. They are not stored in the canonical event log and are not subject to immutability guarantees. Delivery plans MAY be reconstructed at any time by re-running the routing and planning stages against current configuration.

**Planning decision authority.** `FallbackResolver` produces each `DeliveryPlan` by delegating capability strategy decisions to `CapabilityDecisionResolver`. The resulting plan is the authoritative planning decision for that `(event, target)` pair. Downstream stages — `TargetDeliveryService` (execution), `RenderingPipeline` (rendering), `RenderingEvidence`/`DeliveryReceipt` (evidence), and diagnostics — consume plan fields (`primary_strategy`, `capability_level`, `capability_field`, `capability_reason`) without re-deciding capability or strategy. The only exception is replay, which intentionally re-runs planning against current capabilities and configuration rather than reusing the original live plan (see § 6.3.9).

### 6.2 DeliveryStrategy

```python
@dataclass
class DeliveryStrategy:
    method: str                # Delivery method identifier
    max_retries: int = 3       # Maximum attempts before permanent failure
    timeout_seconds: float = 30.0  # Per-attempt timeout in seconds
```

`DeliveryStrategy` defines how a single delivery attempt works. `DeliveryStrategy.method` is interpreted by the runtime delivery/rendering pipeline. Adapters consume `RenderingResult` and do not reinterpret delivery strategy. The `primary_strategy` is the first attempt. If it fails, each entry in `fallback_chain` is tried in order.

### 6.2.1 Delivery Strategy Method Vocabulary

The `method` field is a closed vocabulary. Implementations MUST treat unknown
method values as configuration errors. The well-known methods are:

| Method            | Semantics                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `"direct"`        | Target-native rendering. The event is rendered through the standard renderer pipeline (the target-native renderer matching the adapter's platform) and delivered natively. This is the default strategy when the target adapter supports the event's relation types at capability level `"native"`.                                                                                                                                                                                                                                                                         |
| `"fallback_text"` | Degraded text rendering within the target-native format. The target-native renderer produces its native output format but embeds relation context as inline text drawn from `EventRelation.fallback_text`. The pipeline does **not** bypass the target-native renderer or switch to a generic text renderer. The adapter still receives a payload in its native format, not a generic `{"text": ...}` envelope.                                                                                                                                                             |
| `"skip"`          | Pre-outbox suppression before rendering and adapter invocation. Triggers: self-loop guard, route-trace cycle, policy denial, or capability-level `"unsupported"` for the event's relation type. No renderer invoked. No adapter call made. Produces `DeliveryOutcome(status="skipped")`. For route-target suppressions after the event has been stored and planned, the pipeline also persists `DeliveryReceipt(status="suppressed")` with route, target, plan, and reason evidence. Events matching zero routes produce no `DeliveryOutcome` and no receipt (Section 3.4). |
| `"propagated"`    | Relayed through an intermediate hop (e.g. LXMF propagation node). Reserved for future use.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `"opportunistic"` | Best-effort delivery with no guarantee. Reserved for future use.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `"paper"`         | Store-and-forward. Reserved for future use.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |

### 6.3 Capability Decision Model

Capability decisions are resolved by a single stateless resolver during
planning (`FallbackResolver` via `CapabilityDecisionResolver`) and replay
re-planning. `FallbackResolver` produces each `DeliveryPlan` by delegating
capability strategy decisions to `CapabilityDecisionResolver`. Downstream
live-delivery stages — `TargetDeliveryService` (execution), `RenderingPipeline`
(rendering), `RenderingEvidence`/`DeliveryReceipt` (evidence), and diagnostics —
consume the precomputed `DeliveryPlan` capability/strategy fields
(`primary_strategy`, `capability_level`, `capability_field`,
`capability_reason`) and MUST NOT re-resolve capability. The only exception is
replay, which intentionally re-runs planning against current capabilities and
configuration rather than reusing the original live plan (see § 6.3.9).

#### 6.3.1 CapabilityDecision

```python
@dataclass(frozen=True)
class CapabilityDecision:
    target_adapter: str | None
    event_kind: str
    capability_level: str       # "native" | "fallback" | "unsupported"
    delivery_strategy: str      # "direct" | "fallback_text" | "skip"
    supported: bool             # True when native or fallback
    capability_field: str | None  # AdapterCapabilities field that decided
    reason: str | None          # Human-readable reason for fallback/unsupported
```

#### 6.3.2 Capability Level Semantics

| Level           | Delivery Strategy | Meaning                                                       |
| --------------- | ----------------- | ------------------------------------------------------------- |
| `"native"`      | `"direct"`        | First-class support; deliver natively.                        |
| `"fallback"`    | `"fallback_text"` | No native support; target-native renderer embeds inline text. |
| `"unsupported"` | `"skip"`          | Adapter cannot handle; delivery suppressed before rendering.  |

Boolean capability fields (`text`, `attachments`, `presence`,
`metadata_fields`) map `True` → native, `False` → unsupported.

Three-level string fields (`reactions`, `edits`, `deletes`, `replies`)
map directly: `"native"` → native, `"fallback"` → fallback, `"unsupported"` → unsupported.

> **Known gap: fallback capability level is dormant in production transport profiles.** As of this writing, no production transport profile declares a three-level string field at `"fallback"`. All current profile declarations use `"native"` or `"unsupported"`. The fallback path (`"fallback_text"` strategy, inline text degradation) is exercised by tests with synthetic capability configurations but has not been validated against a live transport endpoint with a real adapter producing degraded output. The `CapabilityLevel.METADATA_NATIVE` and `CapabilityLevel.METADATA_NATIVE_OR_FALLBACK` enum values exist and map to the `"fallback"` decision level; they are not currently used in any transport profile. This gap does not affect correctness (the code path exists and is tested) but means the fallback rendering path has no R-tier evidence.

#### 6.3.3 Event-Kind Mapping

| Event Kind           | Capability Field  | Field Type |
| -------------------- | ----------------- | ---------- |
| `message.reacted`    | `reactions`       | String     |
| `message.edited`     | `edits`           | String     |
| `message.deleted`    | `deletes`         | String     |
| `message.file`       | `attachments`     | Boolean    |
| `message.created`    | `text`            | Boolean    |
| `message.text`       | `text`            | Boolean    |
| `presence.changed`   | `presence`        | Boolean    |
| `telemetry.received` | `metadata_fields` | Boolean    |
| `telemetry.position` | `metadata_fields` | Boolean    |

Event kinds not in this table produce no event-kind candidate and
default to native/direct (passthrough).

> **Passthrough semantics for unknown event kinds.** Event kinds not in the mapping table above are treated as natively supported. The resolver produces no event-kind candidate, and the decision defaults to native/direct. This means unknown event kinds are delivered rather than suppressed. If a future event kind requires capability gating, it **MUST** be added to this table with an appropriate capability field.

#### 6.3.4 Relation Mapping

| Relation Type | Capability Field | Note                                                                                                           |
| ------------- | ---------------- | -------------------------------------------------------------------------------------------------------------- |
| `reply`       | `replies`        | Checked for every reply relation.                                                                              |
| `reaction`    | `reactions`      | Checked for every reaction relation.                                                                           |
| `edit`        | `edits`          | Checked for every edit relation.                                                                               |
| `delete`      | `deletes`        | Checked for every delete relation.                                                                             |
| `thread`      | —                | **Deferred.** No `AdapterCapabilities.threads` field exists. Thread relations produce no capability candidate. |

Relation-level capability checking for `reaction`, `edit`, and `delete`
is intentionally centralized in the resolver alongside `reply`. All
four relation types follow the same three-level semantics (native /
fallback / unsupported) and the same precedence rules. The resolver
evaluates every relation in `event.relations` order; there is no
relation-type-specific short-circuit or special case.

> **Fail-closed for unknown non-thread relation types.** `thread` is the only
> deferred relation type: it produces no relation-level capability candidate
> and does not suppress delivery. Any other relation type not in the mapping
> table above is unsupported and MUST produce an unsupported/skip decision with
> `capability_field="relation"` and reason `unsupported relation type ...`.
> If a relation type requires first-class capability gating in the future, it
> **MUST** be added to the table. Thread relations are explicitly deferred (see
> § 6.3.6).

#### 6.3.5 Multiple-Relation Precedence

When an event has multiple capability candidates (event-kind + relations),
the resolver picks the **most severe** decision:

1. `unsupported` (severity 2) > `fallback` (severity 1) > `native` (severity 0).
2. At the same severity, the **first candidate in evaluation order** breaks ties.
3. Evaluation order: event-kind candidate first, then relations in `event.relations` order.

This ensures `unsupported` always wins over `fallback` or `native`,
regardless of candidate ordering, while maintaining deterministic
tie-breaking.

#### 6.3.6 Thread Capability Deferral

`AdapterCapabilities.threads` does not exist. The thread relation itself
does not introduce a capability candidate and MUST NOT be considered during
capability evaluation. Events carrying a thread relation still participate
in normal capability checks (event-kind, attachments, other relations) which
can suppress or alter delivery. The resolver preserves current behaviour
only insofar as the thread relation contributes no candidate: when no other
candidate overrides, thread-carrying events receive native/direct delivery
with `capability_field=None`. This deferral MUST NOT be interpreted as
unconditional native thread support or as an override of other capability
rules.

#### 6.3.7 Rendering Evidence

When a delivery proceeds to rendering, `RenderingContext.capability_level`
is populated from the `CapabilityDecision` so that rendering evidence
captures the capability context. `RenderingEvidence.capability_level`
reflects this value, providing durable capability context per delivery.

#### 6.3.8 Replay Parity

Replay BEST_EFFORT mode uses the same `CapabilityDecisionResolver`
as live delivery. Plans filtered by `_filter_plans_by_capability`
use `decision.supported` to determine inclusion, ensuring live and
replay capability logic share one source of truth.

### 6.3.9 Live/Replay Plan Parity

Live delivery and replay planning MUST produce semantically equivalent :class:`DeliveryPlan` instances for the same event and route configuration. The following fields MUST be identical across live and replay paths:

| Field                     | Parity requirement                                |
| ------------------------- | ------------------------------------------------- |
| `plan_id`                 | Deterministic via :func:`stable_delivery_plan_id` |
| `event_id`                | Same canonical event                              |
| `target`                  | Same :class:`RouteTarget`                         |
| `primary_strategy.method` | Same capability-derived strategy                  |
| `route_id`                | Same matched route                                |
| `target_identity`         | Same :func:`delivery_target_identity` result      |
| `capability_level`        | Same :class:`CapabilityDecision` level            |
| `capability_field`        | Same capability field or `None`                   |
| `capability_reason`       | Same reason or `None`                             |

Both paths use the same :class:`CapabilityDecisionResolver` (see § 6.3) to decide capability, ensuring one source of truth.

Repeated replay runs over the same event and configuration MUST produce the same `plan_id` values. Repeated equivalent targets within a single route (same adapter + channel) MUST receive distinct `plan_id` values disambiguated by `target_index`.

### 6.3.10 Live/Replay Receipt Parity

When replay in `BEST_EFFORT` mode produces a delivery, the resulting :class:`DeliveryReceipt` MUST match the live receipt on the following fields:

| Receipt field        | Parity requirement                             |
| -------------------- | ---------------------------------------------- |
| `event_id`           | Same                                           |
| `delivery_plan_id`   | Same (deterministic plan ID)                   |
| `target_adapter`     | Same                                           |
| `target_channel`     | Same                                           |
| `route_id`           | Same                                           |
| `status`             | Same (both `sent`, or both `suppressed`, etc.) |
| `error`              | Same suppression or failure reason             |
| `failure_kind`       | Same                                           |
| `retry_*` fields     | Same when retry policy is configured           |
| `rendering_evidence` | Same delivery strategy and capability level    |
| `next_retry_at`      | Same when applicable                           |

The following fields intentionally differ between live and replay:

| Field                | Live value         | Replay value          | Why                    |
| -------------------- | ------------------ | --------------------- | ---------------------- |
| `source`             | `"live"`           | `"replay"`            | Distinguishes origin   |
| `replay_run_id`      | `None`             | Replay run identifier | Run attribution        |
| `receipt_id`         | Unique per call    | Unique per call       | Append-only semantics  |
| `parent_receipt_id`  | Depends on chain   | Depends on chain      | Independent chains     |
| `created_at`         | Live timestamp     | Replay timestamp      | Different wall-clock   |
| `adapter_message_id` | Transport-assigned | Transport-assigned    | May differ per attempt |

```python
@dataclass
class RetryPolicy:
    max_attempts: int = 5        # Maximum total delivery attempts (including initial)
    backoff_base: float = 2.0    # Base delay in seconds for exponential backoff
    max_delay_seconds: float = 60.0  # Upper bound for backoff delay
    jitter: bool = True          # Whether to add jitter to avoid thundering-herd
```

### 6.4 Route-Decision Metadata Persistence

When `OutboxManager.create_for_delivery()` creates a `DeliveryOutboxItem`, the outbox `metadata` JSON dict includes route-decision fields alongside destination metadata. These fields are:

| Metadata key        | Source                                 | Type                   |
| ------------------- | -------------------------------------- | ---------------------- |
| `capability_level`  | `DeliveryPlan.capability_level`        | `str \| None`          |
| `delivery_strategy` | `DeliveryPlan.primary_strategy.method` | `str`                  |
| `capability_field`  | `DeliveryPlan.capability_field`        | `str \| None`          |
| `capability_reason` | `DeliveryPlan.capability_reason`       | `str \| None`          |
| `deadline`          | `DeliveryPlan.deadline`                | ISO 8601 `str \| None` |

Retry reconstruction (`reconstruct_retry_delivery_plan()`) reads these keys back from the outbox `metadata` dict and populates the reconstructed `DeliveryPlan`. This ensures retry delivery uses the same capability and strategy decisions as the original live delivery, rather than defaulting to `capability_level=None` (silently treated as `"native"`) and `strategy="direct"`.

Outbox rows with missing or corrupt prerelease metadata that predate this persistence gracefully default to `capability_level=None` and `strategy="direct"` when the keys are absent.

**Wire protocol**: These keys are internal correlation metadata stored in the SQLite `metadata` column. They MUST NOT be rendered into Meshtastic, Matrix, MeshCore, or LXMF wire payloads.

### 6.5 Fallback Resolution

When primary delivery fails, the fallback resolution chain executes in order:

1. Try `primary_strategy`.
2. If it fails, try each `DeliveryStrategy` in `fallback_chain` in sequence.
3. If all fallbacks fail, mark the event as `dead_lettered`.

Fallback types MAY include:

- Retry with delay (same adapter, same strategy, after backoff)
- Deliver to alternative channel (same adapter, different channel)
- Degraded text rendering via `fallback_text` strategy (target-native renderer embeds relation context as inline text)
- Store for later delivery (queue until adapter recovers)

The fallback chain is part of the `DeliveryPlan`. It is constructed at planning time, not at execution time. The executor walks the chain and reports receipts for each attempt.

## 7. Retry Semantics

### 7.1 Opt-In

Retry is **opt-in** — it is disabled by default. The `RetryWorker` only activates when a `RetryPolicy` is configured on the route or delivery plan. Without a `RetryPolicy`, transient failures are not automatically retried; they remain as `failed` receipts.

### 7.2 Auto-Retried Failures

When `RetryPolicy` is configured, the following failure kind is auto-retried:

- `ADAPTER_TRANSIENT` — timeout, connection error, `OSError` hierarchy. The RetryWorker picks up the failed receipt when `next_retry_at` is due and re-invokes delivery through the same planning path, incrementing `attempt_number`.

### 7.3 Non-Retryable Failures

The following failure kinds are never auto-retried:

- `ADAPTER_PERMANENT`, `RENDERER_FAILURE`, `PLANNER_FAILURE`, `DEADLINE_EXCEEDED`, `ADAPTER_MISSING`, `LOOP_SUPPRESSED`, `POLICY_SUPPRESSED`, `CAPABILITY_SUPPRESSED`, `CAPACITY_REJECTION`, `SHUTDOWN_REJECTION`

### 7.4 Retry Flow

1. `deliver_to_target` records a `failed` receipt with `next_retry_at` populated and `failure_kind=ADAPTER_TRANSIENT`.
2. `RetryWorker` loads due receipts (where `next_retry_at <= now` and `status = 'failed'` and `failure_kind = 'adapter_transient'`). The query excludes dead-lettered receipts sharing the same delivery lineage.
3. The worker attempts to acquire delivery capacity. If capacity is unavailable, the worker advances the existing receipt's `next_retry_at` by one backoff interval. No new receipt is created. Capacity rejection does not advance `attempt_number`.
4. If capacity is acquired, the worker re-invokes delivery with the same `delivery_plan_id` and `event_id`, incrementing `attempt_number` and linking via `parent_receipt_id`. The retry receipt carries `source='retry'`, `target_channel`, and `route_id` from the original delivery context. Retry reconstruction preserves the original `delivery_plan_id`, `route_id`, `target_adapter`, `target_channel`, and `target_identity` — these identity fields are frozen at first delivery and carried through the entire retry chain. **Route-decision metadata recovery**: retry also preserves the original `capability_level`, `delivery_strategy` (primary strategy method), `capability_field`, `capability_reason`, and `deadline` from the outbox `metadata` dict. These fields were persisted at outbox creation time by the live delivery path. Outbox rows with missing or corrupt prerelease metadata that predate this persistence gracefully default to `capability_level=None` (which downstream code treats as `"native"`) and `strategy="direct"`. Each retry attempt appends a new receipt row; earlier receipts are not overwritten.
5. If `retry_policy` is set and `is_exhausted(attempt_number)` is true, a `dead_lettered` receipt is appended instead of retrying. This receipt carries the same `delivery_plan_id`, `route_id`, `target_adapter`, and `target_channel` as the preceding failed receipts, with `parent_receipt_id` linking to the last failed attempt. Retry exhaustion produces durable dead-lettered evidence — the receipt remains in storage with `status="dead_lettered"`, `next_retry_at=None`, and the full retry chain is visible via `parent_receipt_id` links.
6. Retry uses the same delivery planning pipeline. No special bypass path exists.

### 7.5 Policy Persistence

When a delivery first fails with `failure_kind='adapter_transient'` and a `RetryPolicy` is configured, the policy parameters (`max_attempts`, `backoff_base`, `backoff_max`, `jitter`) are persisted as columns on the failure receipt. The `RetryWorker` reads these values from the stored receipt rather than re-reading route configuration. The retry policy is frozen at first failure: subsequent route or `RetryPolicy` configuration changes do not affect in-flight retry behavior.

### 7.6 Frozen Target Metadata

Retry uses the `target_adapter` and `target_channel` from the original failed receipt, not the current route configuration. Route targets, channel assignments, and adapter mappings MAY change between the original failure and a retry attempt, but the retry continues to target the originally recorded adapter and channel. Before executing the retry, the `RetryWorker` validates that the target adapter still exists at runtime. If the adapter has been removed, the retry is not attempted and the receipt is dead-lettered.

### 7.7 Retry Properties Summary

| Property                     | Detail                                                                      |
| ---------------------------- | --------------------------------------------------------------------------- |
| Single-process               | Retry is single-process, in-process, and bounded by `RetryPolicy`           |
| Survives restart             | Persistent receipts with `next_retry_at` survive process restart            |
| NOT EXISTS exclusion         | RetryWorker excludes receipts that already have a `dead_lettered` successor |
| Capacity rejection           | No new receipt is created; existing receipt is rescheduled                  |
| Opt-in                       | Requires explicit `RetryPolicy`; no automatic retry without it              |
| Policy persistence           | Retry policy parameters are stored on first failure receipt                 |
| Frozen target metadata       | Retry targets original adapter and channel from failed receipt              |
| Adapter existence validation | Missing adapters are dead-lettered before retry attempt                     |

### 7.8 Backoff Formula

Backoff: `delay = min(backoff_base * 2 ** (attempt - 1), max_delay_seconds)`, with optional jitter.

Exhaustion check: `attempt_number >= policy.max_attempts`.

## 8. DeliveryReceipt

### 8.1 Receipt Dataclass

```python
@dataclass(frozen=True)
class DeliveryReceipt:
    sequence: int = 0                      # Monotonically increasing sequence number
    receipt_id: str = ""                   # Unique receipt record identifier
    event_id: str = ""                     # The canonical event being delivered
    delivery_plan_id: str = ""             # Delivery plan this receipt belongs to
    target_adapter: str = ""               # Name of the target adapter
    target_channel: str | None = None      # Target channel/room from RouteTarget
    route_id: str = ""                     # Route that produced this delivery
    status: Literal["queued", "sent", "suppressed", "failed", "dead_lettered"] = "queued"
    error: str | None = None               # Error message if delivery failed
    failure_kind: str | None = None        # DeliveryFailureKind value
    adapter_message_id: str | None = None  # Platform-specific message ID
    next_retry_at: datetime | None = None  # Scheduled time for next retry attempt
    attempt_number: int = 1                # 1-indexed attempt number
    parent_receipt_id: str | None = None   # Receipt ID of preceding attempt
    source: str = "live"                   # "live", "retry", or "replay"
    replay_run_id: str | None = None       # Replay run ID when source="replay"
    retry_max_attempts: int | None = None  # Persisted retry policy: max attempts
    retry_backoff_base: float | None = None # Persisted retry policy: backoff base
    retry_max_delay: float | None = None   # Persisted retry policy: max delay
    retry_jitter: bool | None = None       # Persisted retry policy: jitter enabled
    rendering_evidence: str | None = None  # Structured rendering evidence for this attempt
    outbox_id: str | None = None           # Internal correlation key — not wire metadata (see § 8.5.3)
    created_at: datetime = ...             # Timestamp when this receipt was created
```

Receipt status is a string literal constrained to five values:

| Status          | Meaning                                                                           |
| --------------- | --------------------------------------------------------------------------------- |
| `queued`        | Delivery enqueued for adapter execution                                           |
| `sent`          | Adapter reported successful handoff                                               |
| `suppressed`    | Post-planning direct-call suppression. MAY be recorded for defense-in-depth audit |
| `failed`        | Delivery attempt failed                                                           |
| `dead_lettered` | All retries exhausted; final terminal state                                       |

### 8.2 Append-Only Semantics

Receipts are **append-only records**. Every delivery attempt produces a new `DeliveryReceipt` row in storage.

- Existing receipt rows MUST NOT be updated or deleted.
- A delivery that retried three times produces **four receipt rows** (one per attempt), each with its own `created_at` value and `status`.
- The storage table uses an auto-increment `sequence` column for deterministic ordering.

### 8.3 Storage Schema

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
    rendering_evidence TEXT,
    outbox_id TEXT,                         -- Internal correlation key (see § 8.5.3)
    created_at TEXT NOT NULL
);
```

### 8.3.1 Rendering Evidence and Receipts

Each delivery attempt passes through the rendering pipeline before reaching the adapter. The rendering pipeline produces a `RenderingResult` whose `truncated` and `fallback_applied` fields are evidence signals explaining the rendering decision. These signals are durable: the `rendering_evidence` column on `delivery_receipts` stores a structured record of the rendering evidence for each delivery attempt.

`rendering_evidence` is attached **only** for `sent` and `queued` receipt statuses. The following paths leave `rendering_evidence` as `None`:

| Path                                       | Status             | `rendering_evidence` |
| ------------------------------------------ | ------------------ | -------------------- |
| Successful delivery                        | `sent`             | Populated            |
| Queued for delivery                        | `queued`           | Populated            |
| Post-planning suppression                  | `suppressed`       | `None`               |
| Pre-outbox skip (loop, policy, capability) | No receipt created | N/A                  |
| Rendering failure                          | `failed`           | `None`               |
| Adapter failure                            | `failed`           | `None`               |

When inspecting a receipt, operators can determine:

- Whether the rendered content was truncated.
- Whether fallback rendering was applied and which kind.
- What delivery strategy governed the render.

For the full rendering evidence semantics, payload vs evidence distinction, and replay-readiness limits, see the Diagnostics and Evidence Specification, § 14.

### 8.4 Receipt Lineage

`DeliveryReceipt` carries two fields for receipt chain ordering:

| Field               | Type                | Description                                  |
| ------------------- | ------------------- | -------------------------------------------- | -------------------------------------------------------------- |
| `attempt_number`    | `int` (default `1`) | 1-indexed attempt number. First attempt = 1. |
| `parent_receipt_id` | `str                | None`(default`None`)                         | Receipt ID of the preceding attempt. `None` for first attempt. |

When retries are exhausted, the receipt chain ends with a `dead_lettered` receipt:

```text
rcpt-1 (attempt=1, parent=None, status=failed)
  └→ rcpt-2 (attempt=2, parent=rcpt-1, status=dead_lettered)
```

### 8.5 Queued-to-Sent Receipt Correlation

Queue-based adapters (e.g., Meshtastic) produce a `queued` receipt at enqueue time and a `sent` receipt when the adapter confirms handoff. Correlating the queued receipt to the correct delivery plan requires deterministic matching because multiple deliveries to the same adapter and channel may be in-flight simultaneously.

#### 8.5.1 Correlation Mechanism

The `outbox_id` field provides **exact** correlation between a `queued` receipt and its corresponding `sent` receipt. Queue adapters MUST populate both `outbox_id` and `attempt_number` on callback records. The threading path is:

1. `TargetDeliveryService` stamps `RenderingResult.outbox_id` and `RenderingResult.attempt_number` before adapter delivery.
2. Queue-based adapters propagate `outbox_id` and `attempt_number` through their internal queue items (e.g., `QueuedOutboundItem`).
3. When the adapter reports send confirmation, `DeliveryLifecycleService.append_queued_to_sent_receipt()` requires `outbox_id` and `attempt_number` on the `OutboundNativeRefRecord`, then validates all callback fields against the authoritative outbox row.

The correlation algorithm in `append_queued_to_sent_receipt`:

1. **Missing `outbox_id`** — hard reject. No supplemental receipt, no outbox mutation. Logged as a warning.
2. **Missing `attempt_number`** — hard reject. Same behavior as missing `outbox_id`.
3. **Outbox row lookup** — the service loads the outbox item by `outbox_id`. If not found or already terminal, the callback is rejected as stale.
4. **Field validation** — `event_id`, `adapter`, `delivery_plan_id` (when present), `native_channel_id` (when present), and `attempt_number` are validated against the outbox row. Any mismatch rejects the callback.
5. **Exact receipt selection** — the queued receipt is selected by `receipt.outbox_id == record.outbox_id` **and** `receipt.attempt_number == record.attempt_number`. The two-key match preserves the stale-safe invariant end-to-end. No plan-id-only or heuristic fallback exists.
6. **Supplemental receipt** — if all validations pass, exactly one `sent` receipt is appended and the validated outbox row is transitioned to `sent`.

#### 8.5.2 Invariant: Exact Outbox Correlation

> Queue callbacks MUST carry `outbox_id` and `attempt_number`. The pipeline MUST use exact outbox-level correlation. When either field is missing, no heuristic fallback is attempted and the service logs a warning. This ensures deterministic, stale-safe correlation even when multiple deliveries to the same adapter and channel overlap.

#### 8.5.3 Internal Correlation Keys

`outbox_id` and `attempt_number` are internal lifecycle correlation keys. They are:

- Not sent over transports (not wire metadata).
- Not persisted in `native_message_refs` storage.
- Propagated by adapters only through internal local queues and callback records.

`delivery_plan_id` is retained as an internal delivery-plan identity. It is validated against the outbox row when present on the callback, but it is not sufficient for queued callback correlation on its own.

#### 8.5.4 RenderingResult and OutboundNativeRefRecord Threading

| Dataclass                 | Field            | Set by                                              |
| ------------------------- | ---------------- | --------------------------------------------------- |
| `RenderingResult`         | `outbox_id`      | `TargetDeliveryService` via `dataclasses.replace()` |
| `RenderingResult`         | `attempt_number` | `TargetDeliveryService` via `dataclasses.replace()` |
| `OutboundNativeRefRecord` | `outbox_id`      | Adapter queue processing (required)                 |
| `OutboundNativeRefRecord` | `attempt_number` | Adapter queue processing (required)                 |

## 9. delivery_status Projection

### 9.1 View Definition

The "current status" of a delivery is a **projection**, not a stored value. The `delivery_status` view derives current state from the latest receipt per delivery plan:

```sql
CREATE VIEW delivery_status AS
SELECT dr.* FROM delivery_receipts dr
JOIN (
    SELECT delivery_plan_id, target_adapter, MAX(sequence) AS max_seq
    FROM delivery_receipts GROUP BY delivery_plan_id, target_adapter
) latest ON dr.sequence = latest.max_seq;
```

### 9.2 How It Works

- The view groups receipts by `(delivery_plan_id, target_adapter)`.
- It selects the row with the highest `sequence` for each group.
- `MAX(sequence)` is used instead of `MAX(timestamp)` to avoid timestamp collision ambiguity.
- The returned row's `status` is the current status from the latest receipt row. It is never written directly.

### 9.3 Key Invariant

The `delivery_status` view is read-only. No code path writes to it. If the "current status" of a delivery needs to change, a new receipt row MUST be appended. The view picks it up automatically.

## 10. DeliveryFailureKind

Every delivery failure is classified into one of eleven categories:

```python
class DeliveryFailureKind(Enum):
    PLANNER_FAILURE = "planner_failure"       # Routing/planning stage error (permanent)
    RENDERER_FAILURE = "renderer_failure"     # Rendering stage error (permanent)
    ADAPTER_TRANSIENT = "adapter_transient"   # Timeout, connection error (retryable)
    ADAPTER_PERMANENT = "adapter_permanent"   # Business logic rejection (permanent)
    ADAPTER_MISSING = "adapter_missing"       # No runtime adapter instance for target ID (permanent)
    DEADLINE_EXCEEDED = "deadline_exceeded"   # Delivery plan deadline passed (permanent)
    CAPACITY_REJECTION = "capacity_rejection" # Capacity controller exhausted or timed out (permanent)
    SHUTDOWN_REJECTION = "shutdown_rejection" # Runtime shutdown cancelled delivery (permanent)
    LOOP_SUPPRESSED = "loop_suppressed"       # Loop-prevention guard fired (permanent)
    POLICY_SUPPRESSED = "policy_suppressed"   # Route-policy denial (permanent, not retryable)
    CAPABILITY_SUPPRESSED = "capability_suppressed"  # Target adapter lacks capability for event kind (permanent)
```

Classification rules:

| Failure kind            | Pipeline stage     | Retryable | Auto-classified from exception                                               |
| ----------------------- | ------------------ | --------- | ---------------------------------------------------------------------------- |
| `PLANNER_FAILURE`       | Routing / planning | No        | Exception during `route_event()`                                             |
| `RENDERER_FAILURE`      | Rendering          | No        | Exception during `render()`                                                  |
| `ADAPTER_TRANSIENT`     | Adapter delivery   | **Yes**   | `TimeoutError`, `ConnectionError`, `OSError` hierarchy                       |
| `ADAPTER_PERMANENT`     | Adapter delivery   | No        | All other adapter exceptions                                                 |
| `ADAPTER_MISSING`       | Adapter lookup     | No        | Target adapter ID has no runtime adapter instance                            |
| `DEADLINE_EXCEEDED`     | Deadline check     | No        | `plan.deadline < now`                                                        |
| `CAPACITY_REJECTION`    | Capacity gate      | No        | Capacity controller semaphore exhausted or timed out                         |
| `SHUTDOWN_REJECTION`    | Capacity gate      | No        | Runtime shutdown cancelled delivery before capacity acquire                  |
| `LOOP_SUPPRESSED`       | Loop prevention    | No        | Self-loop or route-trace guard fired                                         |
| `POLICY_SUPPRESSED`     | Route policy       | No        | Route-policy evaluator denied delivery                                       |
| `CAPABILITY_SUPPRESSED` | Capability check   | No        | Target adapter does not support the event kind or required delivery features |

## 11. DeliveryOutcome

```python
@dataclass(frozen=True)
class DeliveryOutcome:
    event_id: str
    target_adapter: str
    target_channel: str | None
    route_id: str
    delivery_plan_id: str
    status: Literal["success", "queued", "transient_failure", "permanent_failure", "skipped"]
    failure_kind: DeliveryFailureKind | None = None
    receipt: DeliveryReceipt | None = None
    error: str | None = None
    duration_ms: float = 0.0
```

`failure_kind` is `None` on success. On failure, it carries the specific taxonomy member.

### 11.1 Per-Destination Independence

When a single event matches a route with multiple destinations, each destination produces an independent `DeliveryOutcome`:

- A success on one target does not imply success on another.
- A failure on one target does not prevent delivery to other targets.
- Each outcome has its own `DeliveryReceipt` (if a receipt was produced).

### 11.2 Success/Failure/Skip Semantics

| Status              | Meaning                                                                                                                                               | Receipt created?                                                            | Retryable?             |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- | ---------------------- |
| `success`           | Adapter reported successful handoff. The transport accepted the message.                                                                              | Yes                                                                         | N/A                    |
| `queued`            | Delivery enqueued for execution.                                                                                                                      | Yes                                                                         | N/A                    |
| `transient_failure` | Adapter reported a recoverable error (timeout, connection reset).                                                                                     | Yes                                                                         | Yes, per `RetryPolicy` |
| `permanent_failure` | Adapter reported an unrecoverable error, or delivery exhausted retries.                                                                               | Yes (last attempt)                                                          | No                     |
| `skipped`           | Pre-outbox suppression. A guard fired before rendering, capacity acquisition, or adapter invocation. Reason in `error`. No renderer. No adapter call. | Yes for stored route-target suppressions; none for no-route/pre-store dedup | No                     |

> **Distinction from failed send:** A `skipped` outcome and its corresponding `suppressed` receipt are semantically distinct from a failed send. Suppressed deliveries never invoke the adapter's `send()` method. The receipt `status` is `"suppressed"`, not `"failed"`. The `failure_kind` is a suppression kind (`LOOP_SUPPRESSED`, `POLICY_SUPPRESSED`, or `CAPABILITY_SUPPRESSED`), not an adapter error kind (`ADAPTER_TRANSIENT`, `ADAPTER_PERMANENT`). Suppressed deliveries do not enter the retry queue — `next_retry_at` is always `None` and `is_retryable` is always `False`.

### 11.2.1 Three Suppression Categories

Delivery suppression falls into three distinct categories:

| Category                              | Trigger                                                                                                           | Outcome                               | Receipt?                                                        |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------- | ------------------------------------- | --------------------------------------------------------------- |
| No route / no target                  | Event matched zero routes, or no target adapter found                                                             | No `DeliveryOutcome` produced         | None                                                            |
| Pre-outbox skip                       | Self-loop guard, route-trace cycle, policy denial, or capability `"unsupported"` before outbox/capacity/rendering | `DeliveryOutcome(status="skipped")`   | `DeliveryReceipt(status="suppressed")` for stored routed events |
| Post-planning direct-call suppression | Suppression after the planning stage, where recording provides defense-in-depth audit value                       | `DeliveryOutcome` with skip semantics | `DeliveryReceipt(status="suppressed")` MAY be recorded          |

**No route or no target.** The event matched zero routes (Section 3.4) or no target adapter was identified. No `DeliveryOutcome` is produced. No receipt is created. The event remains in the canonical event log and is available for replay if routes are added later.

**Pre-outbox skip.** A route matched and a target was identified, but a guard fired before the outbox, capacity, rendering, or adapter stages. This includes: self-loop guard (`target_adapter == source_adapter`), route-trace cycle detection (route ID appears more than once), route-policy denial (`failure_kind="policy_suppressed"`), and capability-level `"unsupported"` suppression for the event's relation type. Produces `DeliveryOutcome(status="skipped")` and persists `DeliveryReceipt(status="suppressed")` when the event has already been stored. No renderer invocation. No adapter call. The suppression reason is recorded in the outcome and receipt `error` fields; route and target attribution (`route_id`, `target_adapter`, `target_channel`) belong in dedicated receipt fields.

**Post-planning direct-call suppression.** Suppression that occurs after the planning stage, where recording a receipt provides defense-in-depth audit value. Produces `DeliveryReceipt(status="suppressed")`. The receipt includes `route_id`, `target_adapter`, `target_channel`, and the suppression reason. This category is distinct from pre-outbox skip: a receipt MAY exist, but no renderer or adapter was invoked.

#### 11.2.2 Skip and Suppression Examples

The following table shows concrete scenarios and which suppression category applies:

| Scenario                                                                           | Guard / Trigger                                       | Outcome                                                                        | Receipt?               | Renderer? | Adapter? |
| ---------------------------------------------------------------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------ | ---------------------- | --------- | -------- |
| Meshtastic reaction → Meshtastic target where target has `reactions="unsupported"` | Capability `"unsupported"` for reaction relation type | `DeliveryOutcome(status="skipped")`, `failure_kind="capability_suppressed"`    | Yes                    | No        | No       |
| Matrix message routed back to same Matrix adapter (`target == source`)             | Self-loop guard (`target_adapter == source_adapter`)  | `DeliveryOutcome(status="skipped")`, `failure_kind="loop_suppressed"`          | Yes                    | No        | No       |
| Route-policy `sender_allowlist` denies sender                                      | Route-policy evaluator denial                         | `DeliveryOutcome(status="skipped")`, `failure_kind="policy_suppressed"`        | Yes                    | No        | No       |
| Delivery plan constructed with `method="skip"` after Phase 2.75 planning           | Post-planning skip gate                               | `DeliveryReceipt(status="suppressed")`, `failure_kind="capability_suppressed"` | Yes (defense-in-depth) | No        | No       |
| Event matches zero routes                                                          | No matching route in routing engine                   | No `DeliveryOutcome` produced                                                  | None                   | No        | No       |

## 12. Policy Pipeline

### 12.1 Four-Stage Architecture

Policies are split into four stages that run at distinct pipeline positions:

| Stage        | Pipeline Position                                 | Scope              | What It Controls                                                                     |
| ------------ | ------------------------------------------------- | ------------------ | ------------------------------------------------------------------------------------ |
| **ingress**  | Before storage                                    | Raw inbound events | Rejects malformed, unauthorized, or rate-limited ingress at the adapter boundary     |
| **event**    | After transforms                                  | Derived events     | Rate limiting, content filtering, permission checks, deduplication                   |
| **route**    | After routing, before delivery planning           | Matched routes     | Per-route rate limits, quiet hours, permission checks, route-policy allowlist checks |
| **delivery** | Before adapter execution, after delivery planning | Delivery plans     | Adapter-specific size limits, capability downgrade, final content filtering          |

### 12.2 Route Policy Evaluator

The route-policy evaluator is a pure-function evaluator that checks five allowlist fields after a route matches, in deterministic order:

1. `allowed_source_adapters` — source adapter name MUST be in the list (empty = any)
2. `allowed_dest_adapters` — destination adapter name MUST be in the list (empty = any)
3. `sender_allowlist` — canonical sender identity MUST be in the list (empty = any)
4. `room_allowlist` — source room identifier MUST be in the list when present (empty = any)
5. `channel_allowlist` — target channel MUST be in the list (empty = any)

The first denial wins. A denial produces `failure_kind="policy_suppressed"` and is not retryable.

### 12.3 Policy Denial Outcome

A policy denial is a pre-outbox skip. It produces a `DeliveryOutcome(status="skipped")` with `failure_kind="policy_suppressed"` and the reason code recorded in the `error` field. For stored routed events, a `DeliveryReceipt(status="suppressed")` is also persisted with the same route, target, plan, and reason evidence. No renderer is invoked. No adapter call is made. Policy denials are not retryable and are a permanent classification.

Policy suppression reason codes:

| Reason code                  | Policy field              |
| ---------------------------- | ------------------------- |
| `source_adapter_not_allowed` | `allowed_source_adapters` |
| `dest_adapter_not_allowed`   | `allowed_dest_adapters`   |
| `sender_not_allowed`         | `sender_allowlist`        |
| `room_not_allowed`           | `room_allowlist`          |
| `channel_not_allowed`        | `channel_allowlist`       |

### 12.4 Pipeline Flow

```text
derived event
    │
    ▼
[event policy stage]  (rate limit, dedup, content filter, permission)
    │
    ▼
[router]  (evaluate all enabled routes, collect matches)
    │
    ▼
[delivery planner]  (construct DeliveryPlan per surviving target)
    │
    ▼
[route policy stage]  (per-target delivery preflight: allowlist checks)
    │   Routes that survive become candidate deliveries
    │   Policy denials → DeliveryOutcome(status="skipped"), failure_kind="policy_suppressed"
    ▼
[delivery policy / rendering]  (size limits, capability fallback, final rendering)
    │
    ▼
[adapter execution]  (dequeued and delivered per adapter rate limits and state)
    │
    ▼
[receipt]  (append-only receipt row)
```

## 13. Local Acceptance vs Remote Delivery

### 13.1 Route Layer Preserves Adapter Semantics

The routing layer does not alter the delivery semantics of any transport. Each adapter's `deliver()` method returns an `AdapterDeliveryResult`, and the pipeline records what the adapter reported — honestly and without upgrade.

| Transport  | Adapter reports              | Routing layer records            | Does routing upgrade?                    |
| ---------- | ---------------------------- | -------------------------------- | ---------------------------------------- |
| Matrix     | `event_id` from homeserver   | `sent` with `adapter_message_id` | No.                                      |
| Meshtastic | Local node acceptance only   | `sent` without confirmation      | No. Radio best-effort stays best-effort. |
| MeshCore   | Local node acceptance only   | `sent` without confirmation      | No. Radio best-effort stays best-effort. |
| LXMF       | Local `LXMRouter` acceptance | `sent` without confirmation      | No. Store-and-forward stays eventual.    |

### 13.2 Status Semantics Are Transport-Relative

| Status   | Matrix meaning           | Radio meaning            | LXMF meaning             |
| -------- | ------------------------ | ------------------------ | ------------------------ |
| `sent`   | Homeserver accepted      | Local node queued        | Local router accepted    |
| `failed` | Adapter-reported failure | Adapter-reported failure | Adapter-reported failure |

All adapters report `sent` on successful handoff; there is no separate `confirmed` status.

### 13.3 Receipt Honesty

Receipts are the audit trail. They MUST be trustworthy. The runtime:

- Records the adapter's reported status honestly.
- MUST NOT upgrade a receipt status retroactively.
- Records `attempt_number` and `parent_receipt_id` to form retry lineage.
- Records `route_id` on every receipt for attribution.

### 13.4 Native Message ID Requirements

`native_message_id` and `native_channel_id` on `AdapterDeliveryResult` MUST be platform-provided values. Adapters MUST NOT fabricate or backfill these values. The pipeline MUST NOT backfill `native_channel_id` or any other native ref field from route configuration.

### 13.5 Per-Adapter Delivery Semantics

| Adapter        | `deliver()` completion meaning                                      | `native_message_id` source                           | ACK limitation                                                                                          |
| -------------- | ------------------------------------------------------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| **Matrix**     | SDK `room_send` returns `event_id`; homeserver accepted and stored  | Matrix event ID from `RoomSendResponse`              | Synchronous server ACK. Server received ≠ delivered to clients. No end-to-end delivery receipt.         |
| **MeshCore**   | SDK `send_text()` / `send_data()` returns; message locally accepted | MeshCore message reference (timestamp-based)         | No end-to-end ACK. `delivery_note` documents local-acceptance only.                                     |
| **Meshtastic** | Message locally enqueued to outbound queue                          | `None` — no native send confirmation at enqueue time | Local-acceptance only. Actual radio send is async via queue worker. No platform ACK returned to caller. |
| **LXMF**       | LXMF message dispatched to `LXMRouter`                              | LXMF message hash (hex of `LXMessage.hash`)          | Store-and-forward eventual delivery. Async state progression through delivered/failed.                  |

**Key asymmetries:**

- Matrix is the only adapter where `deliver()` completion implies confirmed server-side storage.
- Meshtastic is the only adapter where `native_message_id` is `None` at `deliver()` return time.
- MeshCore returns a local reference but explicitly notes local-acceptance since there is no end-to-end ACK.
- LXMF returns a content-addressed hash immediately, but actual delivery is asynchronous through the mesh.
- All four adapters treat not-connected and SDK-not-initialized as **permanent** errors.

## 14. Failure Taxonomy Per Transport

### 14.1 Matrix

#### Connection Failures

| Failure                | Transient/Permanent               | Reconnectable   |
| ---------------------- | --------------------------------- | --------------- |
| Network unreachable    | Transient                         | Yes             |
| DNS resolution failure | Transient                         | Yes             |
| TLS handshake failure  | Transient (misconfig → permanent) | Yes (transient) |
| HTTP 429 (rate limit)  | Transient                         | Yes             |
| HTTP 401/403 (auth)    | Permanent                         | No              |
| Homeserver shutdown    | Transient                         | Yes             |

#### Send Failures

| Failure                | Transient/Permanent         | Duplicate-Send Risk |
| ---------------------- | --------------------------- | ------------------- |
| `room_send` HTTP error | Transient (4xx → permanent) | Low–Medium          |
| `room_send` timeout    | Transient                   | Medium              |
| Room not joined        | Permanent                   | None                |
| Message too large      | Permanent                   | None                |

#### Queue-Drain Semantics

No outbound queue. `deliver()` calls `room_send` directly.

#### Delivery Uncertainty Window

~0 (server-side) to one sync cycle for inbound confirmation.

#### Encrypted-Room Failure Classes

| Failure                                            | Class                    | Recovery                                   |
| -------------------------------------------------- | ------------------------ | ------------------------------------------ |
| Missing crypto dependency (vodozemac)              | Permanent, startup-fatal | Install dependency and restart             |
| Device not verified                                | Permanent per message    | Verify device via interactive verification |
| Megolm session not received                        | Transient                | Wait for session key from other device     |
| `encryption_mode="e2ee_required"` + plaintext room | Permanent                | Adapter raises `AdapterPermanentError`     |

#### Connection Failures (Meshtastic)

| Failure                 | Transient/Permanent                         | Reconnectable         |
| ----------------------- | ------------------------------------------- | --------------------- |
| TCP connection refused  | Transient                                   | Yes                   |
| Serial port unavailable | Transient (permission) → Permanent (absent) | Yes (transient)       |
| BLE pairing failure     | Transient                                   | Yes                   |
| Radio firmware crash    | Transient                                   | Yes (if node reboots) |

#### Send Failures (Meshtastic)

| Failure                          | Transient/Permanent | Duplicate-Send Risk |
| -------------------------------- | ------------------- | ------------------- |
| `sendText` exception (transient) | Transient           | **High**            |
| `sendText` exception (permanent) | Permanent           | None                |
| Channel busy                     | Transient           | Medium              |
| Packet too large                 | Permanent           | None                |

#### Queue-Drain Semantics (Meshtastic)

Bounded-retry outbound queue (`MeshtasticOutboundQueue`). Transient send failures are retried up to `queue_send_max_attempts`. Exhausted retries and permanent failures are dropped. No persistence; queue contents are lost on adapter shutdown.

#### Delivery Uncertainty Window (Meshtastic)

Unbounded. No end-to-end delivery confirmation exists in the Meshtastic protocol for text messages.

#### Outbound Gate

The Meshtastic adapter supports `outbound_mode` with values `"enabled"` (default) and `"listen_only"`. When `outbound_mode = "listen_only"`, `deliver()` rejects all outbound payloads as non-retryable failures before RF transmission. This is an intentional operator-configured gate, not a transport failure.

#### Shutdown Queue Non-Guarantee

The Meshtastic adapter-local outbound queue is in-memory and non-durable. Items remaining at process termination are lost — not persisted, not requeued, not recovered on restart.

### 14.3 MeshCore

#### Connection Failures (MeshCore)

| Failure                 | Transient/Permanent   | Reconnectable   |
| ----------------------- | --------------------- | --------------- |
| TCP connection refused  | Transient             | Yes             |
| Serial port unavailable | Transient → Permanent | Yes (transient) |
| BLE pairing failure     | Transient             | Yes             |
| SDK connect timeout     | Transient             | Yes             |

#### Send Failures (MeshCore)

| Failure                           | Transient/Permanent | Duplicate-Send Risk |
| --------------------------------- | ------------------- | ------------------- |
| `send_text` exception (transient) | Transient           | **Medium**          |
| `send_text` exception (permanent) | Permanent           | None                |
| Channel index invalid             | Permanent           | None                |

#### Queue-Drain Semantics (MeshCore)

No outbound queue. `send_text()` is called directly on the session.

#### Delivery Uncertainty Window (MeshCore)

Unbounded. No end-to-end delivery confirmation.

### 14.4 LXMF / Reticulum

#### Connection Failures (LXMF)

| Failure                       | Transient/Permanent | Reconnectable      |
| ----------------------------- | ------------------- | ------------------ |
| RNS.Reticulum init failure    | Permanent           | No (session-level) |
| Identity file missing/corrupt | Permanent           | No                 |
| LXMRouter init failure        | Permanent           | No                 |
| Transport interface down      | Transient           | Yes                |

#### Send Failures (LXMF)

| Failure                       | Transient/Permanent                        | Duplicate-Send Risk |
| ----------------------------- | ------------------------------------------ | ------------------- |
| `handle_outbound` exception   | Transient (network) → Permanent (identity) | **Low**             |
| Destination unreachable       | Transient (long-lived)                     | None                |
| Message rejected by recipient | Permanent                                  | None                |
| Propagation node unavailable  | Transient                                  | Low                 |

#### Queue-Drain Semantics (LXMF)

No outbound queue in MEDRE. `send()` calls `handle_outbound` directly on the LXMRouter. The router manages its own internal delivery queue.

#### Delivery Uncertainty Window (LXMF)

Effectively unbounded for propagated delivery. Multi-hop Reticulum transport can introduce seconds to hours of delivery latency.

## 15. Cross-Transport Failure Summary

| Dimension                           | Matrix                                   | Meshtastic                               | MeshCore                                 | LXMF                                     |
| ----------------------------------- | ---------------------------------------- | ---------------------------------------- | ---------------------------------------- | ---------------------------------------- |
| **Transient failure primary cause** | Network/auth/rate-limit                  | Radio/link/serial                        | Radio/link/serial                        | Network/RNS transport                    |
| **Permanent failure primary cause** | Auth revocation, config error            | Config error, port error                 | Config error                             | Identity/RNS init error                  |
| **Reconnect model**                 | Exponential backoff, 10 attempts, 1–60 s | Exponential backoff, 10 attempts, 1–30 s | Exponential backoff, 10 attempts, 1–30 s | Exponential backoff, 10 attempts, 1–30 s |
| **Duplicate-send risk**             | Low–Medium                               | High                                     | Medium                                   | Low                                      |
| **Outbound queue**                  | None (direct send)                       | Bounded retry (lossy drain)              | None (direct send)                       | None (router-managed)                    |
| **Delivery confirmation**           | Server event_id (sync)                   | None (fire-and-forget)                   | None (fire-and-forget)                   | Async state callback                     |
| **Uncertainty window**              | ~0 (server-side) to one sync cycle       | Unbounded                                | Unbounded                                | Unbounded                                |
| **E2EE failure class**              | Megolm session loss, device verification | N/A                                      | N/A (radio-level, not MEDRE-managed)     | N/A (identity-based signing)             |
| **ACK model**                       | HTTP response                            | LoRa hop-by-hop (unreliable)             | Link-level (unreliable)                  | Reticulum transport-dependent            |

### 15.1 Route Policy Suppression (Cross-Transport)

Route policy suppression is a cross-transport failure classification. It occurs when the route-policy evaluator denies a delivery after route matching but before delivery side effects.

| Property       | Value                                                                                                                                                                                                                        |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Failure kind   | `policy_suppressed`                                                                                                                                                                                                          |
| Retryable      | No — permanent classification                                                                                                                                                                                                |
| Pipeline stage | Route policy (after route match, before delivery)                                                                                                                                                                            |
| Outcome        | `DeliveryOutcome(status="skipped")` plus `DeliveryReceipt(status="suppressed")` for stored routed events (Section 11.2.1)                                                                                                    |
| Error context  | `error` field carries the denial reason/message (e.g., `'text capability unsupported'`, `'loop detected'`). Route and target attribution belong in dedicated receipt fields: `route_id`, `target_adapter`, `target_channel`. |

## 16. Duplicate-Send Risk Classification

| Risk Level | Transport(s) | Rationale                                                                                                                                                      |
| ---------- | ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Low        | LXMF         | Content-addressed message hashes naturally deduplicate. Session does not retry outbound sends automatically.                                                   |
| Low–Medium | Matrix       | Server-assigned event IDs. Deterministic tx_id for dedup within homeserver window. Duplicates possible under timeout/retry or across restarts/replay.          |
| Medium     | MeshCore     | Session retries transient failures up to 3 times. ACK may have been lost. Consumers MUST tolerate duplicates.                                                  |
| High       | Meshtastic   | Session retries transient failures up to 3 times. Radio ACKs unreliable. Firmware-level CSMA may independently retransmit. Consumers MUST tolerate duplicates. |

**General principle:** The runtime does not suppress duplicate sends. Retries after transient failures MAY produce duplicates if the first send succeeded but the response was lost. Radio operators expect duplicates. Bridge fan-out produces independent deliveries per target.

## 17. Route Startup and Dynamic Reload

### 17.1 Startup Validation

When the runtime loads route configuration:

1. All route IDs MUST be unique. Duplicate `route_id` values are a startup error.
2. Every `to[].adapter` MUST reference an adapter that exists in the `adapters` configuration. Referencing a non-existent adapter is a startup error.
3. Every `from_.adapter` that is not `None` MUST reference an existing adapter.
4. `from_.event_kinds` MUST NOT be empty.
5. `to` list MUST NOT be empty.
6. `channel` and `destination` on the same `RouteTarget` MUST NOT both be set when `destination.kind` is `"channel"` or `"matrix_room"`.
7. Routes referencing disabled adapters MAY be loaded but will never match.
8. Unknown keys in route policy sections are rejected at config load time. Allowlist values MUST be arrays of strings; bare strings are rejected.
9. Source and destination adapters MUST NOT overlap within a single route.

### 17.2 Dynamic Reload

When route configuration is reloaded at runtime:

- New routes are added to the active set immediately and begin matching new events.
- Removed routes are removed from the active set. In-flight delivery plans for removed routes continue to completion.
- Modified routes are replaced atomically. Events currently being routed that already matched the old version continue with the old match. New events see the updated route.
- Configuration validation runs before applying changes. If validation fails, the old configuration remains active and an error is logged.

### 17.3 Route Registration Order

Routes register in the same order they appear in configuration. Registration is deterministic. `validate_route_adapter_refs` runs before any route is registered. If any enabled route references an adapter ID not present in the assembled runtime, startup fails.

## 17.5 Relay Attribution Prefix

### 17.5.1 Purpose and Scope

Transport renderers MAY prepend a human-readable relay attribution prefix to
outbound message text. The prefix is derived from a template string
configured per adapter (e.g. `radio_relay_prefix`, `meshcore_relay_prefix`,
`lxmf_relay_prefix`) or per target adapter (e.g. `relay_prefix` on
`MatrixConfig`).

The prefix is **human-readable attribution only**. It does not constitute
delivery evidence and MUST NOT be interpreted as provenance by downstream
consumers. The authoritative source for machine-readable provenance is the
MEDRE metadata namespace (`medre.envelope` on Matrix, `fields[0xFD]` on
LXMF, `RenderingResult.metadata` on all transports).

### 17.5.2 origin_label

`origin_label` is the platform-neutral, operator-defined source label for
relay prefixes. Each adapter config declares an `origin_label` field
(string, default `""`), set by the operator to a short human-readable name
for the adapter's origin (e.g. `"East Mesh"`, `"HQ Matrix"`).

- `origin_label` is NOT delivery evidence.
- `origin_label` is NOT a routing key.
- `origin_label` is NOT a native transport identity (it is not a node ID,
  MXID, pubkey prefix, or Reticulum hash).

The canonical field on `RelayAttribution` is `source_origin_label`. The
template alias is `{origin_label}`. Renderers look up `source_origin_label`
from the source adapter config via the runtime source-attribution registry.

### 17.5.3 Label and Identity Distinctions

| Concept               | Template variable | Source                                      | Scope                                                           |
| --------------------- | ----------------- | ------------------------------------------- | --------------------------------------------------------------- |
| `origin_label`        | `{origin_label}`  | Source adapter config `origin_label`        | Platform-neutral operator label                                 |
| `source_sender_id`    | `{from_id}`       | Native sender ID from source event metadata | Per-transport native identity                                   |
| `source_display_name` | —                 | Best-effort human-readable display name     | Per-transport native name                                       |
| `meshnet_name`        | `{meshnet_name}`  | Source adapter config `meshnet_name`        | Transport-specific network name, NOT MEDRE-generic              |
| `route_id`            | `{route_id}`      | Matched route                               | Route identification (may be empty if no route trace available) |

Operators SHOULD prefer `{origin_label}` over `{meshnet_name}` in
cross-platform prefix templates. `meshnet_name` is transport-specific
(radio mesh network name) and may be empty or semantically irrelevant for
non-radio transports. `origin_label` is the single MEDRE-generic label.

### 17.5.4 Renderer Lookup

Renderers resolve `source_origin_label` through the runtime
source-attribution registry. The registry maps adapter instance names to
their `origin_label` config value. When a source adapter has no
`origin_label` configured (empty string), the template variable resolves to
an empty string — no label is rendered.

For the Matrix outbound prefix specifically, `MatrixConfig.relay_prefix`
(string, default `""`) is the target-local prefix template. The Matrix
renderer reads this field from its own config (target-local), not from the
source adapter's config. The old path through
`MeshtasticConfig.matrix_relay_prefix` is a backward-compatibility fallback
only — new configurations SHOULD use `MatrixConfig.relay_prefix`.

For Meshtastic, MeshCore, and LXMF outbound prefixes, the prefix template
lives on the respective target adapter config (`radio_relay_prefix`,
`meshcore_relay_prefix`, `lxmf_relay_prefix`). Variables within those
templates are resolved from the source event's `RelayAttribution`, which
includes `source_origin_label` from the source-attribution registry.

### 17.5.5 Shared Formatter and Variable Schema

The shared prefix formatter (`format_relay_prefix` in
`src/medre/core/rendering/attribution.py`) defines a single set of template
variables (canonical `source_*` fields plus aliases `longname`, `shortname`,
`shortname5`, `from_id`, `meshnet_name`, `origin_label`). All four transport
renderers use the same formatter and the same variable schema. The
authoritative variable list is documented in the Meshtastic Transport
Profile §Relay Attribution Prefix.

Formatting rules: `None`/missing values format as empty strings. Unknown
placeholders are left unchanged in the output and recorded in diagnostic
metadata. The formatter never raises exceptions.

### 17.5.6 False Delivery Claims

No relay prefix, regardless of its content, constitutes a delivery claim.
Prefixes are prepended to message text for human readability only. The
metadata namespace remains the authoritative source for machine-readable
provenance and delivery evidence. Operators MUST NOT rely on prefix text for
routing decisions, delivery confirmation, or identity verification.

## 18. Non-Goals

This specification explicitly does **not** provide:

- **Exactly-once delivery.** No transport provides exactly-once semantics, and MEDRE does not synthesize them. Duplicates are a transport-level reality that consumers MUST tolerate.

- **RF confirmation.** Meshtastic, MeshCore, and similar radio transports are fire-and-forget. No end-to-end delivery confirmation exists at the radio layer. A `sent` status means local acceptance only.

- **Universal ACKs.** ACK semantics differ fundamentally across transports (synchronous HTTP, asynchronous radio ACK, implicit, or none). MEDRE does not normalize these into a universal confirmation model.

- **End-to-end delivery confirmation for any transport.** Matrix provides server-side confirmation only. The three constrained transports provide none. Even Matrix's confirmation does not guarantee delivery to end clients.

- **Distributed loop prevention.** Loop prevention operates within a single MEDRE process only. No shared state exists between instances.

- **Transport-level ordering guarantees across adapters.** Events from different adapters are ordered by timestamp at best, and timestamps are not reliable clocks on constrained devices.

- **Per-user or per-identity route ownership.** Routes are operator-owned configuration that apply globally to all matching events.

- **E2EE at the pipeline level.** Encryption is an adapter-internal concern, not a pipeline concern. MEDRE sends and receives plaintext payloads; the transport encrypts them.

- **Attachment or media delivery.** MEDRE handles text and replies only in the current release.

- **Cross-transport deduplication.** Native message IDs have different scopes and stability across transports. Code that compares native IDs across adapters is incorrect.

# Adapter Runtime Specification

> **Classification:** Normative
> **Authority:** Authoritative specification for MEDRE adapter runtime, protocols, lifecycle, capabilities, and delivery semantics

## Conformance Keywords

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHALL**, **SHALL NOT**, **SHOULD**, **SHOULD NOT**, **RECOMMENDED**, **MAY**, and **OPTIONAL** in this document are to be interpreted as described in RFC 2119.

## 1. Purpose

This document is the authoritative normative specification for the MEDRE adapter runtime. It defines the protocols, types, lifecycle, capabilities, delivery semantics, session boundaries, and containment rules that every adapter implementation **MUST** satisfy.

Adapters are the boundary between MEDRE's internal event pipeline and external transports or presentation platforms. An adapter that conforms to this specification can be registered with the runtime, and the runtime handles routing, delivery planning, policy evaluation, receipt tracking, and observability.

---

## 2. Adapter Roles

Every adapter declares a role. The role **MUST** be inferred from the adapter type at configuration load time; operators **MUST NOT** set it manually.

```python
class AdapterRole(Enum):
    TRANSPORT    = "transport"
    PRESENTATION = "presentation"
    HYBRID       = "hybrid"
```

| Role             | Responsibility                                                                                                                         | Examples                                                   |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| **TRANSPORT**    | Moves data to/from a physical or logical transport. Handles protocol specifics, connection management, and raw data encoding/decoding. | Meshtastic, MeshCore, LXMF, MQTT, TCP serial bridge, AX.25 |
| **PRESENTATION** | Presents events to human users. Handles formatting, rich content, threading, reactions, and user interaction.                          | Matrix, Discord, Telegram, Slack, Web UI                   |
| **HYBRID**       | Both transports and presents. Acts as a message source and a display target simultaneously.                                            | IRC, XMPP                                                  |

TRANSPORT adapters typically ingest raw protocol data and produce canonical events. PRESENTATION adapters receive delivery plans and render events for human consumption. HYBRID adapters do both. The role determines which pipeline stages the adapter participates in and how the routing engine treats it.

---

## 3. Adapter Protocol

The adapter interface is defined by the `AdapterContract` abstract base class. Every adapter **MUST** satisfy this interface.

### 3.1 Interface Definition

```python
class AdapterContract(ABC):
    adapter_id: str            # Unique adapter instance identifier
    platform: str              # Human-readable platform name (e.g. "meshtastic", "matrix")
    role: AdapterRole          # TRANSPORT, PRESENTATION, or HYBRID

    @abstractmethod
    async def start(self, ctx: AdapterContext) -> None: ...

    @abstractmethod
    async def stop(self, timeout: float) -> None: ...

    @abstractmethod
    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult: ...

    @abstractmethod
    async def health_check(self) -> AdapterInfo: ...

    def get_codec(self) -> AdapterCodec | None: ...
```

### 3.2 `start(context)`

The runtime calls `start()` once during initialization. The adapter **MUST**:

1. Establish whatever connection or session its transport requires.
2. Register internal listeners or callbacks that feed into `context.publish_inbound()`.
3. Transition its internal health state from `"unknown"` to `"healthy"` or `"degraded"` as appropriate.
4. Call `self._mark_started(ctx)` to record the adapter's start time for stale-event filtering.
5. Return only after the adapter is ready to accept delivery work or after the connection attempt has progressed far enough to report a definitive health state.

The runtime does not time out `start()`. The adapter **MUST** handle its own connection timeouts internally and report `"failed"` if the transport cannot be reached.

### 3.3 `stop(timeout)`

The runtime calls `stop()` once during graceful shutdown. The adapter **MUST**:

1. Reject new delivery work.
2. Complete in-flight deliveries within `timeout` seconds if the transport permits.
3. Close connections, cancel all spawned background asyncio tasks, and release resources.
4. Transition health state to `"unknown"` or `"stopped"`.

No orphaned asyncio tasks **MUST** remain after `stop()` returns. Leaked tasks after `stop()` returns are a bug.

The runtime **MUST** invoke and await the stop hook inside its supervised
deadline task. Call-time errors, including argument-binding errors, **MUST NOT**
bypass cleanup of other adapters, the pipeline, or storage. A hook may return a
coroutine or another awaitable such as a Future. Stop implementations **MUST**
remain non-blocking and cooperate with cancellation.

### 3.4 `deliver(result)`

```python
async def deliver(self, result: RenderingResult) -> AdapterHandoffResult
```

The pipeline guarantees that `result` has already been rendered by a `Renderer` operating within a strict `RenderingContext`. The adapter **MUST NOT** re-render, reformat, or inspect the event kind to decide formatting. It **SHALL** merely transport the pre-rendered payload to the external platform.

On success, the adapter **MUST** return an `AdapterHandoffResult`. Native IDs are optional transport facts; absence of a native ID is not absence of a successful hand-off.

If delivery fails, the adapter **MUST** raise `AdapterSendError` (transient) or `AdapterPermanentError` (permanent). The adapter **MUST NOT** write receipts, update delivery state, or trigger pipeline-level retries. The pipeline owns all of that. Bounded transport-call retries within the session send path (e.g., up to 3 attempts for transient SDK send failures — see §14.1 Session Ownership table, "Send retry" row) are permitted and expected; what is forbidden is the adapter implementing its own durable retry loops or retry scheduling outside the single `deliver()` call.

This is the only outbound method. There is no `send()`, no `push()`, no `emit()`. Delivery is always `RenderingResult`-driven.

### 3.5 `health_check()`

The runtime calls `health_check()` periodically via the lifecycle manager. It **MUST** be cheap and non-blocking. It **MUST** return a fresh `AdapterInfo` describing the adapter's current state.

### 3.6 Stale Event Filtering

Adapters **MUST** call `self.publish_inbound(event)` (the base class method) instead of `self.ctx.publish_inbound(event)` directly. The base class method silently drops events whose `timestamp` predates the adapter's start time, preventing historical or replayed events from previous sessions from entering the inbound pipeline.

### 3.7 Codec Access

Adapters **MAY** expose an `AdapterCodec` via `get_codec()`. The default implementation returns `None`.

---

## 4. AdapterCodec Protocol

The codec handles conversion between native protocol data and canonical events. It is an adapter-private concern. Adapters **MAY** implement it as a separate class or inline the logic.

```python
class AdapterCodec(ABC):
    @abstractmethod
    def decode(self, native_event: Any) -> CanonicalEvent: ...
```

### 4.1 `decode(native_event)`

Converts a native (adapter-specific) event into a `CanonicalEvent`. Called by the adapter's inbound listener after receiving raw data. The codec **MUST** set at minimum: `event_id`, `event_kind`, `schema_version`, `timestamp`, `source_adapter`, `source_transport_id`, and `payload`.

### 4.2 Codec Restrictions

Outbound transformation is owned exclusively by renderers; `AdapterCodec` is decode-only.

The codec owns format translation and nothing else. It **MUST NOT**:

- Call `publish_inbound` (that is the adapter's job).
- Make routing decisions.
- Enrich events with data from other adapters.
- Apply policy rules.

The codec **MUST**:

- Map native fields to canonical event fields.
- Set `source_adapter` to the adapter instance name.
- Set `source_transport_id` to the native actor identity (not the native message ID).
- Set `source_channel_id` to the native channel/room/topic where the event originated.
- Populate `metadata.transport`, `metadata.radio`, `metadata.telemetry`, and `metadata.native` as appropriate.
- Preserve native message references for correlation.

---

## 5. AdapterContext

Each adapter receives an `AdapterContext` on startup. This deliberately narrow
object is the adapter's only runtime integration surface.

```python
@dataclass
class AdapterContext:
    adapter_id: str
    publish_inbound: Callable[[CanonicalEvent], Awaitable[None]]
    logger: logging.Logger
    clock: Callable[[], datetime]
    shutdown_event: Any
    admit_inbound: Callable[..., Awaitable[AdmissionResult]] | None = None
    load_checkpoint: Callable[..., Awaitable[AdapterCheckpoint | None]] | None = None
    commit_checkpoint: Callable[..., Awaitable[None]] | None = None
    report_delivery_feedback: Callable[[DeliveryFeedback], Awaitable[None]] | None = None
```

### 5.1 Field Semantics

| Field                                   | Purpose                                                                                                                  |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `adapter_id`                            | Unique runtime adapter instance identifier.                                                                              |
| `publish_inbound`                       | Publish ordinary live canonical ingress. Adapters call `self.publish_inbound()` so the common stale-event guard applies. |
| `admit_inbound`                         | Optional durable ingress admission for protocols with cursor/provenance semantics.                                       |
| `load_checkpoint` / `commit_checkpoint` | Optional application-owned stream checkpoint persistence.                                                                |
| `logger`                                | Adapter-scoped logger.                                                                                                   |
| `clock`                                 | Runtime UTC clock used instead of wall-clock globals.                                                                    |
| `shutdown_event`                        | Runtime graceful-shutdown signal.                                                                                        |
| `report_delivery_feedback`              | Single optional sink for the closed asynchronous `DeliveryFeedback` union.                                               |

### 5.2 Adapter Restrictions

Protocols that acknowledge events through durable stream checkpoints MUST admit
eligible events received while `start()` is in progress, or explicitly defer them
to their SDK's unaccepted-callback path. Lifecycle inactivity MUST NOT count as
acceptance of an event that has not been retained. This includes cursor-resumed
live/recovered events and initial history; history remains suppressed from routing
by core admission. Inactive or stopping adapters MUST defer durable callbacks
rather than allowing a checkpoint to advance without retaining the event.
Intentional policy filtering and self/echo suppression remain valid acceptance
outcomes. The ordinary non-durable `publish_inbound` startup filter remains
independent of this requirement.

Adapters **MUST NOT** import/call another adapter, bypass the pipeline, mutate
published events, access internal routing/event-bus objects, write receipts or
outbox state, or infer lifecycle decisions from feedback persistence.

---

## 6. Adapter Capabilities

`AdapterCapabilities` describes presentation/planning support only. It is an
immutable dataclass with conservative defaults; a new adapter therefore does
not gain functionality merely by existing. Delivery timing, acknowledgement
strength, and asynchronous completion are **not** capabilities — those facts
are expressed per attempt by `AdapterHandoffResult` and `DeliveryFeedback`.

```python
@dataclass(frozen=True)
class AdapterCapabilities:
    text: bool = True
    title: bool = False
    replies: str = "native"
    reactions: str = "native"
    edits: str = "native"
    deletes: str = "native"
    attachments: bool = False
    metadata_fields: bool = False
    store_and_forward: bool = False
    direct_messages: bool = True
    channels: bool = True
    identity_encryption: bool = False
    presence: bool = False
    topic_rooms: bool = False
    mesh_routing: bool = False
    priority_delivery: bool = False
    max_text_bytes: int | None = None
    max_text_chars: int | None = None
    threads: str = "unsupported"
    typing: bool = False
```

`identity_encryption` remains a boolean describing native identity-level
semantics. It MUST NOT be interpreted as a per-message encryption guarantee or
as an optional/required/disabled conversation-security policy. Those are separate
axes: a transport can use E2EE while this flag is false. Capability and metadata
values MUST NOT imply end-to-end confidentiality across a decrypting bridge.
See [messenger interoperability](../dev/messenger-interoperability.md) for the
rendering, activity, identity, and negotiation boundaries considered before freeze.

Relation fields (`replies`, `reactions`, `edits`, `deletes`, `threads`) use the
closed semantic levels `native`, `fallback`, and `unsupported`. Boolean and
size-limit fields are direct planning facts. `CapabilityDecisionResolver` and
`FallbackResolver` interpret those facts into the authoritative `DeliveryPlan`;
adapters and renderers consume that plan and **MUST NOT** re-decide capability
policy during delivery.

Capabilities that are merely properties of a provider SDK but are not exercised
by MEDRE remain undeclared/false. Conversely, later transport observations do
not require a capability flag: an adapter may emit `PostHandoffObservation`
whenever the transport actually supplies that fact for an outbox-backed attempt.

---

## 7. AdapterInfo and AdapterHealth

### 7.1 AdapterInfo

```python
@dataclass(frozen=True)
class AdapterInfo:
    adapter_id:    str                   # Unique instance identifier
    platform:      str                   # Platform name (e.g., "meshtastic", "matrix")
    role:          AdapterRole           # TRANSPORT, PRESENTATION, or HYBRID
    version:       str                   # Adapter implementation version
    capabilities:  AdapterCapabilities   # What this adapter can do
    health:        str = "unknown"       # One of: "healthy", "degraded", "failed", "unknown", "starting", "stopping"
```

Registered in the adapter registry at startup. Queried by the routing engine, delivery planner, and management interfaces.

### 7.2 Health State Values

`health_check()` **MUST** return an `AdapterInfo` with `health` set to one of the following protocol-neutral strings:

| State        | Meaning                                                                |
| ------------ | ---------------------------------------------------------------------- |
| `"unknown"`  | Adapter not started, stopped, or health indeterminate                  |
| `"healthy"`  | Transport connected and operational                                    |
| `"degraded"` | Transport partially functional (intermittent connection, high latency) |
| `"failed"`   | Transport disconnected or non-functional                               |
| `"starting"` | Adapter is initializing                                                |
| `"stopping"` | Adapter is shutting down                                               |

The adapter sets its own health state. The runtime reads it. The runtime **MUST NOT** set adapter health.

---

## 8. Adapter Lifecycle States

### 8.1 Lifecycle State Enum

The lifecycle state machine has **eight** states, defined by the
`AdapterState` enum in `src/medre/core/lifecycle/states.py`. The runtime owns
the transitions recorded in `VALID_TRANSITIONS`. Terminal states (no outgoing
transitions) are `FAILED` and `STOPPED`.

```python
class AdapterState(Enum):
    INITIALIZING  = "initializing"
    READY         = "ready"
    DEGRADED      = "degraded"
    BACKPRESSURED = "backpressured"
    DISCONNECTED  = "disconnected"
    STOPPING      = "stopping"
    FAILED        = "failed"          # Terminal
    STOPPED       = "stopped"         # Terminal
```

| State           | Meaning                                                                             |
| --------------- | ----------------------------------------------------------------------------------- |
| `INITIALIZING`  | Adapter is being set up; not yet ready to process events.                           |
| `READY`         | Adapter is fully operational.                                                       |
| `DEGRADED`      | Adapter is partially functional (e.g., high latency, missing features).             |
| `BACKPRESSURED` | Adapter's outbound queue is full; inbound traffic **MUST** be throttled.            |
| `DISCONNECTED`  | Adapter has lost its transport connection.                                          |
| `STOPPING`      | Adapter is shutting down gracefully.                                                |
| `FAILED`        | Adapter has encountered an unrecoverable error. Terminal — no outgoing transitions. |
| `STOPPED`       | Adapter has shut down cleanly. Terminal — no outgoing transitions.                  |

### 8.2 State Transition Graph

`VALID_TRANSITIONS[source]` is the frozenset of states that `source` may move
to in a single transition. The runtime **MUST** raise
`InvalidStateTransition` for any move not listed below.

```text
INITIALIZING  -> READY
INITIALIZING  -> STOPPING
INITIALIZING  -> STOPPED
INITIALIZING  -> FAILED

READY         -> DEGRADED
READY         -> BACKPRESSURED
READY         -> DISCONNECTED
READY         -> STOPPING
READY         -> FAILED

DEGRADED      -> READY
DEGRADED      -> BACKPRESSURED
DEGRADED      -> DISCONNECTED
DEGRADED      -> STOPPING
DEGRADED      -> FAILED

BACKPRESSURED -> READY
BACKPRESSURED -> DEGRADED
BACKPRESSURED -> DISCONNECTED
BACKPRESSURED -> STOPPING
BACKPRESSURED -> FAILED

DISCONNECTED  -> READY
DISCONNECTED  -> STOPPING
DISCONNECTED  -> FAILED

STOPPING      -> STOPPED
STOPPING      -> FAILED

FAILED        -> (none)     # Terminal
STOPPED       -> (none)     # Terminal
```

Any transition not listed above is a bug. `is_valid_transition()` returns
`False` and `require_valid_transition()` raises `InvalidStateTransition`.

### 8.3 Behavior per State

| State           | Ingress Policy    | Delivery Policy                     | Notes                                                       |
| --------------- | ----------------- | ----------------------------------- | ----------------------------------------------------------- |
| `INITIALIZING`  | Buffer            | Buffer                              | Connection not yet established. `start()` has not returned. |
| `READY`         | Accept            | Queue and deliver                   | Normal operation.                                           |
| `DEGRADED`      | Accept            | Queue, delay, may fallback          | Connection unstable. Queue events for later delivery.       |
| `BACKPRESSURED` | Throttle          | Queue, refuse new outbound enqueues | Outbound queue full. Inbound traffic **MUST** be throttled. |
| `DISCONNECTED`  | Accept (buffered) | Queue, no remote dispatch           | Transport endpoint unreachable. Recoverable on reconnect.   |
| `STOPPING`      | Reject            | Complete in-flight only             | Graceful shutdown. Reject new work.                         |
| `FAILED`        | Reject            | None                                | Terminal. Adapter is no longer operational.                 |
| `STOPPED`       | Reject            | None                                | Terminal. Clean shutdown.                                   |

### 8.4 State Transition Events

Every lifecycle state change **MUST** emit a `system.lifecycle` canonical event:

```python
{
    "event_kind": "system.lifecycle",
    "payload": {
        "component": "adapter",
        "adapter": "<adapter-instance-name>",
        "old_state": "<previous-state>",
        "new_state": "<new-state>",
        "reason": "<human-readable explanation>"
    }
}
```

### 8.5 Extended State Machines

Adapters that require more granular lifecycle states (e.g., multi-phase
connection handshakes) **MAY** define internal substates. Internal substates
**MUST** map to the eight-state enum. The adapter reports internal state via
`AdapterHealth.details` for observability. The lifecycle manager tracks only
the eight generic states.

| Internal Substate                                         | Maps To                                        |
| --------------------------------------------------------- | ---------------------------------------------- |
| `DISCONNECTED`, `CONNECTING`, `AUTHENTICATING`, `SYNCING` | `INITIALIZING` or `DEGRADED` or `DISCONNECTED` |
| `READY`                                                   | `READY`                                        |
| `DEGRADED`                                                | `DEGRADED`                                     |
| `BACKPRESSURED`                                           | `BACKPRESSURED`                                |
| `STOPPING`                                                | `STOPPING` or `STOPPED`                        |

### 8.6 Simplified Vocabulary Mapping

The operator-facing evidence labels in
[`diagnostics-evidence.md`](diagnostics-evidence.md) §18 derive from the
eight-state enum. The mapping below is the complete correspondence used by
`normalize_adapter_health()`:

| Evidence Label | Source `AdapterState` value(s) |
| -------------- | ------------------------------ |
| `connected`    | `READY`                        |
| `degraded`     | `DEGRADED` or `BACKPRESSURED`  |
| `unavailable`  | `DISCONNECTED`                 |
| `stopping`     | `STOPPING`                     |
| `failed`       | `FAILED`                       |
| `stopped`      | `STOPPED`                      |

`INITIALIZING` is the transient period between `build()` and `start()`
completion; evidence output uses the configuration-derived `starting` label
during that window. See
[`diagnostics-evidence.md`](diagnostics-evidence.md) §18.1 for the
derivation rules.

---

## 9. AdapterHandoffResult and DeliveryFeedback

### 9.1 Synchronous hand-off

Every successful `deliver(RenderingResult)` returns a frozen
`AdapterHandoffResult`. `disposition` is a closed value:

- `transport_handoff` — the adapter reached its external transport boundary
  during the call;
- `deferred` — the adapter accepted work locally and will report the later
  transport result through `DeliveryFeedback`.

`confirmation_level` records the strength of the fact (`unknown`,
`local_queue`, `local_transport`, `remote_service`, `end_to_end`) independently
of durable lifecycle status. Native IDs are optional real transport facts and
MUST NOT be fabricated. A deferred result cannot contain a native message ID
and its confirmation level MUST be `unknown` or `local_queue`;
`local_transport`, `remote_service`, and `end_to_end` imply that the hand-off
boundary has already been crossed. Metadata is recursively immutable and JSON-safe. Top-level metadata keys
closed hand-off, feedback, and attempt-provenance field names are reserved by
the delivery contract and MUST NOT be repeated as opaque adapter metadata.
Transport-specific data SHOULD be namespaced (for example `metadata["matrix"]`)
rather than shadowing `native_*`, `state`, `outbox_id`, or related authority
fields.

### 9.2 Asynchronous feedback

`DeliveryFeedback` is a closed tagged union with one immutable
`DeliveryAttemptProvenance` authority per variant:

- `DeferredHandoffCompleted(attempt_provenance, handoff)`;
- `DeferredHandoffFailed(attempt_provenance, outcome, ...)`;
- `PostHandoffObservation(attempt_provenance, state, ...)`.

Feedback does not duplicate event/plan/outbox/attempt mirrors. Core validates
the envelope against durable authority and owns all receipt/outbox transitions.
Post-hand-off observations are append-only evidence and cannot rewrite lifecycle
state.

### 9.3 Failure and duplicate-send semantics

A synchronous send failure raises `AdapterSendError` or
`AdapterPermanentError`; no successful hand-off object is returned. Deferred
terminal failure is reported only after a successful `deferred` hand-off. MEDRE
is at-least-once; adapters MUST NOT add an independent durable retry/dedup
engine.

---

## 10. Rendering Contract

The rendering pipeline converts canonical events into adapter-ready payloads. The contract has three components: the rendering context, the rendering result, and the boundary rules.

### 10.1 RenderingContext

Every renderer invocation receives a frozen `RenderingContext` carrying all dispatch metadata. The pipeline builds one context per render call and passes it to both `can_render` and `render`. Renderers MUST NOT rely on external state or perform signature introspection.

```python
@dataclass(frozen=True)
class RenderingContext:
    delivery_strategy: DeliveryStrategyMethod
    target_adapter: str
    target_channel: str | None = None
    target_platform: str | None = None
    max_text_chars: int | None = None
    max_text_bytes: int | None = None
    capability_level: CapabilityLevel = "native"
    source_origin_label: str | None = None
    target_destination: RouteDestination | None = None
```

`delivery_strategy` is a **context hint, not a renderer selector**. When the strategy is `"fallback_text"`, the target-native renderer still produces its native output format (e.g. a Matrix renderer produces Matrix msgtype/body, a Meshtastic renderer produces Meshtastic text). The pipeline does **not** bypass target-native renderers or switch to a generic text renderer based on this field. Instead, the target-native renderer uses the hint to degrade relation rendering to inline text within its own format.

`delivery_strategy` is the **authoritative dispatch signal** for renderers. The pipeline populates it from the delivery plan, which is derived from adapter capabilities and routing policy. Renderers **SHOULD** use it as the primary input for deciding how to render.

`max_text_bytes` is wired from the target adapter's `SIZE_LIMITS` capability by the pipeline. When the adapter declares a byte limit, this field carries it; otherwise it is `None`.

`capability_level` is populated from the `CapabilityDecision` resolved by `CapabilityDecisionResolver`. The pipeline sets this field to the three-level decision result (`"native"`, `"fallback"`, or `"unsupported"`) for the event's capability context. This value is carried into `RenderingEvidence` and stored on delivery receipts via `rendering_evidence`, providing durable capability context per delivery. Renderers **MAY** inspect `capability_level` for dispatch decisions; the pipeline guarantees it reflects the resolved capability decision.

`source_origin_label` is the route-resolved source attribution label supplied to renderers. `None` means no route-level override is present and the renderer may fall back to adapter/native attribution according to the routing specification.

`target_destination` carries the structured route destination when one exists. Renderers that address a specific entity **MUST** prefer this structured destination over the convenience `target_channel` value; `None` means the target is channel-addressed only.

### 10.2 RenderingResult

The `RenderingResult` is the output of a rendering pass, ready for adapter delivery. It is produced by the `RenderingPipeline` and consumed by adapters.

The renderer owns `payload`, rendering metadata, and truncation/fallback facts. The rendering pipeline adds the immutable `rendering_evidence` snapshot after the renderer returns. Immediately before adapter delivery, `TargetDeliveryService` stamps the durable delivery identity onto the same frozen value: `delivery_plan_id`, `outbox_id`, `attempt_number`, and the authoritative `attempt_provenance`. Those fields are framework-internal hand-off context, not wire metadata. When provenance is present it is authoritative; the scalar plan/outbox/attempt fields are compatibility mirrors validated and backfilled from it. An outbox-backed result without `attempt_provenance` is invalid.

```python
@dataclass(frozen=True)
class RenderingResult:
    event_id: str
    target_adapter: str
    target_channel: str | None
    payload: dict[str, object]
    metadata: dict[str, object] = field(default_factory=dict)
    truncated: bool = False
    fallback_applied: FallbackApplied | None = None
    rendering_evidence: RenderingEvidence | None = None
    delivery_plan_id: str | None = None
    outbox_id: str | None = None
    attempt_number: int | None = None
    attempt_provenance: DeliveryAttemptProvenance | None = None
```

### 10.3 Rendering Boundary

The rendering boundary is strictly enforced:

- Renderers produce `RenderingResult`. Adapters consume `RenderingResult`.
- No adapter **SHALL** perform rendering logic.
- No renderer **SHALL** deliver.
- Adapters **MUST NOT** re-render, reformat, or inspect the event kind to decide formatting inside `deliver()`.
- Before adapter hand-off, the pipeline **MUST** verify that the returned
  `RenderingResult` carries the requested event ID, target adapter, and
  normalized target channel. This identity fence applies even to direct/
  outbox-less delivery where no `DeliveryAttemptProvenance` envelope exists.
  A contradictory result is a renderer failure and **MUST NOT** reach the
  adapter.

### 10.4 Payload Ownership Boundary

The renderer owns payload construction. The adapter owns transport delivery.

- The renderer produces the complete `RenderingResult.payload` dict. This dict is the adapter-ready payload in the target's native format. The adapter **MUST NOT** modify, augment, or restructure the payload.
- The adapter receives the `RenderingResult` and transports the payload as-is to the external platform. The adapter's `deliver()` method is a transport boundary, not a formatting boundary.
- When `delivery_strategy` is `"fallback_text"`, the target-native renderer already embedded the degraded relation text in the payload. The adapter does not need to handle fallback logic.
- The rendering pipeline selects the renderer. Adapters **MUST NOT** influence renderer selection or inspect `RenderingResult.metadata` to decide formatting.

### 10.5 Rendering Evidence and Inspectability

`RenderingContext` and `RenderingResult` together form an evidence trail for rendering decisions. The context explains the constraints that governed the render call; the result records whether adjustments were made.

**Evidence signals on `RenderingResult`:**

| Field              | Signal                                                      |
| ------------------ | ----------------------------------------------------------- |
| `truncated`        | `True` when the renderer shortened content to fit a budget. |
| `fallback_applied` | Identifies which fallback was used, or `None` if none.      |

These fields are not operational flags. They are evidence that lets operators understand why a particular rendering output looks the way it does. `truncated=True` means content was lost to fit adapter constraints. `fallback_applied="strategy_fallback_text"` means the target-native renderer degraded relation context to inline text. The `FallbackApplied` literal vocabulary (`"relation_reply"`, `"relation_reaction"`, `"relation_edit"`, `"relation_delete"`, `"relation_thread"`, `"strategy_fallback_text"`) is a closed set of fallback reasons.

**Evidence signals on `RenderingContext`:**

| Field               | Signal                                                                                                                                                                                                                                                                               |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `delivery_strategy` | The strategy that governed rendering.                                                                                                                                                                                                                                                |
| `max_text_chars`    | Character budget that may have caused truncation.                                                                                                                                                                                                                                    |
| `max_text_bytes`    | UTF-8 byte budget that may have caused truncation.                                                                                                                                                                                                                                   |
| `capability_level`  | The level from `RenderingContext.capability_level`. Populated from the `CapabilityDecision` resolved by `CapabilityDecisionResolver`. Reflects the same decision used by Phase 2.5 capability suppression, `FallbackResolver` strategy resolution, and replay BEST_EFFORT filtering. |

The payload (`RenderingResult.payload`) is the rendered content. It is not evidence. Evidence is the explanation of decisions, carried by `truncated`, `fallback_applied`, and the context fields. For the full evidence semantics, receipt attachment, and replay-readiness limits, see the Diagnostics and Evidence Specification, § 14.

**Receipt attachment scope.** Rendering evidence is attached to the attempt receipt
created after rendering: the `sent` receipt for immediate hand-off and the `queued`
receipt for deferred local admission. Suppressed, rendering-failure, and
adapter-failure paths leave `rendering_evidence` as `None`. Route-target pre-outbox
skip paths (loop guard, policy denial, capability unsupported) persist
`DeliveryReceipt(status="suppressed")` for traceability, but rendering evidence
remains `None` because no renderer ran and no payload was handed to the adapter.

A deferred completion may legitimately beat persistence of its `queued` receipt. In
that feedback-before-receipt race, core finalizes `sent` from immutable attempt
provenance and leaves queue-only rendering/retry fields absent rather than guessing.
The later append-only `queued` receipt carries the original rendering evidence;
consumers that need it inspect receipt history for that attempt. See
[delivery-lifecycle.md](delivery-lifecycle.md) §3.4.

---

## 11. RateLimitConfig

```python
@dataclass
class RateLimitConfig:
    events_per_second:     float | None = None   # Max inbound events per second
    bytes_per_second:      float | None = None   # Max outbound bytes per second
    burst_size:            int | None = None     # Max burst before rate limiting kicks in
    delivery_concurrency:  int = 1               # Max concurrent deliveries
```

Rate limits are declared per adapter. The adapter is responsible for enforcing pacing and queueing internally. The runtime does not own per-adapter outbound queues, pacing timers, or duty cycle calculations.

---

## 12. Metadata Embedding Modes

When delivering to presentation adapters, metadata **MAY** be embedded in the native event content. The embedding mode is configurable per adapter.

| Mode      | What Gets Embedded                                                                                                          | Use Case                                                  |
| --------- | --------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| `off`     | Nothing                                                                                                                     | Pure display surface. All correlation through storage.    |
| `minimal` | `event_id`, `source_transport_id`                                                                                           | Limited context, less exposure on redaction.              |
| `safe`    | Normalized metadata (event kind, source adapter, transport protocol, radio metrics, telemetry). No secrets or raw payloads. | **RECOMMENDED** default.                                  |
| `full`    | All metadata                                                                                                                | Maximum context. All metadata lost on platform redaction. |

### 12.1 Never-Embed List

Regardless of mode, the following **MUST NOT** be embedded:

- Channel keys, private keys, or access tokens
- Raw encrypted blobs or raw packets
- Raw native protocol data (protobuf, Reticulum packets)
- Identity private keys or signing keys
- Full raw native archive data

### 12.2 Storage Is Authoritative

Storage is always authoritative. Embedded metadata is secondary and **MAY** be lost due to platform redaction, pruning, or API changes. Any feature that needs reliable metadata **MUST** read from storage, not from the presentation platform.

---

## 13. Pacing and Queueing

### 13.1 Queueing Modes

Adapters **MAY** use any of the following queueing modes based on transport characteristics:

| Mode               | Behavior                                                                                         |
| ------------------ | ------------------------------------------------------------------------------------------------ |
| **Immediate-send** | No queuing. `deliver()` sends immediately and returns.                                           |
| **Enqueue-only**   | `deliver()` places the rendered payload into an internal outbound queue and returns immediately. |
| **Paced**          | `deliver()` sends with an inter-message delay to respect transport duty cycles.                  |
| **ACK-driven**     | `deliver()` sends and waits for a transport-level acknowledgment before returning.               |
| **Best-effort**    | `deliver()` attempts to send, ignores failures, returns immediately.                             |

An adapter **MAY** support multiple modes and select based on message kind or configuration. The pipeline does not dictate the mode.

### 13.2 What the Runtime Does NOT Own

The runtime does not own:

- Per-adapter outbound queues. Those live inside the adapter.
- Pacing timers or duty cycle calculations. Those are adapter internals.
- Retry scheduling. The pipeline records retry-eligible failures on receipts but no background scheduler exists to re-attempt delivery.
- Retry budgets or rate limits beyond what the adapter self-imposes.

---

## 14. Session Boundaries

### 14.1 Session Ownership

Each transport session owns its SDK lifecycle end to end. The adapter delegates all SDK interaction to the session. The adapter owns semantic conversion (codec, routing, event publishing); the session owns raw transport management.

All sessions own these responsibilities:

| Responsibility        | Description                                                                 |
| --------------------- | --------------------------------------------------------------------------- |
| SDK client lifecycle  | Construction, initialization, teardown of the SDK client object             |
| Connection management | Establishing and maintaining the transport connection                       |
| Callback registration | Registering transport-level callbacks/subscriptions internally              |
| Inbound forwarding    | Forwarding received messages to the adapter-provided `message_callback`     |
| Connection recovery   | Transport-profile recovery with bounded-rate backoff and shutdown ownership |
| Liveness supervision  | Optional transport-specific active liveness where required by the profile   |
| Outbound send         | Sending messages through the transport SDK                                  |
| Send retry            | Bounded retry (up to 3 attempts) for transient send failures                |
| Diagnostics           | Providing a read-only snapshot of session operational state                 |
| Graceful teardown     | Clean shutdown of SDK client, cancellation of background tasks              |

### 14.2 Session Restrictions

Sessions **MUST NOT**:

- Construct `CanonicalEvent` instances. They forward normalized plain dicts to the adapter callback.
- Make routing decisions.
- Record delivery receipts or interact with storage.
- Evaluate bridge policy.
- Implement generic cross-adapter health polling or restart policy. A session MAY own a transport-specific liveness probe when its transport profile defines that probe as part of raw connection management.
- Throttle or reject based on load.
- Manage secret lifecycles.

### 14.3 Session-to-Adapter Boundary

The adapter provides a `message_callback` to the session constructor. The session calls this callback with normalized plain dicts. The session **MUST** never receive or return `CanonicalEvent` instances.

### 14.4 Session-to-SDK Boundary

The session is the sole owner of the SDK client object. No other module in the adapter package **MAY** import or touch the SDK directly.

### 14.5 Session-to-Diagnostics Boundary

Sessions expose `diagnostics()` returning either a frozen dataclass or a plain dict copy. Diagnostics are read-only snapshots. No consumer **MAY** attempt to modify session state through diagnostics.

---

## 15. SDK Object Containment

### 15.1 Containment Rules

Adapter diagnostics, health reports, and delivery results **MUST NOT** contain references to, or serialized forms of, the following SDK-specific object types:

- Protobuf message objects (e.g., `meshtastic.protobuf.*`)
- `LXMessage` or `LXMRouter` instances from the LXMF/Reticulum SDK
- `nio` client objects or crypto store references from `matrix-nio`
- `AsyncClient`, `SyncClient`, or mesh interface objects from the Meshtastic SDK
- `MeshCore` session or connection objects

### 15.2 Permitted Diagnostic Content

Diagnostics **MUST** contain only:

- JSON-safe simple values (strings, numbers, booleans, `None`)
- Plain dicts and lists of simple values
- Strings representing status, state, counters, and identifiers
- Frozen dataclasses with JSON-safe fields

### 15.3 Rationale

This containment rule ensures that diagnostics are serializable, loggable, and safe to expose via APIs or snapshot files without leaking SDK internals, unserializable objects, or sensitive connection state.

---

## 16. Health and Diagnostics Requirements

### 16.1 Health Transitions

The adapter owns its health state machine. The runtime observes it through `health_check()`. Valid transitions:

```text
unknown -> healthy
unknown -> degraded
unknown -> failed
healthy -> degraded
healthy -> failed
healthy -> unknown   (on stop)
degraded -> healthy
degraded -> failed
degraded -> unknown  (on stop)
failed -> healthy  (on reconnect)
failed -> degraded
failed -> unknown  (on stop)
```

### 16.2 Failure Reporting

When a transport-level failure occurs during `deliver()`, the adapter **MUST** raise an exception. The pipeline classifies the exception into a `DeliveryFailureKind`. The adapter does **not** classify its own failures. It **MUST** report them honestly and let the pipeline decide.

Adapters **MAY** log transport-specific diagnostics at whatever verbosity their configuration permits. They **MUST NOT** write receipts, update delivery state, or trigger retries.

### 16.3 Background Task Management

Adapters **MAY** spawn background asyncio tasks for listener loops, ACK waiters, or queue drainers. These tasks are owned by the adapter. The runtime does not track or manage them.

All spawned tasks **MUST** be cancelled and awaited during `stop()`. The adapter **MUST** ensure no orphaned asyncio tasks remain after `stop()` returns.

---

## 17. Sync and Async Callback Requirements

### 17.1 Inbound: Adapter-Controlled Receive Loop

Adapters do not implement `receive(raw_data, metadata)` as a primary interface. Inbound events flow through the adapter's internal listener loop: the adapter receives native data from its transport, converts it via its codec, and publishes the canonical event by calling `self.publish_inbound(event)`.

The runtime **MUST NOT** push raw data into an adapter. The adapter is in control of its own receive loop and event loop integration.

### 17.2 Outbound: hand-off and feedback

`deliver()` returns `AdapterHandoffResult` or raises. Immediate adapters return
`transport_handoff`; locally queued adapters return `deferred`. The return value
reports the adapter/transport boundary reached during that call, never a generic
recipient-delivery claim.

A deferred adapter captures the exact `RenderingResult.attempt_provenance` at
admission and carries it as opaque caller-owned context. When the transport
worker progresses, the adapter sends exactly one of the closed `DeliveryFeedback`
variants through `AdapterContext.report_delivery_feedback`.

Deferred admission requires durable attempt provenance. If
`RenderingResult.attempt_provenance` is absent, a deferred adapter MUST reject
the call before queue/session admission. The adapter likewise MUST reject when
`AdapterContext.report_delivery_feedback` is unavailable. It MUST NOT accept
uncorrelatable or unreportable work and then suppress feedback. Outbox-less
direct delivery is therefore limited to an immediate `transport_handoff`
result.

`DeferredHandoffCompleted` may contain a real native reference and finalizes the
queued attempt. `DeferredHandoffFailed` terminates the queued attempt before
transport hand-off. `PostHandoffObservation` records later transport evidence
without mutating receipts or reopening outbox state. Route `target_channel` and
transport-resolved `native_channel_id` are separate namespaces.

Core contains persistence failures so a stale/contradictory callback cannot
crash an adapter worker.

### 17.3 Callback isolation

Adapters report facts only. They are not notified of retry decisions, receipt
writes, or lifecycle classification. Core validates provenance and remains the
sole lifecycle authority.

---

## 18. Ingress Immutability

After an adapter codec produces a `CanonicalEvent` and the adapter publishes it via `publish_inbound()`, the event is frozen. No component **MAY** mutate the canonical event after ingress.

1. Adapters **MUST NOT** mutate canonical events after calling `publish_inbound()`. The event reference held by the adapter is now shared with the pipeline.
2. `CanonicalEvent` uses `frozen=True` in its struct definition, enforced at attribute assignment time.
3. Derived events are new events. Pipeline stages that transform, enrich, or derive from a source event **MUST** create a new `CanonicalEvent` with a new `event_id`.
4. Metadata enrichment is additive and produces a derived event. The source event's metadata remains unchanged.

---

## 19. Ownership Boundaries

Every row in the following table is a hard boundary. Violations indicate a design error.

| Concern                                                               | Owner                   | Others May                         |
| --------------------------------------------------------------------- | ----------------------- | ---------------------------------- |
| Transport lifecycle (connect, disconnect, reconnect)                  | Adapter                 | Read health state                  |
| Pacing, queueing, duty cycle management                               | Adapter                 | Set rate limit config              |
| Payload construction within RenderingContext constraints              | Renderer                | Provide RenderingResult            |
| Payload formatting (text, rich content, transport-specific layout)    | Renderer                | Provide RenderingResult            |
| Payload transport delivery (send to external platform)                | Adapter                 | None; receives pre-rendered result |
| Payload encoding/decoding (native format to CanonicalEvent)           | Codec                   | Read codec output                  |
| Packet classification (type detection, ACK detection)                 | Classifier              | Read classification result         |
| Pipeline orchestration (routing, delivery planning, receipt tracking) | Runtime                 | None; adapters **MUST NOT** bypass |
| Event authority, correlation, and lineage storage                     | Storage                 | Read via storage API               |
| Retry/backoff computation (stateless)                                 | Runtime (RetryExecutor) | Record on receipts                 |
| Retry scheduling (timed re-attempt)                                   | Runtime (RetryWorker)   | Storage persists due time/leases   |
| Native message reference persistence                                  | Storage                 | Read via storage API               |
| Post-handoff delivery observation persistence                         | Core runtime + storage  | Adapter reports facts only         |

---

## 20. Installed Adapter Type Registry

MEDRE discovers installed Python distributions through the `medre.adapters`
entry-point group and stores their declarative `AdapterSpec` values in the
process-static `ADAPTER_REGISTRY`. Each entry point **MUST** resolve to a
zero-argument factory returning an `AdapterSpec`. A broken provider **MUST**
be retained as a discovery failure rather than silently skipped. Conflicting
transport or native-kind owners **MUST** all be quarantined; discovery **MUST
NOT** choose a winner by installation order. Unrelated healthy providers remain
usable. Basic CLI help **MUST** remain available, and `medre adapters check`
**MUST** report broken providers distinctly from missing distributions and exit
with a configuration error. Configuring a broken provider **MUST** fail with an
actionable configuration error.

Distribution names, import packages, repository hosts, and transport names are
independent. Providers **MAY** be installed from a package index, Git, a wheel,
or a local path using ordinary Python tooling. Runtime discovery **MUST NOT**
install, download, or update packages. Changing installed providers requires
a process restart.

### 20.1 Registry Model

Providers **MUST** import declaration types from `medre.adapter_spec`. Importing
that module **MUST NOT** start entry-point discovery. This keeps direct factory
imports independent of registry initialization order.

Each `AdapterSpec` identifies one transport and the lazy symbols generic
assembly needs: config type, optional adapter-owned runtime wrapper, live and
optional fake adapter class, renderer factory, dependency probe/package metadata,
native-metadata readers, attribution projector, optional runtime-config
preparation/state-directory hooks, optional adapter-owned CLI contribution
hooks, support-bundle field classification metadata, dependency logger levels, and optional native
addressing kinds with pure `DestinationSpec` validators.

All implementation references **MUST** remain lazy `SymbolRef` values. Importing
`medre.adapter_registry`, configuration modules, or basic CLI discovery **MUST
NOT** import an optional transport SDK.

The registry is immutable after import. Installed transport names **MUST** be
unique. Runtime code **MUST** reject an enabled adapter whose transport is not
registered.

Core **MUST** own the `medre adapter <transport>` command namespace. Optional
`cli_register` hooks receive that provider's `argparse.ArgumentParser` and
configure only its arguments and descendants. They **MUST NOT** use the core
dispatch destinations/defaults `command`, `adapter_command`, or
`_adapter_cli_failures`, including in nested parsers. Both CLI hooks **MUST**
resolve to callables without importing optional SDKs.

CLI contribution failures **MUST** remain isolated from healthy command trees
and runtime registration. Basic help and version **MUST** remain available;
adapter preflight **MUST** diagnose the broken contribution and exit 2. Exception
details **MUST** be sanitized before display. A partially configured failing
parser **MUST NOT** be exposed.

A provider **MAY** declare `sample_config: SymbolRef`, an SDK-free zero-argument
callback returning `AdapterSample(config, channel=None)`. Native
values and the opaque conversation address are adapter-owned. `config` **MUST
NOT** override `adapter_id`, `adapter_kind`, or `enabled`. The sample generator
**MUST** include only installed providers declaring both a fake and a sample;
it supplies unique instance IDs, fake lifecycle fields, and generic routes. With one contribution it
creates two distinct instances; with none it emits an empty, loadable config
and installation guidance. Sample callbacks **MUST NOT** include real secrets,
perform I/O, or import SDKs.

A provider **MAY** declare `native_setup_fields`, an SDK-free tuple of
`NativeSetupField` values naming fields of its provider-owned config dataclass.
Field declarations **MUST** use unique, valid config fields and **MUST NOT**
include the core lifecycle wrapper fields (`adapter_id`, `adapter_kind`,
`enabled`). A field listed in `support_secret_fields` **MUST** use
`secret=True`, and secret prompts **MUST** belong to that redaction list.
Choices **MUST** be unique non-empty strings and **MUST NOT** be combined with
secret or numeric input. Conditional fields **MUST** reference a choice of a
preceding field. Labels **MUST** be non-empty strings and prompt flags **MUST**
be booleans. The guided native CLI **MUST** validate the completed document with
the authoritative loader and provider's pure preflight, **MUST NOT** import
native SDKs or contact hardware, and **MUST NOT** overwrite existing output.
Secret input **MUST NOT** be echoed; any saved secret makes the private YAML
credential-bearing. This optional metadata does not replace provider-native
validation, qualification, or SDK setup.

External SDK import roots receive a warning-level logging default; the root
logger and MEDRE namespace remain controlled by core/operator settings. Providers **MAY**
declare `logger_levels`, a tuple of dependency logger name / canonical level
name pairs. Names **MUST NOT** target the root logger or MEDRE's own logger namespace. Shared
provider declarations use the most restrictive level; explicit operator
logging overrides **MUST** take precedence. Logger policy is metadata-only and
**MUST NOT** import SDKs.

### 20.2 Assembly Flow

1. The config loader obtains the allowed `adapters.<transport>` vocabulary from
   `ADAPTER_REGISTRY`.
2. For each configured instance, the loader resolves the registered config
   class and constructs the adapter-owned wrapper or `GenericAdapterRuntimeConfig`.
3. `RuntimeBuilder` resolves that transport's `AdapterSpec`.
4. Before constructing any adapter, the builder runs every registered
   runtime-config preparation hook for enabled instances that have adapter
   configuration, using generic paths and expanded route context. Preparation is fail-closed configuration preflight:
   it **MUST** run for both fake and live instances, and any preparation failure
   **MUST** abort the build rather than being recorded as an isolated adapter
   construction failure. Transport-specific route or state preparation **MUST**
   live behind that adapter-owned hook rather than a shared transport branch.
5. After configuration preflight succeeds, the builder constructs the
   registered fake adapter when `adapter_kind: fake`; otherwise it checks the
   registered dependency probe and constructs the live adapter. The adapter-owned
   compatibility module **MUST** translate genuine optional-SDK absence into the
   probe's false value; failure to import or resolve the registered probe itself
   **MUST** remain a startup-visible implementation error. Construction or
   dependency failures may then be isolated per adapter according to the runtime
   degradation policy.
6. The adapter-owned renderer factory is resolved from the same spec and
   registers the transport renderer with the shared rendering pipeline. Renderer
   factories are adapter-owned assembly code and **MUST NOT** import optional
   transport SDKs at import time. Import or construction failures from a
   registered renderer factory **MUST** fail startup rather than silently falling
   back to the generic text renderer.
7. Shared native-metadata and attribution dispatch resolve adapter-owned
   readers/projectors through the same spec.
8. `MedreApp.adapters` owns the resulting live instances and performs the
   normal `start(context)` / `stop(timeout)` lifecycle.

Generic config, env, path, runtime assembly, CLI transport discovery and
contribution dispatch, support-bundle field classification, metadata dispatch,
and architecture-policy code **MUST NOT** maintain parallel hardcoded transport
enumerations.

### 20.3 Configuration Shape

The adapter transport is the first key below `adapters`; an instance name is
the second key. Operators **MUST NOT** configure a Python class path or adapter
role.

```yaml
adapters:
  meshcore:
    radio-1:
      enabled: true
      connection_type: tcp
      host: "192.168.1.100"
      port: 5000

  matrix:
    home:
      enabled: true
      homeserver: "https://matrix.example.com"
      user_id: "@medre:matrix.example.com"
```

Transport-specific fields are validated by that transport's registered config
class. Unknown transport groups are rejected.

### 20.4 Adding an Independent Adapter

An adapter distribution owns its implementation, config models, optional runtime
wrappers, fakes, native addressing, SDK requirements, and protocol tests. Its
registration factory **MUST** return an `AdapterSpec` through the installed
entry-point group. Generic core assembly and dispatch **MUST NOT** need a
transport-specific branch, a package-name convention, or a registry edit.

The four first-party distributions extend MEDRE's namespace with
`pkgutil.extend_path`. Community adapters **MAY** use their own top-level Python
package. Config schemas accept community transport and addressing names;
installed-provider checks and adapter-owned validators enforce their fields.
Native destination kinds **MUST** be supported by their configured target,
including destinations nested in context-map entries. Generic `channel`
addressing remains core-owned.

Core's compatibility manifest selects a reviewed CI test set. Runtime discovery
**MUST NOT** consult it, and community providers need no manifest entry.
`medre.plugins` remains a separate event-plugin protocol; adapter discovery
uses installed entry points rather than arbitrary class paths in runtime YAML.

---

## 21. Optional Dependencies

No adapter's SDK is a required MEDRE dependency. The core runtime and its tests **MUST** pass without any transport SDK installed.

| Component           | Requires real SDK | Fallback                                  |
| ------------------- | ----------------- | ----------------------------------------- |
| Fake adapter        | No                | Uses deterministic fixtures               |
| Codec unit tests    | No                | Uses fixture dicts matching native format |
| Renderer unit tests | No                | Uses fixture RenderingResults             |
| Live smoke harness  | Yes               | Skipped by default, enabled by env vars   |

When the real SDK is not installed, importing the live adapter class **MUST** fail gracefully. The fake adapter **MUST** never import the real SDK.

---

## 22. Fake Adapter Requirements

Fake adapters are first-class contract participants. They enforce the same boundaries as real adapters.

### 22.1 Requirements

Every fake adapter **MUST**:

1. Satisfy the full `AdapterContract` protocol: `start()`, `stop()`, `deliver()`, `health_check()`.
2. Enforce the rendering boundary: `deliver()` accepts `RenderingResult` only, not `CanonicalEvent`.
3. Report deterministic health transitions: `"unknown"` on construction, `"healthy"` after `start()`, `"unknown"` after `stop()`.
4. Return deterministic `AdapterHandoffResult` values using the same closed disposition vocabulary as real adapters and a confirmation level no stronger than the boundary the fake actually simulates. A lifecycle-fidelity fake **SHOULD** preserve the real adapter's immediate/deferred timing; a simpler deterministic fake **MAY** collapse internal queueing into an immediate transport hand-off when asynchronous timing is not the behavior under test.
5. Exercise the codec/classifier pipeline with fixture data matching the real native format.
6. Never import the real SDK.
7. Support the same `supported_event_kinds` as the real adapter.

### 22.2 Prohibitions

Fake adapters **MUST NOT**:

1. Open network connections.
2. Import the real transport SDK.
3. Bypass the rendering boundary by accepting `CanonicalEvent` directly.
4. Produce non-deterministic output (random IDs, varying timestamps).
5. Depend on external state (files, environment variables beyond test configuration).

### 22.3 Fake smoke evidence

A declared fake **MUST** be constructible with `adapter_id=` as its only required
argument, implement `AdapterContract`, and require no native SDK, credentials,
network, or hardware. Additional optional constructor arguments are permitted.
The [registration reference](../dev/adapter-registration.md) defines every
`AdapterSpec` field and hook signature, including the public metadata helpers.

Fake smoke, run sessions, and configured failure drills **MUST** reject any
enabled real adapter before constructing or starting the runtime. Their fake evidence classification does
not authorize live transport connections or failure injection into real adapters.

A fake adapter **MAY** provide a synchronous `make_event(text=..., event_kind=...)`
helper returning a `CanonicalEvent` originating from that adapter. Normal smoke
source selection **MUST** require successful startup and this helper. Missing
helpers **MUST NOT** prevent a fake from participating as a delivery target.

An explicit normal-smoke `--source-context` requests a conversation address
through the fake factory's optional `channel` argument. The factory **MUST**
return that exact `source_channel_id`; core **MUST NOT** rewrite native evidence
to pretend a different context was exercised. A factory that rejects or ignores
the argument **MUST** fail the smoke. Factories without this optional argument
remain usable when no context is requested. Factory events **MUST** preserve
the originating adapter's identity. The normal smoke report's `source_context`
records the actual injected conversation address. Drills and run sessions
**MUST** reject this normal-smoke-only option rather than silently ignore it.

The smoke CLI **MUST** select exactly one execution mode: normal smoke, a
`--drill` failure simulation, or `--run-session`. The latter two options
**MUST NOT** be combined. `--message`, `--source-adapter`, and
`--source-context` apply only to normal smoke; `--scenario` and
`--snapshot-dir` apply only to run-session. Explicit options for a different
mode **MUST** fail during argument validation, before any runtime is built.
`--config`, `--storage-path`, and `--json` are common to all three modes.

A smoke pass **MUST** require successful delivery for every configured route
and target pair matched by its injected event. Adapter construction or startup
failure **MUST NOT** turn a missing target into a successful reduced fanout.
A target that did not start cannot prove delivery even if its fake returns a
successful handoff. Every configured route/target pair **MUST** have a
persisted `sent` receipt for the injected event; a successful adapter outcome
alone is insufficient. Process-local `outbound_delivered` accounting **MUST**
cover at least the number of expected deliveries. Event-factory errors
**MUST** produce a failed report and trigger runtime cleanup. A smoke pass
**MUST** also observe a completed runtime shutdown and a readable final
snapshot with `runtime_state="stopped"`. The report **MUST** fail when these
checks cannot be established, even if delivery previously succeeded.

Happy-path run sessions **MUST** apply the same configured-fanout check to each
injected event in both direct-pipeline and adapter-callback ingress modes.
Successful delivery of a later event **MUST NOT** hide an earlier event's missing
target. Callback evidence polling **MUST** wait for all available configured
route/target pairs or its bounded deadline, rather than stop at the first receipt.
Missing or failed deliveries **MUST** identify the event, route, and target in
the report's existing `fail_reasons` field. Failure scenarios continue to judge
their deliberately injected fault instead of requiring successful delivery.

Every run-session scenario **MUST** also require persisted events, successfully
collected required evidence and snapshot artifacts, and error-free stopped
shutdown. A matching injected fault **MUST NOT** override lifecycle failure.
Happy-path accounting **MUST** cover the total configured event/route/target
pairs. Smoke cancellation or unexpected post-start exceptions **MUST** trigger
cleanup; cleanup errors **MUST NOT** replace a structured failure report.

Adapter failure drills **MUST** select targets through the runtime router's
matching and target-resolution rules for the injected event, including channel
and event-kind filters.

Every drill evidence check **MUST** succeed for the report to pass. Missing
receipts, unreadable native-reference evidence, unsuccessful manual recovery,
and failed cleanup checks **MUST NOT** be overridden by the expected injected
failure. Drills that start a runtime **MUST** close it on cancellation and
unexpected exceptions, report `shutdown_status`, and require error-free shutdown
with status `stopped`. Cleanup errors **MUST NOT** replace the original report.

Receipt-read failures **MUST** produce a `storage_error` step verdict with
sanitized error detail and fail the report. Unreadable storage **MUST NOT** be
represented as an empty receipt set or used to derive receipt timelines. A
failed pre-replay receipt baseline **MUST** prevent replay verification. Storage
faults **MUST** remain in the failure reasons alongside other observed failures.
The common step shape is published in
[drill-step.schema.json](../schemas/drill-step.schema.json).

### 22.4 Synthetic lifecycle drill coverage

The programmatic `bad_route_config`, `all_adapters_build_fail`,
`partial_degraded_startup`, and `all_adapters_start_fail` drills select one
installed fake provider and configure explicitly named adapter instances.
Instance identifiers **MUST NOT** imply an unrelated transport. Config assembly
**MUST** preserve the declared instance-to-transport mapping without positional
cycling or inferred platform names.

Their reports **MUST** include `adapter_transports` and
`coverage_scope: synthetic_adapter_instances`, with a limitation stating that
cross-transport coverage is not proven. Counts such as `adapters_total` describe
adapter instances, not distinct transport types. Three instances of one provider
remain three independently constructed adapters for startup supervision.

With no installed fake provider, these drills **MUST** return a failed report
with an empty assignment map and an installation reason. The report shape is
specified in `docs/schemas/lifecycle-drill-report.schema.json`.

Synthetic drill paths **MUST** be isolated from operator paths without changing
the process environment. Their temporary state root **MUST** be removed after
completion, including cancellation and failure.

---

## 23. Error Hierarchy

```python
class AdapterSendError(Exception):
    """Base error raised by adapters when delivery fails."""
    transient: bool  # True if retryable, False if permanent

class AdapterPermanentError(AdapterSendError):
    """Permanent delivery error — retrying will not help."""
    # transient is always False
```

The pipeline's `classify_failure` relies only on `AdapterSendError.transient` to map to `DeliveryFailureKind.ADAPTER_TRANSIENT` (retryable) or `DeliveryFailureKind.ADAPTER_PERMANENT` (dead-letter). It **MUST NOT** inspect the transport-specific error hierarchy.

---

## 24. Key Architectural Rules

1. Adapters **MUST NOT** call other adapters directly. All inter-adapter communication **MUST** flow through the event pipeline.
2. `deliver()` receives a pre-rendered `RenderingResult`. The adapter **MUST NOT** re-render.
3. Role is inferred from type, not operator-set.
4. Lifecycle state changes **MUST** emit `system.lifecycle` events.
5. Receipts are append-only. Every delivery attempt produces a new receipt row. Existing rows **MUST NOT** be updated or deleted.
6. Adapters **MUST NOT** own durable retry loops, schedule pipeline retries, write receipts, or mutate delivery lifecycle state. Bounded transport-call retries within a single `deliver()` invocation (e.g., up to 3 attempts for transient SDK send failures, as documented in the transport profile and §14.1 Session Ownership table, "Send retry" row) are permitted. After all bounded retries are exhausted, the adapter **MUST** raise `AdapterSendError` (transient) or `AdapterPermanentError` (permanent).
7. The pipeline does not provide global delivery deduplication. Replay atomically
   claims one target generation for each non-empty run ID in durable storage;
   different/empty run IDs and transport-level retry/recovery remain repeatable.
   Adapters **MUST NOT** add independent delivery deduplication.
8. The adapter's `publish_inbound` is the only way to inject events into the pipeline.
9. No adapter **MAY** swallow `CancelledError`.
10. Storage is always authoritative over embedded metadata.

---

## 25. Data Flow Summary

### 25.1 Inbound

```text
Transport --> raw data
  --> adapter listener loop
  --> codec.decode(NativeEvent)
  --> CanonicalEvent
  --> publish_inbound(event)
  --> ingress policy
  --> storage
  --> enrichment
  --> transforms
  --> event policy
  --> routing
  --> route policy
  --> delivery planning
  --> rendering
  --> adapter.deliver(RenderingResult)
```

### 25.2 Outbound

```text
RenderingResult
  --> adapter.deliver(result)
  --> transport send
  --> AdapterHandoffResult (or exception)
  --> optional DeliveryFeedback for asynchronous transport facts
  --> pipeline records receipt
  --> pipeline stores native_message_ref (when native_message_id is not None)
```

### Transient typing port

Adapters declaring `typing: true` MUST implement TypingSender.send_typing and
may publish TypingActivity through the optional source-bound context sink.
The port uses the adapter's own native identity and MUST enforce its configured
conversation and lifecycle guards. It MUST NOT use canonical delivery or its
receipt/outbox state machine. See [transient-typing.md](transient-typing.md) for
operator authorization, aggregation, native conversion, and cleanup semantics.

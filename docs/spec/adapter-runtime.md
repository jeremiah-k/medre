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
    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None: ...

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

### 3.4 `deliver(result)`

```python
async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None
```

The pipeline guarantees that `result` has already been rendered by a `Renderer` operating within a strict `RenderingContext`. The adapter **MUST NOT** re-render, reformat, or inspect the event kind to decide formatting. It **SHALL** merely transport the pre-rendered payload to the external platform.

On success, the adapter **MUST** return an `AdapterDeliveryResult` populated with platform-native IDs, or `None` when the adapter has no native ID to report.

If delivery fails, the adapter **MUST** raise `AdapterSendError` (transient) or `AdapterPermanentError` (permanent). The adapter **MUST NOT** write receipts, update delivery state, or trigger pipeline-level retries. The pipeline owns all of that. Bounded transport-call retries within the session send path (e.g., up to 3 attempts for transient SDK send failures — see §14.1 "Send retry") are permitted and expected; what is forbidden is the adapter implementing its own durable retry loops or retry scheduling outside the single `deliver()` call.

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

    def encode(self, event: CanonicalEvent, target: Any) -> Any:
        raise NotImplementedError  # Outbound rendering is handled by Renderers
```

### 4.1 `decode(native_event)`

Converts a native (adapter-specific) event into a `CanonicalEvent`. Called by the adapter's inbound listener after receiving raw data. The codec **MUST** set at minimum: `event_id`, `event_kind`, `schema_version`, `timestamp`, `source_adapter`, `source_transport_id`, and `payload`.

### 4.2 `encode(event, target)`

Outbound rendering is handled by `Renderer` instances registered with the `RenderingPipeline`, **not** by the codec's `encode` method. The default implementation raises `NotImplementedError`. Subclasses **SHOULD NOT** override this.

### 4.3 Codec Restrictions

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

Each adapter receives an `AdapterContext` on startup. This is the adapter's only window into the runtime.

```python
@dataclass
class AdapterContext:
    adapter_id: str                     # Unique adapter instance identifier
    event_bus: Any                      # Opaque reference to the framework event bus
    publish_inbound: Callable[[CanonicalEvent], Awaitable[None]]
                                        # Publish a CanonicalEvent into the pipeline
    logger: logging.Logger              # Pre-configured logger scoped to the adapter
    clock: Callable[[], datetime]       # Callable returning current UTC datetime
    shutdown_event: Any                 # asyncio.Event set when graceful shutdown requested
    record_outbound_native_ref: Callable[[OutboundNativeRefRecord], Awaitable[None]] | None = None
                                        # Optional callback for delayed native ref recording
```

### 5.1 Field Semantics

| Field                        | Purpose                                                                                                                                                                     |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `adapter_id`                 | Unique identifier for this adapter instance.                                                                                                                                |
| `event_bus`                  | Opaque reference to the framework's internal event bus. Adapters **SHOULD** prefer `publish_inbound` over direct bus interaction.                                           |
| `publish_inbound`            | The ingress point. Call this with a `CanonicalEvent` to inject it into the pipeline. The event passes through ingress policy, storage, enrichment, transforms, and routing. |
| `logger`                     | A pre-configured `logging.Logger` scoped to the adapter. Use this for all logging.                                                                                          |
| `clock`                      | Callable returning current UTC `datetime`. **MUST** be used instead of `datetime.utcnow()` for deterministic testing.                                                       |
| `shutdown_event`             | An `asyncio.Event` that the framework sets when a graceful shutdown is requested.                                                                                           |
| `record_outbound_native_ref` | Optional async callback for queue-based adapters to record delayed native message IDs after the platform confirms the send. `None` when not wired (e.g., in test mode).     |

### 5.2 Adapter Restrictions

Adapters **MUST NOT**:

- Import or call another adapter directly.
- Bypass the pipeline to send events straight to another transport.
- Modify events after they have been published via `publish_inbound`.
- Access the event bus, routing engine, or policy pipeline beyond what `AdapterContext` provides.

---

## 6. Adapter Capabilities

Adapters declare what they can do. The capability model drives delivery planning, capability downgrade, and relation fallback.

### 6.1 AdapterCapability Enum

The following 22 capabilities **MUST** be declared by every adapter:

| #   | Capability            | Description                                                        |
| --- | --------------------- | ------------------------------------------------------------------ |
| 1   | `TEXT`                | Plain text messages                                                |
| 2   | `TITLE`               | Explicit subject/title field                                       |
| 3   | `METADATA_FIELDS`     | Arbitrary structured key-value metadata                            |
| 4   | `REPLIES`             | Native reply threading                                             |
| 5   | `REACTIONS`           | Emoji or keyword reactions                                         |
| 6   | `EDITS`               | Message editing                                                    |
| 7   | `DELETES`             | Message deletion                                                   |
| 8   | `DELIVERY_RECEIPTS`   | Per-message delivery confirmation                                  |
| 9   | `STORE_AND_FORWARD`   | Message storage for later retrieval                                |
| 10  | `PROPAGATION`         | Propagation node support                                           |
| 11  | `DIRECT_MESSAGES`     | Point-to-point delivery                                            |
| 12  | `ATTACHMENTS`         | File/image/audio attachments                                       |
| 13  | `THREADS`             | Threaded conversations                                             |
| 14  | `CHANNELS`            | Channel, room, topic, or group-style destinations                  |
| 15  | `ACK_TRACKING`        | Transport-level acknowledgement tracking                           |
| 16  | `ASYNC_DELIVERY`      | Delivery completes asynchronously after handoff                    |
| 17  | `IDENTITY_ENCRYPTION` | Native identity-level encryption semantics                         |
| 18  | `PRESENCE`            | Presence or online state semantics                                 |
| 19  | `TOPIC_ROOMS`         | Named topic/room destinations                                      |
| 20  | `MESH_ROUTING`        | Mesh/radio routing semantics                                       |
| 21  | `PRIORITY_DELIVERY`   | Transport-level priority handling                                  |
| 22  | `SIZE_LIMITS`         | Configurable maximum payload size constraints (bytes and/or chars) |

### 6.2 CapabilityLevel Enum

Each capability is reported at one of the following support levels:

```python
class CapabilityLevel(str, Enum):
    TRUE                        = "true"
    FALSE                       = "false"
    METADATA_NATIVE             = "metadata_native"
    METADATA_NATIVE_OR_FALLBACK = "metadata_native_or_fallback"
    FUTURE                      = "future"
```

| Level                         | Meaning                                                                                            |
| ----------------------------- | -------------------------------------------------------------------------------------------------- |
| `TRUE`                        | Fully supported natively                                                                           |
| `FALSE`                       | Not supported                                                                                      |
| `METADATA_NATIVE`             | Target-native renderer degrades relation context into inline text within the native payload format |
| `METADATA_NATIVE_OR_FALLBACK` | Native rendering when available, inline text degradation within native payload format otherwise    |
| `FUTURE`                      | Planned, not yet implemented                                                                       |

> **Note on `METADATA_*` naming.** The `METADATA_NATIVE` and `METADATA_NATIVE_OR_FALLBACK` level names are historical. They originated when relation context was primarily carried as metadata fields. In the current architecture, both levels mean **inline fallback semantics**: the target-native renderer embeds relation context as inline text within its own native payload format (e.g. a Meshtastic renderer produces Meshtastic text with `[replying to: …]` prefixes). The renderer owns the degradation logic; the adapter sees only a normal native payload. The `METADATA_` prefix is retained for enum stability but should be read as "inline fallback within native format."

### 6.2.1 CapabilityLevel to Decision Mapping

The five `CapabilityLevel` values collapse to a three-level decision model used by `CapabilityDecisionResolver` (see Routing and Delivery Specification, § 6.3). The mapping:

| CapabilityLevel               | Decision level | Delivery strategy |
| ----------------------------- | -------------- | ----------------- |
| `TRUE`                        | `native`       | `direct`          |
| `METADATA_NATIVE`             | `fallback`     | `fallback_text`   |
| `METADATA_NATIVE_OR_FALLBACK` | `fallback`     | `fallback_text`   |
| `FALSE`                       | `unsupported`  | `skip`            |
| `FUTURE`                      | `unsupported`  | `skip`            |

The resolver applies this mapping per capability field, then picks the most severe decision across all candidates (event kind + relations). See Routing and Delivery Specification § 6.3.1 through § 6.3.5 for the full precedence rules.

> **Note on `FUTURE`.** `CapabilityLevel.FUTURE` maps to `unsupported` because the capability is not yet implemented. The resolver treats it the same as `FALSE` for delivery planning purposes. When the capability is later implemented, the adapter updates its declaration to `TRUE` or `METADATA_*`, and the resolver picks up the change without code changes elsewhere.

### 6.3 AdapterCapabilities Mapping

```python
@dataclass(frozen=True)
class AdapterCapabilities:
    capabilities: dict[AdapterCapability, CapabilityLevel]

    def supports(self, cap: AdapterCapability) -> bool:
        """True if the capability is at least METADATA_NATIVE."""
        level = self.capabilities.get(cap, CapabilityLevel.FALSE)
        return level != CapabilityLevel.FALSE

    def native_support(self, cap: AdapterCapability) -> bool:
        """True if the capability has TRUE native support."""
        return self.capabilities.get(cap) == CapabilityLevel.TRUE

    def level(self, cap: AdapterCapability) -> CapabilityLevel:
        """Return the support level for a capability."""
        return self.capabilities.get(cap, CapabilityLevel.FALSE)
```

The runtime reads capabilities from the cached `AdapterInfo` at delivery time. It **MUST NOT** query the adapter at delivery time.

### 6.4 How Capabilities Drive Behavior

| Capability    | `unsupported` Behavior                                                                 | `fallback` Behavior                                                                     | `native` Behavior             |
| ------------- | -------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- | ----------------------------- |
| `REPLIES`     | Skip: delivery suppressed. No renderer invoked.                                        | Target-native renderer embeds reply context as inline text within native payload format | Native reply threading used   |
| `REACTIONS`   | Skip: delivery suppressed. No renderer invoked.                                        | Target-native renderer embeds reaction as inline text within native payload format      | Native reactions used         |
| `EDITS`       | Skip: delivery suppressed. No renderer invoked.                                        | Target-native renderer embeds edit context as inline text within native payload format  | Native edit support used      |
| `DELETES`     | Skip: delivery suppressed. No renderer invoked.                                        | Target-native renderer embeds delete notice as inline text within native payload format | Native delete used            |
| `SIZE_LIMITS` | Truncation or splitting applied by `MaxLengthPolicy` when adapter declares byte limits | N/A                                                                                     | Unlimited or platform-handled |

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

```python
class AdapterLifecycleState(str, Enum):
    INITIALIZING = "initializing"
    RUNNING      = "running"
    DEGRADED     = "degraded"
    DRAINING     = "draining"
    STOPPED      = "stopped"
```

### 8.2 State Transitions

```text
INITIALIZING --> RUNNING
RUNNING       --> DEGRADED
DEGRADED      --> RUNNING           (recovered)
DEGRADED      --> DRAINING
RUNNING       --> DRAINING
DRAINING      --> STOPPED
any           --> STOPPED           (forced, e.g., timeout during drain)
```

Any transition not listed above is a bug.

### 8.3 Behavior per State

| State            | Ingress | Delivery                   | Notes                                                       |
| ---------------- | ------- | -------------------------- | ----------------------------------------------------------- |
| **INITIALIZING** | Buffer  | Buffer                     | Connection not yet established. `start()` has not returned. |
| **RUNNING**      | Accept  | Queue and deliver          | Normal operation.                                           |
| **DEGRADED**     | Accept  | Queue, delay, may fallback | Connection unstable. Queue events for later delivery.       |
| **DRAINING**     | Reject  | Complete in-flight only    | Graceful shutdown. Reject new work.                         |
| **STOPPED**      | Reject  | None                       | Terminal state. No further activity.                        |

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

Adapters that require more granular lifecycle states (e.g., multi-phase connection handshakes) **MAY** define internal substates. These internal substates **MUST** map to the generic five-state model. The adapter reports internal state via `AdapterHealth.details` for observability. The lifecycle manager tracks only the generic states.

| Internal Substates                                | Maps To                      |
| ------------------------------------------------- | ---------------------------- |
| DISCONNECTED, CONNECTING, AUTHENTICATING, SYNCING | `INITIALIZING` or `DEGRADED` |
| READY                                             | `RUNNING`                    |
| DEGRADED                                          | `DEGRADED`                   |
| DRAINING                                          | `DRAINING`                   |
| STOPPING                                          | `DRAINING` or `STOPPED`      |

---

## 9. AdapterDeliveryResult

### 9.1 Definition

```python
@dataclass(frozen=True)
class AdapterDeliveryResult:
    native_message_id:  str | None = None
    native_channel_id:  str | None = None
    native_thread_id:   str | None = None
    native_relation_id: str | None = None
    delivery_note:      str = ""
    delivery_status:    str = "sent"
    metadata:           MappingProxyType[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )
```

This is an immutable, frozen dataclass. The pipeline uses it to store `NativeMessageRef` mappings. The pipeline owns receipts and storage. Adapters only report what the platform returned.

### 9.2 Field Semantics

| Field                | Semantics                                                                                                                                                                                                                                                                                                                                               |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `native_message_id`  | Platform-native message ID assigned by the external system. **MUST** be platform-provided; adapters **MUST NOT** fabricate, synthesize, or locally-generate this value. `None` when unavailable.                                                                                                                                                        |
| `native_channel_id`  | Platform-native channel/room/conversation ID. Always platform-provided. `None` when the platform did not return one. The pipeline **MUST NOT** backfill from route configuration.                                                                                                                                                                       |
| `native_thread_id`   | Platform-native thread or parent message ID. Reserved; currently always `None` at runtime.                                                                                                                                                                                                                                                              |
| `native_relation_id` | Platform-native ID of the related entity (e.g., the message being replied to). Reserved; currently always `None` at runtime.                                                                                                                                                                                                                            |
| `delivery_note`      | Human-readable context about the delivery outcome. Informational only; consumers **MUST NOT** parse it for control-flow decisions.                                                                                                                                                                                                                      |
| `delivery_status`    | Adapter delivery fact: `"sent"` (default, synchronous adapters) or `"enqueued"` (queue-based adapters that accepted locally but have not yet sent to the platform). The pipeline maps this to receipt status; it is not lifecycle authority. Transport-specific local-acceptance states are reported in `metadata[<transport>]`, **not** in this field. |
| `metadata`           | Immutable, namespaced delivery metadata. Transport-specific data **MUST** live under `metadata[<transport>]`. No top-level MEDRE-standard keys are permitted — all adapter-specific state is namespaced under the transport key.                                                                                                                        |

### 9.3 Delivery State Semantics

When `deliver()` returns an `AdapterDeliveryResult`, it means the adapter accepted the delivery — the handoff from pipeline to adapter succeeded at the local level. It does **not** mean the message reached its final destination on the native platform, except for Matrix where the homeserver confirms storage.

| Transport  | What `deliver()` return means                                                                                     |
| ---------- | ----------------------------------------------------------------------------------------------------------------- |
| Matrix     | Homeserver accepted and stored the message. `event_id` is proof of server-side persistence.                       |
| Meshtastic | Message was enqueued to the outbound queue. Actual radio send is asynchronous via queue worker.                   |
| MeshCore   | Message was submitted to the SDK `send_text()`. Radio transmission may still fail.                                |
| LXMF       | Message was created and submitted to LXMRouter. Delivery state progresses asynchronously through multiple states. |

### 9.4 Failed Send Behavior

When a send fails, the adapter **MUST** raise `AdapterSendError` (transient, `transient=True`) or `AdapterPermanentError` (permanent, `transient=False`). No `AdapterDeliveryResult` is returned on failure.

Transport-specific `*SendError` classes (`MatrixSendError`, `MeshtasticSendError`, etc.) are session-internal errors and **MUST NOT** subclass `AdapterSendError` or `AdapterPermanentError`. Adapters **MUST** normalize session-internal transport errors into the runtime-facing `AdapterSendError`/`AdapterPermanentError` at the boundary.

No adapter **MAY** swallow `CancelledError`. Adapters **MUST** catch `Exception`, not `BaseException`, so that asyncio task cancellation propagates correctly.

### 9.5 Native Reference Persistence

Native refs are persisted in delivery receipts only when `native_message_id` is not `None`. When `native_message_id` is `None`, no native ref record is created. The pipeline **MUST NOT** fabricate native refs or backfill them from route configuration.

### 9.6 Duplicate-Send Risk

All adapters implement bounded retry with acknowledged duplicate-send risk. This is a fundamental property of at-least-once delivery. Consumers **MUST** be tolerant of duplicate deliveries. `native_message_id` **SHOULD** be used as a dedup key where available.

---

## 10. Rendering Contract

The rendering pipeline converts canonical events into adapter-ready payloads. The contract has three components: the rendering context, the rendering result, and the boundary rules.

### 10.1 RenderingContext

Every renderer invocation receives a frozen `RenderingContext` carrying all dispatch metadata. The pipeline builds one context per render call and passes it to both `can_render` and `render`. Renderers MUST NOT rely on external state or perform signature introspection.

```python
@dataclass(frozen=True)
class RenderingContext:
    delivery_strategy: DeliveryStrategyMethod  # "direct", "fallback_text", "skip", etc.
    target_adapter: str                        # Target adapter instance name
    target_channel: str | None                 # Target channel, if applicable
    target_platform: str | None                # Platform name (e.g. "matrix", "meshtastic")
    max_text_chars: int | None                 # Character budget from adapter capabilities
    max_text_bytes: int | None                 # UTF-8 byte budget from adapter capabilities
    capability_level: CapabilityDecisionLevel  # "native", "fallback", or "unsupported"
    capability_policy: str | None              # Optional policy hint (e.g. "strict", "lenient")
```

`delivery_strategy` is a **context hint, not a renderer selector**. When the strategy is `"fallback_text"`, the target-native renderer still produces its native output format (e.g. a Matrix renderer produces Matrix msgtype/body, a Meshtastic renderer produces Meshtastic text). The pipeline does **not** bypass target-native renderers or switch to a generic text renderer based on this field. Instead, the target-native renderer uses the hint to degrade relation rendering to inline text within its own format.

`delivery_strategy` is the **authoritative dispatch signal** for renderers. The pipeline populates it from the delivery plan, which is derived from adapter capabilities and routing policy. Renderers **SHOULD** use it as the primary input for deciding how to render.

`max_text_bytes` is wired from the target adapter's `SIZE_LIMITS` capability by the pipeline. When the adapter declares a byte limit, this field carries it; otherwise it is `None`.

`capability_level` is populated from the `CapabilityDecision` resolved by `CapabilityDecisionResolver`. The pipeline sets this field to the three-level decision result (`"native"`, `"fallback"`, or `"unsupported"`) for the event's capability context. This value is carried into `RenderingEvidence` and stored on delivery receipts via `rendering_evidence`, providing durable capability context per delivery. Renderers **MAY** inspect `capability_level` for dispatch decisions; the pipeline guarantees it reflects the resolved capability decision.

`capability_policy` is a **reserved field**. It is defined in `RenderingContext` for a future explicit capability-policy stage and defaults to `None`. The current pipeline does not set it. Renderers **MUST NOT** depend on `capability_policy` for dispatch decisions unless they also control the code that populates it.

### 10.2 RenderingResult

The `RenderingResult` is the output of a rendering pass, ready for adapter delivery. It is produced by the `RenderingPipeline` and consumed by adapters.

```python
@dataclass(frozen=True)
class RenderingResult:
    event_id:         str                        # Original canonical event ID
    target_adapter:   str                        # Target adapter instance name
    target_channel:   str | None                 # Target channel, if applicable
    payload:          dict[str, object]          # Rendered payload in adapter-ready format
    metadata:         dict[str, object] = field(default_factory=dict)
    truncated:        bool = False               # Whether content was truncated
    fallback_applied: FallbackApplied | None = None          # Fallback strategy applied, if any
```

### 10.3 Rendering Boundary

The rendering boundary is strictly enforced:

- Renderers produce `RenderingResult`. Adapters consume `RenderingResult`.
- No adapter **SHALL** perform rendering logic.
- No renderer **SHALL** deliver.
- Adapters **MUST NOT** re-render, reformat, or inspect the event kind to decide formatting inside `deliver()`.

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

**Receipt attachment scope.** The `rendering_evidence` field on `DeliveryReceipt` is populated only for `sent` and `queued` statuses. Suppressed, rendering-failure, and adapter-failure paths leave `rendering_evidence` as `None`. Route-target pre-outbox skip paths (loop guard, policy denial, capability unsupported) persist `DeliveryReceipt(status="suppressed")` for traceability, but the rendering evidence remains `None` because no renderer ran and no payload was handed to the adapter.

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

| Responsibility        | Description                                                             |
| --------------------- | ----------------------------------------------------------------------- |
| SDK client lifecycle  | Construction, initialization, teardown of the SDK client object         |
| Connection management | Establishing and maintaining the transport connection                   |
| Callback registration | Registering transport-level callbacks/subscriptions internally          |
| Inbound forwarding    | Forwarding received messages to the adapter-provided `message_callback` |
| Bounded reconnect     | Exponential backoff reconnect with max 10 attempts                      |
| Outbound send         | Sending messages through the transport SDK                              |
| Send retry            | Bounded retry (up to 3 attempts) for transient send failures            |
| Diagnostics           | Providing a read-only snapshot of session operational state             |
| Graceful teardown     | Clean shutdown of SDK client, cancellation of background tasks          |

### 14.2 Session Restrictions

Sessions **MUST NOT**:

- Construct `CanonicalEvent` instances. They forward normalized plain dicts to the adapter callback.
- Make routing decisions.
- Record delivery receipts or interact with storage.
- Evaluate bridge policy.
- Implement health polling loops.
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

### 17.2 Outbound: Synchronous Return, Async Completion

`deliver()` returns synchronously with an `AdapterDeliveryResult` (or raises on failure). For transports with asynchronous delivery models (LXMF, Meshtastic queue), the returned result reflects the local-acceptance state, not the final delivery state.

LXMF is the only transport with formal asynchronous delivery state progression. The eight states are:

```text
generating -> outbound -> sending -> sent -> delivered
                                          -> failed
                                          -> rejected
                                          -> cancelled
```

State progression happens asynchronously via callbacks registered on the LXMRouter. The initial state reported in `AdapterDeliveryResult.metadata` is typically `"outbound"`.

### 17.3 Delayed Native Ref Recording

Queue-based adapters (e.g., Meshtastic) that cannot return a native message ID synchronously from `deliver()` **MUST** use the `record_outbound_native_ref` callback from `AdapterContext` when the platform later provides a real native ID.

```python
@dataclass(frozen=True)
class OutboundNativeRefRecord:
    event_id:           str
    adapter:            str
    native_channel_id:  str | None
    native_message_id:  str           # Must be a real ID from the external platform
    native_thread_id:   str | None = None
    native_relation_id: str | None = None
    metadata:           Mapping[str, object] = field(default_factory=dict)
```

The `native_message_id` field **MUST** be a non-empty string from the external platform. The adapter **MUST NOT** fabricate IDs.

### 17.4 Callback Isolation

The adapter is not notified of retry decisions, receipt recording, or failure classification. It does not receive a callback after `deliver()` returns. This isolation is intentional: the adapter's job is to attempt delivery and report the outcome. The pipeline's job is to decide what happens next.

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
| Retry scheduling (timed re-attempt)                                   | Reserved for future     | Not yet implemented                |
| Native message reference persistence                                  | Storage                 | Read via storage API               |

---

## 20. Adapter Registry

### 20.1 Registry Interface

```python
class AdapterRegistry(Protocol):
    def register(self, info: AdapterInfo, adapter: AdapterContract) -> None: ...
    def get(self, name: str) -> AdapterContract | None: ...
    def get_info(self, name: str) -> AdapterInfo | None: ...
    def list_adapters(self) -> list[AdapterInfo]: ...
    def list_by_role(self, role: AdapterRole) -> list[AdapterInfo]: ...
    def unregister(self, name: str) -> None: ...
```

### 20.2 Registration Flow

1. The runtime loads adapter configuration from YAML.
2. For each adapter entry, it instantiates the adapter class, passing the config block.
3. The adapter constructs its `AdapterInfo` and returns it.
4. The runtime calls `registry.register(info, adapter_instance)`.
5. The runtime calls `adapter.start(context)` with a fresh `AdapterContext`.
6. On shutdown, the runtime calls `adapter.stop(timeout)`, then `registry.unregister(name)`.

### 20.3 Configuration

Adapter type determines the role. The operator **MUST NOT** set `role` manually.

```yaml
adapters:
  meshcore-radio-1:
    type: meshcore # role: TRANSPORT (inferred)
    connection: { ... }

  matrix-home:
    type: matrix # role: PRESENTATION (inferred)
    homeserver: "https://matrix.example.com"

  irc-bridge:
    type: irc # role: HYBRID (inferred)
    server: "irc.example.com"
```

The `type` field maps to a Python class path resolved by the adapter registry. Built-in types resolve to `adapters/<type>/adapter.py`. Custom adapter types **MAY** specify a `class` field explicitly.

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
4. Return deterministic `AdapterDeliveryResult` instances with synthetic native IDs.
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
6. Adapters **MUST NOT** own durable retry loops, schedule pipeline retries, write receipts, or mutate delivery lifecycle state. Bounded transport-call retries within a single `deliver()` invocation (e.g., up to 3 attempts for transient SDK send failures, as documented in the transport profile and §14.1 "Send retry") are permitted. After all bounded retries are exhausted, the adapter **MUST** raise `AdapterSendError` (transient) or `AdapterPermanentError` (permanent).
7. The pipeline does not deduplicate delivery attempts. Adapters **MUST NOT** deduplicate.
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
  --> AdapterDeliveryResult (or exception)
  --> pipeline records receipt
  --> pipeline stores native_message_ref (when native_message_id is not None)
```

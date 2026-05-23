# Adapter Runtime Contract

> Source: [Modular Event Communications Runtime Specification](../spec/modular-event-engine-spec.md), Sections 9, 14, 15, 16, 17
> Version: 0.1.0 (extracted from spec draft)

This document defines everything an adapter implementer needs. Build an adapter that satisfies these protocols, register it in configuration, and the runtime handles the rest: routing, delivery planning, policy evaluation, receipt tracking, and observability.

Adapters never call other adapters. They never bypass the pipeline. All inter-adapter communication flows through the event pipeline. If you find yourself importing from another adapter package, something is wrong.

## 1. Adapter Roles

Every adapter declares a role. The role is inferred from the adapter type at configuration load time; operators do not set it manually.

```python
class AdapterRole(str, Enum):
    TRANSPORT = "transport"
    PRESENTATION = "presentation"
    HYBRID = "hybrid"
```

| Role             | Responsibility                                                                                                                         | Examples                                                   |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| **TRANSPORT**    | Moves data to/from a physical or logical transport. Handles protocol specifics, connection management, and raw data encoding/decoding. | Meshtastic, MeshCore, LXMF, MQTT, TCP serial bridge, AX.25 |
| **PRESENTATION** | Presents events to human users. Handles formatting, rich content, threading, reactions, and user interaction.                          | Matrix, Discord, Telegram, Slack, Web UI                   |
| **HYBRID**       | Both transports and presents. Can act as a message source and a display target simultaneously.                                         | IRC, XMPP                                                  |

TRANSPORT adapters typically ingest raw protocol data and produce canonical events. PRESENTATION adapters receive delivery plans and render events for human consumption. HYBRID adapters do both. The role determines which pipeline stages the adapter participates in and how the routing engine treats it.

## 2. Adapter Protocol

The adapter interface splits into two concerns: lifecycle/delivery and codec format conversion.

### 2.1 Live Adapter Interface

```python
from typing import Protocol, Callable, Awaitable
from enum import Enum

class Adapter(Protocol):
    """Core adapter protocol. Every adapter must satisfy this interface."""

    name: str                            # Unique adapter instance name (from config)
    adapter_role: AdapterRole            # TRANSPORT, PRESENTATION, or HYBRID
    supported_event_kinds: set[str]      # Event kinds this adapter can handle
    rate_limits: RateLimitConfig         # Adapter-specific rate limit configuration

    async def start(self, context: AdapterContext) -> None:
        """Initialize the adapter. Inbound events are published via
        context.publish_inbound().

        Called once during startup. Establish connections, start listener
        loops, register with the event bus. Do not return until the adapter
        is ready to accept delivery plans or until the connection attempt
        has progressed far enough to report health state.
        """
        ...

    async def stop(self) -> None:
        """Gracefully shut down.

        Complete in-flight deliveries if applicable, reject new work, and
        clean up connections. Phase 1 fake adapters have no background
        queues or connections to drain.
        """
        ...

    async def deliver(self, result: RenderingResult) -> None:
        """Deliver a rendered payload to this adapter's target.

        The pipeline guarantees that *result* has already been rendered
        by a Renderer. The adapter must not re-render, reformat, or
        inspect the event kind to decide formatting. It merely transports
        the payload.

        This is the only outbound method. There is no send(), no push(),
        no emit(). Delivery is always rendering-result-driven.
        """
        ...

    async def health_check(self) -> AdapterHealth:
        """Return current health status.

        Called periodically by the lifecycle manager. Must be cheap
        and non-blocking.
        """
        ...
```

### 2.2 Codec Pattern

Adapters do not implement `receive(raw_data, metadata)` as a primary interface. Inbound events flow through the adapter's internal listener loop: the adapter receives native data from its transport, converts it via its codec, and publishes the canonical event by calling `ctx.publish_inbound(event)`.

This keeps the adapter in control of its own receive loop and event loop integration. The runtime never pushes raw data into an adapter.

```python
# Pseudocode for a typical TRANSPORT adapter's start() method:
async def start(self, context: AdapterContext) -> None:
    self.ctx = context
    connection = await self.establish_connection()
    # Start internal listener loop
    asyncio.create_task(self._listen(connection))

async def _listen(self, connection) -> None:
    async for raw_data in connection.stream():
        native = NativeEvent(raw_data=raw_data, metadata={...}, received_at=datetime.utcnow())
        event = await self.codec.decode(native)
        await self.ctx.publish_inbound(event)  # Push into the pipeline
```

For outbound, the `deliver()` method receives a pre-rendered `RenderingResult`:

```python
async def deliver(self, result: RenderingResult) -> None:
    # The result is already rendered. Just transport the payload.
    await self._send(result.payload)
```

## 3. AdapterContext

Each adapter receives an `AdapterContext` on startup. This is the adapter's only window into the runtime. Adapters do not get direct access to other adapters.

```python
from dataclasses import dataclass
from typing import Callable, Awaitable, Any
import logging

@dataclass
class AdapterContext:
    adapter_id: str                     # Unique adapter instance identifier
    event_bus: Any                      # Opaque reference to the framework event bus
    publish_inbound: Callable[[CanonicalEvent], Awaitable[None]]
                                        # Publish an inbound canonical event into the pipeline.
                                        # This is the only way to inject events.
    logger: logging.Logger              # Pre-configured logger scoped to the adapter
    clock: Callable[[], datetime]       # Callable returning current UTC datetime (for deterministic testing)
    shutdown_event: Any                 # asyncio.Event set when graceful shutdown is requested
```

### 3.1 What Each Field Provides

| Field             | Purpose                                                                                                                                                                     |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `adapter_id`      | Unique identifier for this adapter instance.                                                                                                                                |
| `event_bus`       | Opaque reference to the framework's internal event bus. Adapters should prefer `publish_inbound` over direct bus interaction.                                               |
| `publish_inbound` | The ingress point. Call this with a `CanonicalEvent` to inject it into the pipeline. The event passes through ingress policy, storage, enrichment, transforms, and routing. |
| `logger`          | A pre-configured `logging.Logger` scoped to the adapter. Use this for all logging.                                                                                          |
| `clock`           | Callable returning current UTC `datetime`. Use this instead of `datetime.utcnow()` for deterministic testing.                                                               |
| `shutdown_event`  | An `asyncio.Event` that the framework sets when a graceful shutdown is requested.                                                                                           |

### 3.2 What Adapters Cannot Do

- Import or call another adapter directly.
- Bypass the pipeline to send events straight to another transport.
- Modify events after they have been published via `publish_inbound`.
- Access the event bus, routing engine, or policy pipeline.

## 4. AdapterCodec

The codec handles conversion between native protocol data and canonical events. It is an adapter-private concern, not part of the public `Adapter` protocol. Adapters may implement it as a separate class or inline the logic.

```python
class AdapterCodec(Protocol):
    """Handles conversion between native protocol data and canonical events."""

    async def decode(self, native_event: NativeEvent) -> CanonicalEvent:
        """Convert a native protocol event into a preliminary canonical event.

        Called by the adapter's inbound listener after receiving raw data.
        The adapter wraps whatever it received from its transport into a
        NativeEvent, then passes it here.

        Must set at minimum: event_id, event_kind, schema_version,
        timestamp, source_adapter, source_transport_id, payload.
        """
        ...

    async def encode(self, event: CanonicalEvent, plan: DeliveryPlan) -> NativeOutbound:
        """Convert a canonical event into a native protocol payload for delivery.

        Called by the adapter's deliver() implementation. Produces the
        protocol-specific payload that the adapter sends to its transport.
        """
        ...
```

### 4.1 NativeEvent

```python
@dataclass
class NativeEvent:
    """Wrapper for raw data received from a native transport."""
    raw_data: bytes | dict       # Raw protocol data
    metadata: dict               # Transport-specific metadata (headers, connection info)
    received_at: datetime        # Timestamp when the adapter received this data
```

### 4.2 NativeOutbound

```python
@dataclass
class NativeOutbound:
    """Rendered payload ready for delivery to a native transport."""
    payload: bytes | dict        # Protocol-specific payload
    metadata: dict               # Delivery metadata (destination, headers)
    native_message_id: str | None  # Native message ID after successful send
```

### 4.3 Codec Responsibilities

The codec owns format translation and nothing else. It should not:

- Call `publish_inbound` (that is the adapter's job).
- Make routing decisions.
- Enrich events with data from other adapters.
- Apply policy rules.

It should:

- Map native fields to canonical event fields.
- Set `source_adapter` to the adapter instance name.
- Set `source_transport_id` to the native actor identity (not the native message ID).
- Set `source_channel_id` to the native channel/room/topic where the event originated.
- Populate `metadata.transport`, `metadata.radio`, `metadata.telemetry`, and `metadata.native` as appropriate.
- Preserve native message references for correlation (the adapter stores these via `ctx.storage.store_native_ref`).

## 5. Adapter Capabilities

Adapters declare what they can do. The capability model drives delivery planning, capability downgrade, and relation fallback.

### 5.1 Capability Enum

```python
class AdapterCapability(str, Enum):
    TEXT = "text"                          # Plain text messages
    TITLE = "title"                        # Subject/title field
    METADATA_FIELDS = "metadata_fields"    # Arbitrary key-value metadata
    REPLIES = "replies"                    # Native reply threading
    REACTIONS = "reactions"                # Emoji/keyword reactions
    EDITS = "edits"                        # Message editing
    DELETES = "deletes"                    # Message deletion
    DELIVERY_RECEIPTS = "delivery_receipts"  # Per-message delivery confirmation
    STORE_AND_FORWARD = "store_and_forward"  # Message storage for later retrieval
    PROPAGATION = "propagation"            # Propagation node support
    DIRECT_MESSAGES = "direct_messages"    # Point-to-point encrypted delivery
    ATTACHMENTS = "attachments"            # File/image/audio attachments
    THREADS = "threads"                    # Threaded conversations
```

### 5.2 Capability Support Levels

```python
class CapabilityLevel(str, Enum):
    TRUE = "true"                            # Fully supported natively
    FALSE = "false"                          # Not supported
    METADATA_NATIVE = "metadata_native"      # Supported via metadata fields (e.g., LXMF fields dict)
    METADATA_NATIVE_OR_FALLBACK = "metadata_native_or_fallback"  # Metadata between aware peers, inline fallback otherwise
    FUTURE = "future"                        # Planned, not yet implemented
```

### 5.3 AdapterCapabilities

```python
@dataclass(frozen=True)
class AdapterCapabilities:
    """Declares what an adapter can and cannot do.

    The delivery planning and capability fallback stages read this
    to decide how to render events for this adapter.
    """
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

### 5.4 Example Capability Declarations

A TRANSPORT adapter with no rich features (like MeshCore):

```python
AdapterCapabilities(capabilities={
    AdapterCapability.TEXT: CapabilityLevel.TRUE,
    AdapterCapability.REPLIES: CapabilityLevel.FALSE,
    AdapterCapability.REACTIONS: CapabilityLevel.FALSE,
    AdapterCapability.EDITS: CapabilityLevel.FALSE,
    AdapterCapability.DELETES: CapabilityLevel.FALSE,
})
```

A TRANSPORT adapter with metadata-native relations (like LXMF):

```python
AdapterCapabilities(capabilities={
    AdapterCapability.TEXT: CapabilityLevel.TRUE,
    AdapterCapability.TITLE: CapabilityLevel.TRUE,
    AdapterCapability.METADATA_FIELDS: CapabilityLevel.TRUE,
    AdapterCapability.REPLIES: CapabilityLevel.METADATA_NATIVE,
    AdapterCapability.REACTIONS: CapabilityLevel.METADATA_NATIVE,
    AdapterCapability.EDITS: CapabilityLevel.METADATA_NATIVE_OR_FALLBACK,
    AdapterCapability.DELETES: CapabilityLevel.METADATA_NATIVE_OR_FALLBACK,
    AdapterCapability.DELIVERY_RECEIPTS: CapabilityLevel.TRUE,
    AdapterCapability.STORE_AND_FORWARD: CapabilityLevel.TRUE,
    AdapterCapability.PROPAGATION: CapabilityLevel.TRUE,
    AdapterCapability.DIRECT_MESSAGES: CapabilityLevel.TRUE,
})
```

A PRESENTATION adapter with native rich features (like Matrix):

```python
AdapterCapabilities(capabilities={
    AdapterCapability.TEXT: CapabilityLevel.TRUE,
    AdapterCapability.METADATA_FIELDS: CapabilityLevel.TRUE,
    AdapterCapability.REPLIES: CapabilityLevel.TRUE,
    AdapterCapability.REACTIONS: CapabilityLevel.TRUE,
    AdapterCapability.EDITS: CapabilityLevel.TRUE,
    AdapterCapability.DELETES: CapabilityLevel.TRUE,
    AdapterCapability.THREADS: CapabilityLevel.TRUE,
})
```

### 5.5 How Capabilities Drive Behavior

The `capability_fallback.py` module (Spec Section 8.3) uses adapter capabilities to decide:

- **Replies**: If the target adapter has `REPLIES: FALSE`, the delivery plan renders the reply as inline text (e.g., `[Alice] re: original msg > reply text`).
- **Reactions**: If `REACTIONS: FALSE`, reactions are dropped for that target.
- **Edits**: If `EDITS: METADATA_NATIVE_OR_FALLBACK`, edits are rendered as new messages with metadata signaling for aware peers.
- **Truncation**: If the adapter has a byte limit (e.g., MeshCore at 160 bytes), the `MaxLengthPolicy` handles splitting or truncation.

Adapters report their capabilities via `AdapterInfo` (Section 6). The runtime never asks the adapter at delivery time; it reads from the cached info.

## 6. AdapterInfo and AdapterHealth

### 6.1 AdapterInfo

```python
@dataclass
class AdapterInfo:
    """Static and runtime metadata about an adapter instance.

    Registered in the adapter registry at startup. Queried by the
    routing engine, delivery planner, and future management interface.
    """
    name: str                              # Unique instance name
    adapter_type: str                      # Adapter type key (e.g., "meshcore", "matrix", "lxmf")
    role: AdapterRole                      # TRANSPORT, PRESENTATION, or HYBRID
    capabilities: AdapterCapabilities      # What this adapter can do
    supported_event_kinds: set[str]        # Event kinds this adapter handles
    rate_limits: RateLimitConfig           # Adapter-specific rate limits
    version: str                           # Adapter implementation version
```

### 6.2 AdapterHealth

```python
@dataclass
class AdapterHealth:
    """Current health snapshot of an adapter. Returned by health_check()."""
    adapter: str                           # Adapter instance name
    state: AdapterLifecycleState           # Current lifecycle state
    connected: bool                        # Whether the adapter has an active connection
    latency_ms: float | None               # Round-trip latency to the transport, if measurable
    queue_depth: int                       # Number of pending outbound deliveries
    last_event_at: datetime | None         # Timestamp of last successful event ingress or delivery
    error: str | None                      # Current error message if degraded or disconnected
    details: dict                          # Adapter-specific health details
```

### 6.3 RateLimitConfig

```python
@dataclass
class RateLimitConfig:
    """Rate limit configuration for an adapter."""
    events_per_second: float | None        # Max inbound events per second
    bytes_per_second: float | None         # Max outbound bytes per second
    burst_size: int | None                 # Max burst before rate limiting kicks in
    delivery_concurrency: int              # Max concurrent deliveries (default: 1)
```

## 7. Adapter Lifecycle States

### 7.1 Lifecycle State Enum

```python
class AdapterLifecycleState(str, Enum):
    INITIALIZING = "initializing"          # Loading config, establishing connections
    RUNNING = "running"                    # Fully operational. Sending and receiving events.
    DEGRADED = "degraded"                  # Connection lost or partial failure. Queueing events.
    DRAINING = "draining"                  # Graceful shutdown. Completing in-flight deliveries.
    STOPPED = "stopped"                    # Fully shut down.
```

### 7.2 State Transitions

```text
INITIALIZING --> RUNNING
RUNNING       --> DEGRADED
DEGRADED      --> RUNNING           (recovered)
DEGRADED      --> DRAINING
RUNNING       --> DRAINING
DRAINING      --> STOPPED
any           --> STOPPED           (forced, e.g., timeout during drain)
```

### 7.3 Behavior per State

| State            | Ingress | Delivery                   | Notes                                                       |
| ---------------- | ------- | -------------------------- | ----------------------------------------------------------- |
| **INITIALIZING** | Buffer  | Buffer                     | Connection not yet established. `start()` has not returned. |
| **RUNNING**      | Accept  | Queue and deliver          | Normal operation.                                           |
| **DEGRADED**     | Accept  | Queue, delay, may fallback | Connection unstable. Queue events for later delivery.       |
| **DRAINING**     | Reject  | Complete in-flight only    | Graceful shutdown. Reject new work.                         |
| **STOPPED**      | Reject  | None                       | Terminal state. No further activity.                        |

### 7.4 State Transition Events

Every state change emits a `system.lifecycle` canonical event:

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

### 7.5 Extended State Machines

Some adapters require more granular lifecycle states than the generic five-state model. For example, a transport adapter with a complex connection handshake (authentication, syncing) may define internal substates:

```text
DISCONNECTED --> CONNECTING --> AUTHENTICATING --> SYNCING --> READY
     ^              |              |                 |          |
     |              v              v                 v          v
     +--------------+--------------+----------+------+----> DEGRADED
     |                                                     |
     +<----------------------------------------------------+
     |                                                     v
     |                                                  DRAINING
     +<------------------------------------------------------+
```

These internal substates map to the generic lifecycle states as follows:

| Internal State                                    | Maps To                      | Rationale                          |
| ------------------------------------------------- | ---------------------------- | ---------------------------------- |
| DISCONNECTED, CONNECTING, AUTHENTICATING, SYNCING | `INITIALIZING` or `DEGRADED` | Not yet fully operational          |
| READY                                             | `RUNNING`                    | Fully operational                  |
| DEGRADED                                          | `DEGRADED`                   | Partially functional               |
| DRAINING                                          | `DRAINING`                   | Graceful shutdown                  |
| STOPPING                                          | `DRAINING` or `STOPPED`      | Force stop in progress or complete |

The adapter reports its internal state via `AdapterHealth.details` for observability. The lifecycle manager only tracks the generic states. See Spec Section 15 for a concrete example (MeshCore).

## 8. Delivery Method

### 8.1 Method Signature

```python
async def deliver(self, plan: DeliveryPlan) -> DeliveryReceipt:
```

Every outbound event arrives as a `DeliveryPlan`. The adapter does not receive raw events; it receives a plan containing the event, the target, and delivery metadata.

### 8.2 DeliveryPlan

```python
@dataclass
class DeliveryPlan:
    plan_id: str
    event_id: str                          # Event being delivered
    target: RouteTarget                    # Structured target (adapter, channel, destination)
    primary_strategy: DeliveryStrategy
    fallback_chain: list[DeliveryStrategy] # Ordered fallback attempts
    retry_policy: RetryPolicy | None
    deadline: datetime | None              # Maximum time to keep attempting delivery
```

### 8.3 DeliveryReceipt

```python
@dataclass(frozen=True)
class DeliveryReceipt:
    sequence: int = 0                      # Monotonically increasing sequence number
    receipt_id: str = ""                   # Unique receipt record identifier
    event_id: str = ""                     # The canonical event being delivered
    delivery_plan_id: str = ""             # Delivery plan this receipt belongs to
    target_adapter: str = ""               # Name of the target adapter
    status: Literal["accepted", "queued", "sent", "confirmed", "suppressed", "failed", "dead_lettered"] = "accepted"
    error: str | None = None               # Error message if delivery failed
    adapter_message_id: str | None = None  # Platform-specific message ID after send
    next_retry_at: datetime | None = None  # Scheduled time for next retry attempt
    attempt_number: int = 1                # 1-indexed attempt number (1 for first attempt)
    parent_receipt_id: str | None = None   # Receipt ID of preceding attempt in the chain
    created_at: datetime = ...             # Timestamp when this receipt was created
```

Phase 1 does not define a `DeliveryStatus` enum in code. Receipt status is a string literal constrained to the values shown above.

### 8.4 Receipt Rules

- Receipts are **append-only**. Every delivery attempt produces a new `DeliveryReceipt` row. Existing rows are never updated or deleted.
- The current delivery status is a projection: the latest receipt for a given `(event_id, delivery_plan_id, target_adapter)` tuple.
- Failed deliveries trigger the fallback chain. If all fallbacks fail, the event is dead-lettered.

### 8.5 What deliver() Must Do

1. Receive the pre-rendered `RenderingResult` from the pipeline.
2. Transport the rendered payload to the external platform.
3. The pipeline (not the adapter) records delivery receipts and native message refs.

If the send fails, raise an exception. The pipeline handles retry logic and receipt recording based on the delivery plan's retry policy.

## 9. Adapter Registry

### 9.1 Registry Interface

```python
class AdapterRegistry(Protocol):
    """Tracks all registered adapter instances and their metadata."""

    def register(self, info: AdapterInfo, adapter: Adapter) -> None:
        """Register an adapter instance. Called during startup."""
        ...

    def get(self, name: str) -> Adapter | None:
        """Look up an adapter by instance name."""
        ...

    def get_info(self, name: str) -> AdapterInfo | None:
        """Look up adapter metadata by instance name."""
        ...

    def list_adapters(self) -> list[AdapterInfo]:
        """List all registered adapters and their metadata."""
        ...

    def list_by_role(self, role: AdapterRole) -> list[AdapterInfo]:
        """List adapters filtered by role."""
        ...

    def unregister(self, name: str) -> None:
        """Remove an adapter from the registry. Called during shutdown."""
        ...
```

### 9.2 Registration Flow

1. The runtime loads adapter configuration from YAML.
2. For each adapter entry, it instantiates the adapter class, passing the config block.
3. The adapter constructs its `AdapterInfo` and returns it.
4. The runtime calls `registry.register(info, adapter_instance)`.
5. The runtime calls `adapter.start(context)` with a fresh `AdapterContext`.
6. On shutdown, the runtime calls `adapter.stop(timeout)`, then `registry.unregister(name)`.

### 9.3 Configuration

Adapter type determines the role. The operator does not set `role` manually.

```yaml
adapters:
  meshcore-radio-1:
    type: meshcore # role: TRANSPORT (inferred)
    connection: { ... }
    channels: { ... }

  matrix-home:
    type: matrix # role: PRESENTATION (inferred)
    homeserver: "https://matrix.example.com"
    rooms: { ... }

  irc-bridge:
    type: irc # role: HYBRID (inferred)
    server: "irc.example.com"
    channels: ["#mesh"]
```

The `type` field maps to a Python class path resolved by the adapter registry. Built-in types resolve to `adapters/<type>/adapter.py`. Custom adapter types may specify a `class` field explicitly.

## 10. Metadata Contract

Adapters must populate the structured metadata namespaces defined by the `EventMetadata` model (Spec Section 14). Flat, prefix-based metadata (like `metadata.meshtastic.*`) is not used. The namespaces are:

```python
@dataclass
class EventMetadata:
    transport: TransportMetadata | None     # How the event arrived
    routing: RoutingMetadata | None         # Routing decisions applied
    radio: RadioMetadata | None             # Radio-specific data (frequency, SNR, RSSI, hop)
    telemetry: TelemetryMetadata | None     # Device telemetry at time of event
    native: NativeMetadata | None           # Transport-native fields not yet normalized
    custom: dict                            # Plugin/extension metadata
```

| Namespace   | Purpose                    | Example Fields                                                         |
| ----------- | -------------------------- | ---------------------------------------------------------------------- |
| `transport` | Transport layer details    | `protocol`, `gateway_id`, `received_at`, `encoding`                    |
| `routing`   | Routing context            | `matched_routes`, `fanout_group`, `bridge_id`                          |
| `radio`     | Radio-specific data        | `frequency`, `modulation`, `snr`, `rssi`, `hop_limit`, `channel_index` |
| `telemetry` | Device state at event time | `battery_percent`, `voltage_mv`, `uptime_seconds`, `air_util_tx`       |
| `native`    | Unnormalized native fields | Adapter-specific raw fields not yet mapped to canonical fields         |
| `custom`    | Plugin/extension data      | Key-value pairs using reverse-DNS namespacing                          |

The codec should map native fields into the correct namespace during `decode()`. The enrichment stage may further normalize `native` fields into their proper namespaces.

## 11. Embedding Metadata in Outbound Events

When delivering to presentation adapters, metadata may be embedded in the native event content. The embedding mode is configurable per adapter:

| Mode      | What Gets Embedded                                                                                                          | Use Case                                                  |
| --------- | --------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| `off`     | Nothing                                                                                                                     | Pure display surface. All correlation through storage.    |
| `minimal` | `event_id`, `source_transport_id`                                                                                           | Limited context, less exposure on redaction.              |
| `safe`    | Normalized metadata (event kind, source adapter, transport protocol, radio metrics, telemetry). No secrets or raw payloads. | Recommended default.                                      |
| `full`    | All metadata                                                                                                                | Maximum context. All metadata lost on platform redaction. |

Regardless of mode, the following are never embedded:

- Channel keys, private keys, or access tokens
- Raw encrypted blobs or raw packets
- Raw native protocol data (protobuf, Reticulum packets)
- Identity private keys or signing keys
- Full raw native archive data

Storage is always authoritative. Embedded metadata is secondary and may be lost due to platform redaction, pruning, or API changes. Any feature that needs reliable metadata must read from storage, not from the presentation platform.

For TRANSPORT adapters that support metadata fields (like LXMF's `fields` dict), canonical event metadata is embedded using a namespaced key:

```python
# Outbound metadata in transport-native fields dict
"org.medre.event": {
    "schema": 1,
    "canonical_event_id": "...",
    "relation": {"type": "reply", "parent_event_id": "..."},
    "source": "medre-runtime"
}
```

The `org.medre` namespace is a placeholder and will be updated when the project is named.

## 12. Complete Protocol Summary

### 12.1 What You Must Implement

To build an adapter, provide:

1. A class satisfying the `Adapter` protocol (Section 2.1): `start`, `stop`, `deliver`, `health_check`.
2. A codec satisfying `AdapterCodec` (Section 4): `decode` and `encode`.
3. An `AdapterInfo` describing your capabilities (Section 6.1).
4. Proper lifecycle state reporting (Section 7).

### 12.2 What the Runtime Provides

| Concern              | Provided By                            |
| -------------------- | -------------------------------------- |
| Routing              | `core/routing/`                        |
| Delivery planning    | `core/planning/`                       |
| Fallback resolution  | `core/planning/fallback_resolution.py` |
| Relation resolution  | `core/planning/relation_resolution.py` |
| Capability downgrade | `core/planning/capability_fallback.py` |
| Target rendering     | `core/rendering/`                      |
| Policy evaluation    | `core/policies/`                       |
| Storage              | `core/storage/`                        |
| Identity resolution  | `core/identity/`                       |
| Observability        | `core/observability/`                  |
| Lifecycle management | `core/lifecycle/`                      |

### 12.3 Data Flow Summary

```yaml
Inbound: Transport --> raw data
  --> adapter listener loop
  --> codec.decode(NativeEvent)
  --> CanonicalEvent
  --> ctx.publish_inbound(event)
  --> ingress policy
  --> storage
  --> enrichment
  --> transforms
  --> event policy
  --> routing
  --> route policy
  --> delivery planning
  --> delivery policy / rendering
  --> adapter.deliver(DeliveryPlan)

Outbound: DeliveryPlan
  --> adapter.deliver(plan)
  --> codec.encode(event, plan)
  --> NativeOutbound
  --> transport send
  --> DeliveryReceipt
  --> native_message_ref stored
```

### 12.4 Quick Reference: Imports and Types

```python
# Protocols to implement
from core.adapter import Adapter, AdapterCodec

# Data structures to populate
from core.events.canonical import CanonicalEvent, EventRelation, EventMetadata
from core.delivery import DeliveryPlan, DeliveryReceipt
from core.rendering.renderer import RenderingResult
from core.adapter import (
    AdapterRole, AdapterLifecycleState, AdapterContext,
    AdapterInfo, AdapterHealth, AdapterCapabilities,
    AdapterCapability, CapabilityLevel,
    NativeEvent, NativeOutbound, RateLimitConfig,
)

# Registry (provided by runtime)
from core.lifecycle.registry import AdapterRegistry
```

---

_This contract is extracted from the [Modular Event Communications Runtime Specification](../spec/modular-event-engine-spec.md). If this document conflicts with the spec, the spec takes precedence. Report discrepancies._

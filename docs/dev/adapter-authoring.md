# Adapter Authoring Guide

This guide explains how to write a new transport adapter for MEDRE. It covers
the adapter protocol, codec, renderer, session, tests, and registration.

For the normative adapter contract, see [Adapter Runtime Contract](../spec/adapter-runtime.md).
For the event model, see [Event Model](../spec/event-model.md).

## Overview

An adapter bridges between a native transport protocol (Meshtastic, MeshCore,
LXMF, Matrix, Discord, etc.) and MEDRE's canonical event pipeline. Adapters
never call other adapters. All communication flows through the pipeline.

Each adapter has four components:

1. **Adapter class** -- lifecycle and delivery
2. **Codec** -- format conversion between native data and canonical events
3. **Renderer** -- format conversion from canonical events to native payloads
4. **Session** -- connection management, callbacks, reconnect logic

## Adapter Roles

Every adapter declares a role:

| Role             | Description                                                                                                                            | Examples                                                   |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| **TRANSPORT**    | Moves data to/from a physical or logical transport. Handles protocol specifics, connection management, and raw data encoding/decoding. | Meshtastic, MeshCore, LXMF, MQTT, TCP serial bridge, AX.25 |
| **PRESENTATION** | Presents events to human users. Handles formatting, rich content, threading, reactions, and user interaction.                          | Matrix, Discord, Telegram, Slack, Web UI                   |
| **HYBRID**       | Both transports and presents. Can act as a message source and a display target simultaneously.                                         | IRC, XMPP                                                  |

The role is inferred from the adapter type at configuration load time. Operators
do not set it manually.

## Step 1: Implement the Adapter Protocol

Your adapter class satisfies the `Adapter` protocol with four methods:
`start`, `stop`, `deliver`, and `health_check`.

```python
from medre.core.contracts.adapter import (
    AdapterContract,
    AdapterContext,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterRole,
)
from medre.core.rendering.renderer import RenderingResult


class MyAdapter(AdapterContract):
    adapter_id: str
    platform: str = "my_transport"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str, **config):
        super().__init__()
        self.adapter_id = adapter_id
        self._config = config
        self.ctx: AdapterContext | None = None
        self._started = False

    async def start(self, ctx: AdapterContext) -> None:
        """Initialize the adapter and establish connections."""
        self.ctx = ctx
        self._mark_started(ctx)
        self._started = True
        # Start listener loop for TRANSPORT adapters
        if self.role == AdapterRole.TRANSPORT:
            asyncio.create_task(self._listen())

    async def stop(self, timeout: float = 5.0) -> None:
        """Gracefully shut down."""
        self._started = False

    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None:
        """Deliver a pre-rendered payload to the transport."""
        # The result is already rendered. Just transport it.
        await self._send_to_transport(result.payload)
        return AdapterDeliveryResult(
            native_message_id="<transport-assigned-id>",
            native_channel_id=result.target_channel,
        )

    async def health_check(self) -> AdapterInfo:
        """Return current health status."""
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.1.0",
            capabilities=self._capabilities(),
            health="healthy" if self._started else "unknown",
        )
```

### Key rules for `deliver()`

- `deliver()` receives a `RenderingResult`, not a raw `CanonicalEvent`. The
  pipeline handles rendering. The adapter just transports the payload.
- Return an `AdapterDeliveryResult` with the transport-assigned native message
  ID. This is stored in `native_message_refs` for cross-adapter correlation.
- If the send fails, raise an exception. The pipeline handles retry logic and
  receipt recording.
- Do not re-render, reformat, or inspect the event kind inside `deliver()`.

### Key rules for `start()`

- `start()` receives an `AdapterContext`. Store it as `self.ctx`.
- Call `self._mark_started(ctx)` (from `AdapterContract`) to wire the
  framework integration.
- TRANSPORT adapters start an internal listener loop that receives native
  data, decodes it via the codec, and publishes canonical events.
- Do not return until the adapter is ready to accept delivery plans or the
  connection attempt has progressed far enough to report health state.

## Step 2: Implement the Codec

The codec handles conversion between native protocol data and canonical events.
It is an adapter-private concern, not part of the public protocol. You can
implement it as a separate class or inline the logic.

### Inbound: `decode`

TRANSPORT adapters receive native data from their transport, wrap it in a
`NativeEvent`, convert it via the codec, and publish the canonical event:

```python
async def _listen(self):
    """Internal listener loop for TRANSPORT adapters."""
    async for raw_data in self._transport.stream():
        native = NativeEvent(raw_data=raw_data, metadata={...})
        event = await self.codec.decode(native)
        await self.ctx.publish_inbound(event)
```

The `decode` method sets at minimum:

- `event_id`: UUIDv7 for time ordering
- `event_kind`: e.g., `"message.text"`, `"telemetry"`
- `schema_version`: currently `1`
- `timestamp`: UTC datetime
- `source_adapter`: your adapter instance name
- `source_transport_id`: the native actor identity (not the message ID)
- `source_channel_id`: the native channel/room/topic
- `payload`: kind-specific dict (e.g., `{"body": text}`)

### Outbound: `encode`

The `encode` method converts a canonical event into a native protocol payload
for delivery. This is called inside `deliver()` or by the renderer.

```python
class MyCodec:
    async def decode(self, native_event: NativeEvent) -> CanonicalEvent:
        raw = native_event.raw_data
        return CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=self.adapter_id,
            source_transport_id=raw["sender_id"],
            source_channel_id=str(raw.get("channel")),
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": raw["text"]},
            metadata=EventMetadata(),
        )

    async def encode(self, event, plan) -> NativeOutbound:
        return NativeOutbound(
            payload=event.payload["body"].encode("utf-8"),
            metadata={"destination": plan.target.channel},
            native_message_id=None,
        )
```

### Codec responsibilities

The codec owns format translation and nothing else. It should:

- Map native fields to canonical event fields.
- Set `source_transport_id` to the native actor identity.
- Set `source_channel_id` to the native channel/room/topic.
- Populate `metadata.native` with transport-specific raw fields.

It should not:

- Call `publish_inbound` (that is the adapter's job).
- Make routing decisions.
- Enrich events with data from other adapters.
- Apply policy rules.

## Step 3: Declare Capabilities

Adapters declare what they can do. The capability model drives delivery
planning, capability downgrade, and relation fallback.

```python
from medre.core.contracts.adapter import AdapterCapabilities

_capabilities = AdapterCapabilities(
    text=True,                       # Plain text messages
    title=False,                     # Subject/title field
    replies="native",                # Native reply threading
    reactions="fallback",            # Inline text fallback for reactions
    edits="unsupported",             # No edit support
    deletes="unsupported",           # No delete support
    attachments=False,               # No file/image support
    metadata_fields=False,           # No arbitrary key-value metadata
    delivery_receipts=False,         # No per-message confirmation
    store_and_forward=False,         # No message storage
    direct_messages=True,            # Point-to-point delivery
    channels=True,                   # Channel-based addressing
    async_delivery=True,             # Async delivery state model
    max_text_chars=200,              # Character limit
)
```

### Capability support levels

| Level                           | Meaning                                                 |
| ------------------------------- | ------------------------------------------------------- |
| `True`                          | Fully supported natively                                |
| `"native"`                      | Full native support                                     |
| `"fallback"`                    | Supported via inline text fallback                      |
| `"metadata_native"`             | Supported via metadata fields (e.g., LXMF fields dict)  |
| `"metadata_native_or_fallback"` | Metadata between aware peers, inline fallback otherwise |
| `False` / `"unsupported"`       | Not supported                                           |

### How capabilities drive behavior

- **Replies**: If the target adapter lacks reply support, the delivery plan
  renders the reply as inline text.
- **Reactions**: If reactions are unsupported, reactions are dropped for that
  target.
- **Edits**: If edits are `"metadata_native_or_fallback"`, edits render as
  new messages with metadata signaling.
- **Truncation**: If the adapter has a byte limit, the renderer handles
  splitting or truncation.

## Step 4: Implement the Session

The session manages the transport connection, callback wiring, reconnect
logic, and send operations. All four existing adapters follow the same pattern:

1. Construction with config
2. `start()` -- establish connection, register callbacks
3. `stop()` -- clean up connections, cancel tasks
4. `diagnostics()` -- return a snapshot of session state
5. Reconnect with bounded exponential backoff (max 10 attempts)

### Reconnect parameters

| Parameter    | Typical value        |
| ------------ | -------------------- |
| Max attempts | 10                   |
| Backoff cap  | 30s (60s for Matrix) |
| Jitter       | +-25%                |

### Connection modes

Every adapter supports `"fake"` mode for testing. Real connection types
vary by transport:

| Transport  | Real modes       |
| ---------- | ---------------- |
| Matrix     | nio sync         |
| Meshtastic | TCP, Serial, BLE |
| MeshCore   | TCP, Serial, BLE |
| LXMF       | Reticulum        |

When `connection_type` is `"fake"`, the adapter uses a fake client that
does not require any SDK dependency.

### Thread safety

Some SDKs (Meshtastic, LXMF/Reticulum) use background threads for callbacks.
The session normalizes native data to plain dicts on the calling thread, then
bridges onto the asyncio loop:

```python
# Meshtastic/LXMF pattern
loop.call_soon_threadsafe(self._invoke_inbound_callback, normalised)
```

Never call `asyncio.create_task()` from a non-loop thread.

## Step 5: Write the Renderer

The renderer converts canonical events into transport-specific payloads.
Renderers live in `core/rendering/` and are selected by the delivery planner
based on the target adapter.

The renderer produces a `RenderingResult`:

```python
from medre.core.rendering.renderer import RenderingResult

class MyRenderer:
    async def render(self, event, plan, capabilities):
        text = event.payload.get("body", "")
        # Apply capability-based truncation
        if capabilities.max_text_chars:
            text = text[:capabilities.max_text_chars]
        return RenderingResult(
            event_id=event.event_id,
            target_adapter=plan.target.adapter,
            target_channel=plan.target.channel,
            payload={"text": text},
            metadata={},
        )
```

The adapter's `deliver()` method receives this `RenderingResult`, not the raw
event. This separation means adapters never contain formatting logic.

## Step 6: Register in Configuration

Add the adapter type to the configuration:

```yaml
adapters:
  my-transport-1:
    type: my_transport # role: TRANSPORT (inferred)
    connection:
      type: tcp
      host: "192.168.1.100"
      port: 4000
    channels:
      0: "general"
      1: "admin"
```

The `type` field maps to a Python class path resolved by the adapter registry.
Built-in types resolve to `adapters/<type>/adapter.py`. Custom adapter types
may specify a `class` field explicitly.

### Registration flow

1. The runtime loads adapter configuration from YAML.
2. For each adapter entry, it instantiates the adapter class with the config.
3. The adapter constructs its `AdapterInfo`.
4. The runtime calls `registry.register(info, adapter_instance)`.
5. The runtime calls `adapter.start(context)` with a fresh `AdapterContext`.
6. On shutdown, the runtime calls `adapter.stop(timeout)`, then
   `registry.unregister(name)`.

## Step 7: Write Tests

### Use the fake adapter pattern

Every adapter has a fake variant in `src/medre/adapters/fakes/` that exercises
the full pipeline without network or hardware dependencies. See the
[testing guide](./testing.md) for tier definitions.

The fake adapters (`FakeTransportAdapter`, `FakePresentationAdapter`) are the
reference implementation. They demonstrate the contract:

- `start()` stores the context and marks started
- `stop()` marks stopped
- `deliver()` receives a `RenderingResult`, returns an `AdapterDeliveryResult`
- `simulate_inbound()` publishes events into the pipeline
- `health_check()` returns an `AdapterInfo` snapshot

### Test your adapter with these tiers

| Tier | What to test                                                          |
| ---- | --------------------------------------------------------------------- |
| 1    | Pipeline processes your canonical events correctly                    |
| 2    | `simulate_inbound()` produces the same results as direct injection    |
| 3    | SDK callback wiring works with mocked SDK                             |
| 4    | Real SDK works against containerized services (`@pytest.mark.docker`) |
| 5    | Real adapter against real endpoint (`@pytest.mark.live`)              |

### Codec tests

Test that your codec correctly maps native fields to canonical event fields:

```python
async def test_codec_maps_sender_to_source_transport_id():
    native = NativeEvent(raw_data={"sender_id": "node42", "text": "hello"})
    event = await codec.decode(native)
    assert event.source_transport_id == "node42"
```

### Session tests

Test connection lifecycle, reconnect behavior, and callback normalization:

```python
async def test_session_reconnect_on_failure(fake_client):
    session = MySession(config)
    fake_client.next_connect_fails(3)  # Fail 3 times
    await session.start(ctx)
    assert session.diagnostics().reconnect_attempts <= 10
```

### Capability tests

Verify that your capability declarations match your adapter's actual behavior.
If you declare `replies="native"`, the codec should produce `EventRelation`
objects with `relation_type="reply"`.

## AdapterContext Reference

Each adapter receives an `AdapterContext` on startup. This is the adapter's
only window into the runtime.

| Field             | Purpose                                                                 |
| ----------------- | ----------------------------------------------------------------------- |
| `adapter_id`      | Unique identifier for this adapter instance                             |
| `publish_inbound` | The ingress point. Call with a `CanonicalEvent` to inject into pipeline |
| `logger`          | A pre-configured `logging.Logger` scoped to the adapter                 |
| `clock`           | Callable returning current UTC `datetime` (for deterministic testing)   |
| `shutdown_event`  | An `asyncio.Event` set when graceful shutdown is requested              |

### What adapters cannot do

- Import or call another adapter directly.
- Bypass the pipeline to send events straight to another transport.
- Modify events after they have been published via `publish_inbound`.
- Access the event bus, routing engine, or policy pipeline directly.

## Metadata Contract

Adapters populate the structured metadata namespaces defined by `EventMetadata`.
All transport-specific details go into `metadata.native.data[<transport_name>]`.

| Namespace   | Purpose                    | Example fields                                        |
| ----------- | -------------------------- | ----------------------------------------------------- |
| `transport` | Transport layer details    | `protocol`, `gateway_id`, `received_at`, `encoding`   |
| `routing`   | Routing context            | `matched_routes`, `fanout_group`, `bridge_id`         |
| `radio`     | Radio-specific data        | `frequency`, `modulation`, `snr`, `rssi`, `hop_limit` |
| `telemetry` | Device state at event time | `battery_percent`, `voltage_mv`, `uptime_seconds`     |
| `native`    | Unnormalized native fields | Adapter-specific raw fields not yet mapped            |
| `custom`    | Plugin/extension data      | Key-value pairs using reverse-DNS namespacing         |

No adapter injects loose transport-specific fields directly onto
`CanonicalEvent` or top-level `EventMetadata`. All transport data goes
through `metadata.native.data[<transport_name>]`.

## Lifecycle States

Adapters report their state through `health_check()` and diagnostics:

| State            | Ingress | Delivery                   | Notes                               |
| ---------------- | ------- | -------------------------- | ----------------------------------- |
| **INITIALIZING** | Buffer  | Buffer                     | `start()` has not returned yet      |
| **RUNNING**      | Accept  | Queue and deliver          | Normal operation                    |
| **DEGRADED**     | Accept  | Queue, delay, may fallback | Connection unstable                 |
| **DRAINING**     | Reject  | Complete in-flight only    | Graceful shutdown. Reject new work. |
| **STOPPED**      | Reject  | None                       | Terminal state                      |

Every state change emits a `system.lifecycle` canonical event.

## What the Runtime Provides

Adapters don't need to implement routing, retry, policy, or observability.
The runtime handles all of it:

| Concern              | Provided by                            |
| -------------------- | -------------------------------------- |
| Routing              | `core/routing/`                        |
| Delivery planning    | `core/planning/`                       |
| Fallback resolution  | `core/planning/fallback_resolution.py` |
| Relation resolution  | `core/planning/relation_resolution.py` |
| Capability downgrade | `core/planning/capability_fallback.py` |
| Target rendering     | `core/rendering/`                      |
| Policy evaluation    | `core/policies/`                       |
| Storage              | `core/storage/`                        |
| Observability        | `core/observability/`                  |
| Lifecycle management | `core/lifecycle/`                      |

## Existing Adapter Reference

The `src/medre/adapters/fakes/` directory contains working reference
implementations:

| File              | Purpose                                                   |
| ----------------- | --------------------------------------------------------- |
| `transport.py`    | `FakeTransportAdapter` -- minimal TRANSPORT adapter       |
| `presentation.py` | `FakePresentationAdapter` -- minimal PRESENTATION adapter |
| `meshcore.py`     | Fake MeshCore adapter with queue tracking                 |
| `meshtastic.py`   | Fake Meshtastic adapter with outbound queue               |
| `matrix.py`       | Fake Matrix adapter with session diagnostics              |
| `lxmf.py`         | Fake LXMF adapter with delivery state tracking            |

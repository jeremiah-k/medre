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
from medre.core.contracts import (
    AdapterContract,
    AdapterContext,
    AdapterHandoffResult,
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

    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        """Deliver a pre-rendered payload to the transport."""
        # The result is already rendered. Just transport it.
        await self._send_to_transport(result.payload)
        return AdapterHandoffResult(
            disposition="transport_handoff",
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
- Return an `AdapterHandoffResult` on every successful call. Use
  `disposition="transport_handoff"` when the external transport boundary was
  reached during the call, or `disposition="deferred"` when local work was
  accepted and completion will be reported through `DeliveryFeedback`. A
  deferred result may report only `confirmation_level="unknown"` or
  `"local_queue"`; stronger confirmation means the transport boundary was
  already reached and therefore requires `transport_handoff`.
- Native IDs are transport facts, not required success markers. When a real
  platform ID is known at hand-off it is stored in `native_message_refs`. Use
  `None` for absence; empty or whitespace-only native identifiers are invalid.
- Keep opaque hand-off metadata namespaced. Do not use the reserved top-level
  keys closed hand-off, feedback, or attempt-provenance field names (for example
  `native_channel_id`, `state`, or `outbox_id`).
- If the send fails, raise an exception. The pipeline handles retry logic and
  receipt recording.
- Do not re-render, reformat, or inspect the event kind inside `deliver()`.

### Deferred delivery feedback

Adapters that return `disposition="deferred"` **must** carry the
`RenderingResult.attempt_provenance` alongside their queued/session work. Later
facts are reported through the single optional
`ctx.report_delivery_feedback(...)` sink using one of:

- `DeferredHandoffCompleted` — the deferred attempt reached the transport boundary;
- `DeferredHandoffFailed` — it terminated before that boundary;
- `PostHandoffObservation` — append-only evidence after a successful hand-off.

Do not reconstruct attempt identity from timing, mutable adapter state, or
receipts. A deferred adapter **must reject before local admission** when
`RenderingResult.attempt_provenance` is absent or when its `AdapterContext`
does not provide `report_delivery_feedback`. Outbox-less direct delivery is
valid only for an immediate `transport_handoff`; accepting deferred work
without durable identity or a feedback path would create an attempt that core
can never finalize safely.

### Key rules for `start()`

- `start()` receives an `AdapterContext`. Store it as `self.ctx`.
- Call `self._mark_started(ctx)` (from `AdapterContract`) to wire the
  framework integration.
- TRANSPORT adapters start an internal listener loop that receives native
  data, decodes it via the codec, and publishes canonical events.
- Do not return until the adapter is ready to accept delivery plans or the
  connection attempt has progressed far enough to report health state.

## Step 2: Implement the Codec

The codec handles inbound conversion from native protocol data to canonical
events. It is an adapter-private concern exposed through the decode-only
`AdapterCodec` contract. Outbound conversion belongs exclusively to renderers.

### Inbound: `decode`

TRANSPORT adapters receive native data from their transport, wrap it in a
`NativeEvent`, convert it via the codec, and publish the canonical event:

```python
async def _listen(self):
    """Internal listener loop for TRANSPORT adapters."""
    async for raw_data in self._transport.stream():
        native = NativeEvent(raw_data=raw_data, metadata={...})
        event = self.codec.decode(native)
        await self.publish_inbound(event)
```

The `decode` method sets at minimum:

- `event_id`: UUIDv7 for time ordering
- `event_kind`: e.g., `"message.created"`, `"telemetry.received"`
- `schema_version`: currently `1`
- `timestamp`: UTC datetime
- `source_adapter`: your adapter instance name
- `source_transport_id`: the native actor identity (not the message ID)
- `source_channel_id`: the native channel/room/topic
- `payload`: kind-specific dict (e.g., `{"body": text}`)

### Outbound rendering

There is no codec `encode()` contract. The rendering pipeline converts a
canonical event into `RenderingResult` before `deliver()` is called. Keeping
outbound presentation in renderers prevents transport sessions from growing a
second formatting path.

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
    store_and_forward=False,         # No message storage
    direct_messages=True,            # Point-to-point delivery
    channels=True,                   # Channel-based addressing
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
logic, and send operations. Built-in adapters follow the same lifecycle pattern:

1. Construction with config
2. `start()` -- establish connection, register callbacks
3. `stop()` -- clean up connections, cancel tasks
4. `diagnostics()` -- return a snapshot of session state
5. Apply the transport profile's recovery policy with bounded-rate backoff and explicit shutdown ownership

### Reconnect parameters

Reconnect attempt budgets are transport-specific. Do not copy a finite attempt
ceiling from another adapter into generic session code. A transport profile may
use a finite budget or lifetime recovery, but retry cadence must remain bounded
and shutdown must always terminate recovery promptly.

| Parameter      | Typical guidance                               |
| -------------- | ---------------------------------------------- |
| Attempt budget | Transport-profile specific                     |
| Backoff cap    | Transport-profile specific (commonly 30–60 s)  |
| Jitter         | Commonly +-25% where exponential retry is used |

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
Transport renderers live inside their adapter packages and are registered with
the shared rendering pipeline by the installed adapter registry. The delivery
planner selects them by target platform.

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

## Step 6: Package and Register the Adapter

An adapter is an independent Python distribution with a `medre.adapters`
entry point. Its zero-argument factory returns `AdapterSpec`. Use
`dependencies = ["medre>=0.1.0,<0.2.0"]` to declare the core interface range
supported by the adapter, and declare native SDK requirements in the adapter's
own metadata. Repository, distribution, module, and transport names can differ.
See [installation](../ops/adapter-installation.md#adapter-author-contract) for
an example that uses a community distribution and top-level Python module.

The first-party repositories retain `medre.adapters.<transport>` and
`medre.config.adapters.*` through standard namespace-path extension. Community
adapters can use their own import packages; sharing MEDRE's namespace is optional.

The adapter owns its config dataclass, optional runtime wrapper, fake, session,
codec, renderer factory, native-metadata readers, attribution projector, SDK
requirements, and protocol tests. MEDRE owns the generic contracts, config
orchestration, routing, storage, evidence, and runtime lifecycle.

`AdapterSpec` references implementation symbols through `SymbolRef`. The
registration module and config validators stay SDK-free at import time. SDK
imports belong behind the live session/compatibility implementation. Optional
hooks support runtime preparation, directories, CLI contributions, and
support-bundle field classification. SDK logger roots default to WARNING;
`logger_levels=(("your_sdk.crypto", "ERROR"),)` declares more specific noise
policy without importing the SDK. Operator overrides take precedence.

An adapter with native structured addresses declares a `DestinationSpec` for
each addressing kind it owns. Its lazy validator accepts a
`RouteDestinationConfig`, returns `None` for a valid address, and raises
`ValueError` for invalid input. It can validate opaque identifiers and custom
`metadata` without extending core's parser. Validators are pure and SDK-free.
Core owns the generic `channel` kind; duplicate native kind names are rejected.
Use a distinctive transport-qualified kind to avoid clashes.

Installed entry points are discovered once per process and ordered by name.
Duplicate transport names, broken factories, and conflicting declarations
are quarantined with diagnostics available through `medre adapters check`.
Conflicting owners are all rejected; healthy providers remain usable. Discovery never fetches packages. Restart after changing
installed providers. Config YAML selects `adapters.<transport>.<instance>`;
it contains neither installer URLs nor arbitrary Python class paths.

Adding an adapter requires no core registration-list edit. Generic consumers
use `iter_adapter_specs()` and `AdapterConfigSet.groups()`; loader, environment,
runtime, renderer, CLI, and native-metadata dispatch share those declarations.
Core's CI compatibility manifest is a curated test set, independent of runtime
discovery and open third-party distribution.

### Contribute an offline sample

Set `sample_config=SymbolRef("your_package.sample", "sample_config")` to a
zero-argument callback returning `AdapterSample(native_values, channel="opaque-conversation")`. Core supplies distinct instance IDs,
`enabled: true`, and `adapter_kind: fake`. Leave lifecycle fields out of
`native_values`. This callback is optional and SDK-free. Providers without a
fake or sample remain supported, but are omitted from `medre config sample`.
The callback owns native config values and addressing; core generates routes
without recognizing your platform name.

## Step 7: Write Tests

The [registration reference](adapter-registration.md) documents every field,
hook signature, loading phase, public metadata helper, and conformance tool.

### Use the fake adapter pattern

An adapter may declare a fake variant through `AdapterSpec.fake_adapter`,
implemented in that adapter's distribution. Its fake exercises the full
pipeline without network or hardware dependencies. `fake_adapter=None` is valid;
core does not fabricate fake support for that provider. Lifecycle drills choose
only declared fake providers and report failure when none is available.
Fake event factories may expose `make_event(text=..., event_kind=...)` for smoke
injection. Run-session callback ingress uses the canonical
`AdapterContract.publish_inbound(event)` seam; native packet simulation methods
are adapter-specific and are not part of that generic interface. See the
[testing guide](./testing.md) for tier definitions.

The fake adapters (`FakeTransportAdapter`, `FakePresentationAdapter`) are the
reference implementation. They demonstrate the contract:

- `start()` stores the context and marks started
- `stop()` marks stopped
- `deliver()` receives a `RenderingResult`, returns an `AdapterHandoffResult`
- `publish_inbound(event)` publishes canonical events into the pipeline
- `health_check()` returns an `AdapterInfo` snapshot

**Optional smoke-source contract:** if the fake participates in `medre smoke`,
it provides a synchronous `make_event(*, text: str, event_kind: EventKind)`
returning a `CanonicalEvent` with its own `source_adapter`. MEDRE uses this
for deterministic smoke and run-session injection. The core does not ask real
transport adapters to implement this testing helper. Adapter-owned unit tests
should check the method's signature and originating adapter ID. A multi-route
smoke selects only one source per run; use `--source-adapter` to exercise each
configured source, and inspect `source_candidates` in the JSON evidence report.
Only started fakes with this helper are eligible sources; a fake without it can
still be a delivery target. A pass requires delivery for every configured route
target matched by the event, including targets lost during construction/startup.
Event-factory errors fail the smoke and trigger runtime cleanup.

### Test your adapter with these tiers

| Tier | What to test                                                           |
| ---- | ---------------------------------------------------------------------- |
| 1    | Pipeline processes your canonical events correctly                     |
| 2    | `publish_inbound(event)` produces the same results as direct injection |
| 3    | SDK callback wiring works with mocked SDK                              |
| 4    | Real SDK works against containerized services (`@pytest.mark.docker`)  |
| 5    | Real adapter against real endpoint (`@pytest.mark.live`)               |

### Codec tests

Test that your codec correctly maps native fields to canonical event fields:

```python
async def test_codec_maps_sender_to_source_transport_id():
    native = NativeEvent(raw_data={"sender_id": "node42", "text": "hello"})
    event = codec.decode(native)
    assert event.source_transport_id == "node42"
```

### Session tests

Test connection lifecycle, reconnect behavior, and callback normalization:

```python
async def test_session_reconnect_on_failure(fake_client):
    session = MySession(config)
    fake_client.next_connect_fails(3)  # Fail 3 times
    await session.start(ctx)
    # Prove recovery ran: attempts were made and the replacement client is live.
    assert session.diagnostics().reconnect_total_attempts >= 1
    assert session.client is not None
    # Then assert the reconnect policy declared by this adapter's transport profile.
    assert session.diagnostics().reconnecting is False
```

### Capability tests

Verify that your capability declarations match your adapter's actual behavior.
If you declare `replies="native"`, the codec should produce `EventRelation`
objects with `relation_type="reply"`.

## AdapterContext Reference

Each adapter receives an `AdapterContext` on startup. This is the adapter's
only window into the runtime.

| Field                                   | Purpose                                                                          |
| --------------------------------------- | -------------------------------------------------------------------------------- |
| `adapter_id`                            | Unique identifier for this adapter instance                                      |
| `publish_inbound`                       | Basic ingress point for a `CanonicalEvent`                                       |
| `logger`                                | Pre-configured `logging.Logger` scoped to the adapter                            |
| `clock`                                 | Callable returning current UTC `datetime` for deterministic testing              |
| `shutdown_event`                        | `asyncio.Event` set when graceful shutdown is requested                          |
| `admit_inbound`                         | Optional durable ingress-admission boundary for transports with cursor ownership |
| `load_checkpoint` / `commit_checkpoint` | Optional application-owned external cursor persistence                           |
| `report_delivery_feedback`              | Optional single sink for the closed `DeliveryFeedback` union                     |

Both inbound seams cross the runtime's bounded admission controller. The
runtime binds the adapter ID automatically so overload is attributed and queued
fairly across adapters. `publish_inbound` is the ordinary callback boundary: if
the bounded gate rejects an arrival, the adapter receives
`InboundAdmissionRejected` and must treat that event as not durably admitted.
Use `admit_inbound` only when the transport can retain or redispatch a native
event until an application-owned cursor advances; capacity rejection on this
seam becomes `DurableIngressDeferredError`, allowing the transport to leave the
event pending instead of losing it.

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

Adapters report their state through `health_check()` and diagnostics. The
normative state vocabulary, transition graph, and per-state ingress/delivery
policies live only in
[spec/adapter-runtime.md §8](../spec/adapter-runtime.md#8-adapter-lifecycle-states).
Adapter implementations and authoring guidance must defer to that table rather
than restating the state machine here.

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

| File                     | Purpose                                                   |
| ------------------------ | --------------------------------------------------------- |
| `transport.py`           | `FakeTransportAdapter` -- minimal TRANSPORT adapter       |
| `presentation.py`        | `FakePresentationAdapter` -- minimal PRESENTATION adapter |
| Transport-specific fakes | Live in their corresponding adapter distributions         |

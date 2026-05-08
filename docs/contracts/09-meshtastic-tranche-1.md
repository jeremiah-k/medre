# Meshtastic Adapter Tranche 1: Radio Transport Validation

> Contract version: 1
> Last updated: 2026-05-08


## Overview

This is a constrained radio transport validation adapter. The Meshtastic adapter uses the `mtjk` library (distribution name `mtjk`, import name `meshtastic`) for optional Meshtastic protocol support. Everything in this tranche validates that the MEDRE runtime's decode/render/deliver pipeline works against a radio transport with short messages, channel-based routing, and protocol-native packet IDs.

The adapter does not route, does not plan, and does not render fallback text. It decodes inbound Meshtastic text packets into canonical events and delivers outbound rendered content. The pipeline owns receipts, relation resolution, and storage. Adapters transport messages and report native delivery metadata back to the pipeline. The Meshtastic-specific renderer lives inside the adapter package (`medre.adapters.meshtastic.renderer`), not in core. Core owns the generic rendering protocol and pipeline machinery. Core never imports from the Meshtastic adapter package.

Meshtastic capabilities in tranche 1 are limited to text message ingress and egress over named channels. Telemetry, node database caching, position data, E2EE, and production hardware connections are all deferred.


## Supported Features

- **Inbound text packet decoding.** Meshtastic `TEXT_MESSAGE_APP` packets are decoded into canonical events by `MeshtasticCodec`. The packet's text payload becomes the canonical event `payload["body"]`. Radio metadata (SNR, RSSI, hop limit, channel index) maps into the structured metadata namespaces defined by `EventMetadata`: `metadata.radio` for signal metrics, `metadata.transport` for protocol details. No flat `metadata.meshtastic.*` namespace exists.
- **Outbound text rendering and delivery.** `MeshtasticRenderer` turns canonical events into Meshtastic text payloads suitable for sending on a specific channel. This renderer lives at `medre.adapters.meshtastic.renderer`, owned by the adapter layer. The adapter's `deliver()` method receives a pre-rendered `RenderingResult` from the pipeline, extracts the rendered text payload, and sends it via the configured Meshtastic client.
- **Basic channel mapping.** `MeshtasticConfig` carries channel mapping fields that associate Meshtastic channel indices (0-7) with canonical channel IDs used by the routing engine. The adapter uses these mappings during decode to set `source_channel_id` and during delivery to determine which Meshtastic channel to target for outbound messages.
- **Native refs via packet IDs.** Inbound Meshtastic packet IDs become native refs through the existing pipeline flow: `MeshtasticCodec.decode()` sets `CanonicalEvent.source_native_ref` with the packet's numeric ID (as a string). The pipeline persists this as `NativeMessageRef(direction="inbound")` after canonical event storage. Outbound delivery IDs become native refs through the existing `AdapterDeliveryResult`: on successful delivery, the adapter returns an `AdapterDeliveryResult` populated with the Meshtastic packet ID assigned by the radio, and the pipeline persists it as `NativeMessageRef(direction="outbound")`.
- **Packet classifier scope.** `MeshtasticCodec` includes a packet type classifier that distinguishes text packets from other Meshtastic portnums (telemetry, position, admin, etc.). In tranche 1, only `TEXT_MESSAGE_APP` packets are fully decoded into canonical events. Other packet types are logged and dropped. The classifier is scaffolding for future telemetry and position decoding.
- **Queue/pacing ownership boundary.** The Meshtastic adapter owns its own outbound pacing and queue management. Meshtastic radios have strict duty cycle limits and slow data rates. The adapter maintains an internal outbound queue and applies pacing between sends. The pipeline delivers `RenderingResult` to the adapter's `deliver()` method; the adapter queues the payload internally and returns immediately with an `AdapterDeliveryResult` (or `None` if no packet ID is available yet). The pipeline does not impose its own pacing on Meshtastic delivery. This is a deliberate ownership boundary: radio transports with physical layer constraints own their own transmit timing.
- **Fake client for tests.** `FakeMeshtasticClient` is a test double that requires no real hardware, no `mtjk` installation, and no network connection. It simulates inbound packet reception and outbound packet sending against in-memory state. All default tests use this fake client.
- **Optional dependency handling.** The `mtjk` package (distribution name `mtjk`, import name `meshtastic`) is guarded by a `HAS_MESHTASTIC` compat flag in `medre.adapters.meshtastic.compat`. When `mtjk` is not installed, the compat flag is `False` and the adapter raises a configuration error on `start()`. Core tests pass without `mtjk` present. The adapter package's own tests use `FakeMeshtasticClient` and do not require `mtjk`.


## Architecture Boundaries

These boundaries are enforced by design, not by convention. Tests verify them.

- `MeshtasticAdapter` does not route. No `Router` import.
- `MeshtasticAdapter` does not plan delivery. No `FallbackResolver`, no `DeliveryPlan` construction.
- `MeshtasticAdapter` does not render fallback text. Rendering lives in `MeshtasticRenderer`.
- `MeshtasticRenderer` does not perform delivery. No Meshtastic client calls.
- `MeshtasticRenderer` is adapter-owned. It lives at `medre.adapters.meshtastic.renderer`. Core owns the generic rendering protocol (interface, pipeline dispatch), not this Meshtastic-specific implementation. Core never imports from the adapter package.
- `MeshtasticCodec` does not route, plan, or render. It is a pure decode/encode layer. It does not resolve native refs or query storage.
- The adapter owns outbound pacing and queueing, not the pipeline. The pipeline calls `deliver()` and the adapter manages its own transmit timing internally.
- Storage remains the authoritative source for event correlation. The pipeline owns receipts and persistence. Adapters transport and report native delivery metadata.
- No real hardware or network is required for default tests. `FakeMeshtasticClient` simulates the full cycle.


## Relation and Reply Behavior

**Relation and reply support: deferred.** Meshtastic's text message protocol in tranche 1 has no native reply threading. The `TEXT_MESSAGE_APP` portnum carries a flat text string with no structured reply reference. Replies and reactions are deferred to a later tranche.

The adapter declares `replies="unsupported"` in its `AdapterCapabilities`. When the pipeline renders a reply for Meshtastic delivery, the capability fallback mechanism renders the reply context as inline text (e.g., `[Alice] re: original msg > reply text`). The adapter does not participate in relation resolution.

This is not a permanent limitation. Future tranches may encode reply metadata in the text payload using a convention (e.g., a `@` reference or a structured prefix), or Meshtastic itself may add a reply portnum. The adapter's capability declaration is the single source of truth for what the pipeline can expect.


## Telemetry Deferral

Meshtastic radios regularly emit telemetry (battery, voltage, uptime, air utilization) and position data. These are distinct portnums in the Meshtastic protocol. Tranche 1 does not decode or process them.

The packet classifier in `MeshtasticCodec` recognizes telemetry and position portnums but does not decode them into canonical `telemetry.received` or `telemetry.position` events. They are logged at debug level and dropped. The canonical event taxonomy defines these kinds; they are reserved for future use.

No `TelemetryMetadata` or `RadioMetadata` is populated from telemetry packets in tranche 1. When telemetry support is added in a future tranche, the codec will decode telemetry portnums into canonical telemetry events with structured radio and telemetry metadata. No schema changes are required: the metadata namespaces already exist.


## Native Ref Flow

### Inbound

1. A Meshtastic `TEXT_MESSAGE_APP` packet arrives at the adapter with a numeric packet ID assigned by the sending radio.
2. `MeshtasticCodec.decode()` converts the packet into a `CanonicalEvent` with `source_native_ref=NativeRef(adapter=<adapter_id>, native_channel_id=<channel_index_as_string>, native_message_id=<packet_id_as_string>)`.
3. The adapter calls `ctx.publish_inbound(event)`, pushing the canonical event into the pipeline.
4. The pipeline's `_persist_inbound_native_ref` method reads `event.source_native_ref` and persists a `NativeMessageRef(direction="inbound")` mapping the Meshtastic packet ID to the canonical event ID.
5. Future inbound packets that reference this packet ID (e.g., implicit acknowledgments) can be correlated via `resolve_native_ref`.

### Outbound

1. The pipeline renders a canonical event into a `RenderingResult` via `MeshtasticRenderer`.
2. The pipeline calls `adapter.deliver(result)` on the Meshtastic adapter.
3. The adapter extracts the rendered text payload from `result.payload`, determines the target Meshtastic channel, and sends the message.
4. On success, the adapter returns `AdapterDeliveryResult(native_message_id=<packet_id>, native_channel_id=<channel_index>)`.
5. The pipeline reads the `AdapterDeliveryResult` and persists `NativeMessageRef(direction="outbound")` mapping the Meshtastic packet ID to the canonical event ID.
6. The adapter does not manage its own storage for native refs. It reports them through the standard `AdapterDeliveryResult` and the pipeline handles persistence.


## Queue and Pacing Ownership

The Meshtastic adapter owns its own outbound queue and pacing. This is different from presentation adapters like Matrix, where the pipeline controls delivery timing.

**Why the adapter owns pacing.** Meshtastic radios operate on shared ISM bands with strict duty cycle limits (typically 10% in the US, 1-10% in the EU depending on band). Transmit timing is a physical layer concern that the adapter must manage based on radio configuration, channel activity, and regulatory constraints. The pipeline has no visibility into these constraints and should not be responsible for them.

**How it works in tranche 1.** The adapter's `deliver()` method receives a `RenderingResult`, queues the payload internally, and returns. The returned `AdapterDeliveryResult` may carry `native_message_id=None` if the packet has not been sent yet (queued for later transmission). The pipeline accepts `None` as a valid outcome: no outbound native ref is persisted until the adapter reports a packet ID. In tranche 1, the fake client sends immediately and always returns a packet ID. Future tranches with real radio connections may implement deferred sending.

**Pipeline boundary.** The pipeline calls `deliver()` and records receipts. It does not retry on its own schedule for Meshtastic. The adapter's internal queue is opaque to the pipeline. The pipeline's retry policy and deadline apply to the `deliver()` call itself, not to the radio's transmit timing.


## Configuration (MeshtasticConfig)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `adapter_id` | `str` | Yes | Unique adapter instance ID |
| `channel_map` | `dict[int, str]` | No | Mapping from Meshtastic channel index (0-7) to canonical channel ID. Default maps channel 0 to `"default"`. |
| `default_channel` | `int` | No | Default Meshtastic channel index for outbound when no target channel is specified. Default: 0. |
| `hop_limit` | `int` | No | Hop limit for outbound packets. Default: 3. |
| `pacing_interval_ms` | `int` | No | Minimum milliseconds between outbound sends. Default: 0 (no pacing in fake client mode). |
| `node_id` | `str | None` | No | Local node identifier. Used for `source_transport_id`. Default: `None` (uses adapter_id). |

Configuration is a frozen dataclass with a `validate()` method that checks field constraints. Invalid configuration raises a config error before the adapter starts.


## Dependency

```
pip install medre[meshtastic]
```

This installs `mtjk>=0.1`. The core install (`pip install medre`) does not include it. All core tests pass without `mtjk` present. The adapter's own tests use `FakeMeshtasticClient` and do not require `mtjk`.

### Why `mtjk`

- **Distribution name:** `mtjk>=0.1` on PyPI.
- **Python import name:** `meshtastic` (matches upstream `meshtastic` package).
- **Rationale.** `mtjk` is the Meshtastic Python SDK, published under the distribution name `mtjk`. The import name `meshtastic` is what code uses at runtime. This split is analogous to how the Matrix adapter uses `mindroom-nio` (distribution) imported as `nio` (import name).
- **Optional.** The dependency is optional. The compat module (`medre.adapters.meshtastic.compat`) sets `HAS_MESHTASTIC = False` when `mtjk` is not installed. The adapter's `start()` method checks this flag and raises a descriptive error if the library is missing. No import of the `meshtastic` package occurs unless the compat check passes.

```python
# medre/adapters/meshtastic/compat.py
HAS_MESHTASTIC: bool
try:
    import meshtastic  # noqa: F401
    HAS_MESHTASTIC = True
except ImportError:
    HAS_MESHTASTIC = False
```


## Testing Approach

- **FakeMeshtasticClient.** No real hardware, no `mtjk` dependency, no network. Simulates inbound packet reception and outbound packet sending. Stores sent packets in memory for test assertions. Returns deterministic packet IDs.
- **Unit isolation.** `MeshtasticRenderer` and `MeshtasticCodec` are tested independently of the adapter.
- **Pipeline integration.** Tests combine `FakeMeshtasticClient` with `SQLiteStorage` to exercise the full decode/store/render/deliver path.
- **Boundary verification.** Tests assert that core imports don't leak into the adapter package, and that the adapter doesn't import routing, planning, or storage modules.
- **Optional dependency.** `mtjk` is guarded by `HAS_MESHTASTIC`. Core tests pass without it installed. Adapter tests use the fake client and do not require it.
- **No real hardware or network required.** No test in the default suite requires a physical Meshtastic radio, BLE connection, serial port, or TCP connection to a radio.


## Non-Goals (This Tranche)

These are explicitly out of scope for tranche 1:

- **Full telemetry decoding.** Battery, voltage, uptime, air utilization, and other device metrics are not decoded into canonical telemetry events. The packet classifier recognizes telemetry portnums and drops them.
- **Position data.** GPS coordinates and location information are not decoded.
- **Node database cache.** No local cache of known Meshtastic nodes, their IDs, or their metadata. Node discovery is deferred.
- **BLE, serial, or TCP production connection.** No real hardware connection code in tranche 1. Production connections (BLE, serial, TCP) are only considered behind the optional `mtjk` dependency and are not required by any test. The fake client is the only client used in tranche 1 tests.
- **End-to-end encryption (E2EE).** No encryption key management, no encrypted channel support, no key exchange.
- **MMRelay configuration compatibility.** No support for reading or converting MMRelay configuration files. The Meshtastic adapter is a standalone MEDRE adapter, not an MMRelay replacement.
- **Meshtastic plugin commands.** No `!command` handling, no remote administration, no Meshtastic plugin system integration.
- **Matrix changes.** No modifications to the Matrix adapter, renderer, or configuration. The Meshtastic adapter is a separate TRANSPORT adapter that interacts with the pipeline, not with the Matrix adapter directly.
- **Reactions, edits, deletes.** No native support for any relation types beyond flat text.
- **Store-and-forward.** No Meshtastic store-and-forward integration.
- **ACK tracking.** No explicit acknowledgment tracking for delivered packets beyond what the adapter reports through `AdapterDeliveryResult`.
- **Admin portnum.** No admin packet handling.
- **Remote hardware portnum.** No remote hardware control.


## Capability Declaration

The Meshtastic adapter declares the following capabilities in tranche 1:

```python
AdapterCapabilities(
    text=True,
    title=False,
    replies="unsupported",
    reactions="unsupported",
    edits="unsupported",
    deletes="unsupported",
    attachments=False,
    metadata_fields=False,
    delivery_receipts=False,
    store_and_forward=False,
    direct_messages=False,
    max_text_chars=200,  # Conservative Meshtastic text limit
)
```

This is an honest declaration. The adapter does what it says and nothing more. The pipeline uses these capabilities to drive fallback rendering: when a reply targets a Meshtastic adapter, the pipeline renders the reply context as inline text because native replies are unsupported.


---

*This contract describes the planned Meshtastic adapter tranche 1. It is a pre-implementation specification. If the implementation diverges from this document, the document should be updated to match the implementation's actual behavior.*

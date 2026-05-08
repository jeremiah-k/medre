# Meshtastic Adapter Tranche 1: Radio Transport Validation

> Contract version: 2
> Last updated: 2026-05-08


## Overview

This is a constrained radio transport adapter. The Meshtastic adapter declares `AdapterRole.TRANSPORT` and uses the `mtjk` library (distribution name `mtjk`, import name `meshtastic`) as an optional dependency. Tranche 1 validates that the MEDRE runtime's decode/render/deliver pipeline works against a radio transport with short text messages, channel-based routing, and protocol-native packet IDs.

The adapter does not route, does not plan, and does not render fallback text. It decodes inbound Meshtastic text packets into canonical events and delivers outbound rendered content. The pipeline owns receipts, relation resolution, and storage. Adapters transport messages and report native delivery metadata back to the pipeline. The Meshtastic-specific renderer lives inside the adapter package (`medre.adapters.meshtastic.renderer`), not in core. Core owns the generic rendering protocol and pipeline machinery. Core never imports from the Meshtastic adapter package.

Meshtastic capabilities in tranche 1 are limited to text message ingress and egress over named channels. Telemetry, node database caching, position data, E2EE, and production hardware connections are all deferred.


## Supported Features

- **Inbound text packet decoding.** Meshtastic `TEXT_MESSAGE_APP` packets are decoded into canonical events by `MeshtasticCodec`. The packet's text payload becomes `payload["body"]`. Packet metadata (packet_id, from_id, to_id, channel, portnum, is_direct_message) is stored in `metadata.native.data` as a flat dict. There is no separate `metadata.radio` or `metadata.transport` namespace in tranche 1.
- **Outbound text rendering.** `MeshtasticRenderer` turns canonical events into Meshtastic content payloads: a dict with keys `text` (the body string), `channel_index` (integer parsed from target_channel, default 0), and `meshnet_name` (empty string placeholder). The renderer lives at `medre.adapters.meshtastic.renderer`, owned by the adapter layer. Length-limit enforcement is noted but not applied in tranche 1.
- **Packet classification.** `MeshtasticPacketClassifier` is a standalone class that examines raw packet dicts and returns a classification dict with keys: `category` ("text", "telemetry", "nodeinfo", "position", "admin", "unknown", or "plugin_only"), `is_direct_message` (bool), `channel_index` (int or None), `packet_id` (int or None), `sender_id` (str or None), `portnum` (str or None), and `is_ack` (bool). Only "text" category packets that are not ACKs are processed in tranche 1. Other categories are dropped. The classifier also provides static `_is_broadcast()` for detecting broadcast destination addresses: empty string, `"^all"`, integer `0xffffffff`, and string `"4294967295"`.
- **Native refs via packet IDs.** Inbound: `MeshtasticCodec.decode()` sets `source_native_ref` with the packet's numeric ID as a string. The pipeline's `_persist_inbound_native_ref` persists this as a `NativeMessageRef(direction="inbound")`. Outbound: `FakeMeshtasticAdapter.deliver()` returns an `AdapterDeliveryResult` with `native_message_id` and `native_channel_id`. The real `MeshtasticAdapter.deliver()` is scaffolded and returns `None` in tranche 1, so no outbound native ref is persisted for the real adapter.
- **Reply relations.** When an inbound packet contains `decoded.replyId`, the codec creates an `EventRelation(relation_type="reply")` with `target_event_id=None` and a `target_native_ref` pointing at the reply's native packet ID. This is an unresolved relation: the pipeline must resolve it later. The adapter does not resolve relations itself.
- **Direct messages.** The codec computes `is_direct_message` from the packet's `toId` field (any non-broadcast address). This flag is stored in `metadata.native.data["is_direct_message"]`. The adapter declares `direct_messages=False` in its capabilities, meaning outbound DM delivery is unsupported. Inbound DM metadata is preserved for pipeline inspection only.
- **Queue/pacing scaffolding.** `MeshtasticOutboundQueue` provides `enqueue`, `dequeue`, and `process_one` methods. In tranche 1, `process_one` dequeues an item but performs no real send and returns `None`. The queue owns pacing (`delay_between_messages` property). The pipeline does not perform Meshtastic-specific sleeping.
- **Background tasks.** `MeshtasticAdapter._on_packet()` is synchronous. It schedules async publishing via `asyncio.create_task`, tracking each task in `_background_tasks`. All tracked tasks are cancelled and awaited in `stop()`.
- **Fake adapter for tests.** `FakeMeshtasticAdapter` is a full adapter (not a client-facing test utility) that mirrors the real adapter's lifecycle and inbound/outbound flow. It uses an internal `FakeMeshtasticClient` that generates sequential deterministic packet IDs starting from 1 and tracks all sent packets in `sent_packets`. The fake adapter's `deliver()` returns an `AdapterDeliveryResult` with the deterministic packet ID. `set_deliver_failure(True)` triggers a `MeshtasticSendError` on the next delivery for error testing. No real hardware, no `mtjk` dependency, no network required.
- **Optional dependency handling.** The `mtjk` package is guarded by `HAS_MESHTASTIC` in `medre.adapters.meshtastic.compat`. When `mtjk` is not installed, the flag is `False`. The adapter's `start()` raises `MeshtasticConnectionError` for non-fake connection types when the library is missing. Core tests pass without `mtjk`. Adapter tests use `FakeMeshtasticAdapter` and do not require `mtjk`.


## Architecture Boundaries

These boundaries are enforced by design, not by convention. Tests verify them.

- `MeshtasticAdapter` does not route. No `Router` import.
- `MeshtasticAdapter` does not plan delivery. No `FallbackResolver`, no `DeliveryPlan` construction.
- `MeshtasticAdapter` does not render fallback text. Rendering lives in `MeshtasticRenderer`.
- `MeshtasticRenderer` does not perform delivery. No Meshtastic client calls.
- `MeshtasticRenderer` is adapter-owned. It lives at `medre.adapters.meshtastic.renderer`. Core owns the generic rendering protocol (interface, pipeline dispatch), not this Meshtastic-specific implementation. Core never imports from the adapter package.
- `MeshtasticCodec` does not route, plan, or render. It is a pure decode layer. It does not resolve native refs or query storage.
- `MeshtasticPacketClassifier` is a separate class from the codec. It is a pure function with no side effects.
- The adapter owns outbound queueing. The queue owns pacing. The pipeline does not sleep for Meshtastic.
- Storage remains the authoritative source for event correlation. The pipeline owns receipts and persistence. Adapters transport and report native delivery metadata.
- No real hardware or network is required for default tests. `FakeMeshtasticAdapter` simulates the full cycle.


## Capability Declaration

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
    max_text_bytes=512,
    max_text_chars=512,
)
```

This is an honest declaration. The adapter does what it says and nothing more. `max_text_bytes=512` and `max_text_chars=512` advertise the transport's willingness to handle payloads up to that size. The renderer does not enforce truncation in tranche 1. `direct_messages=False` means outbound DM delivery is not supported, even though inbound DM metadata is preserved in `metadata.native.data`.


## Configuration (MeshtasticConfig)

`MeshtasticConfig` is a frozen dataclass with a `validate()` method that checks field constraints. Invalid configuration raises `MeshtasticConfigError` before the adapter starts.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `adapter_id` | `str` | Yes | Unique adapter instance ID. Must be non-empty. |
| `connection_type` | `Literal["fake", "tcp", "serial", "ble"]` | No | Connection mode. Defaults to `"fake"` for testing without hardware. |
| `host` | `str \| None` | No | Hostname or IP for TCP connections. Required when `connection_type="tcp"`. |
| `port` | `int \| None` | No | Port number for TCP connections. |
| `serial_port` | `str \| None` | No | Serial device path for serial connections. |
| `meshnet_name` | `str` | No | Human-readable meshnet name. Informational. Defaults to `""`. |
| `default_channel` | `int` | No | Default radio channel index for outbound messages. Defaults to `0`. Must be >= 0. |
| `channel_mapping` | `dict[int, str]` | No | Mapping of channel index to human-readable channel name. Defaults to empty dict. |
| `message_delay_seconds` | `float` | No | Minimum delay between outbound messages (pacing). Defaults to `0.5`. Must be >= 0. |
| `startup_backlog_suppress_seconds` | `float` | No | Seconds after start to suppress stale backlog packets. Defaults to `5.0`. |
| `sync_timeout_ms` | `int` | No | Timeout in milliseconds for sync operations. Defaults to `30000`. |


## Native Ref Flow

### Inbound

1. A Meshtastic `TEXT_MESSAGE_APP` packet arrives at the adapter with a numeric packet ID.
2. `MeshtasticCodec.decode()` converts the packet into a `CanonicalEvent` with `source_native_ref=NativeRef(adapter=<adapter_id>, native_channel_id=<channel_index_as_string>, native_message_id=<packet_id_as_string>)`.
3. The adapter calls `ctx.publish_inbound(event)`, pushing the canonical event into the pipeline.
4. The pipeline's `_persist_inbound_native_ref` reads `event.source_native_ref` and persists a `NativeMessageRef(direction="inbound")` mapping the Meshtastic packet ID to the canonical event ID.

### Outbound (Fake Adapter)

1. The pipeline renders a canonical event into a `RenderingResult` via `MeshtasticRenderer`.
2. The pipeline calls `adapter.deliver(result)` on the `FakeMeshtasticAdapter`.
3. The fake adapter sends through `FakeMeshtasticClient.send_text()`, getting a sequential deterministic `packet_id`.
4. The fake adapter returns `AdapterDeliveryResult(native_message_id=<packet_id_as_string>, native_channel_id=<channel_index_as_string>)`.
5. The pipeline reads the `AdapterDeliveryResult` and persists `NativeMessageRef(direction="outbound")`.

### Outbound (Real Adapter)

1. The pipeline renders and calls `MeshtasticAdapter.deliver(result)`.
2. The real adapter enqueues the payload via `MeshtasticOutboundQueue.enqueue()`.
3. `deliver()` returns `None` in tranche 1 (scaffolded). No outbound native ref is persisted.


## Relation and Reply Behavior

Meshtastic's text message protocol in tranche 1 carries an optional `replyId` field in the decoded payload. When present, the codec creates an `EventRelation` with `relation_type="reply"`, `target_event_id=None`, and a `target_native_ref` pointing at the referenced packet's native ID. This is an unresolved relation. The pipeline must resolve `target_native_ref` to a `target_event_id` later.

The adapter declares `replies="unsupported"` in its capabilities. The adapter does not participate in relation resolution beyond producing the `EventRelation` from the native packet data.

Outbound reply delivery (sending a reply that references a previous message) is not supported. Future tranches may add structured reply metadata in the text payload or handle a Meshtastic reply portnum if one is added.


## Telemetry Deferral

Meshtastic radios emit telemetry (battery, voltage, uptime, air utilization) and position data as distinct portnums. Tranche 1 does not decode or process them.

The packet classifier recognizes telemetry, position, and nodeinfo portnums and assigns the appropriate `category`. `MeshtasticAdapter._on_packet()` drops any packet where `category != "text"`. No canonical events are produced for these packet types.

No `TelemetryMetadata` or `RadioMetadata` is populated in tranche 1. When telemetry support is added in a future tranche, the codec will decode telemetry portnums into canonical telemetry events with structured metadata. No schema changes are required: the metadata namespaces already exist.


## Queue and Pacing Ownership

`MeshtasticOutboundQueue` owns the delay between outbound messages (`delay_between_messages` property, sourced from `MeshtasticConfig.message_delay_seconds`). The pipeline must not perform Meshtastic-specific sleeping. The queue is the sole owner of transmit pacing.

In tranche 1, the queue is scaffolding. `enqueue()` appends payloads to an internal deque. `dequeue()` returns the next item. `process_one()` dequeues one item but performs no real send and returns `None`. The real Meshtastic adapter's `deliver()` enqueues and returns `None`. The fake adapter bypasses the queue entirely and sends immediately through `FakeMeshtasticClient`.


## Dependency

```
pip install medre[meshtastic]
```

This installs `mtjk>=0.1`. The core install (`pip install medre`) does not include it. All core tests pass without `mtjk` present. The adapter's own tests use `FakeMeshtasticAdapter` and do not require `mtjk`.

- **Distribution name:** `mtjk>=0.1` on PyPI.
- **Python import name:** `meshtastic`.
- **Optional.** The compat module sets `HAS_MESHTASTIC = False` when `mtjk` is not installed. The adapter's `start()` raises `MeshtasticConnectionError` for non-fake connection types when the library is missing.

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

- **FakeMeshtasticAdapter.** No real hardware, no `mtjk` dependency, no network. Uses `FakeMeshtasticClient` internally for deterministic sequential packet IDs and `sent_packets` tracking. `set_deliver_failure()` triggers errors for pipeline error handling tests.
- **Unit isolation.** `MeshtasticRenderer`, `MeshtasticCodec`, and `MeshtasticPacketClassifier` are tested independently of the adapter.
- **Pipeline integration.** Tests combine `FakeMeshtasticAdapter` with `SQLiteStorage` to exercise the full decode/store/render/deliver path.
- **Boundary verification.** Tests assert that core imports don't leak into the adapter package, and that the adapter doesn't import routing, planning, or storage modules.
- **Optional dependency.** `mtjk` is guarded by `HAS_MESHTASTIC`. Core tests pass without it installed. Adapter tests use the fake adapter and do not require it.
- **No real hardware or network required.** No test in the default suite requires a physical Meshtastic radio, BLE connection, serial port, or TCP connection to a radio.


## Non-Goals (This Tranche)

These are explicitly out of scope for tranche 1:

- **Full telemetry decoding.** Battery, voltage, uptime, air utilization, and other device metrics are not decoded into canonical telemetry events. The packet classifier recognizes telemetry portnums and the adapter drops them.
- **Position data.** GPS coordinates and location information are not decoded.
- **Node database cache.** No local cache of known Meshtastic nodes, their IDs, or their metadata. Node discovery is deferred.
- **BLE, serial, or TCP production connection.** No real hardware connection code in tranche 1. Production connections are only considered behind the optional `mtjk` dependency and are not required by any test. The fake adapter is the only adapter used in tranche 1 tests.
- **End-to-end encryption (E2EE).** No encryption key management, no encrypted channel support, no key exchange.
- **MMRelay configuration compatibility.** No support for reading or converting MMRelay configuration files. The Meshtastic adapter is a standalone MEDRE adapter, not an MMRelay replacement.
- **Meshtastic plugin commands.** No `!command` handling, no remote administration, no Meshtastic plugin system integration.
- **Matrix changes.** No modifications to the Matrix adapter, renderer, or configuration. The Meshtastic adapter is a separate TRANSPORT adapter that interacts with the pipeline, not with the Matrix adapter directly.
- **Outbound DM delivery.** `direct_messages=False` is declared in capabilities. Inbound DM metadata is preserved in `metadata.native.data`, but sending to a specific node ID is not supported.
- **Reactions, edits, deletes.** No native support for any relation types beyond the unresolved reply relation from `replyId`.
- **Store-and-forward.** No Meshtastic store-and-forward integration.
- **ACK tracking.** No explicit acknowledgment tracking. ACK packets are classified and dropped.
- **Admin portnum.** No admin packet handling.
- **Remote hardware portnum.** No remote hardware control.
- **Renderer truncation enforcement.** The renderer notes Meshtastic payload size limits but does not truncate in tranche 1.


---

*This contract describes the implemented Meshtastic adapter tranche 1. If the implementation diverges from this document, the document should be updated to match the implementation's actual behavior.*

# Meshtastic Adapter Tranche 1: Radio Transport Validation

> **Status:** Active
> **Classification:** Normative
> **Authority:** Current contract for Meshtastic adapter features, classifier, queue, and config
> **Last reviewed:** 2026-05-26
>
> Contract version: 5
> Last updated: 2026-05-26

## Overview

This is a constrained radio transport adapter. The Meshtastic adapter declares `AdapterRole.TRANSPORT` and uses the `mtjk` library (distribution name `mtjk`, import name `meshtastic`) as an optional dependency. Tranche 1 validates that the MEDRE runtime's decode/render/deliver pipeline works against a radio transport with short text messages, channel-based routing, and protocol-native packet IDs.

The adapter does not route, does not plan, and does not render fallback text. It decodes inbound Meshtastic text packets into canonical events and delivers outbound rendered content. The pipeline owns receipts, relation resolution, and storage. Adapters transport messages and report native delivery metadata back to the pipeline. The Meshtastic-specific renderer lives inside the adapter package (`medre.adapters.meshtastic.renderer`), not in core. Core owns the generic rendering protocol and pipeline machinery. Core never imports from the Meshtastic adapter package.

Meshtastic capabilities in tranche 1 are limited to text message ingress and egress over named channels. Telemetry, node database caching, position data, E2EE, and production hardware connections are all deferred.

## Supported Features

- **Inbound text packet decoding.** Meshtastic `TEXT_MESSAGE_APP` packets are decoded into canonical events by `MeshtasticCodec`. The packet's text payload becomes `payload["body"]`. Packet metadata (packet_id, from_id, to_id, channel, portnum, is_direct_message) is stored in `metadata.native.data` as a flat dict. There is no separate `metadata.radio` or `metadata.transport` namespace in tranche 1.
- **Outbound text rendering.** `MeshtasticRenderer` turns canonical events into Meshtastic content payloads: a dict with keys `text` (the body string), `channel_index` (integer parsed from target_channel, default 0), and `meshnet_name` (empty string placeholder). The renderer lives at `medre.adapters.meshtastic.renderer`, owned by the adapter layer. Length-limit enforcement is noted but not applied in tranche 1.
- **Packet classification.** `MeshtasticPacketClassifier` is a standalone class that examines raw packet dicts and returns a `ClassificationResult` dataclass with fields: `action` (`"relay"`, `"ignore"`, `"drop"`, or `"deferred"`), `category` (`"text"`, `"ack"`, `"telemetry"`, `"nodeinfo"`, `"position"`, `"admin"`, `"unknown"`, or `"plugin_only"`), `reason` (human-readable string), `is_direct_message` (bool), `channel_index` (int or None), `packet_id` (int or None), `sender_id` (str or None), `portnum` (normalized str or None), and `is_ack` (bool). Real symbolic meshtastic-python / mtjk portnums are explicitly normalized for recognized categories: `TEXT_MESSAGE_APP` → `text_message`, `TELEMETRY_APP` → `telemetry`, `POSITION_APP` → `position`, `NODEINFO_APP` → `nodeinfo`, `ADMIN_APP` → `admin`, and `ROUTING_APP` → `routing`. Already-normalized fixture values such as `text_message` continue to work. Numeric portnum handling is SDK-derived when the `mtjk` package is installed (via `compat.get_portnum_table()`), with a protocol-correct fallback map for environments without the SDK. Only `action == "relay"` packets produce canonical events. ACKs and admin packets receive the `ignore` action. Malformed and encrypted packets receive the `drop` action. Detection sensor, unknown/custom portnum, and plugin_only packets receive the `deferred` action. The classifier also provides static `_is_broadcast()` for detecting broadcast destination addresses: empty string, `"^all"`, integer `0xffffffff`, and string `"4294967295"`. Sender identity resolves from `fromId` (string) with fallback to `from` (numeric NodeNum). Broadcast detection checks both `toId` (string) and `to` (numeric) fields.
- **Native refs via packet IDs.** Inbound: `MeshtasticCodec.decode()` sets `source_native_ref` with the packet's numeric ID as a string. The pipeline's `_persist_inbound_native_ref` persists this as a `NativeMessageRef(direction="inbound")`. Outbound: `FakeMeshtasticAdapter.deliver()` returns an `AdapterDeliveryResult` with `native_message_id` and `native_channel_id`. The real `MeshtasticAdapter.deliver()` is scaffolded and returns `None` in tranche 1, so no outbound native ref is persisted for the real adapter.
- **Reply relations.** When an inbound packet contains `decoded.replyId`, the codec creates an `EventRelation(relation_type="reply")` with `target_event_id=None` and a `target_native_ref` pointing at the reply's native packet ID. This is an unresolved relation: the pipeline must resolve it later. The adapter does not resolve relations itself.
- **Direct messages.** The codec uses classifier-derived `is_direct_message` so metadata is consistent for both `toId` string and `to` numeric destination variants. This flag is stored in `metadata.native.data["is_direct_message"]`. The adapter declares `direct_messages=False` in its capabilities, meaning outbound DM delivery is unsupported. Inbound DM metadata is preserved for pipeline inspection only.
- **Queue/pacing scaffolding.** `MeshtasticOutboundQueue` provides `enqueue`, `dequeue`, and `process_one` methods. In tranche 1, `process_one` dequeues an item but performs no real send and returns `None`. The queue owns pacing (`delay_between_messages` property). The pipeline does not perform Meshtastic-specific sleeping.
- **Background tasks.** `MeshtasticAdapter._on_packet()` is synchronous. It schedules async publishing via `asyncio.create_task`, tracking each task in `_background_tasks`. All tracked tasks are cancelled and awaited in `stop()`.
- **Fake adapter for tests.** `FakeMeshtasticAdapter` is a full adapter (not a client-facing test utility) that mirrors the real adapter's lifecycle and inbound/outbound flow. It uses an internal `FakeMeshtasticClient` that generates sequential deterministic packet IDs starting from 1 and tracks all sent packets in `sent_packets`. The fake adapter's `deliver()` returns an `AdapterDeliveryResult` with the deterministic packet ID. `set_deliver_failure(True)` triggers an `AdapterSendError` (transient) on the next delivery for error testing. No real hardware, no `mtjk` dependency, no network required.
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
    max_text_bytes=227,
    max_text_chars=None,
)
```

This is an honest declaration. The adapter does what it says and nothing more. `max_text_bytes=227` (configurable via `MeshtasticConfig.max_text_bytes`, default 227) advertises the transport's byte budget for final rendered radio text. The renderer enforces UTF-8 byte-budget truncation after all prefix, reply, and reaction formatting is applied. `max_text_chars` is `None` as MEDRE does not enforce a character limit separately from the byte budget. `direct_messages=False` means outbound DM delivery is not supported, even though inbound DM metadata is preserved in `metadata.native.data`.

## Configuration (MeshtasticConfig)

`MeshtasticConfig` is a frozen dataclass with a `validate()` method that checks field constraints. Invalid configuration raises `MeshtasticConfigError` before the adapter starts.

| Field                              | Type                                      | Required | Description                                                                        |
| ---------------------------------- | ----------------------------------------- | -------- | ---------------------------------------------------------------------------------- |
| `adapter_id`                       | `str`                                     | Yes      | Unique adapter instance ID. Must be non-empty.                                     |
| `connection_type`                  | `Literal["fake", "tcp", "serial", "ble"]` | No       | Connection mode. Defaults to `"fake"` for testing without hardware.                |
| `host`                             | `str \| None`                             | No       | Hostname or IP for TCP connections. Required when `connection_type="tcp"`.         |
| `port`                             | `int \| None`                             | No       | Port number for TCP connections.                                                   |
| `serial_port`                      | `str \| None`                             | No       | Serial device path for serial connections.                                         |
| `meshnet_name`                     | `str`                                     | No       | Human-readable meshnet name. Informational. Defaults to `""`.                      |
| `default_channel`                  | `int`                                     | No       | Default radio channel index for outbound messages. Defaults to `0`. Must be >= 0.  |
| `channel_mapping`                  | `dict[int, str]`                          | No       | Mapping of channel index to human-readable channel name. Defaults to empty dict.   |
| `message_delay_seconds`            | `float`                                   | No       | Minimum delay between outbound messages (pacing). Defaults to `0.5`. Must be >= 0. |
| `startup_backlog_suppress_seconds` | `float`                                   | No       | Seconds after start to suppress stale backlog packets. Defaults to `5.0`.          |
| `sync_timeout_ms`                  | `int`                                     | No       | Timeout in milliseconds for sync operations. Defaults to `30000`.                  |
| `max_text_bytes`                   | `int`                                     | No       | Maximum UTF-8 byte budget for final radio text. Defaults to `227`. Must be >= 0.   |

## Reply Relation Flow

When an inbound packet carries `decoded.replyId`, the codec creates an `EventRelation(relation_type="reply")` with `target_event_id=None` and a `target_native_ref` pointing at the referenced native packet ID. This unresolved relation is resolved by the pipeline's `RelationResolver` during event processing (Stage 2).

Three cases are tested:

1. **Unresolved** — target native ref not yet in storage. The relation is preserved with `target_event_id=None`.
2. **Resolved** — target native ref already exists (from a prior inbound packet). The relation is updated with the correct `target_event_id`.
3. **Missing** — reply references a packet ID that never arrived. The relation remains unresolved (no crash, no data loss).

Relation resolution is pipeline-owned. The adapter and codec do not query storage or resolve references.

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

The packet classifier recognizes telemetry, position, and nodeinfo portnums and assigns the appropriate `category`. `MeshtasticAdapter._on_packet()` drops any packet where `action != "relay"`. No canonical events are produced for these packet types.

No `TelemetryMetadata` or `RadioMetadata` is populated in tranche 1. When telemetry support is added in a planned update, the codec will decode telemetry portnums into canonical telemetry events with structured metadata. No schema changes are required: the metadata namespaces already exist.

## Queue and Pacing Ownership

`MeshtasticOutboundQueue` owns the delay between outbound messages (`delay_between_messages` property, sourced from `MeshtasticConfig.message_delay_seconds`). The pipeline must not perform Meshtastic-specific sleeping. The queue is the sole owner of transmit pacing.

In tranche 1, the queue is scaffolding. `enqueue()` appends payloads to an internal deque. `dequeue()` returns the next item. `process_one()` dequeues one item but performs no real send and returns `None`. The real Meshtastic adapter's `deliver()` enqueues and returns `None`. The fake adapter bypasses the queue entirely and sends immediately through `FakeMeshtasticClient`.

## Dependency

```bash
pip install medre[meshtastic]
```

This installs `mtjk>=0.1`. The core install (`pip install medre`) does not include it. All core tests pass without `mtjk` present. The adapter's own tests use `FakeMeshtasticAdapter` and do not require `mtjk`.

- **Distribution name:** `mtjk` on PyPI. Versions 2.7.8.post2+ verified.
- **Python import name:** `meshtastic`.
- **Package source:** Fork maintained at `github.com/jeremiah-k/mtjk`. Not the upstream `meshtastic` library.
- **Optional.** The compat module sets `HAS_MESHTASTIC = False` when `mtjk` is not installed. The adapter's `start()` raises `MeshtasticConnectionError` for non-fake connection types when the library is missing.
- **Protobuf PortNum enum:** Available at `meshtastic.protobuf.portnums_pb2.PortNum` when the dependency is installed. The compat module provides `get_portnum_table()` for optional authoritative portnum resolution.
- **Callback shape:** The mtjk library normalizes protobuf MeshPackets to dicts via `MessageToDict()` and enriches them with `fromId`/`toId` via `_node_num_to_id()`. Inbound packets arrive via `pubsub` topic `"meshtastic.receive"`. The callback receives `(packet_dict, interface)`.

```python
# medre/adapters/meshtastic/compat.py
HAS_MESHTASTIC: bool
_PORTNUM_ENUM: type | None = None

try:
    import meshtastic  # noqa: F401
    from meshtastic.protobuf import portnums_pb2  # noqa: F401
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

### Tranche 1.5: Fixture Hardening (This Bundle)

Tranche 1.5 is a fixture hardening and realism audit pass, not a feature expansion. It makes the existing MEDRE contract tests more faithful to real Meshtastic packet behaviour without adding production connection support.

Changes in this pass:

- Centralised fixture corpus at `tests/fixtures/meshtastic_packets.py` with named factories for all packet shapes
- Fixture corpus covers both MEDRE-normalized portnums and real symbolic values such as `TEXT_MESSAGE_APP`, `TELEMETRY_APP`, `POSITION_APP`, `NODEINFO_APP`, `ADMIN_APP`, and `ROUTING_APP`
- Explicit `normalize_portnum()` helper normalizes real symbolic portnums without claiming full protobuf enum coverage
- Classifier now handles both `from` (numeric NodeNum) and `fromId` (string) sender fields
- Classifier now handles both `to` (numeric) and `toId` (string) broadcast/DM fields
- Codec uses classifier-derived truth for category, normalized portnum, packet ID, channel, sender, ACK detection, and direct-message metadata
- Codec rejects unsupported categories and ACKs deterministically instead of silently converting them to `message.created`
- Numeric portnum resolution is SDK-derived when `mtjk` is installed (via `compat.get_portnum_table()`), with a fallback map for environments without the SDK dependency (see `docs/contracts/10-meshtastic-source-audit.md` for the authoritative protobuf PortNum table and fallback mismatch details)
- Native ref reply relation tests confirm pipeline-owned resolution
- `known_adapters` mechanism documented with a TODO for future registry improvement
- Optional dependency (`mtjk`) verified against the installed package (v2.7.8.post2, import name `meshtastic`)

### Tranche 2: Source-of-Truth Audit (Current Bundle)

Tranche 2 is a source-of-truth audit and connection boundary preparation pass. It does not add production connection support, telemetry decoding, or any new real adapter functionality.

Changes in this pass:

- Source audit document at `docs/contracts/10-meshtastic-source-audit.md` covering MMRelay behavioral facts, mtjk callback shapes, PortNum enum verification, and send-result analysis
- Connection boundary design note at `docs/contracts/11-meshtastic-connection-boundary.md` for future real connection implementation
- Optional dependency verified: `mtjk` v2.7.8.post2 installed, imports as `meshtastic`, protobuf PortNum enum accessible at `meshtastic.protobuf.portnums_pb2.PortNum`
- `compat.get_portnum_table()` helper added for optional authoritative PortNum resolution when the dependency is installed
- `_NUMERIC_PORTNUM_FALLBACK` documented as protocol-correct fallback; SDK-derived table preferred when available; cross-reference to the audit doc added
- Fixture corpus refined with MMRelay-derived packet shapes (emoji flag, encrypted packets, DM shapes)
- Send-result behavior documented: both mtjk `sendText` and MMRelay `_sendPacket` return `MeshPacket` protobuf with poplulated `id` field
- No MMRelay code copied, no real hardware support added, no new adapter protocols implemented

### Tranche 2.1: Fixture Provenance and Live Harness (This Bundle)

Tranche 2.1 adds fixture provenance labeling, an optional live test harness,
and comprehensive documentation. It does not add production connection
support or any new adapter functionality.

Changes in this pass:

- Fixture provenance labels added to every factory in
  `tests/fixtures/meshtastic_packets.py`: mtjk-derived, MMRelay-derived,
  synthetic scaffold, unverified
- New fixtures: `make_stale_backlog_packet` (startup backlog scenario),
  `make_channel_message_packet` (non-default channel index)
- Optional live smoke test harness at `tests/test_meshtastic_live.py`,
  gated by `MESHTASTIC_CONNECTION_TYPE` env var and `pytest.mark.live`,
  skipped by default
- Live smoke runbook at `docs/runbooks/meshtastic-live-smoke.md`
- Source audit (contract 10) updated with master-branch verified API
  signatures, sendText/sendData parameter tables, fixture provenance
  matrix, and uncertainty documentation
- Contract 09 updated with Tranche 2.1 section
- Contract 16 updated with Meshtastic live smoke harness status
- Contract 18 updated with Meshtastic runbook and live test coverage
- Pytest `live` marker description updated to cover all services
- No MMRelay compatibility mode implemented, no hardware/network required
  in default tests, no production connectivity added

### Tranche 2.2: Classifier Hardening and Diagnostics (This Bundle)

Tranche 2.2 is a classifier field-extraction and diagnostics wiring pass on
branch `t2-meshtastic-reference-alignment`. It does not add new adapter
capabilities, new classification actions, or any change to classification
policy. **No classification POLICY changed — only data extraction fidelity
improved.**

Changes in this pass:

- Classifier now extracts `encrypted` field from packet dicts and assigns
  the `drop` action with reason `"encrypted packet"` (hardened with decoded-level fallback for edge-case SDK versions)
- Classifier now extracts `hopStart` and `hopLimit` from packet dicts for
  diagnostic use (previously not extracted)
- Classifier now extracts `rxTime` from packet dicts via the
  `extract_meshtastic_rx_time` helper for startup backlog suppression and
  diagnostic tracking (previously extracted only in backlog suppression path)
- Classifier now extracts `rxSnr` and `rxRssi` from packet dicts for
  diagnostic/radio-quality tracking (previously not extracted)
- Classifier now extracts `priority` from packet dicts for diagnostic use
  (previously not extracted)
- Queue diagnostics wired into adapter diagnostics via existing `queue_health`
  property — no new diagnostic fields added, just confirmed wiring
- Renderer UTF-8 byte-budget truncation verified as MMRelay-conceptual pattern
  (matches MMRelay's payload truncation concept but is an independent MEDRE
  implementation)
- Source audit document (`10-meshtastic-source-audit.md`) updated with Tranche 2
  resolution notes in section 2.3

**What did NOT change:**

- The 4-action classification model (relay/ignore/drop/deferred) is unchanged
- The classifier's decision tree and action assignments are unchanged
- Canonical event structure is unchanged
- Queue behavior, pacing, and retry semantics are unchanged
- Startup backlog suppression semantics are unchanged
- Adapter capabilities declaration is unchanged
- No new live validation evidence was produced
- No real hardware was used; all changes verified through fake-tested unit tests

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
- **Renderer truncation enforcement.** UTF-8 byte-budget truncation is now applied after final radio text rendering (see `feat/meshtastic-byte-budget-rendering` tranche).

---

_This contract describes the implemented Meshtastic adapter tranche 1. If the implementation diverges from this document, the document should be updated to match the implementation's actual behavior._

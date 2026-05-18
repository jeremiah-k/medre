# Metadata Embedding Contract

> Source: [Modular Event Engine Spec](../spec/modular-event-engine-spec.md) Sections 14, 15, 16, 17
> Contract version: 1
> Last updated: 2026-05-08

This contract defines how adapters embed runtime metadata into external platforms (Matrix, LXMF) and how normalized metadata namespaces are structured. An implementer building a Matrix adapter or LXMF adapter should be able to determine exactly what metadata to emit, where to put it, and what to leave out.

---

## 1. Normalized Metadata Namespaces

All event metadata lives in a structured `EventMetadata` object with six namespaces. Adapters normalize their native fields into these namespaces. Fields that don't map cleanly go into `native` until the enrichment stage categorizes them.

```python
import msgspec


class EventMetadata(msgspec.Struct, frozen=True):
    transport: TransportMetadata | None = None     # How the event arrived
    routing: RoutingMetadata | None = None         # Routing decisions applied
    radio: RadioMetadata | None = None             # Radio-specific data
    telemetry: TelemetryMetadata | None = None     # Device telemetry at time of event
    native: NativeMetadata | None = None           # Transport-native fields not yet normalized
    custom: dict[str, object] = {}                 # Plugin/extension metadata (frozen)
```

> **Note**: All `dict` fields (`custom`, `TelemetryMetadata.metrics`, `NativeMetadata.data`, `EventRelation.metadata`) are wrapped in `_FrozenDict` at construction, providing deep immutability while remaining `dict`-compatible for msgspec serialization.

### 1.1 Namespace Definitions

| Namespace            | Purpose                    | Example Fields                                                                                                                                                          |
| -------------------- | -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `metadata.transport` | Transport layer details    | `protocol` (`"meshcore-tcp"`, `"lxmf"`, `"mqtt"`), `gateway_id`, `delivery_method`, `delivery_confirmed`, `transport_encrypted`, `signature_valid`, `propagation_state` |
| `metadata.routing`   | Routing context            | `matched_routes` (tuple of strings), `fanout_group`                                                                                                                     |
| `metadata.radio`     | Radio-specific data        | `frequency`, `snr`, `rssi`, `channel_index`                                                                                                                             |
| `metadata.telemetry` | Device state at event time | `metrics` dict: `battery`, `voltage`, etc. (frozen)                                                                                                                     |
| `metadata.native`    | Unnormalized native fields | `data` dict: adapter-specific raw fields (frozen)                                                                                                                       |
| `metadata.custom`    | Plugin/extension data      | Key-value pairs from plugins, using reverse-DNS namespacing (frozen)                                                                                                    |

### 1.2 Migration from Flat Metadata

Legacy flat namespaces are mapped as follows:

| Old Path                          | New Path                       |
| --------------------------------- | ------------------------------ |
| `metadata.meshtastic.snr`         | `metadata.radio.snr`           |
| `metadata.meshtastic.channel`     | `metadata.radio.channel_index` |
| `metadata.meshtastic.from`        | `metadata.transport.source_id` |
| `metadata.meshtastic.telemetry.*` | `metadata.telemetry.metrics.*` |

The enrichment stage normalizes `metadata.native` fields into their proper namespaces when possible.

---

## 2. Matrix Metadata Embedding

### 2.1 Namespace Convention

Metadata embedded in Matrix events uses a reverse-DNS namespace under `org.<project>.*`. Until the project is named, the placeholder `org.medre` is used. The top-level key is `org.medre.event`.

The embedded object contains these fields:

| Field                 | Type            | Description                                                    |
| --------------------- | --------------- | -------------------------------------------------------------- |
| `event_id`            | string (UUIDv7) | Canonical event ID                                             |
| `event_kind`          | string          | Event kind (e.g. `"message.text"`, `"telemetry"`)              |
| `source_adapter`      | string          | Adapter instance that created the event                        |
| `source_transport_id` | string          | Native actor/source that produced the event                    |
| `metadata`            | object          | Structured metadata following the namespace model in Section 1 |

### 2.2 Matrix Event Content JSON (MeshCore Source)

```json
{
  "msgtype": "m.text",
  "body": "Hello from node 1234",
  "org.medre.event": {
    "event_id": "0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",
    "event_kind": "message.text",
    "source_adapter": "meshcore-radio-1",
    "source_transport_id": "1234",
    "metadata": {
      "native": {},
      "transport": { "protocol": "meshcore-tcp", "gateway_id": "radio-1" },
      "routing": { "matched_routes": ["mesh-to-matrix-general"] },
      "radio": { "snr": 5.2, "rssi": -78, "channel_index": 1 },
      "telemetry": {}
    }
  }
}
```

### 2.3 Matrix Event Content JSON (LXMF Source)

```json
{
  "msgtype": "m.text",
  "body": "Hello from LXMF peer",
  "org.medre.event": {
    "event_id": "0190b2c3-d4e5-7f6a-8b9c-0d1e2f3a4b5c",
    "event_kind": "message.text",
    "source_adapter": "lxmf-node-a",
    "source_transport_id": "a1b2c3d4e5f6a7b8",
    "metadata": {
      "native": {
        "lxmf": {
          "source_hash": "a1b2c3d4e5f6a7b8",
          "destination_hash": "e5f6a7b8c9d0e1f2"
        }
      },
      "transport": {
        "protocol": "lxmf",
        "substrate": "reticulum",
        "gateway_id": "lxmf-node-a",
        "delivery_method": "propagated",
        "delivery_confirmed": true,
        "propagation_state": "delivered"
      },
      "routing": { "matched_routes": ["lxmf-to-matrix-general"] },
      "radio": { "rssi": -90, "snr": 3.1 },
      "telemetry": {}
    }
  }
}
```

The metadata structure follows the same `native/transport/routing/radio/telemetry` namespaces regardless of source adapter. Adapter-specific fields with no canonical mapping go in `native`.

### 2.4 Metadata Sub-Namespace Contents in Matrix Embedding

Within the `metadata` object embedded in Matrix:

- **`native`**: Adapter-specific fields not yet normalized. For LXMF, contains `lxmf.source_hash`, `lxmf.destination_hash`, `lxmf.message_id`, `lxmf.title`, `lxmf.field_keys`.
- **`transport`**: `protocol`, `gateway_id`, and adapter-specific transport fields (`substrate`, `delivery_method`, `delivery_confirmed`, `transport_encrypted`, `signature_valid`, `stamp_valid`, `propagation_state` for LXMF).
- **`routing`**: `matched_routes`, `fanout_group`, `bridge_id`.
- **`radio`**: `snr`, `rssi`, `frequency`, `modulation`, `hop_limit`, `channel_index`. Values present only when available.
- **`telemetry`**: `battery_percent`, `voltage_mv`, `uptime_seconds`, `air_util_tx`. Values present only when available.

All sub-namespaces may be empty objects (`{}`) when no data is available for that category.

### 2.5 Storage is Authoritative

The canonical event log is the single source of truth. Embedded Matrix metadata is secondary and may be:

- Lost due to Synapse redaction (redaction destroys message content).
- Unavailable if the Matrix homeserver is down.
- Incomplete if the Matrix adapter was offline when the event was processed.

Features that need reliable metadata (replay, correlation, identity resolution) must read from storage, not from Matrix.

---

## 3. Privacy Modes

Privacy modes control what metadata gets embedded in Matrix events. This is a per-adapter configuration setting.

### 3.1 Mode Definitions

| Mode      | Behavior                                                                                                                                | Use Case                                                                        |
| --------- | --------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `off`     | Do not embed any runtime metadata. Matrix is purely a display surface. All correlation goes through storage.                            | Maximum privacy, Matrix is untrusted or shared.                                 |
| `minimal` | Embed only `event_id` and `source_transport_id`. Users see limited context. Less data exposed on redaction.                             | Low-trust environments, minimal footprint.                                      |
| `safe`    | Embed normalized metadata (event kind, source adapter, transport protocol, radio metrics, telemetry) but never secrets or raw payloads. | **Default mode.** Operators who want visible context without security exposure. |
| `full`    | Embed all metadata in Matrix events. Maximum context for users, but all metadata is lost on redaction.                                  | Trusted environments, debugging, development.                                   |

**Default: `safe`**

### 3.2 Never-Embed List

Regardless of the configured privacy mode, the following are **never** embedded in Matrix events:

- Channel keys, private keys, or access tokens
- Raw encrypted blobs or raw packets
- Raw native protocol data (Meshtastic protobuf, Reticulum packets)
- Identity private keys or signing keys
- Full raw native archive data

### 3.3 Redaction Behavior

Synapse redacts the `content` body of an event when redacted. The `org.medre.event` field is part of `content` and will be destroyed. The canonical event in storage is unaffected.

---

## 4. MeshCore Adapter State Machine

The MeshCore adapter tracks its connection lifecycle through defined states. Each state change emits a `system.lifecycle` event.

### 4.1 State Diagram

```
DISCONNECTED --> CONNECTING --> AUTHENTICATING --> SYNCING --> READY
     ^              |              |                 |          |
     |              v              v                 v          v
     +--------------+--------------+----------+------+----> DEGRADED
     |                                           |             |
     +<------------------------------------------+             v
     |                                                       STOPPING
     +<----------------------------------------------------------+
```

### 4.2 State Definitions

| State              | Description                                                                                                                  | Transitions From                                  |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| **DISCONNECTED**   | No active connection. Adapter is idle.                                                                                       | Initial, STOPPING, DEGRADED (reconnect abandoned) |
| **CONNECTING**     | Establishing TCP/serial/Bluetooth connection to the radio.                                                                   | DISCONNECTED                                      |
| **AUTHENTICATING** | Connection established, performing MeshCore authentication handshake.                                                        | CONNECTING                                        |
| **SYNCING**        | Authenticated, syncing node database, channel config, and initial state.                                                     | AUTHENTICATING                                    |
| **READY**          | Fully operational. Sending and receiving events.                                                                             | SYNCING                                           |
| **DEGRADED**       | Partially functional. Connection unstable, high latency, or missing features. Receiving events but delivery may be impaired. | READY, SYNCING                                    |
| **DRAINING**       | Graceful shutdown in progress. Completing in-flight operations.                                                              | READY, DEGRADED                                   |
| **STOPPING**       | Force stop. Aborting operations.                                                                                             | Any state                                         |

### 4.3 State Transition Events

Each state change emits a `system.lifecycle` event:

```python
{
    "event_kind": "system.lifecycle",
    "payload": {
        "component": "adapter",
        "adapter": "meshcore-radio-1",
        "old_state": "CONNECTING",
        "new_state": "AUTHENTICATING",
        "reason": "TCP connection established"
    }
}
```

### 4.4 Delivery Behavior per State

| State                       | Ingress                   | Delivery                            |
| --------------------------- | ------------------------- | ----------------------------------- |
| READY                       | Accept                    | Queue and deliver                   |
| DEGRADED                    | Accept                    | Queue, delay delivery, may fallback |
| SYNCING                     | Buffer                    | Buffer                              |
| CONNECTING / AUTHENTICATING | Buffer                    | Buffer                              |
| DISCONNECTED                | Reject (emit error event) | Queue for later, apply deadline     |
| DRAINING                    | Reject                    | Complete in-flight only             |
| STOPPING                    | Reject                    | Abort                               |

---

## 5. LXMF Delivery Metadata

When events arrive from LXMF, the adapter normalizes delivery metadata into the canonical event's structured namespaces. Core consumers should prefer the normalized `metadata.transport` fields. The `metadata.native.lxmf` namespace is reserved for LXMF-specific debugging and correlation only.

### 5.1 Normalized Transport Metadata

```python
metadata.transport = {
    "protocol": "lxmf",
    "substrate": "reticulum",
    "delivery_method": "propagated",    # direct | propagated | opportunistic | paper
    "delivery_confirmed": None,         # True | False | None
    "transport_encrypted": True,        # True | False | None
    "signature_valid": None,            # True | False | None
    "stamp_valid": None,                # True | False | None
    "propagation_state": "queued",      # queued | sent | delivered | failed
    "link_quality": {                   # When available from underlying transport
        "rssi": -90,
        "snr": 3.1,
        "q": 0.85
    }
}
```

### 5.2 Nullable Security and Delivery Fields

`transport_encrypted`, `signature_valid`, `stamp_valid`, and `delivery_confirmed` are tri-state: `True`, `False`, or `None`.

| Value   | Meaning                                                                                             |
| ------- | --------------------------------------------------------------------------------------------------- |
| `True`  | Confirmed positive.                                                                                 |
| `False` | Confirmed negative.                                                                                 |
| `None`  | Status could not be determined. Consumers must treat `None` as "unknown", not as `False` or `True`. |

Examples:

- An opportunistic LXMF message with no signature: `signature_valid=None`.
- A propagated message awaiting delivery confirmation: `delivery_confirmed=None`.
- An unsigned message: `stamp_valid=None` (no stamp present or not checked).

Link quality values (`rssi`, `snr`, `q`) are carried when the underlying Reticulum transport provides them. Not guaranteed on every message.

### 5.3 Native LXMF Metadata

```python
metadata.native = {
    "lxmf": {
        "message_id": "abc123...",       # LXMessage.message_id (SHA-256 derived, not transmitted)
        "title": "...",                  # LXMessage.title (bytes decoded)
        "source_hash": "a1b2c3d4...",   # 16-byte hex
        "destination_hash": "e5f6a7b8...",
        "field_keys": ["org.medre.event"]  # Top-level keys in LXMessage.fields
    }
}
```

### 5.4 LXMF Fields Dict Mapping

Framework metadata is embedded in LXMF messages using a namespaced field in the `LXMessage.fields` dict:

```python
# LXMessage.fields entry for framework-aware peers
"org.medre.event": {
    "schema": 1,
    "canonical_event_id": "0190b2c3-d4e5-...",
    "relation": {"type": "reply", "parent_event_id": "0190a1b2-c3d4-..."},
    "source": "medre-runtime"
}
```

The adapter may use LXMF field constants (`FIELD_EVENT`, `FIELD_CUSTOM_TYPE`, `FIELD_CUSTOM_DATA`, `FIELD_CUSTOM_META`, `FIELD_THREAD`) as implementation details for how data is packed into the fields dict. These constants are adapter internals and are not exposed in the canonical event model.

When the remote peer is not framework-aware, relation resolution falls back to inline text rendering (e.g., `[Alice] re: original msg > reply text`).

---

## 6. LXMF Identity Mapping

LXMF identities map to native IDs as follows:

| LXMF Concept       | native_id value                                   | native_metadata key       |
| ------------------ | ------------------------------------------------- | ------------------------- |
| Source hash        | `LXMessage.source_hash` (16-byte hex string)      | `source_hash`             |
| Destination hash   | `LXMessage.destination_hash` (16-byte hex string) | `destination_hash`        |
| Reticulum identity | `RNS.Identity.hash` (hex string)                  | `reticulum_identity_hash` |

Source hash and destination hash are opaque native IDs. They are not assumed to correspond to any other transport's identity. Identity reconciliation follows the standard flow defined in the master spec (Section 11).

The `source_transport_id` field on a canonical event originating from LXMF is set to the LXMF source hash (16-byte hex string).

---

## 7. LXMF Delivery Method Selection

Delivery planning for LXMF targets must account for:

| Factor                  | Description                                                                                                                                                                                            |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Delivery method**     | `direct`, `propagated`, or `opportunistic`, depending on whether the destination is currently reachable and whether propagation nodes are configured.                                                  |
| **Propagation delay**   | Propagated delivery has no guaranteed latency. Delivery plans use longer deadlines and different retry strategies.                                                                                     |
| **Content size**        | LXMF messages exceeding single-packet size are conveyed as Reticulum resources. The adapter handles this internally, but delivery planning should be aware of size constraints for metadata embedding. |
| **Receipt correlation** | LXMF per-message delivery and failed callbacks map directly to the core receipt system. Propagated messages may receive delivery confirmation much later than the send time.                           |

---

## 8. Reticulum Containment

All Reticulum internals remain inside the LXMF adapter package. The core runtime must not expose or depend on:

- Raw `RNS.Packet`, `RNS.Link`, or `RNS.Resource` objects
- `RNS.Destination` instances or direct destination addressing
- `RNS.Transport` path management or path request APIs
- `RNS.Request`/`RNS.Response` channel APIs
- Link state, resource transfer, or announce handling outside the adapter

Reticulum initialization, identity loading/generation, `LXMRouter` setup, propagation node handling, announce handling, delivery callbacks, and path/link/resource internals are all adapter-private concerns.

---

## 9. LXMF Capabilities Reference

| Capability          | Value                       | Notes                                                                                                                                               |
| ------------------- | --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `text`              | true                        | Primary content in `LXMessage.content`                                                                                                              |
| `title`             | true                        | Subject line in `LXMessage.title`                                                                                                                   |
| `metadata_fields`   | true                        | Arbitrary key-value via `LXMessage.fields` dict                                                                                                     |
| `replies`           | metadata_native             | No native reply threading. Relation metadata carried in fields dict between framework-aware peers.                                                  |
| `reactions`         | metadata_native             | Same as replies: no native mechanism, carried in fields.                                                                                            |
| `edits`             | metadata_native_or_fallback | Framework-aware peers signal edits via fields; fallback renders edit as new message.                                                                |
| `deletes`           | metadata_native_or_fallback | Same pattern as edits.                                                                                                                              |
| `delivery_receipts` | true                        | LXMF per-message delivery/failed callbacks map to core receipt system.                                                                              |
| `store_and_forward` | true                        | Propagation nodes store encrypted messages for later retrieval.                                                                                     |
| `propagation_nodes` | true                        | Configurable outbound propagation node.                                                                                                             |
| `direct_messages`   | true                        | Point-to-point encrypted delivery.                                                                                                                  |
| `attachments`       | future                      | LXMF defines constants (`FIELD_FILE_ATTACHMENTS`, `FIELD_IMAGE`, `FIELD_AUDIO`) but handling is not implemented by LXMF. Application-level concern. |

---

## 10. Matrix Adapter Notes

- Custom content fields (the `org.*` namespace) are preserved by Synapse under normal operation. They are not pruned by the server.
- The Matrix adapter uses the `m.relates_to` field for threading and reactions, mapping them to the runtime's relation resolution system.
- The Matrix adapter handles HTML formatting for presentation of enriched events (telemetry summaries, position maps, etc.).

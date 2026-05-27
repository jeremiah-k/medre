# Metadata

Metadata boundaries, embedding modes, privacy modes, and the never-embed list.

See also: [event-model.md](event-model.md), [identity-addressing.md](identity-addressing.md),
[security-privacy.md](security-privacy.md).

---

## 1. Structured Metadata

Event metadata is organized into well-defined namespaces rather than a flat
namespace with transport-specific prefixes.

```python
@dataclass
class EventMetadata:
    transport: TransportMetadata | None     # How the event arrived
    routing: RoutingMetadata | None         # Routing decisions applied
    radio: RadioMetadata | None             # Radio-specific data
    telemetry: TelemetryMetadata | None     # Device telemetry at event time
    native: NativeMetadata | None           # Transport-native fields not yet normalized
    custom: dict                            # Plugin/extension metadata
```

## 2. Namespace Definitions

| Namespace            | Purpose                    | Example Fields                                                                               |
| -------------------- | -------------------------- | -------------------------------------------------------------------------------------------- |
| `metadata.transport` | Transport layer details    | `protocol`, `gateway_id`, `received_at`, `encoding`, `delivery_method`, `delivery_confirmed` |
| `metadata.routing`   | Routing context            | `matched_routes`, `fanout_group`, `bridge_id`                                                |
| `metadata.radio`     | Radio-specific data        | `frequency`, `modulation`, `snr`, `rssi`, `hop_limit`, `channel_index`                       |
| `metadata.telemetry` | Device state at event time | `battery_percent`, `voltage_mv`, `uptime_seconds`, `air_util_tx`                             |
| `metadata.native`    | Unnormalized native fields | Adapter-specific raw fields that haven't been mapped to canonical fields yet                 |
| `metadata.custom`    | Plugin/extension data      | Key-value pairs from plugins, using reverse-DNS namespacing                                  |

The `metadata.native` namespace is a temporary holding area for fields that
haven't been categorized yet. The enrichment stage normalizes `metadata.native`
fields into their proper namespaces when possible.

## 3. Migration from Flat Metadata

Legacy flat namespaces map to the new structured model:

| Old Path                          | New Path                       |
| --------------------------------- | ------------------------------ |
| `metadata.meshtastic.snr`         | `metadata.radio.snr`           |
| `metadata.meshtastic.channel`     | `metadata.radio.channel_index` |
| `metadata.meshtastic.from`        | `metadata.transport.source_id` |
| `metadata.meshtastic.telemetry.*` | `metadata.telemetry.*`         |

## 4. Embedding Modes

Metadata embedding controls what runtime information is included in outbound
messages on external platforms. The mode is configured per operator preference.

### 4.1 Privacy Modes

| Mode      | Behavior                                                                                   |
| --------- | ------------------------------------------------------------------------------------------ |
| `off`     | Do not embed any runtime metadata. External platforms are purely display surfaces.         |
| `minimal` | Embed only `event_id` and `source_transport_id`. Less data exposed on redaction.           |
| `safe`    | Embed normalized metadata (event kind, source adapter, transport protocol, radio metrics). |
| `full`    | Embed all metadata. Maximum context for users, but all metadata is lost on redaction.      |

**Default**: `safe`. Operators SHOULD choose based on their threat model.

### 4.2 Matrix Embedding Convention

Metadata embedded in Matrix events uses a reverse-DNS namespace under
`org.medre.*`. Example:

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
      "transport": { "protocol": "meshcore-tcp" },
      "radio": { "snr": 5.2, "rssi": -78 }
    }
  }
}
```

### 4.3 LXMF Embedding Convention

Metadata embedded in LXMF messages uses a namespaced field in the `fields`
dict:

```python
"org.medre.event": {
    "schema": 1,
    "canonical_event_id": "0190b2c3-d4e5-...",
    "relation": {"type": "reply", "parent_event_id": "..."},
    "source": "medre-runtime"
}
```

### 4.4 Constrained Transport Envelopes

Meshtastic (~227 bytes) and MeshCore (184 bytes) payloads are too small for
meaningful envelope data. On these transports, envelopes are typically omitted
entirely or reduced to a bare minimum (`event_id` only, if space permits).

## 5. Never-Embed List

Regardless of privacy mode, the following MUST NEVER be embedded in outbound
messages on any platform:

- Channel keys, private keys, or access tokens
- Raw encrypted blobs or raw packets
- Raw native protocol data (Meshtastic protobuf, Reticulum packets)
- Identity private keys or signing keys
- Full raw native archive data
- Connection credentials
- Transport-internal routing state
- Raw network addresses

## 6. Storage Authoritative

The canonical event log in storage is the single source of truth. Embedded
Matrix metadata is secondary and may be:

- Lost due to Synapse redaction (redaction destroys message content).
- Unavailable if the Matrix homeserver is down.
- Incomplete if the Matrix adapter was offline when the event was processed.

Any feature that needs reliable metadata MUST read from storage, not from
external platforms.

## 7. Redaction and Privacy

Synapse redacts the `content` body of an event when redacted. The
`org.medre.event` field is part of `content` and will be destroyed. The
canonical event in storage is unaffected.

Custom content fields (the `org.*` namespace) are preserved by Synapse under
normal operation. They are not pruned by the server.

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
    native: NativeMetadata | None           # Adapter-owned native metadata
    custom: dict                            # Plugin/extension metadata
```

## 2. Namespace Definitions

| Namespace            | Purpose                    | Example Fields                                                                                                                                |
| -------------------- | -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `metadata.transport` | Transport layer details    | `protocol`, `substrate`, `gateway_id`, `delivery_method`, `delivery_confirmed`, `transport_encrypted`, `signature_valid`, `propagation_state` |
| `metadata.routing`   | Routing context            | `matched_routes` (tuple), `fanout_group`, `route_trace` (tuple)                                                                               |
| `metadata.radio`     | Radio-specific data        | `frequency`, `snr`, `rssi`, `channel_index`                                                                                                   |
| `metadata.telemetry` | Device state at event time | `metrics` dict (frozen)                                                                                                                       |
| `metadata.native`    | Adapter-owned native data  | Versioned transport sub-namespaces                                                                                                            |
| `metadata.custom`    | Plugin/extension data      | Key-value pairs from plugins, using reverse-DNS namespacing                                                                                   |

The `metadata.native` namespace is adapter-owned. Adapters SHOULD promote facts
that have transport-neutral meaning into the standard namespaces, but MAY keep
stable protocol-specific contracts under a versioned native sub-namespace when
those details would otherwise pollute the core model. Native metadata MUST NOT
contain private key material, credential material, SDK objects, or opaque raw
protocol payloads that cannot be serialized safely.

## 3. Built-In Native Metadata

Each built-in adapter has one persisted canonical native-metadata namespace.
The transport name is the top-level key inside `NativeMetadata.data`; the
transport object carries its own positive integer `schema_version`.

| Transport  | Canonical path                       | Current version | Machine schema                                                                                |
| ---------- | ------------------------------------ | --------------: | --------------------------------------------------------------------------------------------- |
| Matrix     | `metadata.native.data["matrix"]`     |             `1` | [`matrix-native-metadata.schema.json`](../schemas/matrix-native-metadata.schema.json)         |
| Meshtastic | `metadata.native.data["meshtastic"]` |             `1` | [`meshtastic-native-metadata.schema.json`](../schemas/meshtastic-native-metadata.schema.json) |
| MeshCore   | `metadata.native.data["meshcore"]`   |             `1` | [`meshcore-native-metadata.schema.json`](../schemas/meshcore-native-metadata.schema.json)     |
| LXMF       | `metadata.native.data["lxmf"]`       |             `1` | [`lxmf-native-metadata.schema.json`](../schemas/lxmf-native-metadata.schema.json)             |

A current-version projection helper MUST interpret only the schema version it
implements. Platform detection MAY recognize a positively versioned future
transport object without interpreting that future version's fields. Flat,
dotted, or unversioned built-in native metadata is not an alternate canonical
shape.

Matrix has an additional detailed event-shape contract in
[matrix-event-shape.md](matrix-event-shape.md). Cross-product MMRelay wire
interoperability, when present, lives separately under
`metadata.native.data["interop"]["mmrelay"]`; it is not another Matrix or
Meshtastic native shape.

Delivery-result and outbound-native-reference metadata follows the same
transport-key ownership rule and carries the transport's current
`schema_version`, but those records have context-specific field sets and are not
instances of the canonical-event native-metadata JSON Schemas above.

See [compatibility-boundaries.md](compatibility-boundaries.md) for the boundary
between external interoperability and rejected MEDRE development shapes.

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

MEDRE provenance embedded into outbound Matrix message content uses the
versioned `medre.envelope` object produced by `MatrixMetadataEnvelope`:

```json
{
  "msgtype": "m.text",
  "body": "Hello from node 1234",
  "medre": {
    "envelope": {
      "schema_version": 1,
      "canonical_event_id": "0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",
      "source_adapter": "meshcore-radio-1",
      "source_channel": "0",
      "provenance": "",
      "relation_info": "",
      "lineage_pointer": "",
      "metadata_mode": "safe",
      "native_source_summary": ""
    }
  }
}
```

Inbound readers interpret only the current envelope schema version. A missing
or unsupported version is not treated as the current envelope shape. The
Matrix canonical-native projection stores a safe copy of a valid envelope under
`metadata.native.data["matrix"]["relay"]["medre_envelope"]`.

### 4.3 LXMF Embedding Convention

LXMF carries MEDRE provenance in custom field `0xFD`. The value is namespaced
under `"medre"` and uses its own explicit schema version:

```python
{
    0xFD: {
        "medre": {
            "schema_version": 1,
            "event_id": "0190b2c3-d4e5-...",
            "source_adapter": "matrix-1",
            "source_transport_id": "@alice:example.com",
            "source_channel_id": "!room:example.com",
            "lineage": [],
            "relations": [],
            "metadata_keys": [],
        }
    }
}
```

Inbound readers interpret only the current LXMF fields-envelope version.
Unsupported or unversioned MEDRE envelopes are ignored. This custom field is a
MEDRE interoperability envelope carried by LXMF; it is distinct from the
canonical `metadata.native.data["lxmf"]` transport namespace.

### 4.4 Constrained Transport Envelopes

Meshtastic and MeshCore payload budgets are too small for the full MEDRE
provenance envelopes above. MEDRE does not define an alternate persisted
canonical-native shape for this purpose. Transport-specific outbound fields are
rendered only when the current transport contract explicitly requires them.

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
`medre.envelope` field is part of `content` and will be destroyed. The canonical
event in storage is unaffected.

Custom content fields are preserved by Synapse under normal operation. They are
not pruned by the server.

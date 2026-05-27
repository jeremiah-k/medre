# Security and Privacy

Security model, credential handling, no-secret-leakage guarantees, and privacy
boundaries.

See also: [identity-addressing.md](identity-addressing.md),
[metadata.md](metadata.md), [configuration.md](configuration.md).

---

## 1. Credential Handling

### 1.1 Where Credentials Live

Credentials are adapter-internal configuration, never event data. They live in:

- **TOML config files** (e.g., `access_token` in the Matrix adapter section).
- **Environment variables** via `MEDRE_ADAPTER__<TOKEN>__<FIELD>` overrides.
- **Credential sidecar files** (Matrix credential sidecar managed by
  `medre.config.adapters.matrix_credentials`).

### 1.2 Credential Sidecar

The Matrix credential sidecar (`medre.config.adapters.matrix_credentials`)
provides:

- `get_credentials_path()` -> Path
- `load_credentials_json(path=None)` -> dict | None
- `write_credentials_json(data, path=None)` -> Path

The adapter's auth module delegates credential persistence to this config-layer
module for testability.

### 1.3 Where Credentials Must NOT Appear

Credentials MUST NOT appear in:

- `CanonicalEvent` fields or metadata
- Delivery receipts
- Diagnostic snapshots
- Log output
- Envelopes embedded in outbound native payloads
- The canonical event log
- Replay output

### 1.4 Log Sanitization

All log output passes through sanitization that redacts known secret patterns.
The canonical sanitization implementation lives in
`medre.core.observability.sanitization`. It defines secret key patterns that
are shared across logging, diagnostics, and error rendering.

## 2. No-Secret-Leakage Guarantees

### 2.1 Event Model

The `CanonicalEvent` model has no field for credentials, tokens, or private
keys. The `metadata` namespaces (`transport`, `routing`, `radio`, `telemetry`,
`native`, `custom`) do not carry secrets.

### 2.2 Diagnostics

The `diagnostics()` method on adapters and the `Diagnostician` in core use
the shared sanitization module. Sensitive fields are redacted before the
diagnostic snapshot leaves the adapter boundary.

### 2.3 Error Rendering

`sanitize_error` (from `medre.core.observability.sanitization`) is used by
routing stats and other error surfaces to strip credentials from error strings
before they enter logs or diagnostic output.

### 2.4 Receipts

`DeliveryReceipt` records contain `error` and `adapter_message_id` fields.
These are adapter-reported values. Adapter implementations MUST ensure that
error messages and message IDs do not contain credentials.

## 3. Privacy Boundaries

### 3.1 What Never Goes Into Canonical Metadata

1. **Private keys.** No Ed25519 private keys, no X25519 private keys, no
   Matrix access tokens, no Meshtastic channel PSKs.
2. **Connection credentials.** Homeserver URLs with embedded tokens, serial
   port paths, BLE device addresses.
3. **Transport-internal routing state.** Meshtastic hop limits, MeshCore flood
   parameters, Reticulum link states.
4. **Raw network addresses.** IP addresses, serial port identifiers, BLE MAC
   addresses.
5. **Cross-transport display names.** If an event originates on Meshtastic, the
   canonical event carries the Meshtastic node number. It does not carry a
   display name looked up from a Matrix user directory.

### 3.2 Why These Boundaries Exist

- **Private keys in events** would leak them to storage, replay logs, and any
  consumer that reads events. Storage is append-only and not encrypted at rest.
- **Credentials in events** would expose them to any code that processes events,
  including plugins and replay consumers.
- **Routing state in events** would couple the event model to transport
  internals, breaking the adapter boundary.
- **Cross-transport display names** would require real-time lookup at event
  creation time, which is not possible when the source adapter is offline.

### 3.3 What Envelopes May Expose

Outbound envelopes (MEDRE metadata in native payloads) MAY contain:

- `schema_version`, `event_id`, `source_adapter`, `parent_event_id`
- `relation_type`, `target_native_ref`, `trace_id`

Envelopes MUST NOT contain:

- Private keys or credentials
- Internal routing state
- Configuration details
- User PII beyond what the transport already exposes

### 3.4 What the Transport Already Exposes

Each transport exposes sender identity natively. This is not under MEDRE's
control:

- Matrix exposes MXIDs to all room members.
- Meshtastic exposes node numbers to all listeners on the channel.
- MeshCore exposes public keys to all recipients.
- LXMF exposes source hashes to recipients and propagation nodes.

MEDRE does not add to this exposure. It records what the transport already
makes visible.

## 4. Adapter Isolation

### 4.1 No Cross-Adapter Access

Adapters do not get direct access to other adapters. All communication goes
through the event pipeline. An adapter cannot read another adapter's
configuration, credentials, or connection state.

### 4.2 Scoped Context

Each adapter receives an `AdapterContext` with controlled access to runtime
services:

- `publish_inbound()`: publish events into the pipeline
- `logger`: adapter-scoped logger
- `clock`: deterministic clock hook

Adapters do not receive the storage backend, other adapter references, or the
event bus directly.

## 5. Plugin Security Boundaries

Plugins operate within capability-scoped boundaries:

1. **Capability declaration**: Plugins declare required capabilities at load
   time. The runtime grants only what is declared.
2. **Route permissions**: Plugins that emit events can only send to routes the
   operator has explicitly allowed.
3. **Rate limits**: Each plugin has configurable rate limits for event emission,
   storage queries, and API calls.
4. **Audit logging**: All plugin actions are logged with plugin identity and
   capability used.

## 6. Encryption Model

### 6.1 Matrix E2EE

Matrix encryption is controlled by `encryption_mode: "plaintext" |
"e2ee_required" | "e2ee_optional"` (default `"plaintext"`).

When set to a non-plaintext mode, MEDRE internally passes
`ignore_unverified_devices=True` to nio's `room_send`. This is an upstream nio
client limitation — nio lacks cross-signing support, providing no API for
programmatic device verification. This flag is not operator-configurable; it is
applied automatically based on `encryption_mode`.

### 6.2 MeshCore

E2EE is at the radio level. MEDRE does not manage MeshCore keys.

### 6.3 LXMF

Reticulum provides link-layer encryption. MEDRE does not manage Reticulum
identity keys beyond loading them for the owned session.

### 6.4 Meshtastic

Encryption is optional per-packet. MEDRE does not manage Meshtastic channel
keys.

## 7. Never-Embed List

Regardless of embedding mode, the following are never embedded in outbound
messages on any platform:

- Channel keys, private keys, or access tokens
- Raw encrypted blobs or raw packets
- Raw native protocol data
- Identity private keys or signing keys
- Full raw native archive data

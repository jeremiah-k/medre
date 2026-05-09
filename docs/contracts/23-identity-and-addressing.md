# Identity and Addressing Across Transports

> Contract version: 1
> Last updated: 2026-05-09
> Track: 9 (Transport Capability Contracts)
> Supersedes: Partially supersedes 12-adapter-platform-identity.md and 12-constrained-transport-comparison.md (identity sections). This document is the canonical reference for cross-transport identity semantics.
> Supplements: Contracts 01, 02, 15.

This document compares identity and addressing across MEDRE's four adapter families, defines what goes into canonical metadata, what never goes into canonical metadata, and what envelopes may expose. It is explicit about privacy boundaries and the limits of cross-transport identity normalization.

This is an audit and contract, not a redesign. It records what exists and where the seams are.


## 1. Scope

- Native identity models per transport (Matrix, Meshtastic, MeshCore, LXMF).
- Canonical sender and destination semantics.
- Native references vs. transport identities.
- Privacy boundaries: what never enters canonical metadata.
- What envelopes may expose and what they must not.
- Cross-transport identity resolution boundaries.

## 2. Non-goals

- Implementing a cross-transport identity directory or resolver.
- Proposing a unified identity model beyond what `source_transport_id` already provides.
- Implementing key management, identity rotation, or trust-on-first-use workflows.
- Claiming that any identity system works against real hardware or services in default CI.


## 3. Native Identity Models

### 3.1 Matrix

| Concept | Representation | Example | Scope |
|---------|---------------|---------|-------|
| User identity | MXID | `@alice:matrix.org` | Global (federated) |
| Room identity | Room ID | `!abc123:matrix.org` | Global (federated) |
| Room alias | Canonical alias | `#general:matrix.org` | Global (federated, resolvable) |
| Event identity | Event ID | `$event_id_string` | Global (federated) |
| Device identity | Device ID | `DEVICEID` | Per-user |

**Characteristics:**

- MXIDs are human-readable, stable, and globally unique within the Matrix federation.
- Room IDs are opaque strings. Room aliases are human-readable but mutable (an alias can be remapped to a different room).
- User identity is server-anchored. The `@user:server.org` syntax encodes both the local name and the authoritative homeserver.
- Device IDs are per-user and per-session. MEDRE does not currently track device identity.

### 3.2 Meshtastic

| Concept | Representation | Example | Scope |
|---------|---------------|---------|-------|
| Node identity | Node number (int) | `1234` | Local mesh, session-scoped |
| Node identity (string) | fromId (str) | `"!abcdef12"` | Local mesh, derived from hardware |
| Channel identity | Channel index (int) | `0` through `7` | Local mesh |
| Packet identity | Packet ID (int) | `42` | Session-scoped, per-node |

**Characteristics:**

- Node numbers are assigned dynamically. A node that rejoins the mesh may receive a different number.
- The `fromId` string is more stable (derived from hardware MAC or similar) but not guaranteed unique across disconnected meshes.
- Channel indices are local configuration. Channel 0 on one mesh is not the same as channel 0 on another.
- There is no global identity. Two disconnected Meshtastic meshes may have colliding node numbers.

### 3.3 MeshCore

| Concept | Representation | Example | Scope |
|---------|---------------|---------|-------|
| Node identity | Ed25519 public key (32 bytes, hex-encoded) | `"a1b2c3...` (64 hex chars) | Global (cryptographic) |
| Channel identity | Channel name + index | `"default"` / `0` | Local mesh |
| Contact identity | Public key (via contact list) | Same as node identity | Global (cryptographic) |
| Message identity | Sender timestamp (int) | `1715234567` | Per-sender |

**Characteristics:**

- Identity is cryptographic. The public key is the identity. There is no human-readable name at the protocol level.
- Contact lists map pubkeys to display names locally. This mapping is device-local and not shared.
- Channel identities combine a name string and an index. Channels are encrypted; joining requires the channel key.
- Message IDs are sender-assigned timestamps. Collision is possible when a sender transmits rapidly.

### 3.4 LXMF

| Concept | Representation | Example | Scope |
|---------|---------------|---------|-------|
| Identity | Reticulum Identity (X25519 + Ed25519) | Internal keypair | Global (cryptographic) |
| Identity hash | 16-byte hex string | `"a1b2c3d4e5f6a7b8"` | Global (derived from public key) |
| Destination | Destination hash (16 bytes) | Same format as identity hash | Global (derived from app name + identity) |
| LXMF address | Source/destination hash | `"a1b2c3d4e5f6a7b8"` | Global |
| Message identity | LXMF hash (content-derived) | Internal bytes | Global (content-addressed) |

**Characteristics:**

- Identity is cryptographic. The identity hash is derived from SHA-256 of the public key (first 16 bytes).
- LXMF addresses are 16-byte hex strings. They are stable, globally unique (within the Reticulum network), and opaque.
- There is no human-readable addressing at the protocol level. Side channels (announce messages, address books) map hashes to names.
- The identity hash is derived, not assigned. Two independent keypairs will produce different hashes with overwhelming probability.


## 4. Canonical Sender and Destination Semantics

### 4.1 source_transport_id

The `source_transport_id` field on `CanonicalEvent` carries the native sender identity from the source transport. It is always a string.

| Transport | source_transport_id value | Example |
|-----------|-------------------------|---------|
| Matrix | Sender MXID | `@alice:matrix.org` |
| Meshtastic | Node number (as string) | `"1234"` |
| MeshCore | Ed25519 public key (hex) | `"a1b2c3...` (64 hex chars) |
| LXMF | Source hash (16-byte hex) | `"a1b2c3d4e5f6a7b8"` |

**Rules:**

1. `source_transport_id` is always set for source events (events produced by adapter codecs). It is never `None` for ingress.
2. The value is the transport's native sender identifier, converted to string. No synthesis, no normalization beyond string conversion.
3. The same sender on the same transport always produces the same `source_transport_id` value, with the caveat that Meshtastic node numbers may change between sessions.

### 4.2 source_channel_id

The `source_channel_id` field carries the native channel, room, or topic where the event originated.

| Transport | source_channel_id value | Example |
|-----------|------------------------|---------|
| Matrix | Room ID | `!abc123:matrix.org` |
| Meshtastic | Channel index (as string) | `"0"` |
| MeshCore | Channel name or index (as string) | `"0"` |
| LXMF | `None` (no channel concept) or app name | `None` |

**Rules:**

1. `source_channel_id` may be `None` if the transport has no channel concept (e.g., direct LXMF messages).
2. The value is the transport's native channel identifier, converted to string.
3. Route matching evaluates `source_channel_id` for filtering (e.g., Matrix room allowlists, Meshtastic channel selection).

### 4.3 Destination Addressing

Destination addressing is handled at the routing and delivery planning level, not on the canonical event itself. The delivery plan specifies which adapter and which native destination (room, channel, node) to target.

| Transport | Destination type | Specified by |
|-----------|-----------------|-------------|
| Matrix | Room ID | Delivery plan target |
| Meshtastic | Node number or broadcast | Delivery plan target |
| MeshCore | Public key or flood | Delivery plan target |
| LXMF | Destination hash | Delivery plan target |

The canonical event model does not carry a canonical destination. Routing determines destinations. This is intentional: a single canonical event may be delivered to multiple adapters at multiple destinations.


## 5. Native References vs. Transport Identities

### 5.1 NativeMessageRef

`NativeMessageRef` correlates a native message to a canonical event. It is stored by the storage layer and used for cross-adapter correlation (e.g., "which Matrix message corresponds to this Meshtastic packet?").

```python
class NativeMessageRef(msgspec.Struct, frozen=True):
    adapter_id: str
    native_channel_id: str | None
    native_message_id: str
    event_id: str
```

**Key points:**

- `native_message_id` is the transport's message-level identifier, not the sender identity. Matrix event IDs, Meshtastic packet IDs, MeshCore timestamps, and LXMF hashes all go here.
- `adapter_id` ties the ref to a specific adapter instance. The same native_message_id from two different adapter instances is not the same message.
- `native_channel_id` scopes the message within the adapter's channel/room space.

### 5.2 EventRelation target_native_ref

When a canonical event carries a reply relation, the `target_native_ref` on the `EventRelation` points to the native message being replied to. This is a `NativeMessageRef` that may or may not correspond to a canonical event already in storage.

**Scenarios:**

1. The replied-to message was also ingested by MEDRE. The `target_native_ref.event_id` can be resolved to a canonical event via storage lookup.
2. The replied-to message was not ingested by MEDRE (it predates MEDRE's operation, or it was on a channel MEDRE doesn't monitor). The `target_native_ref` exists but `event_id` cannot be resolved. The relation is still recorded but cannot be walked.

### 5.3 What Is NOT a Native Reference

- **Sender identity** is carried on `source_transport_id`, not in `NativeMessageRef`.
- **Destination identity** is not carried on the canonical event at all. It lives in the delivery plan.
- **Adapter identity** (`adapter_id`) is an instance name from configuration, not a transport identity.


## 6. Privacy Boundaries

### 6.1 What Never Goes Into Canonical Metadata

The following must never appear in `CanonicalEvent.metadata` or any field of `CanonicalEvent`:

1. **Private keys.** No Ed25519 private keys, no X25519 private keys, no Matrix access tokens, no Meshtastic channel PSKs. These are adapter-internal configuration, never events.
2. **Connection credentials.** Homeserver URLs with embedded tokens, serial port paths, BLE device addresses. These are adapter configuration, not event data.
3. **Transport-internal routing state.** Meshtastic hop limits, MeshCore flood parameters, Reticulum link states. These are adapter internals.
4. **Raw network addresses.** IP addresses, serial port identifiers, BLE MAC addresses. These are connection-layer details, not message-layer identity.
5. **User-displayable names from other transports.** If an event originates on Meshtastic, the canonical event carries the Meshtastic node number as `source_transport_id`. It does not carry a display name looked up from a Matrix user directory. Display name resolution is a rendering concern, not a canonical event concern.

### 6.2 Why These Boundaries Exist

- **Private keys in events** would leak them to storage, replay logs, and any consumer that reads events. Storage is append-only and not encrypted at rest in Phase 1.
- **Credentials in events** would expose them to any code that processes events, including plugins and replay consumers.
- **Routing state in events** would couple the event model to transport internals, breaking the adapter boundary.
- **Cross-transport display names** would require real-time lookup at event creation time, which is not possible when the source adapter is offline or disconnected.

### 6.3 What Envelopes May Expose

Envelopes (MEDRE metadata embedded in outbound native payloads) may contain:

| Field | Allowed? | Rationale |
|-------|----------|-----------|
| `schema_version` | Yes | Public versioning info |
| `event_id` | Yes | Needed for correlation |
| `source_adapter` | Yes | Identifies the MEDRE adapter, not credentials |
| `parent_event_id` | Yes | Lineage tracking |
| `relation_type` | Yes | Reply/thread metadata |
| `target_native_ref` | Yes | Cross-adapter correlation |
| `trace_id` | Yes | Distributed tracing |

Envelopes must NOT contain:

| Field | Reason |
|-------|--------|
| Private keys or credentials | Same privacy boundary as canonical metadata |
| Internal routing state | Adapter-internal |
| Configuration details | Adapter-internal |
| User PII beyond what the transport already exposes | Privacy |

**Transport-specific limits:** Meshtastic and MeshCore payloads are too small (228 and 184 bytes respectively) for meaningful envelope data. On these transports, envelopes are typically omitted entirely or reduced to a bare minimum (event_id only, if space permits). Matrix and LXMF have room for richer envelopes.

### 6.4 What the Transport Already Exposes

Each transport exposes sender identity natively. This is not under MEDRE's control:

- Matrix exposes MXIDs to all room members.
- Meshtastic exposes node numbers to all listeners on the channel.
- MeshCore exposes public keys to all recipients.
- LXMF exposes source hashes to recipients and propagation nodes.

MEDRE does not add to this exposure. It records what the transport already makes visible. If a transport broadcasts sender identity, MEDRE's canonical event will carry that identity. This is inherent to the transport, not a MEDRE privacy decision.


## 7. Cross-Transport Identity Resolution

### 7.1 Current State

MEDRE does not implement cross-transport identity resolution. The `IdentityResolver` mentioned in Contract 12 (`12-adapter-platform-identity.md`) stores native-to-canonical mappings but does not bridge identities across transports.

A Meshtastic node number `1234` and a Matrix MXID `@alice:server.org` are stored as separate `source_transport_id` values on separate canonical events. MEDRE does not know they represent the same human.

### 7.2 Why No Cross-Transport Mapping

Cross-transport identity mapping requires either:

1. **Manual configuration** (operator declares that Meshtastic node 1234 is Matrix user @alice:server.org). This is operator workload and error-prone.
2. **Cryptographic proof** (the same keypair is used on both transports). None of the four transports share a keypair format.
3. **Heuristic matching** (matching display names, message timing, etc.). This is unreliable and privacy-invasive.

None of these approaches is implemented. None is planned for Phase 1. The canonical event model carries transport-scoped identities. Cross-transport correlation, when needed, is an operator concern handled outside MEDRE.

### 7.3 What Storage Provides

Storage provides:

- Native refs that map `(adapter_id, native_channel_id, native_message_id)` to `event_id`.
- Events keyed by `source_transport_id` within an adapter.
- Relations that link events across adapters (e.g., a reply on Matrix that references a Meshtastic-originated event).

This is sufficient for event-level correlation (same message, different transports). It is not sufficient for user-level correlation (same person, different transports).


## 8. Implications

### 8.1 For Adapter Authors

- `source_transport_id` must be the transport's native sender identifier, as a string. Do not synthesize, normalize, or obfuscate it.
- `source_channel_id` must be the native channel identifier. `None` if the transport has no channels.
- Never put private keys, credentials, or configuration in canonical events or envelopes.
- Respect payload limits when embedding envelopes. On constrained transports, omit envelopes entirely if they would exceed the payload budget.

### 8.2 For Pipeline Authors

- Do not assume `source_transport_id` values are comparable across adapters. They are strings from different namespaces.
- Do not implement cross-transport identity mapping in the pipeline. This is an operator concern.
- `NativeMessageRef` is the only correlation mechanism. Use it for reply threading and cross-adapter linking.

### 8.3 For Operators

- Identity is transport-scoped. A user on Matrix has a different identity on Meshtastic. MEDRE does not bridge these.
- If you need cross-transport identity mapping, maintain it externally and use MEDRE's event metadata to annotate.
- Monitor `source_transport_id` values for unexpected changes. Meshtastic node numbers can shift between sessions.
- LXMF and MeshCore identities are stable (cryptographic). Matrix identities are stable (server-anchored). Meshtastic identities are the least stable of the four.

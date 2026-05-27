# Identity and Addressing

Identity model, native identities, canonical actors, and addressing semantics
across transports.

See also: [event-model.md](event-model.md), [metadata.md](metadata.md),
[security-privacy.md](security-privacy.md).

---

## 1. Overview

The identity model bridges between native transport identities and canonical
actors within the runtime. Each transport has its own identity scheme. The
identity layer reconciles these into canonical actors without assuming any two
native IDs represent the same entity.

No native ID from any transport is treated as a universal identifier. All
reconciliation is explicit and operator-auditable.

## 2. Native Identity Models

### 2.1 Matrix

| Concept         | Representation  | Example               | Scope          |
| --------------- | --------------- | --------------------- | -------------- |
| User identity   | MXID            | `@alice:matrix.org`   | Global (federated) |
| Room identity   | Room ID         | `!abc123:matrix.org`  | Global (federated) |
| Event identity  | Event ID        | `$event_id_string`    | Global (federated) |

MXIDs are human-readable, stable, and globally unique within the Matrix
federation. User identity is server-anchored.

### 2.2 Meshtastic

| Concept         | Representation  | Example     | Scope                  |
| --------------- | --------------- | ----------- | ---------------------- |
| Node identity   | Node number     | `1234`      | Local mesh, session-scoped |
| Channel         | Channel index   | `0` through `7` | Local mesh         |

Node numbers are assigned dynamically. A node that rejoins the mesh MAY
receive a different number. There is no global identity.

### 2.3 MeshCore

| Concept         | Representation              | Example                | Scope                  |
| --------------- | --------------------------- | ---------------------- | ---------------------- |
| Node identity   | Ed25519 public key (hex)    | `"a1b2c3..."` (64 chars) | Global (cryptographic) |
| Channel         | Channel name + index        | `"default"` / `0`      | Local mesh             |

Identity is cryptographic. The public key is the identity. Contact lists map
pubkeys to display names locally; this mapping is device-local and not shared.

### 2.4 LXMF

| Concept         | Representation             | Example                  | Scope                  |
| --------------- | -------------------------- | ------------------------ | ---------------------- |
| Identity        | Reticulum Identity         | Internal keypair         | Global (cryptographic) |
| Identity hash   | 16-byte hex string         | `"a1b2c3d4e5f6a7b8"`     | Global                 |
| Destination     | Destination hash (16 bytes)| Same format as identity  | Global                 |

Identity is cryptographic. The identity hash is derived from SHA-256 of the
public key (first 16 bytes). There is no human-readable addressing at the
protocol level.

## 3. Canonical Event Identity Fields

### 3.1 source_transport_id

The `source_transport_id` field on `CanonicalEvent` carries the native sender
identity from the source transport. It is always a string.

| Transport  | Value                         | Example                     |
| ---------- | ----------------------------- | --------------------------- |
| Matrix     | Sender MXID                   | `@alice:matrix.org`         |
| Meshtastic | Node number (as string)       | `"1234"`                    |
| MeshCore   | Ed25519 public key (hex)      | `"a1b2c3..."` (64 chars)    |
| LXMF       | Source hash (16-byte hex)     | `"a1b2c3d4e5f6a7b8"`        |

Rules:

1. `source_transport_id` MUST be set for all source events. It MUST NOT be
   `None` for ingress.
2. The value is the transport's native sender identifier, converted to string.
   No synthesis, no normalization beyond string conversion.
3. The same sender on the same transport always produces the same value, with
   the caveat that Meshtastic node numbers MAY change between sessions.

### 3.2 source_channel_id

The `source_channel_id` field carries the native channel, room, or topic where
the event originated.

| Transport  | Value                          | Example              |
| ---------- | ------------------------------ | -------------------- |
| Matrix     | Room ID                        | `!abc123:matrix.org` |
| Meshtastic | Channel index (as string)      | `"0"`                |
| MeshCore   | Channel name or index          | `"0"`                |
| LXMF       | `None` (no channel concept)    | `None`               |

`source_channel_id` MAY be `None` if the transport has no channel concept.
Route matching evaluates this field for filtering.

### 3.3 Destination Addressing

Destination addressing is handled at the routing and delivery planning level,
not on the canonical event. The delivery plan specifies which adapter and
which native destination to target. A single canonical event MAY be delivered
to multiple adapters at multiple destinations.

## 4. NativeIdentity and CanonicalActor

### 4.1 NativeIdentity

```python
@dataclass
class NativeIdentity:
    adapter: str            # Adapter name (e.g., "meshcore-radio-1")
    native_id: str          # Transport-specific ID (node number, MXID, hash)
    native_name: str | None # Display name on the transport
    native_metadata: dict   # Transport-specific identity data
```

### 4.2 CanonicalActor

```python
@dataclass
class CanonicalActor:
    actor_id: str                    # Runtime-unique actor ID
    display_name: str
    linked_identities: list[NativeIdentity]
    verification_status: str         # "verified", "manual", "auto", "unverified"
    permissions: set[str]
    created_at: datetime
    last_seen_at: datetime
```

### 4.3 Identity Resolution Flow

1. An event arrives with a `source_transport_id`.
2. The identity resolver looks up any existing `CanonicalActor` linked to this
   native identity.
3. If found, the actor ID is attached to the event during enrichment.
4. If not found, a new actor is created with `verification_status: "unverified"`
   and the native identity is linked.
5. Operators MAY manually merge native identities into a single canonical actor.
6. Auto-linking rules MAY be configured (e.g., match by callsign).

### 4.4 Verification States

| State          | Meaning                                                                      |
| -------------- | ---------------------------------------------------------------------------- |
| **Verified**   | Operator has confirmed this identity mapping.                                |
| **Manual**     | Operator created or edited this mapping.                                     |
| **Auto**       | System auto-linked based on configurable rules. Subject to operator review. |
| **Unverified** | No mapping exists. The native identity is treated as a standalone actor.    |

## 5. Cross-Transport Identity Boundaries

MEDRE does not implement cross-transport identity resolution. A Meshtastic
node number `1234` and a Matrix MXID `@alice:server.org` are stored as
separate `source_transport_id` values on separate canonical events. MEDRE does
not know they represent the same human.

Cross-transport identity mapping requires either manual configuration,
cryptographic proof (no two transports share a keypair format), or heuristic
matching (unreliable). None is implemented.

## 6. Privacy Boundaries

The following MUST NEVER appear in `CanonicalEvent.metadata` or any field of
`CanonicalEvent`:

1. Private keys (Ed25519, X25519, Matrix access tokens, channel PSKs).
2. Connection credentials (homeserver URLs with embedded tokens, serial ports,
   BLE addresses).
3. Transport-internal routing state (hop limits, flood parameters, link states).
4. Raw network addresses (IP addresses, serial port identifiers, BLE MACs).
5. User-displayable names from other transports (display name resolution is a
   rendering concern, not a canonical event concern).

## 7. Adapter Identity Concepts

| Concept               | Scope           | What it identifies                                   |
| --------------------- | --------------- | ---------------------------------------------------- |
| `adapter_id`          | Transport-local | A specific adapter instance in this MEDRE process    |
| `platform`            | Platform-level  | The protocol family (meshtastic, meshcore, matrix)   |
| `source_transport_id` | Transport-local | Native actor identity (MXID, node number, pubkey)    |
| `target_platform`     | Platform-level  | Renderer selection key (resolved from adapter_id)    |

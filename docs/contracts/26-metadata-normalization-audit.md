# Metadata Normalization Audit Observations

> Last updated: 2026-05-10
> Scope: Track 9 — Metadata Normalization Across Matrix/Meshtastic/MeshCore/LXMF
> Status: Audit observations. No code changes proposed.

This document records observations from auditing how metadata flows through MEDRE's four adapter families (Matrix, Meshtastic, MeshCore, LXMF). It documents what stays transport-specific, what becomes canonical, where the boundaries are, and where asymmetries exist that cannot be abstracted away.

This is an audit document, not a design document. No canonical event redesign, bridge policy redesign, or cross-transport orchestration changes are proposed.

## 1. Core Principle

**Transport-rich metadata stays in namespaced envelopes. Canonical events remain transport-agnostic.**

The `CanonicalEvent` model contains no transport-specific types. All transport-native details live in `EventMetadata` namespaces, primarily `metadata.native` and `metadata.transport`. Core and pipeline code never import adapter packages.

## 2. Metadata Architecture

### 2.1 EventMetadata Namespaces

All event metadata lives in a structured `EventMetadata` object with six namespaces:

| Namespace   | Purpose                                                           | Transport-Specific?                   |
| ----------- | ----------------------------------------------------------------- | ------------------------------------- |
| `transport` | Protocol, gateway, delivery method, encryption, propagation state | Partially (values vary per transport) |
| `routing`   | Matched routes, fanout groups                                     | No (pipeline-owned)                   |
| `radio`     | Frequency, SNR, RSSI, channel index                               | Yes (Meshtastic/MeshCore only)        |
| `telemetry` | Battery, voltage, device metrics                                  | Yes (constrained transports only)     |
| `native`    | Unnormalized raw fields from the transport                        | Yes (per-adapter)                     |
| `custom`    | Plugin/extension data                                             | No (user-defined)                     |

### 2.2 What Is Canonical

The `CanonicalEvent` fields are transport-agnostic:

- `event_id`: UUIDv7 (not a native message ID from any transport).
- `source_transport_id`: string (MXID for Matrix, node number for Meshtastic/MeshCore, identity hash hex for LXMF).
- `source_channel_id`: string or None (room ID, channel index, or absent).
- `source_native_ref`: `NativeMessageRef` carrying the transport's own message ID.
- `payload`: dict with `body`, `title`, etc. (no transport types).
- `metadata`: `EventMetadata` struct with namespaced fields.

### 2.3 What Stays Transport-Specific

Each adapter's codec produces transport-specific metadata that goes into `metadata.native.data`:

- **Matrix**: sender MXID, room ID, event type, `m.relates_to`, server timestamp, unsigned data.
- **Meshtastic**: SNR, RSSI, hop limit, channel index, packet ID, from/to IDs.
- **MeshCore**: channel index, sender pubkey, CRC status, ACK state.
- **LXMF**: source hash, destination hash, message hash, method, signature validated, transport encrypted, fields dict.

These are stored as-is. No cross-transport normalization is attempted.

## 3. Per-Adapter Observations

### 3.1 Matrix

Matrix's rich metadata model maps cleanly to the canonical structure:

- Room ID → `source_channel_id`
- Sender MXID → `source_transport_id`
- Event ID → `NativeMessageRef.native_message_id`
- `m.relates_to` → `EventRelation` with `target_native_ref`
- Server timestamp → `CanonicalEvent.timestamp`
- Room-specific metadata (membership, power levels, encryption state) → `metadata.native.data`

Matrix's metadata is the richest of the four adapters. It has native support for replies, reactions, edits, redactions, threads, and read receipts. Most of these have no equivalent in the other three transports.

### 3.2 Meshtastic

Meshtastic's metadata is radio-oriented:

- Node number → `source_transport_id` (string)
- Channel index → `source_channel_id` (string)
- Packet ID → `NativeMessageRef.native_message_id`
- SNR/RSSI → `metadata.radio`
- Hop limit, channel utilization → `metadata.native.data`
- `replyId` → `EventRelation` with `target_native_ref`

Key asymmetry: packet IDs are 32-bit integers that wrap. They are session-scoped, not globally unique. Two packets from different sessions may share the same ID.

### 3.3 MeshCore

MeshCore's metadata is compact and crypto-oriented:

- Sender Ed25519 pubkey → `source_transport_id` (hex string)
- Channel index → `source_channel_id` (string)
- Sender timestamp → `NativeMessageRef.native_message_id`
- CRC status, ACK state → `metadata.native.data`

Key asymmetry: no native reply mechanism. No channel-level addressing beyond index. Timestamps can collide for simultaneous sends.

### 3.4 LXMF

LXMF's metadata is identity-centric and store-and-forward oriented:

- Source identity hash → `source_transport_id` (32-char hex string)
- Message hash → `NativeMessageRef.native_message_id` (64-char hex string)
- Delivery method → `metadata.native.data` (direct/opportunistic/propagated/paper)
- Signature validated, transport encrypted → `metadata.native.data`
- Fields dict → parsed by `LxmfFieldsHelper`, MEDRE envelope extracted from `FIELD_CUSTOM_META` (0xFD)

Key asymmetry: LXMF has no native reply, reaction, edit, or threading mechanism. The fields dict is extensible but LXMF does not define reply semantics natively. MEDRE carries reply metadata in the canonical event model, but the LXMF transport will not render it as a threaded reply — it will appear as plain text with an optional quoted prefix.

## 4. Outbound Metadata Embedding

### 4.1 Pattern

When MEDRE renders a canonical event for outbound delivery, the adapter's renderer produces a transport-specific payload. Metadata embedding varies:

- **Matrix**: metadata can be sent in the event's `content` dict or as custom fields.
- **Meshtastic**: no metadata embedding. Payload is plain text only (~228 bytes).
- **MeshCore**: no metadata embedding. Payload is plain text only (184 bytes).
- **LXMF**: MEDRE metadata envelope embedded in `fields[FIELD_CUSTOM_META]` (0xFD).

### 4.2 LXMF Envelope

The LXMF adapter uses `LxmfFieldsHelper` to embed/extract a structured envelope under `FIELD_CUSTOM_META` (0xFD):

- Contains provenance data only: event IDs, adapter names, relation metadata.
- No private keys or secrets are ever embedded.
- This is a MEDRE convention. Other LXMF clients will see the field but may ignore it.
- The adapter does not validate or enforce schema conformance within the envelope.

### 4.3 Asymmetry

Only LXMF and Matrix support metadata embedding in outbound messages. Meshtastic and MeshCore have payload limits that preclude structured metadata (228 and 184 bytes respectively). This means:

- A message that carries MEDRE provenance metadata when routed through LXMF or Matrix will lose that metadata when routed through Meshtastic or MeshCore.
- The pipeline does not guarantee metadata round-tripping across transports.
- Metadata is best-effort, not contractual.

## 5. Boundary Observations

### 5.1 Runtime/Core Must Not Know Transport Types

Confirmed by inspection:

- `src/medre/core/` never imports from any adapter package.
- `CanonicalEvent` fields are all standard library types (`str`, `datetime`, `dict`, `tuple`).
- `EventMetadata` structs are defined in core with no transport-specific imports.
- Adapter codecs produce canonical events from transport-native data. The reverse direction (renderer) produces transport-native payloads from canonical events.

This boundary is clean. No violations observed.

### 5.2 No Canonical Redesign Needed

The existing canonical event model handles all four transport families without structural changes:

- `source_transport_id` is a string — works for MXIDs, node numbers, pubkeys, and identity hashes.
- `source_channel_id` is optional — works for Matrix rooms, radio channels, and LXMF (which has no channel concept).
- `NativeMessageRef` carries transport-specific IDs as strings — works for all ID types.
- `EventRelation` handles replies where the transport supports them, and degrades gracefully where it doesn't.

No canonical redesign is warranted based on this audit.

### 5.3 What Cannot Be Normalized

Confirmed from `docs/contracts/22-delivery-semantics-matrix.md`:

1. **Delivery confirmation semantics**: sync HTTP (Matrix) vs async radio ACK (Meshtastic/MeshCore) vs store-and-forward (LXMF). These are fundamentally different reliability models.
2. **Message ordering**: only Matrix has server-assigned ordering. All others are unordered.
3. **Persistence**: Matrix and LXMF persist. Meshtastic and MeshCore are ephemeral.
4. **Reply rendering**: only Matrix and Meshtastic have native reply mechanisms.
5. **Native ID semantics**: globally unique (Matrix) vs session-scoped (Meshtastic) vs content-addressed (LXMF) vs collision-prone (MeshCore).
6. **Payload limits**: ~100KB (Matrix) vs ~228B (Meshtastic) vs 184B (MeshCore) vs multi-KB (LXMF).

Code that treats these as equivalent is incorrect by contract.

## 6. Gaps and Observations

### 6.1 No Enrichment Stage

There is currently no metadata enrichment or normalization stage. Raw transport metadata goes into `metadata.native.data` and stays there. No code moves fields from `native` to more specific namespaces (`radio`, `telemetry`) at runtime.

The Meshtastic and MeshCore codecs populate `metadata.radio` at decode time. The LXMF session normalises inbound `LXMessage` objects into plain dicts at the session boundary (`_normalise_inbound_message`), converting SDK types (bytes hashes, raw fields, method enums) into plain strings/bools/dicts before they reach the adapter or codec. This is codec/session-level normalization, not a separate enrichment stage. Other adapters could do the same but there is no shared framework for it.

### 6.2 Diagnostics Are Per-Adapter

Each adapter that implements `diagnostics()` returns a different shape:

- Meshtastic: `MeshtasticSessionDiagnostics` (typed dataclass).
- MeshCore: `dict[str, Any]` with known keys.
- Matrix: `MatrixSessionDiagnostics` (typed dataclass).
- LXMF: `LxmfSessionDiagnostics` (frozen dataclass, 12 fields including `connected`, `router_running`, `reconnecting`, `reconnect_attempts`, `known_path_count`, `propagation_enabled`, `pending_delivery_count`, failure counts). Also exposes `delivery_state_counts()` for outbound state tracking.

There is no cross-adapter diagnostics normalization. Each adapter's diagnostics are self-contained.

### 6.3 No Cross-Transport Metadata Correlation

There is no mechanism to correlate metadata across transports. If the same conceptual message is delivered via Matrix and then forwarded to LXMF, the two canonical events share a `lineage` tuple but their metadata envelopes are independent. No merge, no dedup, no reconciliation.

This is correct behavior for Phase 1. Cross-transport metadata correlation is a future concern.

## 7. Summary

| Observation                                                                         | Implication                                                                                             |
| ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| Transport-rich metadata stays in namespaced envelopes                               | Clean separation. Core never knows about LXMF/RNS types.                                                |
| Canonical events remain transport-agnostic                                          | No canonical redesign needed for four-transport support.                                                |
| Runtime/core must not import adapter packages                                       | Boundary is clean. No violations observed.                                                              |
| No enrichment stage exists                                                          | Raw metadata stays in `native`. No runtime normalization.                                               |
| Metadata embedding is transport-dependent (LXMF/Matrix yes, Meshtastic/MeshCore no) | Metadata round-tripping is best-effort, not contractual.                                                |
| Diagnostics shapes differ per adapter                                               | No cross-adapter diagnostics normalization.                                                             |
| Delivery semantics are fundamentally asymmetric                                     | No false equivalence. Code that assumes ordered/confirmed delivery will fail on constrained transports. |
| No canonical redesign warranted                                                     | The existing model handles all four transports structurally.                                            |

---

_This document was produced by auditing the metadata flow through all four adapter families. It documents observations, not prescriptions. No code changes are proposed._

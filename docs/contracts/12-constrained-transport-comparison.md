# 12: Constrained Transport Comparison

**Status:** Draft  
**Scope:** MEDRE adapter families, protocol neutrality audit

## Overview

MEDRE supports three adapter families. Matrix is a **presentation adapter**: rich message model, HTTP/JSON transport, designed for human-facing UI. Meshtastic and MeshCore are **constrained transport adapters**: low-bandwidth radio links with tight payload limits, designed for off-grid messaging over LoRa.

This note compares them across the dimensions that matter for MEDRE's abstraction layer, and flags which parts of that layer are genuinely protocol-neutral versus which parts carry accidental Meshtastic assumptions.

## Comparison Table

| Dimension | Matrix | Meshtastic | MeshCore |
|---|---|---|---|
| Role | Presentation | Transport/Radio | Transport/Radio |
| Identity | MXID (`@user:server.org`) | NodeNum (int) + fromId (str) | Ed25519 pubkey (32B hex) |
| Channels | Room ID string | Channel index (0-7) | Channel index (0-7) + encrypted |
| Message ID | Event ID string | Packet ID int | Sender timestamp int |
| Wire format | JSON events | Protobuf | Custom binary |
| Reply mechanism | `m.in_reply_to` | `replyId` int | None native |
| Payload limit | ~100 KB | ~228 bytes | 184 bytes |
| Encryption | Homeserver TLS | Optional per-packet | Always-on E2EE |
| ACK model | Sync `/sync` confirm | Async ROUTING_APP | Async ACK event + CRC |
| Send returns | Event ID string | MeshPacket protobuf | Event + expected_ack + timeout |

## Identity and Addressing

All three identity models reduce to strings at the MEDRE boundary. `source_transport_id` carries MXIDs, Meshtastic node numbers, and Ed25519 pubkeys equally well. The IdentityResolver stores native-to-canonical mappings without caring what the native ID looks like.

This abstraction is **protocol-neutral**. Nothing about it assumes Meshtastic's integer node numbers or Matrix's user@domain syntax.

MeshCore's contact-based addressing (send to a specific pubkey or flood) is a minor wrinkle. Meshtastic also supports directed sends, but MeshCore makes it more central to the protocol. Since MEDRE's routing layer already distinguishes between directed and broadcast delivery, this fits without changes.

## Relation and Reply Semantics

Here's where the first accidental assumption shows up.

Matrix has native replies via `m.relates_to` / `m.in_reply_to`. Meshtastic has `replyId` at the packet layer. Both of these are protocol-level constructs that MEDRE's `EventRelation` model captures cleanly through `target_native_ref`.

MeshCore has **no native reply mechanism**. If two MeshCore devices want to express a reply relationship, they have to do it at the application layer, something like quoting the original text or embedding a convention in the message body.

MEDRE's `EventRelation` model with its `target_native_ref` field assumes the protocol itself can carry a reply reference. That's true for Matrix and Meshtastic but not for MeshCore. This is an **accidental Meshtastic assumption**: the abstraction was shaped by protocols that have native reply IDs, and it doesn't account for protocols where reply semantics are purely application-level.

For MeshCore, the adapter would need to synthesize `target_native_ref` from application conventions (e.g., parsing `> quoted text` patterns) or simply not populate relation fields. The adapter contract should explicitly mark relation support as **capability-gated** so MeshCore can declare "no native replies" without violating the contract.

## Pacing and Queue Ownership

All three adapters need send pacing. The adapter-owned queue pattern works for all of them, but the constraints differ wildly.

Meshtastic's firmware asks for roughly 0.5 seconds between packets. MeshCore's firmware minimum delay is stricter at around 2 seconds. Matrix has no per-message rate limit worth worrying about at typical meshnet scale.

The queue being adapter-owned means each adapter sets its own pacing. This is **protocol-neutral** by design. No shared pacing constant or assumption leaks between adapters.

## Metadata Richness

Matrix provides the richest metadata: room context, sender profiles, edit history, read receipts. Meshtastic contributes radio-layer metadata: SNR, RSSI, hop limit. MeshCore contributes contact metadata: `adv_name`, path info, CRC codes.

The `NativeMetadata.data` dict swallows all of these without structural assumptions. This is **protocol-neutral**.

## Payload Constraints

Matrix allows messages up to roughly 100 KB. Meshtastic caps at around 228 bytes. MeshCore caps at 184 bytes.

The `max_text_bytes` / `max_text_chars` capabilities declaration handles this cleanly. Each adapter declares its limit at registration time, and the routing/planning layer respects it. This is **protocol-neutral**.

## Send Results

Each adapter returns something different from a send operation:

- **Matrix:** event ID string from nio
- **Meshtastic:** full MeshPacket protobuf with an `id` field
- **MeshCore:** Event object containing `expected_ack` bytes and `suggested_timeout`

MEDRE wraps all three in `AdapterDeliveryResult(native_message_id=str, native_channel_id=str)`. This works as long as each adapter handles its own native ID extraction internally. Matrix pulls the event ID. Meshtastic pulls the packet ID from the protobuf. MeshCore would use the sender timestamp as the message ID.

This is **protocol-neutral**, with one caveat: MeshCore's `suggested_timeout` and `expected_ack` fields carry delivery expectation metadata that the other two adapters don't produce. If MEDRE ever needs to expose delivery timeouts upstream, the `AdapterDeliveryResult` model may need an optional field for this. Not a problem for tranche 1.

## ACK and Delivery Confirmation

The three ACK models are fundamentally different:

- **Matrix:** synchronous confirmation through `/sync` polling
- **Meshtastic:** asynchronous ROUTING_APP ACK, separate from the send call
- **MeshCore:** asynchronous ACK event with CRC code, also separate from send

MEDRE doesn't track delivery confirmation in tranche 1. When it does, the abstraction should probably be event-based (adapter emits a delivery status event) rather than request-based (caller asks "was this delivered?"). All three models map cleanly to an event-based approach. This is an open design question, not a neutrality problem.

## Conclusions

**Protocol-neutral abstractions (safe as-is):**

- `source_transport_id` as a string
- IdentityResolver native-to-canonical mapping
- `NativeMetadata.data` dict
- `max_text_bytes` / `max_text_chars` capability declarations
- Adapter-owned pacing queues
- `AdapterDeliveryResult` with adapter-internal ID extraction
- `AdapterRole` enum distinguishing presentation from transport

**Accidentally Meshtastic-shaped abstractions (need attention):**

- `EventRelation.target_native_ref` assumes the protocol carries a reply reference. MeshCore has no such mechanism. Relations should be capability-gated so adapters can declare "no native replies" without violating the contract.
- Any implicit assumption that `native_message_id` is always a single scalar value. MeshCore's send result bundles `expected_ack` and `suggested_timeout` alongside the timestamp ID. The adapter can extract the timestamp as the ID, but future delivery tracking may need the richer structure.

The short version: MEDRE's core abstractions are solid and genuinely protocol-neutral. The one real leak is in relation semantics, where the model assumes a protocol-level reply reference exists. Making relations capability-gated fixes this without breaking anything for Matrix or Meshtastic.

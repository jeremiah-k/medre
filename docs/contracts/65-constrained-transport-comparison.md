# 65: Constrained Transport Comparison

**Status:** Draft  
**Scope:** MEDRE adapter families, protocol neutrality audit

## Overview

MEDRE supports three adapter families. Matrix is a **presentation adapter**: rich message model, HTTP/JSON transport, designed for human-facing UI. Meshtastic and MeshCore are **constrained transport adapters**: low-bandwidth radio links with tight payload limits, designed for off-grid messaging over LoRa.

This note compares them across the dimensions that matter for MEDRE's abstraction layer, and flags which parts of that layer are genuinely protocol-neutral versus which parts carry accidental Meshtastic assumptions.

## Comparison Table

| Dimension                | Matrix                    | Meshtastic                              | MeshCore                        | LXMF                            |
| ------------------------ | ------------------------- | --------------------------------------- | ------------------------------- | ------------------------------- |
| Role                     | Presentation              | Transport/Radio                         | Transport/Radio                 | Transport/Radio                 |
| Identity                 | MXID (`@user:server.org`) | NodeNum (int) + fromId (str)            | Ed25519 pubkey (32B hex)        | Reticulum LXMF destination hash |
| Channels                 | Room ID string            | Channel index (0-7)                     | Channel index (0-7) + encrypted | Propagation node addressing     |
| Message ID               | Event ID string           | Packet ID int                           | Sender timestamp int            | LXMF message hash               |
| Wire format              | JSON events               | Protobuf                                | Custom binary                   | Reticulum binary                |
| Reply mechanism          | `m.in_reply_to`           | `replyId` int                           | None native                     | None native                     |
| Payload limit            | ~100 KB                   | ~227 bytes (configurable, target-aware) | 184 bytes                       | Variable (link-dependent)       |
| Encryption               | Homeserver TLS            | Optional per-packet                     | Always-on E2EE                  | Reticulum link-layer            |
| ACK model                | Sync `/sync` confirm      | Async ROUTING_APP                       | Async ACK event + CRC           | Link-level ACK                  |
| Send returns             | Event ID string           | MeshPacket protobuf                     | Event + expected_ack + timeout  | Delivery status                 |
| Startup backlog suppress | Excluded (sync semantics) | Implemented (rxTime-based, best-effort) | Deferred (no backlog)           | Deferred                        |

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

All three adapters need send pacing, but the current implementations differ.

Meshtastic uses an adapter-owned outbound queue (`MeshtasticOutboundQueue`) that spaces sends with a configurable delay (`message_delay_seconds`, default 0.5s). Messages are enqueued and processed by a queue worker. Transient local SDK send failures are retried from the adapter-local queue up to `queue_send_max_attempts` times. Queue overflow is explicit: when full, new enqueues are rejected with a transient error. Queue stats (depth, enqueued, sent, failed, rejected, requeued, exhausted, max attempts) are visible in diagnostics. Being queued or locally accepted means the local node accepted the packet, not that RF transmission or remote reception occurred. Retry is best-effort, adapter-local, in-memory, non-durable across process restart, and not exactly-once.

MeshCore currently sends directly through the session to the SDK client without an intermediary queue. The adapter accepts a `message_delay_seconds` config field as a reserved pacing parameter, but it is not enforced at send time. When implemented, pacing will introduce a minimum delay between outbound sends. There is no queue, no retry, and no requeue. A successful send means local node acceptance, not mesh delivery, ACK receipt, or RF confirmation.

Matrix has no per-message rate limit worth worrying about at typical meshnet scale. MEDRE does not model Matrix rate-limit headers or adaptive transport backoff as runtime policy. M_LIMIT_EXCEEDED / HTTP 429 responses are classified as transient and retried with bounded backoff.

The pacing architecture being adapter-owned means each adapter controls its own send timing. This is **protocol-neutral** by design. No shared pacing constant or assumption leaks between adapters.

## Metadata Richness

Matrix provides the richest metadata: room context, sender profiles, edit history, read receipts. Meshtastic contributes radio-layer metadata: SNR, RSSI, hop limit. MeshCore contributes contact metadata: `adv_name`, path info, CRC codes.

The `NativeMetadata.data` dict swallows all of these without structural assumptions. This is **protocol-neutral**.

## Payload Constraints

Matrix allows messages up to roughly 100 KB. Meshtastic caps at around 227 bytes (configurable via `max_text_bytes`). MeshCore caps at 184 bytes.

The `max_text_bytes` / `max_text_chars` capabilities declaration handles this cleanly. Each adapter declares its limit at registration time, and the routing/planning layer respects it. This is **protocol-neutral**.

Meshtastic rendering is **target-adapter aware**: when multiple Meshtastic adapters are configured with different `max_text_bytes`, `radio_relay_prefix`, or `meshnet_name` values, the renderer resolves the target adapter's config at render time so each radio receives correctly-budgeted text.

## Send Results

Each adapter returns something different from a send operation:

- **Matrix:** event ID string from nio
- **Meshtastic:** full MeshPacket protobuf with an `id` field
- **MeshCore:** Event object containing `expected_ack` bytes and `suggested_timeout`

MEDRE wraps all three in `AdapterDeliveryResult(native_message_id=str, native_channel_id=str)`. This works as long as each adapter handles its own native ID extraction internally. Matrix pulls the event ID. Meshtastic pulls the packet ID from the protobuf. MeshCore would use the sender timestamp as the message ID.

This is **protocol-neutral**, with one caveat: MeshCore's `suggested_timeout` and `expected_ack` fields carry delivery expectation metadata that the other two adapters don't produce. If MEDRE ever needs to expose delivery timeouts upstream, the `AdapterDeliveryResult` model may need an optional field for this. Not a problem in the current scope.

## ACK and Delivery Confirmation

The three ACK models are fundamentally different:

- **Matrix:** synchronous confirmation through `/sync` polling
- **Meshtastic:** asynchronous ROUTING_APP ACK, separate from the send call
- **MeshCore:** asynchronous ACK event with CRC code, also separate from send

MEDRE doesn't track delivery confirmation in the current implementation. When it does, the abstraction should probably be event-based (adapter emits a delivery status event) rather than request-based (caller asks "was this delivered?"). All three models map cleanly to an event-based approach. This is an open design question, not a neutrality problem.

## Direct Message Semantics

`direct_messages=False` in MeshCore's adapter capabilities means MEDRE does not model explicit outbound DM targeting. It does not mean the transport cannot relay inbound PRIV (private) packets. The MeshCore packet classifier tags PRIV packets with `is_direct_message=True` and relays them through the same pipeline as channel messages. This is relay, not DM initiation. Future contributors should not "fix" the apparent contradiction by blocking PRIV relay or toggling `direct_messages` to `True` without understanding this distinction. Inline notes in `adapter.py` (near `_MESHCORE_CAPS_BASE`) and `packet_classifier.py` (near the DM TODO) reinforce this.

## Startup Backlog Suppression Semantics

When a constrained transport adapter starts, it may receive a burst of packets that accumulated before the adapter connected. Startup backlog suppression is a best-effort mechanism to drop stale packets from this initial flood. It is timestamp-based, session-scoped, and in-memory only. It is **not** cryptographic replay prevention, not durable across restarts, not distributed dedup, and not exactly-once delivery.

The four transports differ significantly in whether suppression is possible or appropriate:

**Meshtastic: implemented (first-class, best-effort).** The adapter wires `startup_backlog_suppress_seconds` (default 5.0s) to ingress pre-decode stale packet suppression using the packet's `rxTime` field where available. Packets whose `rxTime` predates the adapter's startup window are suppressed before canonical event creation. Missing or malformed timestamps are passed through conservatively (no fake precision injected). Suppressed packets do not create canonical events or delivery/evidence receipts. Suppression counters are in-memory diagnostics only, reset on process restart.

**MeshCore: deferred (not implemented).** MeshCore has no message history, no store-and-forward, and no initial sync. When the adapter connects, events arrive live from the SDK's event dispatcher; there is no backlog to suppress. The `sender_timestamp` field is sender-side and unverified. Attempting timestamp-based suppression on live events would risk dropping fresh packets, not stale ones. If MeshCore later gains store-and-forward or an initial backlog sync mechanism, this decision should be revisited.

**Matrix: excluded.** Matrix uses a separate sync model (`/sync` with tokens). Backlog handling is governed by the homeserver's sync semantics and the client's sync token, not by a startup suppression window. Matrix does not need startup backlog suppression because the sync protocol already handles message ordering and gap detection.

**LXMF: deferred.** LXMF/Reticulum does not provide reliable receive-time timestamps in a form suitable for backlog suppression. No equivalent `startup_backlog_suppress_seconds` config field is currently present or wired in the LXMF adapter. Implementation is deferred until LXMF's delivery and timestamp semantics are better understood through live testing.

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

---

## Current-State Resolution

**Status:** Stabilized (Tracks 1, 3, 4)
**Date:** 2026-05-08

### What was stabilized

The platform identity audit (documented in the companion file `12-adapter-platform-identity.md`) identified three concrete problems with how adapter identity, renderer selection, and cross-transport semantics interacted. All three are now resolved or mitigated.

**Renderer selection uses platform identity.** The `RenderingPipeline` maintains an `adapter_platforms` registry that maps adapter IDs to platform names (e.g., `"local-radio"` to `"meshtastic"`). At render time, the pipeline resolves the platform from the registry and passes it to each renderer's `can_render()` as `target_platform`. Renderers match on the platform string directly, independent of the adapter instance name. This breaks the coupling between adapter naming conventions and renderer dispatch.

**Adapter-name prefixes and known_adapters are now fallback tiers only.** The three-tier `can_render()` dispatch (platform match, then prefix match, then known_adapters set) exists with the platform registry as the primary tier. In production paths where the registry is populated, the prefix and known_adapters fallbacks are never reached. Tests that don't populate the registry still work via the fallback tiers. No existing test broke during this change.

**Transport-family semantic differences are now documented.** Section 6 of the companion audit covers message graph richness, reply semantics, native ref types, actor identity, addressing models, delivery expectations, constrained payloads, and pacing ownership across all three adapter families. The capability-gated relation model for MeshCore is identified as the one real abstraction leak.

### Remaining identity pressure for planned updates

The audit documents four identity categories that are currently conflated into `source_transport_id` and `NativeMetadata.data`:

1. **Transport-local identity** (MXID, node number, pubkey) carried as untyped strings
2. **Canonical actor identity** via `IdentityResolver`, which works but ties resolution to adapter instances rather than platforms
3. **Cryptographic identity** (MeshCore Ed25519), which exists in the protocol but MEDRE ignores, leaving all actors as `UNVERIFIED`
4. **Presentation identity** (display names, avatars), captured at observation time but never updated

These aren't problems today. They're documented as pressure points that will need attention when cross-protocol identity linking or cryptographic verification become requirements.

### Three-transport diversity is sufficient

With Matrix (presentation), Meshtastic (constrained transport), and MeshCore (constrained transport) all implemented, the architecture has enough diversity to validate its protocol-neutral claims. The abstractions that are genuinely protocol-neutral hold across all three. The abstractions that leak (relation semantics, single-scalar native_message_id) are now identified and documented.

Adding a fourth transport (LXMF, MQTT, AX.25) would stress-test the same seams but wouldn't reveal new categories of problems. The three-transport coverage is enough to be confident in the architecture's neutrality bounds.

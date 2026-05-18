# Radio Transport Limitations Contract

> Contract version: 1
> Last updated: 2026-05-10
> Track: Pre-Beta Blocker Burn-Down (Track 8)
> Supersedes: Nothing. Extracts and formalizes radio limitations from contract 33.
> Status: Contract. Documents inherent radio transport limitations.

This document explicitly states the fire-and-forget delivery model of MEDRE's radio transports (Meshtastic, MeshCore, LXMF). These are inherent properties of the radio protocols and SDKs, not bugs in MEDRE. The purpose is to prevent misinterpretation of `AdapterDeliveryResult` values for these transports.

## 1. Core Statement

**Meshtastic, MeshCore, and LXMF transports do not guarantee end-to-end delivery confirmation.** An outbound `deliver()` call that returns `AdapterDeliveryResult(success=True)` confirms only that the message was handed off to the local radio or router layer. It does **not** mean the message was received by any remote party.

This is an honest model. MEDRE reports what it knows (local handoff succeeded) and does not pretend to know what it cannot verify (remote receipt).

## 2. Per-Transport Behavior

### 2.1 Meshtastic

- `sendText` returns a `MeshPacket.id` from the local radio firmware.
- This confirms the packet was transmitted over LoRa from the local node.
- **No end-to-end delivery confirmation exists in the Meshtastic protocol** for text messages.
- ACKs are at the LoRa link level (hop-by-hop), not end-to-end.
- Multi-hop delivery adds additional uncertainty: the message may be in transit for seconds to minutes.
- The session retries transient failures up to 3 times, increasing duplicate-send risk.
- **`AdapterDeliveryResult.success=True` means "local radio accepted the packet."**

### 2.2 MeshCore

- `send_text` returns a link-level confirmation from the local radio.
- No end-to-end delivery confirmation.
- E2EE is at the radio level; MEDRE does not manage keys.
- The session retries transient failures up to 3 times.
- **`AdapterDeliveryResult.success=True` means "local radio accepted the packet."**

### 2.3 LXMF

- `handle_outbound` returns immediately with the message in `OUTBOUND` state.
- Actual delivery is asynchronous via the LXMRouter and Reticulum transport.
- The LXMRouter fires delivery callbacks when state changes (`OUTBOUND → SENDING → SENT → DELIVERED` or `FAILED`).
- Multi-hop Reticulum transport can introduce seconds to hours of delivery latency.
- Propagated messages have no delivery time guarantee — they wait at a propagation node until the recipient connects.
- MEDRE does not currently observe or surface the LXMF delivery state progression.
- **`AdapterDeliveryResult.success=True` means "message was handed to the LXMRouter."**

## 3. AdapterDeliveryResult Semantics

The `AdapterDeliveryResult` type has a `success` field and a `native_message_id` field. For radio transports:

| Field               | Meaning for radio transports                                                                                |
| ------------------- | ----------------------------------------------------------------------------------------------------------- |
| `success=True`      | Local handoff succeeded. The message was accepted by the local radio, SDK, or router.                       |
| `success=False`     | Local handoff failed. The message was not transmitted.                                                      |
| `native_message_id` | Transport-specific local identifier (Meshtastic packet ID, MeshCore link ID, LXMF message hash), or `None`. |

**Neither field implies end-to-end delivery.** This is true for all radio transports. Only Matrix provides server-side delivery confirmation (homeserver persists the event and returns an event_id).

## 4. Why This Is Not a Bug

1. **Protocol limitation.** LoRa-based radio protocols (Meshtastic, MeshCore) operate on shared, unreliable RF links. End-to-end acknowledgment would require application-level confirmation from every intended recipient, which the protocols do not mandate for text messages.

2. **Network topology.** LXMF operates over Reticulum, which supports multi-hop, store-and-forward, and intermittently-connected networks. Delivery times are inherently unbounded.

3. **Honest reporting.** MEDRE could implement synthetic acknowledgment layers, but that would be lying about what the transport actually confirms. The current model reports the truth: "I handed it to the radio" or "I handed it to the router."

4. **Consumers must handle uncertainty.** Applications built on MEDRE must be designed for eventual delivery or no delivery. This is the same contract that the underlying radio protocols provide.

## 5. Relationship to Failure Taxonomy

This contract supplements `docs/contracts/33-failure-taxonomy.md`, which provides detailed per-transport failure classification. The failure taxonomy records specific failure modes (transient vs. permanent, reconnectable, duplicate-send risk). This document states the general principle: radio transports are fire-and-forget at the MEDRE adapter level.

## 6. Implications for Consumers

1. **Idempotent handlers.** Because Meshtastic and MeshCore have high/medium duplicate-send risk, consumers should design message handlers to be idempotent. Processing the same message twice must be safe.

2. **No delivery assumption.** Do not build workflows that assume a `success=True` result means the message was received. It means it was sent.

3. **Eventual consistency.** For LXMF, delivery state may progress asynchronously. MEDRE currently does not surface this state progression. Consumers that need delivery confirmation must implement application-level acknowledgment.

4. **Matrix is the exception.** Matrix is the only transport that provides server-side delivery confirmation. If your use case requires confirmed delivery, use Matrix.

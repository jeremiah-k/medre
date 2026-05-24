# Production Connectivity Roadmap

> **Status:** Planning
> **Classification:** Planning
> **Authority:** Incremental roadmap for first real network operation; no timelines committed
> **Last reviewed:** 2026-05-24
>
> Contract version: 1
> Last updated: 2026-05-09
> Track: 9 (Transport Capability Contracts)
> Supersedes: Nothing. Complements contracts 16, 18, 19, 20.
> Status: Planning document. No production connectivity is claimed or implemented.

This document outlines a conservative, incremental roadmap for achieving first real network operation on each of MEDRE's four transports. It is deliberately cautious. Each step assumes the previous step passed. No step is assumed to work until someone runs it against real hardware or a real service and confirms the result.

This is a roadmap, not a commitment. Timelines are not specified. Dependencies are called out. Risk areas are highlighted. The goal is to provide enough structure that someone attempting first real connectivity knows what to try, in what order, and what to watch for.

## 1. Scope

- Recommended rollout order for first real operation on each transport.
- Per-transport risk areas and known unknowns.
- Replay and diagnostic requirements for validating real connectivity.
- Test harness requirements.
- Constrained-network concerns.

## 2. Non-goals

- Setting dates or deadlines.
- Implementing production connectivity in this tranche.
- Expanding Matrix features beyond text and replies.
- Implementing real Meshtastic, MeshCore, or LXMF networking.
- Production deployment, scaling, or operations guidance.

## 3. Recommended Rollout Order

The order is driven by three factors: (1) how much real client code already exists, (2) how easy it is to set up a test environment, and (3) how much risk the transport's semantics introduce.

### 3.1 Phase A: Matrix (Lowest Risk)

**Why first:** Matrix has the most real client code in the adapter. The `start()`, `deliver()`, and `stop()` methods use real nio calls. An optional live smoke harness already exists. A test Matrix homeserver is trivially self-hosted (Synapse or Conduit in Docker). No hardware is required.

**Steps:**

1. **Verify the existing live smoke harness.** Run `tests/test_matrix_live.py` against a real homeserver. Confirm `start()` connects, `deliver()` produces a real `event_id`, `health_check()` transitions correctly, `stop()` cleans up. This harness already exists; the step is to run it and confirm it still passes.
2. **Verify inbound reception.** Send a message from a second Matrix account into the monitored room. Confirm the sync callback fires, the codec produces a canonical event, and `publish_inbound()` is called. This requires the second account setup documented in the live smoke runbook.
3. **Verify self-message suppression with real sync echoes.** After `deliver()` sends a message, the sync loop will echo it back. Confirm the suppression logic (sender check + envelope check) filters the echo correctly.
4. **Verify reply threading.** Send a reply to a previous message. Confirm `m.in_reply_to` is set correctly on the outbound message and the inbound codec extracts the relation.
5. **Sustained operation.** Run for 24 hours with real traffic. Monitor health state transitions, delivery receipt recording, and sync loop stability.

**Risk areas:**

- `mindroom-nio` is a fork. Its maintenance status relative to upstream `matrix-nio` is unknown. If it diverges, the adapter may need to migrate.
- Access token handling is bare. No rotation, no secure storage. Adequate for testing, not for production.
- Sync loop error handling under real network conditions (timeouts, reconnects, rate limiting) is untested.

### 3.2 Phase B: Meshtastic (Medium Risk)

**Why second:** Meshtastic has real client code structure but no live harness. A test environment requires at minimum one Meshtastic device (a real radio or the Meshtastic native simulator). The SDK is well-documented and the protocol is relatively simple.

**Steps:**

1. **Create a live smoke harness.** Modeled on the Matrix harness. Skipped by default, enabled by environment variables. Tests `start()`, `deliver()`, `health_check()`, `stop()` against a real Meshtastic device or simulator.
2. **Verify connection establishment.** Confirm the adapter connects via TCP or serial to a real device. Verify health transitions. Verify `stop()` disconnects cleanly.
3. **Verify outbound delivery.** Send a text message from MEDRE to the Meshtastic device. Confirm the message appears on the device's screen or log. Confirm the adapter returns an `AdapterDeliveryResult` with the correct `native_message_id`.
4. **Verify inbound reception.** Send a text message from the Meshtastic device. Confirm the adapter's listener callback fires, the codec produces a canonical event, and `publish_inbound()` is called.
5. **Verify pacing.** Send multiple messages in rapid succession. Confirm the `MeshtasticOutboundQueue` enforces the configured inter-message delay. Confirm no messages are lost due to radio contention.
6. **Verify ACK handling.** Send a message with ACK requested. Confirm the adapter correctly processes the ACK or timeout. (This may require directed messages rather than broadcast.)

**Risk areas:**

- The SDK's async behavior under real radio conditions (interference, range limits, duty cycle enforcement) is unknown.
- Meshtastic's protobuf format may vary between firmware versions. The codec must be tested against the firmware version in use.
- BLE connectivity is notoriously unreliable. TCP or serial is recommended for initial testing.
- Payload size limits (228 bytes) require the renderer to truncate or split. Verify that the renderer respects this limit with real data.

### 3.3 Phase C: MeshCore (Medium-High Risk)

**Why third:** MeshCore's SDK is async-native and well-structured, but MeshCore is a newer protocol with a smaller community. Less external documentation and fewer known failure modes. ACK-driven delivery adds a timing dimension that Meshtastic (which defaults to fire-and-forget broadcast) does not have.

**Steps:**

1. **Create a live smoke harness.** Same pattern as Matrix and Meshtastic.
2. **Verify connection establishment.** Connect via TCP, serial, or BLE to a real MeshCore device. Verify health transitions.
3. **Verify outbound delivery with ACK.** Send a directed message. Confirm the ACK arrives within the expected timeout. Confirm `deliver()` returns after ACK or timeout.
4. **Verify flood delivery.** Send a flood message. Confirm delivery without waiting for a specific ACK.
5. **Verify inbound reception.** Receive a message from another MeshCore device. Confirm codec processing and canonical event creation.
6. **Verify channel-based messaging.** Send and receive on a specific encrypted channel. Confirm channel key handling.

**Risk areas:**

- MeshCore's E2EE is always-on. If the key exchange fails silently, messages will be undecryptable without a clear error.
- The 184-byte payload limit is tighter than Meshtastic's 228 bytes. The renderer must handle this.
- The `meshcore` SDK is at version 2.2.5. API stability is not guaranteed.
- BLE, serial, and TCP transports may have different reliability characteristics. Test all three that you plan to use.

### 3.4 Phase D: LXMF (Highest Risk)

**Why last:** LXMF operates over Reticulum, which is a multi-hop mesh network. Setting up a test environment requires at minimum two Reticulum nodes, possibly a propagation node, and understanding of Reticulum's address announcement system. The two-package dependency (`lxmf` plus `rns`) adds surface area. LXMF's store-and-forward model introduces timing and ordering uncertainties that the other three transports do not have.

**Steps:**

1. **Create a live smoke harness.** Same pattern.
2. **Verify Reticulum initialization.** Start a Reticulum instance with a test config. Create a local `RNS.Identity`. Confirm identity creation and hash derivation.
3. **Verify LXMF router setup.** Create an `LXMF.LXMFRouter` with the Reticulum instance. Confirm the router starts and can announce itself.
4. **Verify outbound message delivery.** Create an LXMF message, send it to a known destination. Confirm delivery via propagation node or direct delivery.
5. **Verify inbound message reception.** Receive an LXMF message from another node. Confirm codec processing and canonical event creation.
6. **Verify store-and-forward.** Send a message to a node that is offline. Confirm the message is stored at the propagation node. Confirm the message is delivered when the destination node comes online.
7. **Verify fields metadata.** Send a message with `FIELD_CUSTOM_META` (0xFD) in the fields dict. Confirm the MEDRE envelope survives round-trip.

**Risk areas:**

- Reticulum's multi-hop routing introduces variable and potentially high latency. Messages may arrive seconds, minutes, or hours after sending.
- The propagation node model is centralized in practice (a single propagation node serving a local network). If the propagation node is down, store-and-forward breaks.
- LXMF's content-addressed message IDs mean that modifying a message changes its identity. This is different from the other three transports.
- Reticulum is a single-maintainer project (Mark Qvist). Its long-term maintenance trajectory is uncertain.
- The `lxmf` and `rns` packages have not been tested in MEDRE's CI. Their behavior under concurrent load is unknown.

## 4. Replay and Diagnostic Requirements

Each phase requires diagnostic tooling to validate that real connectivity works correctly.

### 4.1 What Must Be Replayable

- Every real outbound delivery must produce a delivery receipt with `native_message_id`.
- Every real inbound event must produce a canonical event in storage with `source_transport_id` and `source_native_ref`.
- The receipt and event must be queryable after the fact.

### 4.2 Diagnostic Queries

| Question                       | Query                                                                          |
| ------------------------------ | ------------------------------------------------------------------------------ |
| Did my message arrive?         | `list_receipts_for_plan(plan_id, adapter_id)` → check latest receipt status    |
| What did the transport report? | Receipt's `native_message_id` and `native_channel_id`                          |
| Was the event stored?          | `storage.get(event_id)`                                                        |
| What are the native refs?      | `storage.resolve_native_ref(adapter_id, native_channel_id, native_message_id)` |
| What is the lineage?           | Event's `lineage` tuple + `parent_event_id`                                    |
| What relations exist?          | `storage.list_relations(event_id)`                                             |

### 4.3 Replay Requirements

The existing replay system (Contract 07) must work with real events:

- `BEST_EFFORT` replay mode must re-deliver events without requiring original timing or ordering.
- Replay must not re-send events that were already successfully delivered (idempotent check on receipts).
- Replay must produce new receipts for re-delivered events with incremented `attempt_number`.

These requirements are already in the replay contract. Verifying them with real data is part of each connectivity phase.

## 5. Test Harness Requirements

Each transport's live smoke harness must follow the same pattern:

### 5.1 Common Requirements

| Requirement              | Description                                             |
| ------------------------ | ------------------------------------------------------- |
| Skipped by default       | No environment variables set = harness skipped in CI    |
| Enabled by env vars      | `MEDRE_LIVE_MATRIX=1`, `MEDRE_LIVE_MESHTASTIC=1`, etc.  |
| Order-independent        | Each test function can run standalone                   |
| Cleanup guaranteed       | Adapter `stop()` is always called, even on test failure |
| No leaked tasks          | Verify zero orphaned asyncio tasks after `stop()`       |
| Deterministic assertions | Check specific fields, not "something was returned"     |
| Timeout-bounded          | Every async operation has a timeout to prevent hangs    |

### 5.2 Per-Transport Harness Requirements

| Transport  | Additional Requirements                                                                                              |
| ---------- | -------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Requires: homeserver URL, access token, test room ID. Already implemented at `tests/test_matrix_live.py`.            |
| Meshtastic | Requires: connection type (TCP/serial/BLE), device address. Must verify paced send timing.                           |
| MeshCore   | Requires: connection type (TCP/serial/BLE), device address. Must verify ACK timing.                                  |
| LXMF       | Requires: Reticulum config path, identity file, test destination hash. Must verify propagation timing (may be slow). |

### 5.3 Runbook Requirements

Each live smoke harness must have a companion runbook (like `docs/runbooks/matrix-live-smoke.md`) that documents:

- Prerequisites (SDK installed, hardware/service available, credentials configured).
- Environment variables to set.
- Expected output and how to interpret failures.
- Known limitations (e.g., Matrix inbound reception requires a second account).

## 6. Constrained-Network Concerns

### 6.1 Meshtastic and MeshCore

These two transports share similar constraints: LoRa radio links with duty cycle limits, small payloads, and no persistence.

**Specific concerns:**

| Concern                | Impact                                                              | Mitigation                                                 |
| ---------------------- | ------------------------------------------------------------------- | ---------------------------------------------------------- |
| Payload truncation     | Messages longer than 228/184 bytes will be truncated or fail        | Renderer must enforce length limits before delivery        |
| Duty cycle enforcement | Radio firmware may silently drop messages if duty cycle is exceeded | Adapter pacing must respect configured inter-message delay |
| Radio contention       | Multiple senders on the same channel may collide                    | No MEDRE mitigation; this is a transport-layer concern     |
| Range limits           | Messages beyond radio range are lost                                | No MEDRE mitigation; physical deployment concern           |
| Battery impact         | Frequent sending drains device batteries                            | Pacing and batching reduce impact; operator responsibility |

### 6.2 LXMF

LXMF operates over Reticulum, which can use multiple physical layers (LoRa, WiFi, serial, TCP). The concerns are different from direct radio:

| Concern                       | Impact                                                            | Mitigation                                                               |
| ----------------------------- | ----------------------------------------------------------------- | ------------------------------------------------------------------------ |
| Multi-hop latency             | Messages may take seconds to hours depending on network topology  | Operator must set realistic expectations; MEDRE cannot speed up the mesh |
| Propagation node availability | Store-and-forward breaks if propagation node is down              | Operator must ensure propagation node reliability                        |
| Resource transfer size        | Large LXMF messages are split into Reticulum resources            | Adapter must handle multi-part assembly; timeout must be generous        |
| Identity discovery            | New nodes must announce before they can receive directed messages | Operator must ensure announcements are sent and propagated               |

### 6.3 General Guidance

- **Test with real data sizes.** Fixture data in fake adapters is typically short and well-behaved. Real messages may be longer, contain Unicode, or have unexpected formatting.
- **Test with real timing.** Fake adapters return instantly. Real transports have latency. Verify that the pipeline handles slow `deliver()` returns without blocking other adapters.
- **Test with real failures.** Disconnect hardware during operation. Verify that health state transitions correctly and that delivery receipts record failures.
- **Test with real volumes.** Send more messages than you expect in production. Verify that queues, pacing, and receipts behave correctly under load.

## 7. What This Roadmap Does NOT Promise

1. **No production deployment timeline.** This document describes an order of operations, not a schedule.
2. **No guarantee that any phase will pass on first attempt.** Real hardware and real networks are unpredictable. Each phase may reveal issues that require adapter or pipeline changes.
3. **No commitment to all four transports.** An operator may choose to deploy only Matrix and Meshtastic, skipping MeshCore and LXMF entirely. The roadmap respects that choice.
4. **No scheduler implementation.** The retry scheduler discussed in Contracts 21 and 22 is a prerequisite for sustained production operation, but it is not part of this roadmap.
5. **No feature expansion.** This roadmap covers first real text message operation. Reactions, media, E2EE, admin APIs, and rich content are all out of scope.

## 8. Implications

### 8.1 For Developers

- Start with Matrix. It has the most existing code and the easiest test environment.
- Write live smoke harnesses before attempting any sustained real operation. The harness is your safety net.
- Respect constrained-network limits in renderers. A renderer that produces a 500-byte message for Meshtastic is a bug, not a limitation.

### 8.2 For Operators

- Plan hardware and service access before starting any phase. Matrix needs a homeserver. Meshtastic needs a radio. MeshCore needs a radio. LXMF needs a Reticulum network.
- Budget time for each phase. Real connectivity debugging takes longer than deterministic testing.
- Monitor health state, delivery receipts, and native refs. These are your primary diagnostic signals during real operation.

### 8.3 For Reviewers

- Any PR claiming real connectivity must include a live smoke harness or extend an existing one.
- Any PR claiming real connectivity must document what was tested, against what hardware or service, and what was not tested.
- The phase-1-limitations.md document must be updated to reflect any new capabilities or known gaps discovered during real connectivity testing.

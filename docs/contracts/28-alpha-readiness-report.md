# Cross-Transport Alpha Readiness Report

> **Status:** Assessment
> **Classification:** Assessment
> **Authority:** Cross-transport alpha status assessment consolidating contracts 16–28; not normative
> **Last reviewed:** 2026-05-24
>
> Contract version: 1
> Last updated: 2026-05-09
> Track: 9 (Transport Capability Contracts)
> Supersedes: Nothing. Consolidates contracts 16, 18, 19, 20, 21, 22, 24, 25, 26, 27.
> Status: Alpha readiness assessment. No production connectivity claimed.

This document provides a cross-transport assessment of MEDRE's four adapter families at the point where all four have completed their tranche 1 implementation and audit. It records what each transport can do in alpha, what remains before beta readiness, recommended next implementation order, risks, and architectural findings.

This is an audit/contract-hardening tranche report, not a feature expansion plan. No new transports, admin APIs, webhooks, plugin redesigns, bridge policies, runtime redesigns, reactions, media handling, or deployment tooling are proposed.

## 1. Scope

- Per-transport alpha status: what is implemented, what is tested, what is live-validated.
- Remaining blockers before each transport can be considered beta.
- Recommended next implementation order based on risk and readiness.
- Cross-transport architectural findings from the diagnostics consistency audit (contract 27).
- Recommended next tranche of work.

## 2. Non-goals

- Claiming production connectivity for any adapter.
- Proposing new transports, admin APIs, webhooks, or plugin systems.
- Redesigning the runtime, bridge policy, or adapter abstraction layer.
- Implementing reactions, media, attachments, or deployment tooling.
- Over-normalizing transport semantics that are genuinely different.

## 3. Per-Transport Alpha Status

### 3.1 Matrix — Alpha (Most Complete)

| Dimension         | Status                                                                                               |
| ----------------- | ---------------------------------------------------------------------------------------------------- |
| Codec             | Implemented. Converts nio-shaped events to `CanonicalEvent`. No nio import in codec.                 |
| Renderer          | Implemented. Builds `m.room.message` with reply threading and metadata envelope.                     |
| Session           | Implemented (682 LOC). Owns nio `AsyncClient` lifecycle. Sync recovery with bounded reconnect.       |
| Adapter           | Implemented. Full `start/stop/deliver/health_check/diagnostics`.                                     |
| E2EE              | Implemented (Track 5). Text E2EE active in encrypted rooms. `mindroom-nio[e2e]` optional dependency. |
| Outbound delivery | Real `room_send` with bounded retry (3 attempts). Returns `event_id`.                                |
| Inbound reception | Real sync loop with callback. Self-message suppression. Room allowlist.                              |
| Diagnostics       | Rich: E2EE state, room encryption counts, sync recovery, crypto store continuity.                    |
| Live harness      | `test_matrix_live.py` (12 tests, 834 LOC). `test_matrix_e2ee_live.py` (4 tests, 306 LOC).            |
| Runbooks          | `matrix-live-smoke.md`, `matrix-alpha-operation.md`.                                                 |
| Fake mode         | Full `FakeMatrixAdapter` for deterministic testing.                                                  |

**What Matrix can do in alpha:**

- Connect to a real homeserver, send/receive text and replies in plaintext and encrypted rooms.
- Track room encryption state, report undecryptable events, persist crypto store across restarts.
- Suppress self-messages and origin-tagged echoes.
- Report rich diagnostics with no secret leakage.

**What Matrix cannot do yet:**

- Reactions, edits, deletes, attachments, media.
- Multi-device key verification or cross-signing.
- Rate limiting or backpressure handling.
- Confirmed inbound reception from third-party users in live test.

### 3.2 Meshtastic — Alpha (Constrained Transport Baseline)

| Dimension         | Status                                                                            |
| ----------------- | --------------------------------------------------------------------------------- |
| Codec             | Implemented. Converts packet dicts to `CanonicalEvent` with `replyId` extraction. |
| Renderer          | Implemented. Builds text payloads for constrained radio.                          |
| Session           | Implemented (608 LOC). Owns Meshtastic interface lifecycle. Bounded reconnect.    |
| Adapter           | Implemented. Full `start/stop/deliver/health_check/diagnostics`.                  |
| Outbound delivery | Via `MeshtasticOutboundQueue` with pacing. Returns `None` (async queue).          |
| Inbound reception | Pubsub callback with packet classification. ACK filtering.                        |
| Diagnostics       | Adapter-level with queue stats and session sub-dict.                              |
| Live harness      | `test_meshtastic_live.py` (10 tests, 616 LOC). Raw API + adapter tests.           |
| Runbooks          | `meshtastic-live-smoke.md`, `meshtastic-alpha-operation.md`.                      |
| Fake mode         | Full `FakeMeshtasticAdapter` with deterministic packet IDs.                       |

**What Meshtastic can do in alpha:**

- Connect to a real radio node via TCP/serial/BLE (in live harness).
- Send text messages with channel selection and pacing.
- Receive and classify packets by portnum.
- Track queue depth, send counts, and failure counters.

**What Meshtastic cannot do yet:**

- Confirmed delivery (fire-and-forget, optional ACK not de-duplicated).
- Inbound DM processing.
- Backlog suppression on reconnect.
- Reliable multi-hop routing.
- Rich metadata in outbound messages (228-byte limit).

### 3.3 MeshCore — Alpha (Constrained Transport, Simplest Session)

| Dimension         | Status                                                                        |
| ----------------- | ----------------------------------------------------------------------------- |
| Codec             | Implemented. Converts SDK event dicts to `CanonicalEvent`.                    |
| Renderer          | Implemented. Builds text payloads with channel/contact selection.             |
| Session           | Implemented (654 LOC). Owns MeshCore SDK client lifecycle. Bounded reconnect. |
| Adapter           | Implemented. Full `start/stop/deliver/health_check/diagnostics`.              |
| Outbound delivery | Direct `send_text()` with retry (3 attempts). Returns native ID.              |
| Inbound reception | Subscribe callback with event classification.                                 |
| Diagnostics       | Session-level dict with peer count.                                           |
| Live harness      | `test_meshcore_live.py` (8 tests, 401 LOC).                                   |
| Runbooks          | `meshcore-live-smoke.md`, `meshcore-alpha-operation.md`.                      |
| Fake mode         | Full `FakeMeshCoreAdapter` with deterministic IDs.                            |

**What MeshCore can do in alpha:**

- Connect to a real MeshCore radio node via TCP (live harness).
- Send channel messages and direct messages.
- Receive inbound messages with pubkey prefix identity.
- Track peer count and delivery failure counters.

**What MeshCore cannot do yet:**

- Confirmed delivery (fire-and-forget, ACK not de-duplicated).
- BLE connection mode (constructor exists but untested).
- Rich metadata in outbound messages (payload size constrained).
- E2EE channel support.
- Telemetry or position data.

### 3.4 LXMF — Alpha (Most Complex Delivery Model)

| Dimension         | Status                                                                                    |
| ----------------- | ----------------------------------------------------------------------------------------- |
| Codec             | Implemented. Converts normalised LXMF dicts to `CanonicalEvent`.                          |
| Renderer          | Implemented. Builds content dicts with title, fields, destination, delivery method.       |
| Session           | Implemented (1260 LOC). Owns RNS/LXMF lifecycle. Bounded reconnect. Delivery state model. |
| Adapter           | Implemented. Full `start/stop/deliver/health_check`.                                      |
| Outbound delivery | Real LXMF `send()` with delivery state tracking. Returns native ID + state.               |
| Inbound reception | Delivery callback with normalisation to plain dicts.                                      |
| Diagnostics       | Session-level frozen dataclass with router state, path counts, pending deliveries.        |
| Live harness      | `test_lxmf_live.py` (19 tests, 829 LOC). Most comprehensive fake-mode coverage.           |
| Runbooks          | `lxmf-live-smoke.md`, `lxmf-alpha-operation.md`.                                          |
| Fake mode         | Full `FakeLxmfAdapter` with deterministic IDs and fake delivery state.                    |

**What LXMF can do in alpha:**

- Connect to a real Reticulum instance, load/create identity, create LXMRouter.
- Send real LXMF messages with delivery state progression tracking.
- Receive inbound messages with honest normalisation (no SDK object leakage).
- Track delivery states through 8 discrete states.
- Report diagnostics with path counts, propagation state, and pending delivery counts.

**What LXMF cannot do yet:**

- Verified delivery state progression to "delivered" in live test.
- Multi-hop delivery testing beyond local Reticulum instance.
- Propagation node operation.
- Message field type negotiation.
- Inbound from third party confirmed in live test.

## 4. Remaining Blockers Before Beta

### 4.1 Cross-Transport Blockers (Apply to All)

| Blocker                                                  | Severity | Notes                                                                        |
| -------------------------------------------------------- | -------- | ---------------------------------------------------------------------------- |
| No live test exercised in default CI                     | High     | All live tests require manual opt-in via env vars. No CI pipeline runs them. |
| No reconnect resilience testing                          | Medium   | No live test exercises reconnection under real network failure.              |
| No sustained throughput testing                          | Medium   | All live tests are smoke tests. No load/stress testing exists.               |
| No delivery receipt pipeline tested against real network | Medium   | The retry/dead-letter system is unit-tested but not live-validated.          |
| No multi-transport integration test                      | Low      | No test exercises two transports simultaneously.                             |

### 4.2 Matrix-Specific Blockers

| Blocker                                            | Severity | Notes                                                                      |
| -------------------------------------------------- | -------- | -------------------------------------------------------------------------- |
| Access token is plain string in config             | Medium   | No secure storage, rotation, or refresh mechanism.                         |
| `mindroom-nio` fork maintenance                    | Medium   | Unverified relative to upstream `matrix-nio`.                              |
| No confirmed inbound from third party in live test | Medium   | Sync loop starts but no test confirms real event flowing through callback. |
| No rate limiting or backpressure                   | Low      | Homeserver may rate-limit under load.                                      |

### 4.3 Meshtastic-Specific Blockers

| Blocker                                            | Severity | Notes                                                 |
| -------------------------------------------------- | -------- | ----------------------------------------------------- |
| `deliver()` returns `None` (queued, not confirmed) | Medium   | Pipeline cannot correlate delivery receipt with send. |
| No confirmed delivery (ACK not de-duplicated)      | Medium   | Duplicate-send risk documented but not mitigated.     |
| No inbound DM support                              | Low      | Classified as deferred.                               |
| No backlog suppression on reconnect                | Low      | Reconnect may replay already-processed messages.      |

### 4.4 MeshCore-Specific Blockers

| Blocker                                       | Severity | Notes                                  |
| --------------------------------------------- | -------- | -------------------------------------- |
| No confirmed delivery (ACK not de-duplicated) | Medium   | Same as Meshtastic.                    |
| BLE connection untested                       | Low      | Constructor exists, not validated.     |
| `meshcore` SDK is not widely deployed         | Medium   | Smaller community, less field testing. |

### 4.5 LXMF-Specific Blockers

| Blocker                                       | Severity | Notes                                                                                         |
| --------------------------------------------- | -------- | --------------------------------------------------------------------------------------------- |
| Delivery state progression not live-validated | Medium   | State model is implemented but progression to "delivered" is not confirmed with real traffic. |
| Identity file is a 64-byte private key        | Medium   | No secure storage mechanism. File must be protected.                                          |
| No propagation node testing                   | Low      | Only direct delivery tested.                                                                  |
| Reticulum network availability                | Low      | Requires a local or network-accessible Reticulum transport.                                   |

## 5. Recommended Next Implementation Order

The recommended order is based on: (1) existing live harness maturity, (2) transport complexity, (3) risk reduction potential, (4) dependency on other work.

### Phase A: Live Validation of Existing Code (No New Features)

1. **Run Matrix live harness** against a real homeserver. Verify existing tests pass. This is the lowest-risk first step.
2. **Run Meshtastic live harness** against a real radio node. Verify existing tests pass.
3. **Run MeshCore live harness** against a real radio node. Verify existing tests pass.
4. **Run LXMF live harness** against a real Reticulum instance. Verify existing tests pass.
5. **Run Matrix E2EE live harness** with encrypted room. Verify crypto lifecycle.

Phase A proves nothing new. It confirms that existing code works against real endpoints. Every adapter already has live harness code. The step is to run it and record results.

### Phase B: Gap Closure (Small, Focused Work)

1. **Add Matrix inbound reception test** to the live harness. Send from a second account, verify `publish_inbound()` fires.
2. **Add Meshtastic delivery result** plumbing. Currently returns `None`. If the SDK provides a packet ID on send, return it.
3. **Add reconnect resilience test** for at least Matrix. Kill the homeserver connection, verify reconnect succeeds.
4. **Add LXMF delivery state progression test** with real traffic. Verify state transitions from "outbound" through "delivered".

### Phase C: Hardening (If Phase B Passes)

1. Add delivery receipt pipeline validation against real network.
2. Add sustained throughput smoke test for at least Matrix.
3. Document token/identity secure storage recommendations.
4. Consider CI integration for Matrix (only transport not requiring hardware).

## 6. Risks by Transport

### 6.1 Matrix Risks

| Risk                                       | Likelihood | Impact                        | Mitigation                                                  |
| ------------------------------------------ | ---------- | ----------------------------- | ----------------------------------------------------------- |
| `mindroom-nio` fork diverges from upstream | Medium     | High — breaking API changes   | Pin version, monitor upstream                               |
| E2EE key state corruption                  | Low        | High — undecryptable messages | Crypto store backup, `undecryptable_event_count` diagnostic |
| Homeserver rate limiting                   | Medium     | Low — transient failures      | Bounded retry already implemented                           |
| Access token exposure                      | Medium     | High — account compromise     | Env var only, never logged. Future: secure store.           |

### 6.2 Meshtastic Risks

| Risk                          | Likelihood | Impact                            | Mitigation                                            |
| ----------------------------- | ---------- | --------------------------------- | ----------------------------------------------------- |
| Radio duty cycle saturation   | High       | Medium — message loss             | Outbound queue with pacing                            |
| Duplicate messages from retry | Medium     | Low — consumer handles duplicates | Documented; consumer responsibility                   |
| Firmware incompatibility      | Medium     | Medium — API shape changes        | `mtjk` library version pin                            |
| 228-byte payload limit        | Known      | Medium — content truncation       | Renderer strips metadata; honest lossiness documented |

### 6.3 MeshCore Risks

| Risk                                    | Likelihood | Impact                            | Mitigation                                  |
| --------------------------------------- | ---------- | --------------------------------- | ------------------------------------------- |
| SDK maturity (v2.2.5, small community)  | Medium     | High — API instability            | Pin version; test against multiple versions |
| Radio constraints similar to Meshtastic | High       | Medium — message loss             | Documented; same constraints apply          |
| BLE connection instability              | Medium     | Low — fallback to TCP/serial      | TCP recommended for production              |
| Duplicate messages from retry           | Medium     | Low — consumer handles duplicates | Documented; consumer responsibility         |

### 6.4 LXMF Risks

| Risk                                    | Likelihood | Impact                    | Mitigation                                   |
| --------------------------------------- | ---------- | ------------------------- | -------------------------------------------- |
| Reticulum network unavailability        | Medium     | High — no transport       | Local Reticulum instance required            |
| Multi-hop delivery latency (hours/days) | Known      | Low — eventual delivery   | Honest "pending" state; async delivery model |
| Identity file compromise                | Medium     | High — impersonation      | File permissions; future secure store        |
| LXMF/RNS API instability                | Low        | Medium — breaking changes | Version pin; SDK source audited              |

## 7. Cross-Transport Architectural Findings

### 7.1 Session Pattern Is Consistent

All four sessions follow the same lifecycle shape (start/stop/diagnostics), own their callbacks, own their reconnect, and provide safe diagnostics. The pattern is stable and does not need refactoring.

### 7.2 Diagnostics Safety Is Uniform

All four adapters guarantee no secrets, no SDK objects, no protobuf in diagnostics. This is enforced by frozen dataclasses (Matrix, Meshtastic, LXMF) or plain dict copies (MeshCore).

### 7.3 Metadata Namespacing Is Clean

All transport metadata is namespaced under `metadata.native.data[<transport>]`. No loose ad-hoc fields. No SDK object leakage. The codec boundary is clean.

### 7.4 Delivery Semantics Are Honestly Different

Matrix confirms synchronously. Meshtastic queues asynchronously. MeshCore sends directly. LXMF tracks async state progression. These differences are genuine, documented, and should not be normalized away.

### 7.5 Duplicate-Send Risk Is Universal

All four adapters have bounded retry with acknowledged duplicate-send risk. This is a fundamental property of at-least-once delivery, not a bug. Consumers must be duplicate-tolerant.

### 7.6 LXMF Session Complexity Is an Outlier

At 1260 LOC, `LxmfSession` is roughly 2x the size of the other sessions. The complexity is driven by the honest delivery state model (8 states, outbound tracking, state callbacks). This is an inherent transport property. A future refactor could extract delivery tracking, but it is not blocking.

## 8. Recommended Next Tranche

### 8.1 Tranche Scope: Live Validation + Contract Hardening

The next tranche should focus on **running existing live harnesses** and **closing small documentation gaps**. No new features, no runtime redesign, no adapter abstraction changes.

| Item                                                           | Effort  | Risk Reduction                                 |
| -------------------------------------------------------------- | ------- | ---------------------------------------------- |
| Run Matrix live harness against real homeserver                | Small   | High — confirms most mature adapter            |
| Run Meshtastic live harness against real node                  | Small   | High — confirms constrained transport baseline |
| Run MeshCore live harness against real node                    | Small   | Medium — confirms simplest session             |
| Run LXMF live harness against real Reticulum                   | Small   | Medium — confirms most complex session         |
| Add Matrix inbound reception live test                         | Small   | Medium — closes the biggest Matrix gap         |
| Document "diagnostics not authoritative state" in all adapters | Trivial | Low — explicit contract language               |
| Record live test results in runbooks                           | Small   | Low — audit trail                              |

### 8.2 Explicitly Out of Scope for Next Tranche

- No new transports.
- No admin APIs, webhooks, or HTTP servers.
- No plugin system or bridge policy changes.
- No reactions, media, attachments, or edits.
- No deployment tooling, scaling, or operations infrastructure.
- No runtime-level reconnect orchestration or retry scheduler.
- No adapter abstraction refactoring or transport normalization.

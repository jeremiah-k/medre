# Transport Limitations

Cross-transport limitation summary, inherent constraints, and known gaps.

---

## 1. Core Limitations (All Transports)

1. **No exactly-once delivery.** Messages can be lost, duplicated, or dropped
   at any stage. Adapter-level delivery receipts exist and are persisted in
   storage, but there is no end-to-end exactly-once guarantee. The delivery
   pipeline is at-least-once with duplicate suppression on inbound native refs.

2. **No dead-letter admin UI.** Dead-lettered receipts are recorded in storage
   but there is no dedicated CLI command or UI for browsing, replaying, or
   managing dead-lettered events. Operators can inspect them via `medre inspect`
   or evidence bundles.

3. **Local delivery outbox is durable but not exactly-once.** The outbox
   persists pending, retry, and dead-lettered items across process restart.
   Crash timing may cause resend. No end-to-end tracking (no RF confirmation,
   no ACK, no remote receipt).

4. **Runtime capacity control exists; transport-aware rate limiting is
   incomplete.** The runtime enforces a configurable max-inflight-delivery
   limit. Meshtastic has bounded adapter-local outbound queue retry. Matrix
   M_LIMIT_EXCEEDED responses are classified as transient. Full adaptive
   transport backoff as runtime policy is not yet implemented.

5. **Graceful shutdown is bounded, not fully durable.** On stop, the runtime
   waits up to `limits.shutdown_drain_timeout_seconds` for in-flight delivery
   to drain. Work still inside adapter SDK sync loops or adapter-local queues
   may be abandoned after the drain timeout.

6. **No inbound persistence.** Inbound events are published directly to the
   pipeline. If the pipeline is slow or fails, the event is gone. No retry,
   no redelivery at the inbound stage.

7. **No structured logging.** All log output is format-string based. No trace
   IDs, no correlation across events, no structured fields.

8. **No metrics export.** Diagnostics counters exist in memory but there is no
   Prometheus endpoint, no statsd, no external export.

9. **Single-operator only.** Everything is tested and documented for a single
   person on a single machine. Multi-node, multi-operator, and deployment
   scenarios do not exist.

## 2. Transport-Specific Limitations

### 2.1 Matrix

- Multi-room concurrent inbound has not been tested against a real homeserver.
- E2EE text messaging does not support reactions, edits, media, cross-signing, or
  key backup.
- `mindroom-nio` is a fork of `matrix-nio`. Its maintenance cadence relative to
  upstream is unverified.
- `restore_login()` does not validate the token against the server at startup.
  An invalid token is only discovered on the first sync response (HTTP 401).

### 2.2 Meshtastic

- Inbound processing is text messages only. Telemetry, position, and nodeinfo
  portnum types are not processed inbound.
- `mtjk` is a fork of the upstream Meshtastic Python library (version 2.7.8.post2+).
- `sendText` and `sendData` are synchronous in mtjk; MEDRE wraps them in
  `asyncio.to_thread()`.
- Pubsub callbacks fire on a background thread, not the asyncio event loop.
- Node numbers are ephemeral; a node that leaves and rejoins may receive a
  different number.

### 2.3 MeshCore

- SDK findings are based on source extraction (version 2.3.7). BLE
  session-layer behavior was live-validated June 2026 against a MeshCore
  node on Linux BlueZ. TCP and serial transports are source-extracted
  only; no live hardware test has been run against them. BLE requires
  pre-pairing and is subject to BlueZ stack limitations (stale device
  cleanup and pre-scan before connect).
- No native reply mechanism. Relations are capability-gated via `CapabilityDecisionResolver`; unsupported relation types produce `capability_suppressed` delivery outcomes.
- No startup backlog suppression (intentionally absent: MeshCore has no
  store-and-forward).
- Sender identity is a 6-byte pubkey prefix (not globally unique).

### 2.4 LXMF

- Multi-hop mesh delivery is not tested.
- E2EE beyond Reticulum's native link-layer encryption is not in scope.
- The session does not retry outbound sends automatically.
- Delivery confirmation is asynchronous and may never arrive.
- Propagated messages have no delivery time guarantee.

## 3. Fire-and-Forget Model

Meshtastic, MeshCore, and LXMF do not guarantee end-to-end delivery
confirmation. An outbound `deliver()` call that returns success confirms only
that the message was handed off to the local radio or router layer. It does not
mean the message was received by any remote party.

| Transport  | `success=True` means                                  |
| ---------- | ----------------------------------------------------- |
| Meshtastic | Local radio accepted the packet.                      |
| MeshCore   | Local radio accepted the packet.                      |
| LXMF       | Message was handed to the LXMRouter.                  |
| Matrix     | Homeserver persisted the event and returned event_id. |

This is an honest model. MEDRE reports what it knows (local handoff succeeded)
and does not pretend to know what it cannot verify (remote receipt).

## 4. Startup Backlog Suppression

| Transport  | Status      | Notes                                                                                                |
| ---------- | ----------- | ---------------------------------------------------------------------------------------------------- |
| Meshtastic | Implemented | `startup_backlog_suppress_seconds` (default 5.0s), `rxTime`-based, best-effort                       |
| MeshCore   | Deferred    | No message history, no store-and-forward. Suppressing live events would risk dropping fresh packets. |
| Matrix     | Excluded    | Sync protocol handles message ordering and gap detection.                                            |
| LXMF       | Deferred    | No reliable receive-time timestamps suitable for suppression.                                        |

## 5. Protocol-Neutral Abstractions

The following abstractions are genuinely transport-neutral:

- `source_transport_id` as a string
- `NativeMetadata.data` dict
- `max_text_bytes` / `max_text_chars` capability declarations
- Adapter-owned pacing queues
- `AdapterDeliveryResult` with adapter-internal ID extraction
- `AdapterRole` enum
- `IdentityResolver` native-to-canonical mapping

The following carry accidental assumptions from protocols with native reply
mechanisms:

- `EventRelation.target_native_ref` assumes the protocol carries a reply
  reference (true for Matrix and Meshtastic, false for MeshCore). Relations
  are capability-gated via `CapabilityDecisionResolver`; adapters that lack
  native support for a relation type produce `capability_suppressed` delivery
  outcomes.

## 6. Capability Semantics Known Gaps

1. **Fallback capability level is dormant in production transport profiles.** No production transport profile (Matrix, Meshtastic, MeshCore, LXMF) currently declares a three-level string capability field at `"fallback"`. The fallback rendering path (`"fallback_text"` strategy) is tested with synthetic configurations but has no R-tier evidence from a live transport. See Routing and Delivery Specification § 6.3.2.

2. **No hardware or live validation of capability suppression.** All capability suppression, fallback rendering, and budget enforcement tests use fake adapters and synthetic capability configurations. No test exercises capability gating against a real transport endpoint.

3. **RE_RENDER replay mode does not reconstruct full capability-aware rendering context.** The `RE_RENDER` mode re-runs rendering through the pipeline but does not reconstruct `RenderingContext` from stored artifacts. The rendering context used during replay may not match the original context that governed the live render.

4. **Replay pre-filter suppressed evidence is in-memory only.** When replay capability filtering suppresses all plans for an event, the evidence records are carried in the in-memory `ReplayResult` output, not persisted to storage. Process crashes before operator inspection lose this evidence.

5. **Thread relation capability gating is deferred.** No `AdapterCapabilities.threads` field exists. Thread-carrying events receive native/direct delivery when no other capability candidate overrides. This is intentional but means thread relations are never capability-suppressed. See Routing and Delivery Specification § 6.3.6.

6. **`RenderingContext.capability_policy` is reserved and unpopulated.** No test or production code path exercises this field.

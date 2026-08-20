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

5. **Graceful shutdown is bounded, not fully durable outside MEDRE-owned state.**
   Durable-ingress grace and in-flight capacity drain share one
   `limits.shutdown_drain_timeout_seconds` deadline. Work already admitted to
   MEDRE remains persisted, but work still only inside an adapter SDK or
   adapter-local queue can be abandoned when transport-specific shutdown
   guarantees run out.

6. **With runtime storage, durable ingress starts at canonical admission, not at
   raw transport receipt.** When runtime storage is configured, normal live
   callbacks atomically persist the canonical event, inbound native reference, and
   pending work marker before returning. Without runtime storage, the fallback path
   is non-durable and depends on transport checkpoint/recovery semantics. Failures
   before canonical admission likewise depend on transport cursor/ACK/recovery.

7. **Structured correlation is process-local execution context.** Managed JSON
   logs inherit trace/event/route/plan/outbox/receipt correlation fields, but
   MEDRE does not provide distributed tracing or an external log collector.

8. **Metrics export is snapshot-based.** `medre diagnostics --format prometheus`
   emits numeric/boolean diagnostics as Prometheus text, but MEDRE does not run
   a Prometheus HTTP endpoint or statsd exporter.

9. **Single-operator only.** Everything is tested and documented for a single
   person on a single machine. Multi-node, multi-operator, and deployment
   scenarios do not exist.

## 2. Transport-Specific Limitations

### 2.1 Matrix

- Multi-room concurrent inbound has not been tested against a real homeserver.
- Decrypted Matrix ingress normalizes reactions, edits, redactions, and media.
  Outbound edits/deletes/attachments remain unsupported, and MEDRE does not manage
  a room-key backup/import/export workflow.
- Own-device cross-signing is implemented against `mindroom-nio 0.40.0`. Peer-device
  trust is still intentionally permissive for bot operation and is not
  operator-configurable.
- Missing/mismatched local cross-signing identity cannot be repaired destructively at
  runtime; explicit fresh-password authentication is required for reset/rotation.
- `restore_login()` does not validate the token against the server at startup.
  An invalid token is only discovered on the first sync response (HTTP 401).

### 2.2 Meshtastic

- Inbound processing is text messages only. Telemetry, position, and nodeinfo
  portnum types are not processed inbound.
- `mtjk` is the pinned Meshtastic fork at version 2.7.11.post5; a dedicated
  installed-SDK contract tier freezes the private send/protobuf/pubsub surfaces
  MEDRE uses.
- `sendText` and `sendData` are synchronous in mtjk; MEDRE wraps them in
  `asyncio.to_thread()`.
- Pubsub callbacks fire on a background thread, not the asyncio event loop.
- Node numbers are ephemeral; a node that leaves and rejoins may receive a
  different number.

### 2.3 MeshCore

- SDK parity is frozen against meshcore 2.3.8. BLE
  session-layer behavior was live-validated June 2026 against a MeshCore
  node on Linux BlueZ. TCP and serial transports are source-extracted
  only; no live hardware test has been run against them. BLE requires
  pre-pairing and is subject to BlueZ stack limitations (stale device
  cleanup and pre-scan before connect).
- No native reply mechanism. Relations are capability-gated via
  `CapabilityDecisionResolver`; unsupported relation types produce
  `capability_suppressed` delivery outcomes.
- No startup backlog suppression (intentionally absent: MeshCore has no
  store-and-forward).
- Sender identity is a 6-byte pubkey prefix (not globally unique).

### 2.4 LXMF

- Multi-hop mesh delivery is not tested.
- E2EE beyond Reticulum's native link-layer encryption is not in scope.
- The session performs a bounded local retry for transient outbound handoff
  failures; this still does not imply end-to-end delivery.
- `LXMessage.source` is the local `RNS.Destination` returned by
  `register_delivery_identity()`; an `LXMRouter` is not a valid source.
- Delivery confirmation is asynchronous and may never arrive.
- Propagated messages have no delivery time guarantee and require an explicit
  `outbound_propagation_node` destination hash.
- MEDRE shuts down the owned `LXMRouter` but not the process-global Reticulum
  singleton. LXMF has no public join/stop primitive for the router daemon job
  loop, so a stopped router can leave a dormant daemon thread until process
  exit.

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

1. **Thread fallback lacks live endpoint evidence.** Built-in profiles declare
   `threads="fallback"` and exercise the normal planner fallback path, but no R-tier
   live-service/hardware scenario validates degraded thread output end-to-end.
   Native thread emission is intentionally not advertised.

2. **No hardware or live validation of capability suppression.** Most capability
   suppression, fallback rendering, and budget enforcement coverage remains
   synthetic even though the same production planner path is exercised.

3. **Replay same-run suppression is not a concurrency-safe exactly-once primitive.**
   A non-empty replay run ID suppresses targets with durable prior acceptance evidence
   for that same run, plan, and target, but concurrent executions can race before
   either acceptance receipt commits.

4. **Some replay pre-filter evidence remains in-memory only.** Target-level same-run
   duplicate suppression is durable, while capability-filter diagnostics that never
   create a target receipt remain part of the replay result only.

5. **`RenderingContext.capability_policy` is reserved and unpopulated.** No
   production code path currently sets this field.

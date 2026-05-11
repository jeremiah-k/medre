# Bridge Operation Runbook

> Last updated: 2026-05-11
> Scope: Delivery-state discipline for cross-transport bridge operation
> Status: Pre-beta. Not production. Operational model is accurate to code; live bridge validation is not yet complete.

This runbook documents how delivery state works when MEDRE bridges events across transports. It covers what each transport can honestly report, where retry boundaries fall, how the pipeline records results, and what operators should expect when routing events through a multi-transport bridge.


## 1. Core Principle: Adapters Own Transport Delivery

MEDRE separates two concerns:

- **Adapters own transport delivery.** Each adapter owns its connection lifecycle, its retry budget, its reconnect policy, and the truth of what the external system reported back. When an adapter's `deliver()` returns an `AdapterDeliveryResult`, that result contains exactly what the platform returned — a Matrix `event_id`, a Meshtastic packet ID, or nothing if the transport does not confirm. The adapter does not fabricate confirmation that the transport did not provide.

- **The runtime owns routing attribution and orchestration.** The router matches events to routes. The pipeline orchestrates ingress → store → route → plan → deliver → receipt. The runtime records `DeliveryReceipt` objects tracking the progression of each outbound delivery through status states. The runtime never claims final delivery — it records what the adapter reported, honestly.

This boundary is architectural. Nothing outside an adapter touches the transport connection. Nothing inside an adapter decides which events to route where.


## 2. Per-Transport Delivery Semantics

Each transport has fundamentally different delivery guarantees. Operators must understand these differences to interpret receipt states and diagnose delivery issues correctly.

### Matrix

| Property | Value |
|----------|-------|
| Transport type | Persistent async TCP (long-poll or WebSocket sync) |
| Server acknowledgment | Yes — Synapse returns an `event_id` on successful `room_send` |
| Delivery confirmation | Server-level. The message reached the homeserver. Not per-recipient read receipts. |
| Retry semantics | Meaningful. Connection loss is detectable; reconnect and retry will attempt redelivery. |
| Duplicate risk | Low on normal paths. Retries after connection loss may produce duplicates if the first send succeeded but the response was lost. |
| Receipt interpretation | `sent` with a populated `adapter_message_id` means the homeserver accepted the event. This is the strongest confirmation MEDRE can report for any transport. |

Matrix is the only MEDRE transport where `sent` implies server-verified persistence. Even so, this is server-level only — it does not mean any recipient has read the message.

### Meshtastic

| Property | Value |
|----------|-------|
| Transport type | LoRa radio (serial/TCP connection to a local node) |
| Server acknowledgment | None. The local node queues the packet for radio transmission. No mesh-wide ACK exists. |
| Delivery confirmation | None beyond local-node acceptance. Whether any remote node received the packet is unknown. |
| Retry semantics | Limited. The adapter can retry if the local node connection fails, but cannot retry based on remote-node receipt. |
| Duplicate risk | High. Radio environments cause packet loss. Operators routinely send duplicate messages to increase delivery probability. This is by design in LoRa mesh networks. |
| Receipt interpretation | `sent` means the local node accepted the packet for transmission. It does not mean any other node received it. |

Meshtastic delivery is best-effort fire-and-forget at the radio layer. Expect packet loss. Expect to resend. Do not treat `sent` as delivered.

### MeshCore

| Property | Value |
|----------|-------|
| Transport type | MeshCore radio (TCP/serial/BLE connection to a local node) |
| Server acknowledgment | None beyond local-node acceptance. No mesh-wide ACK. |
| Delivery confirmation | None. Same radio best-effort reality as Meshtastic. |
| Retry semantics | Same as Meshtastic — retryable at the local-node connection level, not at the mesh delivery level. |
| Duplicate risk | High. Same radio environment considerations. |
| Receipt interpretation | `sent` means the local node accepted the packet. Nothing more. |

MeshCore and Meshtastic share the same delivery discipline: radio best-effort, no confirmation, duplicates are normal operational reality.

### LXMF (Reticulum)

| Property | Value |
|----------|-------|
| Transport type | Store-and-forward over Reticulum (multi-hop mesh) |
| Server acknowledgment | No single-server ACK. Reticulum uses link-level delivery with propagation delays. |
| Delivery confirmation | Eventual. LXMF messages propagate across the Reticulum network over seconds to hours depending on path length and transport type. |
| Retry semantics | Reticulum handles propagation internally. The adapter delivers to the local `LXMRouter` and trusts the network. Adapter-level retry covers local failures only. |
| Duplicate risk | Low for well-behaved senders. Reticulum's delivery mechanism handles deduplication at the protocol level. |
| Receipt interpretation | `sent` means the local `LXMRouter` accepted the message for propagation. Delivery to the destination may take significant time. Do not assume instantaneous receipt. |

LXMF is the only transport where `sent` means "accepted for eventual delivery" with a potentially long propagation window. The time between `sent` and actual destination receipt can range from seconds to hours depending on network topology.


## 3. Delivery Receipt States

The pipeline records a `DeliveryReceipt` for each outbound delivery attempt. Receipts progress through these states:

```
accepted → queued → sent → confirmed
                  ↘ failed → dead_lettered
```

| Status | Meaning |
|--------|---------|
| `accepted` | Pipeline has accepted the event for delivery. No transport contact yet. |
| `queued` | Delivery plan created, waiting for adapter execution. |
| `sent` | Adapter reported successful handoff to the transport. **This is not final delivery.** See per-transport table above for what `sent` actually means. |
| `confirmed` | Adapter reported positive confirmation from the external system. Only Matrix currently reaches this state. Radio transports never reach `confirmed`. |
| `failed` | Adapter reported a delivery failure. Classified by `DeliveryFailureKind`. |
| `dead_lettered` | Delivery exhausted all retries and fallback strategies. Permanently failed. |

Each receipt carries `attempt_number` and `parent_receipt_id` forming an explicit retry lineage. The first attempt is `attempt_number=1` with `parent_receipt_id=None`. Retries chain through the parent reference.


## 4. Retry Ownership Boundaries

Retry responsibility falls to different components depending on where the failure occurs:

| Failure kind | Who owns the retry | Notes |
|-------------|-------------------|-------|
| `PLANNER_FAILURE` | No retry — permanent | Route or plan misconfiguration. Fix the config. |
| `RENDERER_FAILURE` | No retry — permanent | Deterministic rendering error. Fix the event or renderer. |
| `ADAPTER_TRANSIENT` | Pipeline retry via `RetryPolicy` | Timeout, connection reset, network unreachable. The pipeline schedules retries with exponential backoff up to `max_attempts`. |
| `ADAPTER_PERMANENT` | No retry — permanent | The adapter determined the failure is not recoverable. |
| `TIMEOUT` | Pipeline retry via `RetryPolicy` | Per-attempt timeout exceeded. |
| `DEADLINE_EXCEEDED` | No retry — permanent | The delivery plan's absolute deadline has passed. |

Adapters own their internal reconnect logic (e.g., Matrix sync reconnection, Meshtastic node reconnection). The pipeline owns retry scheduling for transient delivery failures. These are separate mechanisms operating at different layers.


## 5. Duplicate-Send Realities

Duplicate sends are an operational fact in bridge scenarios, not a bug:

- **Radio transports (Meshtastic, MeshCore):** Duplicate sends are expected and often intentional. Packet loss is high in LoRa environments. Operators routinely send the same message multiple times to increase the probability of at least one copy arriving. The bridge does not deduplicate at the radio layer because deduplication is not the bridge's job — it is the application's job on the receiving side.

- **Matrix:** Duplicates are rare but possible when a send succeeds but the response is lost, triggering a retry that sends the same content again. Matrix event IDs will differ for each attempt.

- **LXMF:** Duplicates are low-probability due to Reticulum's protocol-level handling, but store-and-forward semantics mean a late duplicate from a slow propagation path is possible.

- **Bridge fan-out:** When a single inbound event routes to multiple targets (e.g., one Matrix message bridged to both Meshtastic and MeshCore), each target gets an independent delivery. A failure on one target does not affect the other. A success on one target does not guarantee the other.

The runtime does not suppress duplicate sends. It delivers what the routes specify, to the targets the routes specify, and records what happens honestly.


## 6. Runtime Routing and Delivery Honesty

The runtime's routing layer — the `Router` and `RouteEngine` — is a pure in-memory matching engine. It performs no I/O. It matches events against route source specifications and resolves target adapters. It does not know or care about transport delivery semantics.

The pipeline records delivery results honestly:

- If the adapter returns a native message ID, the receipt records it.
- If the adapter returns nothing, the receipt records `sent` without an `adapter_message_id`.
- If the adapter raises, the receipt records `failed` with the error classification.

The runtime never upgrades a receipt state based on assumptions. A `sent` receipt for Meshtastic stays `sent`. It does not become `confirmed` because the runtime has no basis for that claim. This honesty principle is non-negotiable — the receipts must be trustworthy as an audit trail.


## 7. Replay and Route Attribution

The `ReplayEngine` supports re-processing historical events through pipeline stages. Two modes are relevant to bridge delivery state:

| Mode | Route | Deliver | Side effects | Use case |
|------|-------|---------|-------------|----------|
| `RE_ROUTE` | Yes | No | None (read-only) | Re-evaluate which routes match historical events after a route config change. Useful for verifying that new routes would have matched past events. |
| `BEST_EFFORT` | Yes | Yes | Adapter delivery | Re-deliver historical events through current routes and adapters. Use with caution — this produces real outbound messages. |
| `DRY_RUN` | Yes | Skip | None (read-only) | Route and render without actually delivering. Preview what would happen. |

Replay route attribution records which routes matched each historical event. This attribution is metadata about routing decisions, not about delivery outcomes. A route attribution says "this route would have matched" — it does not say "this message was delivered."

**Operational implication:** When re-routing after a config change, use `RE_ROUTE` or `DRY_RUN` first to verify matching behavior. Only use `BEST_EFFORT` when you intend to re-deliver real messages. Re-delivery through `BEST_EFFORT` will produce new outbound messages on all matched targets — including radio transports where duplicates are normal.

**Test coverage note:** The replay pipeline integration path — including route matching, loop prevention via `_filter_replay_loops`, and `ReplayRouteAttribution` — is exercised by `test_replay_pipeline_integration.py` (which tests the real `PipelineRunner` replay path) and `test_replay_routing.py` (which covers route matching through the actual `Router`, `ReplayEngine`, and `_filter_replay_loops` code paths). Boundary tests in `test_architectural_boundaries.py` confirm that replay and routing modules remain free of transport SDK imports. Replay test purity is enforced: no replay test file imports live adapter packages or SDKs.


## 8. Operational Checklist

When operating a multi-transport bridge:

1. **Read receipts in transport context.** A `sent` receipt means different things for Matrix vs. Meshtastic vs. LXMF. Consult the per-transport table in section 2.

2. **Expect radio packet loss.** Meshtastic and MeshCore targets will silently lose messages. This is normal. Monitor `sent` receipt counts, not delivery confirmations that do not exist.

3. **Do not over-retry radio transports.** Retrying a Meshtastic send five times does not guarantee delivery. It increases probability, but each retry adds radio congestion. Tune `RetryPolicy` per transport.

4. **Account for LXMF propagation delay.** An LXMF `sent` receipt does not mean the destination has the message. Do not alert on "sent but no response" for LXMF targets.

5. **Distinguish retry layers.** Adapter reconnect is not the same as pipeline delivery retry. A Meshtastic adapter reconnecting to its local node is independent of the pipeline retrying a failed delivery.

6. **Use replay carefully.** `BEST_EFFORT` replay produces real messages. Always verify route matching with `RE_ROUTE` or `DRY_RUN` first.

7. **Trust receipt lineage.** The `attempt_number` and `parent_receipt_id` chain on receipts provides a complete audit trail. Use it to reconstruct what happened, not to assume what should have happened.


## 9. Route Attribution in Delivery Receipts

Every `DeliveryReceipt` now carries a `route_id` field identifying which route was responsible for the delivery attempt. This provides direct attribution from receipt back to route configuration.

**What this means for operators:**

- When inspecting receipts, the `route_id` field tells you which route triggered this delivery. If `route_id` is empty, the delivery was not routed (e.g., direct adapter-to-adapter delivery without route matching).
- In fan-out scenarios (one event routed to multiple targets), each target's receipt carries the same `route_id`. This lets you group all deliveries from a single route invocation.
- Failed receipts also carry `route_id`. You can query all failed deliveries for a specific route to identify systematic issues.

**Where else attribution appears:**

| Location | Field | Lifecycle |
|----------|-------|-----------|
| `RoutingMetadata.route_trace` | `tuple[str, ...]` | Ephemeral — on the in-flight event after route matching |
| `DeliveryReceipt.route_id` | `str` | Persisted — stored with the receipt in storage |
| `DeliveryOutcome.route_id` | `str` | Ephemeral — pipeline-internal result, not persisted |
| `ReplayRouteAttribution.route_ids` | `tuple[str, ...]` | Replay result only — not persisted to events |

**Attribution does not cross adapter boundaries.** Adapters do not receive or consume route attribution metadata. Attribution is orchestration-layer information for observability and audit.

See: Contract 51 (Route Attribution), Contract 52 (Routed Delivery Result).


## 10. Route Loop Prevention

MEDRE detects and prevents routing loops at multiple layers. This section describes what operators should know about loop behavior in bridge scenarios.

### 10.1 Direct Loop Detection (Startup)

At startup, `check_route_loops` detects two forms of loops in route configuration:

- **Direct loops:** Two routes forming an immediate A↔B cycle (e.g., route X: `bot1 → longfast` and route Y: `longfast → bot1`).
- **Multi-hop cycles:** Routes forming a cycle through three or more adapters via DFS traversal (e.g., `alpha → beta → gamma → alpha`).

Both are logged as warnings. Startup is **not blocked**. The operator should review and fix cycle-inducing routes.

### 10.2 Self-Loop Guard (Runtime, Per-Delivery)

During delivery execution, the pipeline checks each target: if `target_adapter == event.source_adapter`, the delivery is skipped. The outcome records `status="skipped"` with `error="loop_prevented"`. No adapter call is made. `RouteStats` records the prevention.

This guard fires on every delivery attempt. It catches runtime self-loops that configuration-level detection may not prevent (e.g., a bidirectional route where a single adapter appears in both source and destination after expansion).

### 10.3 What Loop Prevention Does Not Cover

- **Cross-instance loops:** If two separate MEDRE instances bridge the same transports in opposite directions, neither instance detects the loop. Loop prevention is local-process only.
- **Application-level loops:** A user on Matrix commanding a bot to send a message to Meshtastic, and a Meshtastic user replying which triggers a message back to Matrix, is not a routing loop — it is normal bidirectional bridge operation. Loop prevention guards against the same event being routed back to its origin adapter, not against new events generated by users.

See: Contract 49 (Routing and Bridge), Routing Correctness Runbook.


## 11. Explicit Non-Guarantees

The bridge operation layer explicitly does **not** provide:

1. **Replay deduplication.** Replay processes events without deduplication. Replayed events may be delivered again if they match current routes.

2. **Exactly-once delivery.** No transport in MEDRE provides exactly-once semantics. Radio transports are probabilistic. Matrix is at-least-once. LXMF is at-least-once with eventual delivery.

3. **Persistent queue.** Delivery state is in-memory only. In-flight deliveries cancelled on shutdown are lost.

4. **Per-adapter restart.** Only full runtime stop/start is supported. Individual adapters cannot be restarted independently.

5. **Distributed coordination.** Delivery state, receipts, and loop prevention are local to the process. There is no shared state between MEDRE instances.

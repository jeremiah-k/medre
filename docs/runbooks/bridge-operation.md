# Bridge Operation Runbook

> Last updated: 2026-05-11
> Scope: Delivery-state discipline for cross-transport bridge operation
> Status: Pre-beta. Not production. Operational model is accurate to code; live bridge validation is not claimed. Docker SDK-boundary bridge tests prove real SDK lifecycle against containerized services.

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

Replay receipts carry `source="replay"` and a populated `replay_run_id` for audit traceability. This distinguishes replay-originated receipts from live deliveries at the storage layer. Traceability supports audit but does not prevent duplicate delivery — multiple BEST_EFFORT replays of the same event produce additional receipt rows, each with a different `replay_run_id`.


## 7a. Docker SDK-Boundary Bridge Validation

Docker SDK-boundary tests prove that real adapter SDKs work against
containerized services (Synapse for Matrix, meshtasticd for Meshtastic).
These tests validate:

- **Real SDK initialization** — adapter code loads and uses real SDK libraries.
- **Config-to-runtime path** — configs with real connection parameters build
  and start correctly.
- **Lifecycle correctness** — start, health check, deliver, stop all work
  through real SDK code paths.
- **SDK boundary integrity** — no SDK objects leak across the adapter boundary
  into diagnostics or snapshots.

Docker SDK-boundary tests do **not** prove live network behavior. Services
run on localhost via Docker containers. See `docs/runbooks/integration-testing.md`
for the full Docker test tier documentation and provenance levels.

The Synapse bridge smoke test (`test_synapse_bridge_smoke.py`) provides the
strongest evidence at this tier: it exercises real nio SDK inbound via sync
loop (with fallback to direct `_on_room_message` if sync does not deliver in
15 seconds), real `MatrixCodec` decode, real `PipelineRunner` routing to a
`FakeMatrixAdapter`, `DeliveryReceipt` persistence with genuine Synapse
event_ids, `NativeMessageRef` inbound mapping, and `RuntimeAccounting`
counter increments — all against a Docker-local Synapse homeserver. The
outbound target is a fake adapter; real cross-transport delivery to a second
real adapter is not proven.

| Provenance tier | Status | What is proven |
|----------------|--------|---------------|
| Fake bridge | **Proven** | Pipeline routing, rendering, receipts, accounting |
| Adapter-wrapper | **Proven** | Per-transport adapter codec, renderer, session |
| Docker SDK-boundary | **Proven** | Real SDK lifecycle, config, dependency resolution |
| Docker SDK-boundary bridge smoke | **Proven** | Real Matrix SDK codec + pipeline routing + storage + accounting with genuine Synapse event_ids |
| Live network | **Not claimed** | No cross-transport bridge test against real endpoints |


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


## 11. Soak Harness and Queue Pressure

### Soak Harness Reference

The soak harness at `tests/test_soak_harness.py` provides a test-only harness for validating bridge stability patterns without live transports. It is **not** a multi-hour CI run — it exercises start/stop cycling, replay cycling, delivery under pressure, and long-running stability within seconds using fake adapters and in-memory storage.

Key characteristics:

- **Fake adapters only.** No real Matrix homeserver, radio, or Reticulum network.
- **In-memory storage.** No filesystem I/O beyond temp directories.
- **Deterministic.** No wall-clock sleeps. Iteration count configurable via `SOAK_HARNESS_ITERATIONS` (default 50, max 200).
- **Validates patterns, not completeness.** The harness verifies that the pipeline correctly routes, delivers, and reports outcomes under repeated cycling. It does not validate that every message reaches its destination (MEDRE does not provide this guarantee).

### Queue Pressure Expectations

When bridging events across transports with different speed profiles (e.g., Matrix → Meshtastic), the pipeline may experience queue pressure:

**Delivery capacity pressure:**

- The `CapacityController` bounds concurrent deliveries to `max_inflight_deliveries` (default 100).
- When the Meshtastic adapter's transport is slow (LoRa PHY, serial write blocking), delivery slots are held longer.
- Other adapters (Matrix, LXMF) compete for the same delivery semaphore pool.
- If delivery acquire times out (`delivery_acquire_timeout_seconds`, default 1.0s), the delivery is permanently failed with `error="delivery_capacity_exceeded"`.

**Meshtastic outbound queue pressure:**

- The Meshtastic adapter's `MeshtasticOutboundQueue` uses a `deque(maxlen=1024)`.
- Under sustained pressure (outbound throughput exceeds send capacity), the oldest items are silently dropped.
- `total_dropped` tracks how many items were shed.
- This is expected behavior for radio transports — the runtime prioritizes stability over completeness.

**Replay pressure:**

- Replay in `BEST_EFFORT` mode acquires a separate replay semaphore (`max_inflight_replay_events`, default 100).
- Replay does not starve real-time delivery — the semaphores are independent.
- If the replay semaphore is exhausted, replay events are rejected with `error="replay_capacity_exceeded"` (or `error="replay_rejected_shutdown"` during runtime shutdown).

### Monitoring Bridge Pressure

During bridge operation, monitor these signals:

| Signal | Source | Interpretation |
|--------|--------|----------------|
| `delivery_timeouts` growing | `CapacityController` | Delivery concurrency is insufficient for the load |
| `total_dropped` growing | Meshtastic adapter | Outbound send rate cannot keep up with inbound rate |
| `replay_timeouts` growing | `CapacityController` | Replay concurrency is insufficient |
| High `delivery_current` sustained | `CapacityController` | Adapters are slow to complete deliveries |

**Remediation:**

- Increase `max_inflight_deliveries` if delivery timeouts are frequent and memory allows.
- Reduce active routes or source event rate if the bridge cannot keep up.
- For Meshtastic specifically, consider whether the channel configuration and radio settings can be optimized for throughput.

**Important:** MEDRE remains best-effort. Queue bounds prevent unbounded accumulation but do not prevent data loss under extreme pressure. No exactly-once guarantees. No transactional delivery guarantees. Radio transports remain probabilistic. The soak harness validates stability patterns for CI — it is not a substitute for operational monitoring with live transports.


## 12. Persistence of Bridge State

Bridge delivery state has a clear persistence boundary. This section describes what bridge operators can rely on and what is ephemeral. For the full contract, see Contract 55 (Runtime Persistence).

### What Persists Across Restarts

- **Delivery receipts** — every receipt written to SQLite survives crash and restart. Receipts include `route_id` attribution, `attempt_number`, retry lineage, and adapter-reported native IDs.
- **Canonical events** — every event that entered the pipeline was stored in SQLite before delivery began. These survive.
- **E2EE sessions** — Matrix crypto keys on disk survive restart. Bridging resumes without re-verification.
- **Logs** — all log entries written before the crash are in `{state}/logs/medre.log`.

### What Does NOT Persist

- **In-flight bridge deliveries** — if the runtime crashes while a Matrix-to-Meshtastic bridge delivery is in progress, the delivery is lost. The source event exists in SQLite (it was stored before delivery), but there is no receipt for the interrupted delivery. The operator cannot distinguish "delivery was attempted but crashed" from "delivery was never attempted."
- **Runtime bridge counters** — `delivery_timeouts`, `delivery_rejections`, per-route delivery counts: all reset to zero on restart. There is no persistent metric store.
- **Active replay deliveries** — if a `BEST_EFFORT` replay was bridging historical events when the crash occurred, the replay run is lost. Completed deliveries from that replay run (those that produced receipts) are preserved. Remaining events must be re-replayed manually.

### Bridge Crash Recovery Example

After a hard crash during active Matrix-to-Meshtastic bridging:

1. Restart the runtime. Both adapters reconnect.
2. Check for orphaned events (stored but not delivered):
   ```sql
   SELECT e.event_id, e.source_adapter, e.created_at
   FROM canonical_events e
   LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
   WHERE r.event_id IS NULL
     AND e.source_adapter = 'bridge'
   ORDER BY e.created_at DESC;
   ```
3. Decide whether to replay the orphaned events. Use `DRY_RUN` first to verify route matching, then `BEST_EFFORT` if re-delivery is warranted.
4. Expect possible duplicate deliveries — replay does not deduplicate.


## 13. Explicit Non-Guarantees

The bridge operation layer explicitly does **not** provide:

1. **Replay deduplication.** Replay processes events without deduplication. Replayed events may be delivered again if they match current routes.

2. **Exactly-once delivery.** No transport in MEDRE provides exactly-once semantics. Radio transports are probabilistic. Matrix is at-least-once. LXMF is at-least-once with eventual delivery.

3. **Persistent queue.** Delivery state is in-memory only. In-flight deliveries cancelled on shutdown are lost.

4. **Per-adapter restart.** Only full runtime stop/start is supported. Individual adapters cannot be restarted independently.

5. **Distributed coordination.** Delivery state, receipts, and loop prevention are local to the process. There is no shared state between MEDRE instances.

6. **Exactly-once or transactional delivery.** MEDRE provides no exactly-once delivery, no transactional delivery guarantees, and no atomic fan-out. Partial delivery in fan-out scenarios is normal.

7. **Queue-bound delivery completeness.** Capacity semaphores and adapter-level queue bounds prevent unbounded memory accumulation but do not guarantee that every message is delivered. Under extreme pressure, messages are dropped or rejected to protect process stability.

8. **Persistent in-flight recovery.** No in-flight delivery state survives shutdown. No replay resume after restart. Cancelled deliveries are lost.

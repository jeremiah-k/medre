# Routing Correctness Runbook

> Last updated: 2026-05-11
> Scope: Operational guide for understanding, verifying, and troubleshooting MEDRE route behavior
> Status: Pre-beta. Not production. Operational model is accurate to code.

This runbook explains how routes work end-to-end, how loop prevention operates, how to reason about delivery outcomes, and how to troubleshoot common routing issues. It is written for operators and developers who need to understand route behavior at runtime.

**Important scoping note:** Routing correctness in this document refers to **local-process** behavior only. MEDRE does not provide distributed loop prevention, distributed consensus, or exactly-once delivery across multiple instances. Radio transports remain probabilistic. See section 8.


## 1. Route Lifecycle

A route progresses through these stages:

### 1.1 Configuration

Routes are defined in TOML under `[routes.<id>]` sections:

```toml
[routes.matrix_to_radio]
source_adapters = ["bot1"]
dest_adapters = ["longfast"]
directionality = "source_to_dest"
enabled = true

[routes.matrix_to_radio.policy]
allowed_event_types = ["message"]
```

Each route declares source adapters, destination adapters, directionality, an optional bridge policy, and an enabled flag. Route IDs must be unique within the configuration.

### 1.2 Validation

At startup, `validate_route_adapter_refs` runs before any route registration. It checks that every enabled route's `source_adapters` and `dest_adapters` reference adapter IDs that exist in the assembled runtime. If any reference is invalid, startup fails with `RouteValidationError`.

Disabled routes (`enabled = false`) are parsed but not validated against adapter IDs.

### 1.3 Registration

After validation passes:

1. `build_runtime_routes` converts TOML configs into core `Route` objects. Bidirectional routes expand into two internal route objects.
2. `check_route_loops` runs DFS cycle detection on the expanded route set (see section 2).
3. Routes register on the `Router` in TOML declaration order.
4. Registration is all-or-nothing: all routes succeed or startup fails.

### 1.4 Matching

At runtime, when an event enters the pipeline:

1. `Router.match(event)` iterates registered routes in registration order.
2. A route matches if the event's `source_adapter` is in the route's source spec.
3. For each matched route, `Router.resolve_targets(event, route)` resolves destination adapters.
4. Matched route IDs are recorded in `RoutingMetadata.route_trace` on the event.

### 1.5 Delivery

For each matched route's targets:

1. **Route-trace loop prevention:** if `route.id` already exists in `event.metadata.routing.route_trace`, the delivery is skipped with `loop_prevented`. This prevents an event from being re-routed through the same route more than once across routing passes (live or replay).
2. **Self-loop guard:** `target_adapter == event.source_adapter` → skip with `loop_prevented`.
3. The event is rendered and delivered to the target adapter.
2. `deliver_to_target` calls the adapter's `deliver()` method.
3. `DeliveryReceipt` is recorded with `route_id` for attribution.
4. `DeliveryOutcome` is produced with status, receipt, and failure classification.
5. `RouteStats` counters are updated per route.

The full lifecycle for a single event through routing:

```
Event arrives → store → route_event() → match routes → populate route_trace
  → execute_route_deliveries() → per target:
    → self-loop check → adapter.deliver() → record receipt → record outcome
```


## 2. Loop Prevention

MEDRE provides three layers of loop prevention, operating at different stages.

### 2.1 Route-Trace Loop Prevention (Runtime, Per-Delivery)

**What it catches:** An event being re-routed through a route whose ID already appears in the event's bounded `route_trace`.

**How it works:** Before any delivery attempt, the pipeline checks whether `route.id` is already in `event.metadata.routing.route_trace`. If found, the delivery is skipped with `status="skipped"` and `error="loop_prevented: route already in route_trace"`.

**Why it matters:** During replay or multi-hop topologies, a route may be matched again for the same event. The bounded trace ensures each route processes the event at most once per trace lifetime (last 16 entries). The bound is enforced by `route_trace` being capped at 16 entries — older entries are dropped when the trace exceeds the limit.

**When it fires:** Every delivery attempt, before the self-loop guard.

**Test coverage:** The real `PipelineRunner` replay path is tested end-to-end in `test_replay_pipeline_integration.py`, which exercises the full pipeline replay flow. Route matching through the actual `Router`, `ReplayEngine`, and `_filter_replay_loops` code paths is covered by `test_replay_routing.py`. Architectural boundary tests in `test_architectural_boundaries.py` verify that neither the replay engine nor the route engine import transport SDKs.

### 2.2 Self-Loop Guard (Runtime, Per-Delivery)

**What it catches:** A route delivering an event back to its own `source_adapter`.

**How it works:** During `execute_route_deliveries`, the pipeline compares `target_adapter` against `event.source_adapter`. If they match, the delivery is skipped with `status="skipped"` and `error="loop_prevented"`.

**When it fires:** Every delivery attempt. This is a runtime guard, not a config-time check.

**Example:** A bidirectional route `matrix_radio_bidir` between `bot1` and `longfast`. An event from `bot1` routed to `longfast` is fine. But if a misconfigured route tried to deliver an event from `bot1` back to `bot1`, the self-loop guard catches it.

### 2.3 Direct Loop Detection (Config-Time, Startup)

**What it catches:** Two routes forming an immediate A↔B loop.

**How it works:** `check_route_loops` builds edge pairs `(source, dest)` from all enabled routes. If both `(A, B)` and `(B, A)` edges exist, a direct loop warning is logged.

**Example:**
```
Route X: bot1 → longfast
Route Y: longfast → bot1
```
This produces: `"Direct routing loop detected between adapters 'bot1' and 'longfast': routes ['X'] and ['Y']"`.

**Effect:** Warning only. Startup is not blocked.

### 2.4 Multi-Hop DFS Cycle Detection (Config-Time, Startup)

**What it catches:** Cycles spanning three or more adapters: X→Y→Z→X.

**How it works:** `check_route_loops` builds a directed adjacency graph of adapter edges and runs DFS with a recursion stack to detect back-edges. When a back-edge is found, the cycle path is extracted and logged.

**Example:**
```
Route A: alpha → beta
Route B: beta → gamma
Route C: gamma → alpha
```
This produces: `"Route cycle detected: alpha -> beta -> gamma -> alpha"`.

**Effect:** Warning only. Startup is not blocked.

### 2.5 Replay Loop Prevention

During replay, `_filter_replay_loops` applies additional filtering:
- Self-loop: route would deliver back to `source_adapter`.
- Previously routed: event's `RoutingMetadata.matched_routes` **or** `route_trace` overlaps with a matched route ID.

Looping routes are skipped with `loop_warnings` attached to `ReplayRouteAttribution`.

**Replay route attribution semantics:** `ReplayRouteAttribution` contains only the **filtered** set of routes — routes that survived loop prevention and self-loop filtering. The `route_trace` is the ordered list of matched route IDs, bounded to 16 entries. Metadata on replay results is cleaned to exclude internal pipeline artifacts before surface exposure.

### 2.7 Replay Routing and Capacity Controls

Replay routing participates in the `CapacityController` (see Contract 53, §15) during `BEST_EFFORT` mode:

1. Before each replay delivery, `ReplayEngine._stage_deliver()` acquires a replay slot via `capacity_controller.acquire_replay()`.
2. If the acquire fails (capacity exhausted or shutdown signaled), the replay result records `status="error"` with `error="replay_capacity_exceeded"` (or `error="replay_rejected_shutdown"` during shutdown). The route was matched but the delivery was not attempted.
3. If the acquire succeeds, the replay slot is released after the delivery completes.

Non-delivery replay modes (`RE_RENDER`, `RE_ROUTE`, `DRY_RUN`) do not acquire capacity slots — they are read-only.

During shutdown, `capacity_controller.stop_accepting()` prevents new replay work from starting. In-flight replay deliveries are drained before adapter teardown (see Contract 54, §12). Replay that was in progress but not completed when shutdown began is abandoned — no persistent in-flight recovery, no replay resume on restart.

### 2.8 Capacity Rejection and Routing

Capacity is acquired **per destination, not per route**. Each target in a fan-out independently calls `capacity_controller.acquire_delivery()` before its delivery proceeds:

- If the acquire fails for a given target, **that target** receives a `DeliveryOutcome` with `status="permanent_failure"`, `failure_kind=CAPACITY_REJECTION` (or `SHUTDOWN_REJECTION` during shutdown), and `error="delivery_capacity_exceeded"` (or `error="delivery_rejected_shutdown"`). Other targets may still succeed if they acquire capacity.
- The route was matched correctly and the delivery plan was created — the failure is a capacity issue, not a routing issue.
- `RouteStats.record_failed()` is called on the capacity-rejected target so per-route counters reflect the failure.

This means a route can be correctly configured and correctly matched, but still produce failed deliveries if the pipeline is under capacity pressure. Check `capacity_rejections` and `outbound_failed` in the diagnostics snapshot to distinguish capacity failures from routing failures.

### 2.6 Loop Prevention Scope

| Context | Mechanism | Blocks startup? | Blocks delivery? |
|---------|-----------|-----------------|------------------|
| Config-time | `check_route_loops` (direct + DFS) | No (warning only) | N/A |
| Runtime | Route-trace loop prevention in `PipelineRunner` | N/A | Yes (skips delivery) |
| Runtime | Self-loop guard in `PipelineRunner` | N/A | Yes (skips delivery) |
| Replay | `_filter_replay_loops` (route_trace + matched_routes) | N/A | Yes (skips route) |


## 3. Route Attribution Visibility

Every routed delivery carries attribution data. This section explains where to find it.

### 3.1 On Events

After route matching, `RoutingMetadata.route_trace` on the event's metadata contains the ordered tuple of matched route IDs. This is ephemeral — it exists on the in-flight event but is not persisted with the stored event.

### 3.2 On Delivery Receipts

`DeliveryReceipt.route_id` persists the route attribution in storage. Every receipt records which route was responsible for that delivery attempt. Use receipts to reconstruct the routing history of any event.

### 3.3 On Delivery Outcomes

`DeliveryOutcome.route_id` carries attribution on the pipeline-internal result. Outcomes are not persisted — they are consumed by the pipeline for stats and logging.

### 3.4 In RouteStats

`RouteStats.snapshot()` returns per-route counters: `delivered`, `failed`, `skipped`, `loop_prevented`, and `last_error`. Use this for a quick summary of route health.

### 3.5 In Replay Results

`ReplayRouteAttribution` on `ReplayResult` records which routes matched during replay, along with replay-specific metadata (mode, loop warnings).

### 3.6 Attribution Is Not Propagated Externally

Route attribution is internal to MEDRE. It does not appear in radio packets, Matrix messages, LXMF messages, or any external output.


## 4. Diagnostics and Topology Visibility

### 4.1 Router Snapshot

`Router` exposes a read-only snapshot of registered routes: route count, route IDs, source/target specs. Use this to verify that routes were registered correctly.

### 4.2 RouteStats Snapshot

`RouteStats.snapshot()` provides per-route counters. Check this to identify routes with high failure rates or loop-prevented deliveries.

### 4.3 Delivery Receipts

Query stored `DeliveryReceipt` records to trace individual delivery histories. Each receipt has `route_id`, `target_adapter`, `status`, `attempt_number`, and `parent_receipt_id` for retry lineage.

### 4.4 Replay Attribution

For route-aware replay modes (`RE_ROUTE`, `BEST_EFFORT`, `DRY_RUN`), `ReplayRouteAttribution` records which routes matched each historical event. Use `DRY_RUN` first to preview matching behavior without side effects.


## 5. Common Troubleshooting

### 5.1 "Unknown adapter" Startup Failure

**Symptom:** `RouteValidationError` during startup.

**Cause:** An enabled route references an adapter ID that is not present in the runtime configuration.

**Fix:** Check the route's `source_adapters` and `dest_adapters` against the `[adapters.*]` sections in TOML. Either add the missing adapter or update the route references.

### 5.2 Cycle Warning at Startup

**Symptom:** Log messages like `"Direct routing loop detected"` or `"Route cycle detected"`.

**Cause:** Routes form a cycle in the adapter adjacency graph.

**Effect:** Startup continues. The self-loop guard at runtime prevents immediate self-loops. Multi-hop cycles are not automatically prevented at delivery time — the config should be fixed.

**Fix:** Review route configuration. Remove or disable one of the routes in the cycle. Or restructure to break the cycle (e.g., use a hub-and-spoke pattern instead of a ring).

### 5.3 Disabled Routes Not Matching

**Symptom:** A route is defined in TOML but events never match it.

**Cause:** The route has `enabled = false` (or defaults to false if explicitly set).

**Fix:** Set `enabled = true` in the route's TOML section and restart.

### 5.4 Stale Route Configuration

**Symptom:** Route behavior doesn't match expectations after a TOML edit.

**Cause:** Route configuration is loaded at startup. Runtime changes to TOML files are not picked up until restart.

**Fix:** Restart the MEDRE process after route configuration changes.

### 5.5 Self-Loop Guard Firing Unexpectedly

**Symptom:** Deliveries showing `status="skipped"` with `error="loop_prevented"`.

**Cause:** A route's destination adapter matches the source adapter of the event.

**Fix:** Check the route's `dest_adapters` list. If the source adapter appears in it, either remove it from the destination list or restructure the route.

### 5.6 Route Matches But No Delivery

**Symptom:** `route_trace` is populated but no delivery occurs.

**Possible causes:**
- The adapter is not started or has no active connection.
- The delivery plan has a `DEADLINE_EXCEEDED` failure.
- The renderer failed to produce output.

**Fix:** Check delivery receipts for the event. Check adapter health. Check `RouteStats` for error details.


## 6. Explicit Non-Guarantees

These are things the routing layer explicitly does **not** provide:

1. **Distributed loop prevention.** Loop detection is local to a single MEDRE process. If two MEDRE instances bridge the same transports in opposite directions, neither will detect the cross-instance loop.

2. **Exactly-once delivery.** No transport in MEDRE provides exactly-once semantics. Radio transports are probabilistic. Matrix is at-least-once. LXMF is at-least-once with eventual delivery.

3. **Radio delivery confirmation.** Meshtastic and MeshCore transports cannot confirm that any remote node received a message. A `sent` receipt means the local node accepted the packet. Nothing more.

4. **Cross-instance coordination.** Routes, attribution, and stats are local to the process. There is no shared state between MEDRE instances.

5. **Automatic route reconfiguration.** Route changes require a restart. There is no hot-reload mechanism.

6. **Delivery ordering guarantees.** Events are matched and delivered in route registration order, but async adapter delivery means actual outbound ordering depends on transport latency.

7. **Replay deduplication.** Replay processes events without deduplication. Replayed events may be delivered again if they match current routes.

8. **Persistent queue.** Delivery state is in-memory only. In-flight deliveries cancelled on shutdown are lost.

9. **Per-adapter restart.** Only full runtime stop/start is supported. Individual adapters cannot be restarted independently.

10. **Queue-bound delivery completeness.** Capacity semaphores prevent unbounded accumulation but do not guarantee delivery. Under pressure, correctly matched and correctly routed events may be rejected at the delivery stage. This is not a routing failure — it is a capacity backpressure signal.

11. **Exactly-once delivery.** No transport in MEDRE provides exactly-once semantics. MEDRE remains best-effort. Radio transports are probabilistic. Queue bounds prevent unbounded accumulation but not data loss under extreme pressure.


## 7. Quick Reference: Route Matching Rules

| Condition | Matches? |
|-----------|----------|
| `event.source_adapter` in route's `source_adapters` | Yes |
| `event.source_adapter` not in route's `source_adapters` | No |
| Route is `enabled = false` | No (not registered) |
| Route policy `allowed_event_types` is empty | Yes (no restriction) |
| Route policy `allowed_event_types` does not include `event.event_kind` | No (filtered by policy) |
| Target adapter is the same as source adapter | Matched, but delivery skipped by self-loop guard |

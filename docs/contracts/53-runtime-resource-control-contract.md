# Contract 53 — Runtime Resource Control Contract

> **Status:** Active
> **Classification:** Normative
> **Authority:** Authoritative specification for capacity limits, backpressure, delivery throttling, and resource containment
> **Last reviewed:** 2026-05-24
>
> **Scope:** Resource control for the MEDRE runtime: delivery concurrency limits, replay event limiting, shutdown drain, capacity timeout behavior, capacity exhaustion behavior, and diagnostics. Sections 2–13 are design reference — some decisions deferred. Section 14 describes the v1 implementation. Section 15 describes the v2 implementation.
> **Audience:** Runtime builders, adapter authors, operators, future implementors.
> **References:** Contract 47 (Runtime Assembly), Contract 48 (Runtime Observability), Contract 31 (Session Boundary), Contract 49 (Routing and Bridge), Contract 54 (Runtime Shutdown), Contract 59 (Runtime Durability), Contract 60 (Runtime Cancellation).

Every agent or document that references MEDRE capacity limits, backpressure semantics, delivery throttling, or resource containment must defer to this contract.

**Non-guarantees (explicit):** MEDRE remains best-effort. The runtime provides no replay deduplication, no exactly-once delivery guarantee, no transactional delivery guarantees, no persistent queue, no per-adapter restart, and no distributed coordination. Radio transports remain probabilistic. Queue bounds prevent unbounded accumulation but do not prevent data loss under extreme pressure. These are all deferred or out of scope.

**Terminology note:** The v2 implementation uses semaphore-based **capacity limiting** (in-flight bounds), not a queue staging system. The `CapacityController` bounds the number of concurrently executing delivery/replay operations. It does not buffer, enqueue, or reorder work. The word "queue" in this document refers either to (a) the design reference for future per-adapter bounded structures, (b) adapter-local bounded structures like the Meshtastic deque, or (c) the conceptual pipeline stages in §2. "Capacity" and "in-flight bounds" refer to the semaphore-based limiter implemented in v1/v2.

## 1. Scope

> **Note:** Sections 1–13 are design reference — some decisions deferred to v2. See Section 14 for what v1 actually implements.

This is a **design contract**. It describes the intended architecture for resource control in the MEDRE runtime. The design sections below record vocabulary, tradeoffs, transport caveats, and pre/post-beta boundaries. No implementation work was authorized under the design-only sections. Section 14 describes the v1 implementation.

- Establish vocabulary for queue types and backpressure policies.
- Define the tradeoffs that inform future implementation decisions.
- Record transport-specific caveats so they are not overlooked.
- Distinguish what must ship before beta from what can wait.

No implementation work is authorized under this contract in the current tranche.

## 2. Queue Types

The MEDRE pipeline has three logical queue boundaries where backpressure may apply.

### 2.1 Adapter Outbound Queues

Each adapter has an outbound delivery queue. When the pipeline resolves a delivery plan and renders the payload, the rendered result is placed on the target adapter's outbound queue. The adapter's `deliver()` method consumes from this queue.

This is the primary backpressure point. If an adapter's transport is slow (radio latency, network congestion), the outbound queue is where pressure builds first.

### 2.2 Route Queues

Each route has a logical queue that buffers events matched by the router before the planning/delivery pipeline processes them. Currently, route matching and delivery happen inline within the pipeline runner. A dedicated route queue would decouple matching from delivery, allowing the system to absorb bursts.

### 2.3 Replay Queues

Replay events (historical message replay from a transport's store-and-forward) produce a burst of events that enter the pipeline through the same ingress path as real-time events. A dedicated replay queue or rate limiter prevents replay from starving real-time traffic.

Current state: none of these queues exist as explicit data structures. The pipeline processes events inline. This contract defines the design for when explicit queues are introduced.

## 3. Backpressure Options

When a queue reaches its configured depth limit, three strategies are available:

### 3.1 Drop

Discard the oldest or newest item in the queue to make room. The dropped item is logged as a diagnostic event via `Diagnostician`.

- **Drop oldest** — preserves the most recent data. Appropriate for telemetry and status streams where freshness matters more than completeness.
- **Drop newest** — preserves the earliest queued items, ensuring delivery order but losing the most recent arrivals. Appropriate for command-and-control messages where the oldest instruction should not be discarded.

Tradeoff: data loss is immediate and silent to the sender. The sender does not know its message was dropped unless it polls diagnostics.

### 3.2 Block

Pause the caller (with a timeout) until space becomes available in the queue. The pipeline runner's delivery coroutine awaits the queue's `put()` with a deadline.

Tradeoff: backpressure propagates upstream. If the caller is the pipeline runner (a single shared coroutine), blocking on one slow adapter stalls delivery to all adapters. This is unacceptable for a multi-adapter runtime unless each adapter has its own delivery task.

### 3.3 Fail

Return an error to the caller immediately. The pipeline records the failure via `Diagnostician.record_adapter_failure()` and moves on.

Tradeoff: the caller must handle the error. This is the cleanest option for the pipeline runner because it preserves forward progress, but it requires the caller (or retry logic) to decide what to do next.

## 4. Per-Adapter vs Global Limits

### 4.1 Per-Adapter Limits

Each adapter's outbound queue has its own depth limit. A slow Meshtastic radio filling its queue does not affect a fast Matrix adapter's delivery.

Advantages:

- Isolation: one slow transport cannot starve others.
- Transport-specific tuning: radio queues can be shallow (messages are expensive to send), Matrix queues can be deeper (HTTP is fast).
- Predictable: each adapter's resource usage is bounded independently.

Disadvantages:

- More configuration surface.
- Requires per-adapter monitoring.
- Total memory usage is `sum(per_adapter_limits)`, which may be large with many adapters.

### 4.2 Global Limits

A single limit on the total number of in-flight deliveries across all adapters. When the global limit is reached, new deliveries are rejected.

Advantages:

- Simple: one number to configure and monitor.
- Bounded total memory regardless of adapter count.

Disadvantages:

- One slow adapter can consume the entire global budget, starving fast adapters.
- No transport-specific tuning.
- Difficult to set a single number that works for both radio (slow, small queues) and Matrix (fast, large queues).

### 4.3 Recommendation

**Per-adapter limits with a global ceiling.** Each adapter gets its own queue depth limit (defaulting by transport type). A global limit acts as a safety net to prevent unbounded memory growth if the operator configures many adapters with large per-adapter limits.

## 5. Delivery Retry Interaction

The pipeline has retry semantics for failed deliveries (see Contract 31, session boundary). Backpressure interacts with retries as follows:

- **Drop policy:** a dropped message is not retried. It is recorded as a diagnostic event. The message is lost.
- **Block policy:** if the queue blocks and the timeout expires, the delivery fails. The retry executor may re-enqueue the message up to its retry budget. Each retry attempt competes for queue space.
- **Fail policy:** the delivery fails immediately. Retry behavior is unchanged — the retry executor treats it the same as any other delivery failure.

Under sustained backpressure, retries can make the problem worse by re-enqueuing messages that the queue cannot drain. A recommended mitigation: **do not retry deliveries that fail due to backpressure.** Distinguish "transport error, retry may help" from "queue full, retry will not help" in the `DeliveryOutcome` discriminant.

## 6. Replay Behavior Under Pressure

Replay produces a burst of historical events. Under backpressure:

1. **Replay should yield to real-time.** Real-time events have higher priority than replayed historical messages. A replay event that encounters a full queue should be dropped or deferred, not allowed to block real-time traffic.

2. **Replay rate limiting.** The replay engine should produce events at a controlled rate (e.g., N events per second) rather than dumping the entire replay buffer into the pipeline at once. This is a producer-side throttle, not a consumer-side backpressure.

3. **Replay diagnostics.** Every replay event dropped due to backpressure should be recorded via `Diagnostician.record_replay_skip()` with reason `"backpressure"`.

4. **Replay does not retry.** If a replay event is dropped due to backpressure, it is not retried. Replay is best-effort for historical completeness. The operator can re-trigger replay later if needed.

## 7. Radio Transport Caveats

### 7.1 Meshtastic Serial

Meshtastic adapters communicate over serial (or TCP) to a radio device. Key constraints:

- **Low bandwidth:** LoRa PHY is extremely slow (hundreds of bytes per second on LongFast).
- **No flow control from the mesh:** the radio accepts packets as fast as the serial link allows, but the mesh itself may drop them.
- **Serial write blocking:** writing to a serial port blocks until the kernel buffer accepts the data. If the radio's receive buffer is full, writes block at the OS level.
- **Implication:** for Meshtastic, **drop is safer than block.** Blocking the pipeline runner's delivery task on a serial write stalls all other adapters. Dropping the message and logging it is preferable.

### 7.2 MeshCore

MeshCore adapters face similar bandwidth constraints. Additionally:

- **No standard flow control:** MeshCore's transport does not expose backpressure signals.
- **Implication:** same as Meshtastic — drop is the safe default.

### 7.3 Recommendation for Radio Transports

Default backpressure policy for radio adapters:

- Queue depth: **small** (5-20 messages).
- Policy: **explicit rejection** — reject enqueue when full, raise transient error; caller decides retry.
- No silent drops; every rejected item is counted in `queue_total_rejected`.
- Log every rejection as a diagnostic event.

## 8. Matrix/LXMF Async Caveats

### 8.1 Matrix

Matrix adapters use `nio` (or the HTTP API) over TCP. Key constraints:

- **Async SDK:** nio is fully async. Sending a message is an awaitable that completes quickly under normal conditions.
- **Rate limiting:** Matrix homeservers impose rate limits (requests per second). Exceeding these returns HTTP 429 with a `retry_after_ms` header.
- **Connection pooling:** a single Matrix adapter has one sync loop and one HTTP client. Outbound sends share this connection.
- **Implication:** Matrix can tolerate a deeper queue because the underlying transport is fast. Rate-limit responses (M_LIMIT_EXCEEDED / HTTP 429) are surfaced immediately as `AdapterSendError(transient=True)`; the adapter does not buffer, block, or sleep. The `retry_after_ms` header is embedded in the error message for diagnostics but is not yet structured or honored. Adaptive backoff based on `retry_after_ms` is deferred to a future tranche.

### 8.2 LXMF

LXMF (Reticulum) adapters use the Reticulum network stack:

- **Async SDK:** Reticulum's API is synchronous, but MEDRE wraps it in an async executor.
- **Delivery is fire-and-forget:** LXMF sends a message to the Reticulum network, which handles store-and-forward. There is no immediate delivery confirmation.
- **Implication:** LXMF deliveries complete quickly (fire-and-forget). Queue pressure is unlikely unless the Reticulum transport itself is saturated. **Drop newest** or **fail** are reasonable defaults.

### 8.3 Recommendation for Async Transports

Default backpressure policy for Matrix/LXMF:

- Queue depth: **moderate** (50-200 messages).
- Policy: **block with timeout** (Matrix), **fail** (LXMF).
- Matrix: rate-limit responses are surfaced immediately as `AdapterSendError(transient=True)`; the adapter does not block or sleep. Pipeline retry/replay handles re-delivery. Adaptive backoff based on `retry_after_ms` is deferred.
- LXMF: fail fast; LXMF's fire-and-forget semantics make backpressure unlikely.

## 9. Default Policy Recommendation

Before beta, the recommended default backpressure configuration:

| Transport  | Queue Depth | Policy               | Retry on BP | Rationale                                                    |
| ---------- | ----------- | -------------------- | ----------- | ------------------------------------------------------------ |
| Meshtastic | 10          | Drop oldest          | No          | Slow radio, serial writes block                              |
| MeshCore   | 10          | Drop oldest          | No          | Slow radio, no flow control                                  |
| Matrix     | 100         | Fail / surface trans | No          | Fast async SDK; rate-limit surfaced as transient immediately |
| LXMF       | 50          | Fail                 | No          | Fire-and-forget, fast                                        |

Matrix has no per-adapter outbound queue. Rate-limit responses are surfaced immediately as `AdapterSendError(transient=True)`; the pipeline retry worker handles re-delivery.

Global ceiling: 500 total in-flight deliveries across all adapters.

These defaults prioritize runtime stability over delivery completeness. Under sustained pressure, the runtime drops messages rather than exhausting memory or stalling.

## 10. Operator Configuration Shape

Proposed TOML shape for resource control. This is a design proposal, not an implemented schema.

```toml
[runtime]
name = "my-bridge"

[runtime.resource_control]
# Global safety net: max in-flight deliveries across all adapters.
global_delivery_limit = 500

# Default per-adapter queue depth (overridden per-adapter below).
default_queue_depth = 50

# Default backpressure policy: "reject", "block", "fail"
default_policy = "fail"

# Block timeout in seconds (only used when policy = "block").
block_timeout_seconds = 5

# Per-adapter overrides.
[runtime.resource_control.adapters.meshtastic_radio]
queue_depth = 10
policy = "reject"

[runtime.resource_control.adapters.matrix_bot]
queue_depth = 100
policy = "block"
block_timeout_seconds = 5
```

This configuration is optional. If absent, the defaults from §9 apply.

## 11. Metrics Required

The following metrics must be observable when resource control is implemented:

### 11.1 Per-Adapter Queue Metrics

| Metric                  | Type      | Description                                             |
| ----------------------- | --------- | ------------------------------------------------------- |
| `queue_depth`           | Gauge     | Current number of items in the adapter's outbound queue |
| `queue_high_water_mark` | Gauge     | Maximum depth reached since last reset                  |
| `queue_drops_total`     | Counter   | Total messages dropped due to backpressure              |
| `queue_block_time_ms`   | Histogram | Time spent blocked waiting for queue space              |
| `queue_time_ms`         | Histogram | Time from enqueue to dequeue (latency through queue)    |

### 11.2 Global Metrics

| Metric                       | Type    | Description                                            |
| ---------------------------- | ------- | ------------------------------------------------------ |
| `in_flight_deliveries`       | Gauge   | Current total in-flight deliveries across all adapters |
| `global_limit_reached_total` | Counter | Times the global delivery limit was reached            |

### 11.3 Diagnostic Events

Every backpressure event (drop, block timeout, global limit) must be recorded via `Diagnostician` so it appears in diagnostic snapshots. The `Diagnostician` will gain a new counter:

- `backpressure_drops: Counter` — keyed by adapter_id and reason.

### 11.4 Logging

At WARNING level:

- "Queue full, applying {policy} for adapter {adapter_id}" — when backpressure activates.
- "Global delivery limit reached ({current}/{limit})" — when the global ceiling is hit.

At INFO level:

- "Queue depth: {depth}/{limit} for adapter {adapter_id}" — periodic snapshot logging (not per-message).

## 12. What Blocks Beta vs What Can Wait

### Must Ship Before Beta

1. **Per-adapter outbound queue depth limits** — without this, a slow adapter can grow its queue without bound.
2. **Drop policy for radio transports** — without this, a blocked serial write stalls the pipeline.
3. **Queue depth metrics** — operators must be able to see when queues are filling.
4. **Diagnostic recording of drops** — operators must know messages were dropped.

### Can Wait Until After Beta

1. **Block policy** — requires per-adapter delivery tasks (architectural change to PipelineRunner).
2. **Global delivery ceiling** — useful but not critical with per-adapter limits in place.
3. **Replay rate limiting** — replay is not in the critical path for beta.
4. **Per-route queues** — current inline delivery works for beta scale.
5. **Operator-configurable backpressure policy** — hardcoded defaults are acceptable for beta.
6. **Queue high-water marks and histograms** — gauges and counters are sufficient for beta.

### Explicitly Not in Scope

1. Adaptive queue sizing (auto-tuning depth based on observed throughput).
2. Priority queues (real-time vs replay within a single adapter's queue).
3. Cross-adapter backpressure signaling (one adapter's queue affecting another's ingress).

## 13. What Should Never Be Generalized

Some resource control behavior is inherently transport-specific and should not be abstracted into a "one policy fits all" model:

1. **Meshtastic serial write behavior.** The fact that serial writes block at the OS level is not a "queue policy" — it is a physical constraint of the transport. The adapter must handle this internally regardless of the configured backpressure policy.

2. **Matrix homeserver rate limits.** HTTP 429 responses are a server-side constraint, not a queue problem. The adapter surfaces `AdapterSendError(transient=True)` immediately, embedding `retry_after_ms` in the error message for diagnostics. It does not parse, sleep, or honor the header. Adaptive backoff based on `retry_after_ms` is deferred to a future tranche.

3. **LXMF fire-and-forget semantics.** LXMF delivers to the Reticulum network, which handles store-and-forward. The adapter does not control when the message actually reaches the recipient. This is fundamentally different from Matrix (where send confirmation is synchronous) and Meshtastic (where send confirmation is implicit in the serial write completing).

4. **MeshCore protocol framing.** MeshCore's internal protocol may have its own flow control that is opaque to MEDRE. The adapter must respect it regardless of the queue policy.

The queue/backpressure system must provide **policy hooks** (drop/block/fail) and **transport-specific defaults**, but must never attempt to abstract away the physical and protocol-level constraints that differ fundamentally between transports.

## 14. v1 Implementation

v1 implements a **global concurrency limit** model (not per-adapter queues). The implementation uses semaphores and timeouts to bound in-flight work and prevent unbounded resource growth.

### 14.1 Runtime Limit Configuration

The `[runtime.limits]` TOML section controls concurrency:

| Field                              | Type    | Default | Description                                                                                                                                                                             |
| ---------------------------------- | ------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `max_inflight_deliveries`          | `int`   | `100`   | Maximum concurrent delivery coroutines across all adapters. Enforced by a `Semaphore` in `PipelineRunner`. Each target in a fan-out independently acquires a slot.                      |
| `max_inflight_replay_events`       | `int`   | `100`   | Maximum concurrent replay event **delivery-phase** operations. Enforced by a `Semaphore` in `ReplayEngine`. Re-routing, re-rendering, and dry-run modes do not consume replay capacity. |
| `shutdown_drain_timeout_seconds`   | `float` | `10`    | Seconds to wait for in-flight deliveries to complete during shutdown before cancelling.                                                                                                 |
| `delivery_acquire_timeout_seconds` | `float` | `1.0`   | Maximum seconds a delivery attempt will wait to acquire a concurrency slot before failing with a timeout error.                                                                         |

If `[runtime.limits]` is absent from TOML, all fields use their defaults.

Example configuration:

```toml
[runtime]
name = "my-bridge"
shutdown_timeout_seconds = 10

[runtime.limits]
max_inflight_deliveries = 32
max_inflight_replay_events = 16
shutdown_drain_timeout_seconds = 10.0
delivery_acquire_timeout_seconds = 15.0
```

### 14.2 Delivery Limiter (PipelineRunner)

`PipelineRunner` holds an `asyncio.Semaphore(max_inflight_deliveries)`. Before each **per-target** delivery attempt, the runner acquires the semaphore:

- **Acquire succeeds:** the delivery proceeds normally.
- **Acquire times out** (after `delivery_acquire_timeout_seconds`): the delivery fails with `status="permanent_failure"` and `failure_kind=CAPACITY_REJECTION` (or `SHUTDOWN_REJECTION` if the runtime is no longer accepting work). A capacity rejection counter is incremented. The delivery is not retried — capacity timeout is treated as a backpressure signal, not a transient error.

The semaphore is released after the adapter's `deliver()` returns (success, failure, or skip). This bounds the total number of concurrent adapter `deliver()` calls across all adapters to `max_inflight_deliveries`. Each target in a fan-out independently acquires/releases, so the limit bounds true delivery concurrency, not batch-level throughput.

### 14.3 Replay Limiter (ReplayEngine)

`ReplayEngine` holds a separate `asyncio.Semaphore(max_inflight_replay_events)`. During BEST_EFFORT replay, each replay delivery acquires the replay semaphore before delivery. This prevents replay from consuming the entire delivery budget and starving real-time traffic.

The replay semaphore is independent of the delivery semaphore. Replay deliveries that proceed past the replay limiter still acquire a slot on the delivery semaphore via the `PipelineRunner`.

### 14.4 Capacity Timeout Behavior

When a delivery cannot acquire a semaphore slot within `delivery_acquire_timeout_seconds`:

1. The delivery outcome records `status="permanent_failure"` with `failure_kind=CAPACITY_REJECTION` (or `SHUTDOWN_REJECTION` during shutdown).
2. `CapacityController` increments its `delivery_timeouts` internal gauge (available via `snapshot()`; maps to `capacity_rejections` in `RuntimeAccounting`).
3. No retry is attempted. Capacity timeout is a backpressure signal.
4. A WARNING log is emitted: `"Capacity timeout: delivery to {adapter_id} timed out waiting for slot ({current}/{limit})"`.

### 14.5 Diagnostics Counters

The `CapacityController` exposes runtime capacity counters via its `snapshot()` method. These are **CapacityController internal gauges** — they track semaphore-level acquire/reject/timeout events inside the controller itself. They are distinct from `RuntimeAccounting` operator counters (`capacity_rejections`, `outbound_failed`, `outbound_delivered`, `loop_prevented`, `inbound_accepted`), which track delivery outcomes at the accounting layer.

| Snapshot field        | Type    | Description                                                              |
| --------------------- | ------- | ------------------------------------------------------------------------ |
| `delivery_current`    | gauge   | Currently acquired delivery semaphore slots.                             |
| `delivery_limit`      | gauge   | Configured `max_inflight_deliveries` bound.                              |
| `delivery_rejections` | counter | Delivery acquire attempts that failed (shutdown or immediate rejection). |
| `delivery_timeouts`   | counter | Delivery acquire attempts that timed out waiting for a slot.             |
| `replay_current`      | gauge   | Currently acquired replay semaphore slots.                               |
| `replay_limit`        | gauge   | Configured `max_inflight_replay_events` bound.                           |
| `replay_rejections`   | counter | Replay acquire attempts that failed (shutdown or immediate rejection).   |
| `replay_timeouts`     | counter | Replay acquire attempts that timed out waiting for a slot.               |

### 14.6 What v1 Does NOT Implement

The following from the design sections (2–13) are **deferred to v2**:

- **Per-adapter outbound queues.** v1 uses a global semaphore, not per-adapter queue structures.
- **Per-adapter queue depth limits.** No per-adapter isolation; the global limit is the only bound.
- **Backpressure policies** (reject, block, fail). v1 uses semaphore-based capacity control with timeout.
- **Replay rate limiting** (producer-side throttle). v1 bounds replay concurrency but does not rate-limit replay event production.
- **Per-route queues.** Delivery remains inline within the pipeline runner.
- **Block policy with per-adapter delivery tasks.** Requires architectural changes to PipelineRunner.
- **Operator-configurable backpressure policy.** Defaults are hardcoded; the only operator knob is `max_inflight_deliveries`.
- **Queue depth metrics, high-water marks, histograms.** v1 provides semaphore gauges and timeout counters only.
- **Replay deduplication.** Replay processes events without deduplication.
- **Exactly-once delivery guarantee.** Not provided by any MEDRE component.
- **Persistent queue.** Delivery state is in-memory only.
- **Per-adapter restart.** Only full runtime stop/start is supported.
- **Distributed coordination.** Limits and state are local to the process.

## 15. v2 Implementation — CapacityController and Capacity Bounds

v2 introduces `CapacityController` as a centralized capacity manager, wires `ReplayEngine` into `RuntimeBuilder` with capacity and shutdown participation, and adds bounded adapter-level queues where applicable. The design sections (2–13) described per-adapter outbound queues with backpressure policies; v2 delivers this through a combination of the global `CapacityController` (reject-with-diagnostics default) and adapter-level bounding (explicit rejection for Meshtastic).

**What v2 does not change:** MEDRE remains best-effort. No exactly-once guarantees, no transactional delivery guarantees, no persistent in-flight recovery. Radio transports remain probabilistic. Queue bounds prevent unbounded accumulation but not data loss under extreme pressure.

### 15.1 CapacityController

`CapacityController` (in `src/medre/core/supervision/capacity.py`) is a semaphore-based capacity manager that bounds in-flight delivery and replay work. It replaces the inline semaphores that were previously held directly by `PipelineRunner` and `ReplayEngine`.

The controller manages two independent semaphores:

| Stream   | Semaphore       | Limit field                  |
| -------- | --------------- | ---------------------------- |
| Delivery | `_delivery_sem` | `max_inflight_deliveries`    |
| Replay   | `_replay_sem`   | `max_inflight_replay_events` |

Both use the same acquire-timeout (`delivery_acquire_timeout_seconds`) for timed waits.

**Lifecycle:**

- `accepting_work` starts `True`. All acquire calls proceed normally.
- `stop_accepting()` sets `accepting_work = False`. All subsequent acquire calls return `False` immediately. This is called during shutdown (see Contract 54, §12).
- `snapshot()` returns a deterministic dict of all counters for diagnostics.

**Acquire/release flow:**

```text
acquire_delivery() / acquire_replay()
  ├── accepting_work == False  →  increment rejection counter; return False
  ├── await semaphore (timeout)
  │   ├── accepting_work == False (re-check)  →  release semaphore; increment rejection counter; return False
  │   └── accepting_work == True  →  increment current; return True
  └── TimeoutError             →  increment timeout counter; return False

release_delivery() / release_replay()
  └── release semaphore; decrement current
```

### 15.2 ReplayEngine Wired into RuntimeBuilder

`RuntimeBuilder.build()` (in `src/medre/runtime/builder.py`) now constructs and wires:

1. `CapacityController(limits)` — step 9.5 in the build sequence.
2. `ReplayEngine(storage, pipeline, capacity_controller, diagnostician)` — step 9.6.

The `CapacityController` is injected into `PipelineRunner` via `set_capacity_controller()`. The same controller is passed to `ReplayEngine` at construction. Both subsystems share the same capacity state, ensuring that:

- Delivery and replay compete for separate semaphore budgets (not the same pool).
- `stop_accepting()` blocks new work for both streams simultaneously.
- Drain logic can observe both `delivery_current` and `replay_current` via a single `snapshot()` call.

### 15.3 Adapter Accumulation Bounding

v2 adds bounded internal queues at the adapter level to prevent unbounded memory accumulation per adapter.

#### Meshtastic — deque with maxlen

`MeshtasticOutboundQueue` (in `src/medre/adapters/meshtastic/queue.py`) uses an unbounded deque with explicit enqueue-time capacity enforcement (default `max_queue_size=1024`). When the queue is at capacity:

- **Policy: explicit rejection.** `enqueue()` raises `MeshtasticSendError(transient=True)` instead of accepting the item. Existing queued items are never evicted.
- `queue_total_rejected` counter is incremented for each rejected enqueue attempt.
- `queue_max_size` and `queue_utilization_pct` are exposed in diagnostics.

This prevents unbounded growth in long-duration runs where outbound throughput exceeds send capacity. Failed items during `process_one` are permanently dropped (not requeued).

#### Other adapters

Matrix, LXMF, and MeshCore adapters do not currently have explicit bounded queues. They rely on the `CapacityController`'s global semaphore to bound in-flight work, and on the transport's own flow control (Matrix rate limiting, LXMF fire-and-forget, MeshCore protocol framing). Per-adapter queue bounds for these transports remain a design reference item (see §4, §9).

### 15.4 Bounded Staging in the Pipeline

The `PipelineRunner` uses `CapacityController` to gate delivery execution **per target**. Before each per-target delivery inside `_deliver_to_targets_inner`, the runner calls `acquire_delivery()`. If the controller rejects the acquire (work stopped or timeout), the runner returns `DeliveryOutcome` with:

- `status="permanent_failure"`
- `error="delivery_capacity_exceeded"` (or `error="delivery_rejected_shutdown"` if the runtime has stopped accepting work)
- `_delivery_rejection_count` incremented

The capacity slot is released in a `finally` block after the per-target delivery completes (success, failure, or skip). This ensures forward progress: the pipeline never blocks indefinitely on capacity — it either proceeds within the timeout or rejects with diagnostics. Fan-out is correct: each target independently acquires/releases, so `max_inflight_deliveries` bounds the true concurrency of adapter `deliver()` calls.

### 15.5 Replay Capacity Participation

The `ReplayEngine` participates in capacity control during `BEST_EFFORT` mode. The replay semaphore limits **delivery-phase** concurrency only — non-delivery replay modes do not consume replay capacity:

1. Before delivery, `_stage_deliver()` calls `capacity_controller.acquire_replay()`.
2. If the acquire fails (work stopped or timeout), the replay result records:
   - `status="error"`
   - `error="replay_capacity_exceeded"` (or `error="replay_rejected_shutdown"` if the runtime has stopped accepting work)
3. If the acquire succeeds, the replay slot is released in a `finally` block after delivery.

Non-delivery replay modes (`RE_RENDER`, `RE_ROUTE`, `DRY_RUN`) do not acquire replay slots — they are read-only and do not consume delivery capacity.

### 15.6 Capacity Exhaustion Behavior

When capacity is exhausted, the system applies one of three behaviors depending on the subsystem:

| Subsystem                 | Default behavior                                                                                                    | Alternative (documented)                                                                                                              |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `PipelineRunner` delivery | **Reject** — return `permanent_failure` with `CAPACITY_REJECTION`/`SHUTDOWN_REJECTION`; increment diagnostics       | N/A (reject is the only policy at this layer)                                                                                         |
| `ReplayEngine` delivery   | **Reject** — return `error` with `replay_capacity_exceeded`/`replay_rejected_shutdown`; increment diagnostics       | N/A (reject is the only policy at this layer)                                                                                         |
| Meshtastic outbound queue | **Explicit rejection** — `enqueue()` raises `MeshtasticSendError(transient=True)`; increment `queue_total_rejected` | `max_queue_size=None` for unbounded (not recommended)                                                                                 |
| Design reference (§3)     | —                                                                                                                   | **Drop-newest** — preserves earliest queued items, loses newest arrivals. Appropriate for command-and-control where ordering matters. |

**Default: reject with diagnostics increment.** The `CapacityController` rejects work when capacity is exhausted or when shutdown has been signaled. Each rejection increments a counter visible in `snapshot()`. The caller records the failure in its outcome (delivery or replay) and moves on. No retry is attempted — capacity rejection is a backpressure signal, not a transient error.

### 15.7 Capacity Rejection Metrics

The `CapacityController` tracks the following **internal gauges**, all visible via `snapshot()`. These are CapacityController-internal names (acquire/timeout/reject at the semaphore level), not the operator-facing `RuntimeAccounting` counter names (`capacity_rejections`, `outbound_failed`, `outbound_delivered`, etc.).

| Counter               | Type     | Description                                                         |
| --------------------- | -------- | ------------------------------------------------------------------- |
| `delivery_current`    | Gauge    | Currently in-flight delivery slots                                  |
| `delivery_limit`      | Constant | Maximum concurrent deliveries                                       |
| `delivery_rejections` | Counter  | Deliveries rejected because `accepting_work == False` (shutdown)    |
| `delivery_timeouts`   | Counter  | Deliveries that timed out waiting for a slot                        |
| `replay_current`      | Gauge    | Currently in-flight replay slots                                    |
| `replay_limit`        | Constant | Maximum concurrent replay events                                    |
| `replay_rejections`   | Counter  | Replay events rejected because `accepting_work == False` (shutdown) |
| `replay_timeouts`     | Counter  | Replay events that timed out waiting for a slot                     |
| `accepting_work`      | Flag     | Whether the controller is still accepting new work                  |

Adapter-level queue metrics (Meshtastic):

| Counter                 | Type    | Description                                                               |
| ----------------------- | ------- | ------------------------------------------------------------------------- |
| `queue_total_rejected`  | Counter | Enqueue attempts rejected due to the Meshtastic outbound queue being full |
| `queue_max_size`        | Gauge   | Configured maximum queue size (or `None` for unbounded)                   |
| `queue_utilization_pct` | Gauge   | Current queue utilization as a percentage of `queue_max_size`             |
| `queue_depth`           | Gauge   | Current number of items in the Meshtastic outbound queue                  |

### 15.8 What v2 Does NOT Implement

The following from the design sections (2–13) remain deferred:

- **Per-adapter outbound queues for Matrix, LXMF, MeshCore.** Only Meshtastic has an explicit bounded queue.
- **Operator-configurable backpressure policy per adapter.** The policy is hardcoded: explicit rejection for capacity (including Meshtastic).
- **Block policy with per-adapter delivery tasks.** Requires architectural changes to PipelineRunner.
- **Per-route queues.** Delivery remains inline within the pipeline runner.
- **Replay rate limiting** (producer-side throttle). v2 bounds replay concurrency but does not rate-limit replay event production.
- **Queue high-water marks and latency histograms.** v2 provides gauges and counters only.
- **Replay deduplication.** Replay processes events without deduplication.
- **Exactly-once delivery guarantee.** Not provided by any MEDRE component.
- **Persistent queue or in-flight recovery.** Delivery state is in-memory only. No recovery of in-flight work after shutdown.
- **Per-adapter restart.** Only full runtime stop/start is supported.
- **Distributed coordination.** Limits and state are local to the process.

## 16. Cross-References

| Topic                                                                         | Contract                           |
| ----------------------------------------------------------------------------- | ---------------------------------- |
| Shutdown ordering, drain phases, in-flight work handling                      | Contract 54 (Runtime Shutdown)     |
| Durability semantics, what survives crash, process-local vs persisted         | Contract 59 (Runtime Durability)   |
| Cancellation semantics, CapacityController stop behavior, stop-during-startup | Contract 60 (Runtime Cancellation) |
| Persistence timing, WAL consistency, receipt durability                       | Contract 55 (Runtime Persistence)  |
| Runtime assembly, lifecycle states, startup classification                    | Contract 47 (Runtime Assembly)     |

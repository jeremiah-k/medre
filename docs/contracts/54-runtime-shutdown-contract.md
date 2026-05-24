# Contract 54 — Runtime Shutdown Contract

> **Status:** Active
> **Classification:** Normative
> **Authority:** Authoritative specification for shutdown ordering, in-flight work handling, drain timeout, and persistence guarantees
> **Last reviewed:** 2026-05-24
>
> **Scope:** Shutdown ordering, in-flight work handling, drain timeout behavior, queue drain, replay cancellation, persistence guarantees, and timeout behavior for the MEDRE runtime.
> **Audience:** Runtime builders, adapter authors, operators.
> **References:** Contract 47 (Runtime Assembly), Contract 48 (Runtime Observability), Contract 31 (Session Boundary), Contract 53 (Resource Control), Contract 59 (Runtime Durability), Contract 60 (Runtime Cancellation).

**Non-guarantees (explicit):** MEDRE remains best-effort. The runtime provides no replay deduplication, no exactly-once delivery guarantee, no transactional delivery guarantees, no persistent queue, no per-adapter restart, and no distributed coordination. Radio transports remain probabilistic. No persistent in-flight recovery. No replay resume after shutdown. These are all deferred or out of scope.

## 1. Shutdown Phases

Shutdown proceeds through five ordered phases. Each phase must complete (or time out) before the next begins.

| Phase | Name               | Description                                                                                                                                                |
| ----- | ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1     | **Signal**         | `shutdown_event.set()` — notifies all waiters that shutdown has begun. New event ingestion stops.                                                          |
| 2     | **Stop accepting** | Adapters stop ingesting new events from their transports. In-flight receives are cancelled.                                                                |
| 3     | **Drain**          | Pending deliveries in the pipeline are completed or abandoned per per-phase timeout. Controlled by `shutdown_drain_timeout_seconds` in `[runtime.limits]`. |
| 4     | **Persist**        | Receipts, counters, and diagnostic state are flushed to durable storage.                                                                                   |
| 5     | **Release**        | Transport connections, SDK clients, and file handles are released.                                                                                         |

## 2. Adapter Stop Order

Adapters are stopped in **reverse start order**. If the runtime started adapters A, B, C, the stop sequence is C, B, A.

This ensures that downstream adapters (which may depend on upstream ones for acknowledgment or correlation) are stopped first.

### Per-transport specifics

- **Matrix:** The SDK client disconnects from the homeserver. The sync loop task is cancelled. Pending `/sync` requests are abandoned.
- **Meshtastic:** The serial or TCP connection to the radio is closed. The receive loop task is cancelled.
- **LXMF (Reticulum):** The Reticulum transport link is torn down. Announce and link tasks are cancelled.
- **MeshCore:** The session is disconnected and the receive loop cancelled.
- **Fake adapters (testing):** In-memory queues are cleared; no real I/O.

Individual adapter stop failures are logged but do not prevent other adapters from shutting down.

## 3. Session Stop

Each adapter manages a transport session. On shutdown:

1. **Matrix sync loop** is cancelled via task cancellation. The SDK client is disconnected.
2. **Meshtastic session** closes the radio interface connection.
3. **LXMF session** tears down the Reticulum link and stops the announce loop.
4. **MeshCore session** disconnects and cancels pending I/O.

Sessions are stopped as part of their adapter's `stop()` method; there is no separate session shutdown phase.

## 4. In-Flight Delivery Handling

When shutdown begins, the following categories of in-flight work exist:

| Category                           | v1 Behavior                                                                                                               | Notes                                           |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| Events being received by adapters  | Cancelled (task cancellation)                                                                                             | Receipt not written                             |
| Events being routed by pipeline    | Completed or cancelled within drain timeout                                                                               | Bounded by `shutdown_drain_timeout_seconds`     |
| Events being delivered to adapters | **Drained** — `PipelineRunner` awaits in-flight deliveries up to `shutdown_drain_timeout_seconds`, then cancels remaining | Delivery semaphore slots are released on cancel |
| Replay events in progress          | Cancelled; receipts preserved for completed deliveries                                                                    | Replay semaphore released on cancel             |

**What is drained vs abandoned:**

- **Drained:** In-flight adapter deliveries. The `PipelineRunner.stop()` method awaits all in-flight delivery tasks for up to `shutdown_drain_timeout_seconds` before cancelling them. Deliveries that complete within the drain window produce normal receipts and outcomes.
- **Abandoned after timeout:** Deliveries that do not complete within the drain window are cancelled. No retry is attempted for cancelled deliveries. The delivery outcome is recorded as a failure in diagnostics.

## 5. Timeout Behavior

### Per-phase max time

| Phase                | Timeout                                                                  | Status      |
| -------------------- | ------------------------------------------------------------------------ | ----------- |
| Adapter stop         | `shutdown_timeout_seconds` (default from config)                         | Implemented |
| Pipeline runner stop | Same global timeout                                                      | Implemented |
| Delivery drain       | `shutdown_drain_timeout_seconds` (from `[runtime.limits]`, default 5.0s) | Implemented |
| Storage close        | Same global timeout                                                      | Implemented |

### Overall shutdown budget

The overall shutdown budget is `shutdown_timeout_seconds` from `RuntimeConfig`. This timeout applies to the entire `stop()` call. Individual adapter stops use the same timeout value.

If the overall budget is exceeded, `RuntimeShutdownError` is raised with a summary of which subsystems failed. The runtime does not force-kill; it relies on the process exit to clean up.

## 6. Replay In-Progress

When shutdown occurs during an active replay:

- **Running replay is cancelled.** The replay async generator is not awaited; the caller's task is expected to handle cancellation.
- **Receipts are preserved.** Any receipts written to storage before shutdown remain durable. Partially-completed replay runs do not corrupt existing receipt data.
- **No replay-specific drain.** Replay does not have its own shutdown hook. The replay engine is not stopped explicitly during shutdown — it relies on the pipeline and storage being torn down.

## 7. Route Execution In-Progress

When shutdown occurs during route execution (pipeline processing an event):

- **Pending deliveries are completed or cancelled.** The pipeline runner's `stop()` method cancels any in-flight tasks.
- **No retry on shutdown.** If a delivery fails due to shutdown, it is not retried. The delivery outcome is recorded as a failure in diagnostics.
- **No deduplication concern.** Since shutdown is a terminal state, there is no risk of duplicate delivery on restart (the runtime does not support hot restart).

## 8. What Must Persist

The following data must be durable across shutdown:

| Data                                   | Storage                   | Persisted on shutdown?                     |
| -------------------------------------- | ------------------------- | ------------------------------------------ |
| Delivery receipts                      | SQLite / memory           | Yes — written on every successful delivery |
| Event store (canonical events)         | SQLite / memory           | Yes — written on append                    |
| Route statistics                       | In-memory (RouteStats)    | **No** — lost on shutdown                  |
| Diagnostic counters                    | In-memory (Diagnostician) | **No** — lost on shutdown                  |
| Adapter state (sync tokens, positions) | Per-adapter (SDK-managed) | Depends on adapter                         |
| Replay run results                     | Ephemeral (streamed)      | No — replay results are not persisted      |

Critical state (events, receipts) is written synchronously during normal operation, so shutdown does not need a separate flush step for these. In-memory counters and statistics are intentionally ephemeral.

## 9. Future Per-Adapter Restart

**Design direction only — not implemented.**

A future enhancement may support per-adapter restart without shutting down the entire runtime. The envisioned design:

- Each adapter manages its own lifecycle (`start()` / `stop()` / `restart()`).
- Restart does not affect other adapters or the pipeline.
- The runtime holds a reference to the adapter's task and can cancel and re-create it.
- Adapter restart preserves its configured routes and delivery plans.

This is not implemented in the current tranche. The current runtime only supports full start/stop cycles.

## 10. v1 Implementation

### What v1 implements

- `stop()` exists on `MedreApp` and stops subsystems in reverse dependency order: adapters → pipeline runner → storage.
- Adapters are stopped in reverse start order with per-adapter timeout.
- Individual adapter stop failures are logged and collected; a `RuntimeShutdownError` is raised if any subsystem fails.
- Pipeline runner has a `stop()` method that removes middleware from the event bus.
- Storage has a `close()` method that flushes and releases resources.
- `shutdown_event` is set before adapter shutdown begins, allowing waiters to react.
- Signal handlers (SIGINT, SIGTERM) set the shutdown event in the runner.
- Task cancellation works: adapter receive loops and sync loops respond to cancellation.

### v1 Shutdown State Machine

The `MedreApp` uses the `RuntimeState` enum (see Contract 47). Shutdown involves a single state transition:

```text
RUNNING → STOPPING → STOPPED
                   ↘ FAILED  (if errors during shutdown)
```

| State      | Description                                                                                                                                                                          |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `RUNNING`  | Normal operation. All adapters active.                                                                                                                                               |
| `STOPPING` | `stop()` has been called. Capacity controller stops accepting work, in-flight work is drained, adapters are stopped in reverse order, pipeline runner is stopped, storage is closed. |
| `STOPPED`  | Shutdown completed without errors.                                                                                                                                                   |
| `FAILED`   | Shutdown completed with one or more subsystem errors. `RuntimeShutdownError` is raised.                                                                                              |

The `RuntimeState` enum does **not** have substates for individual shutdown phases (e.g., there is no `SHUTDOWN_SIGNALLED`, `ADAPTERS_STOPPING`, `PIPELINE_STOPPING`, or `STORAGE_CLOSING` state). The runtime transitions directly from `RUNNING` to `STOPPING` when `stop()` is called, and from `STOPPING` to either `STOPPED` or `FAILED` when `stop()` completes. Phase-level progress is observable through structured logging (see §10.3), not through state transitions.

### v1 Drain Timeout Behavior

When shutdown begins with in-flight deliveries:

1. `capacity_controller.stop_accepting()` is called. This blocks new delivery and replay acquire calls.
2. In-flight work is drained: the runtime polls `capacity_controller.snapshot()` until both `delivery_current` and `replay_current` reach 0, or the `shutdown_drain_timeout_seconds` timeout expires.
3. `shutdown_event.set()` is called. This signals adapters and waiters that shutdown has begun.
4. Adapters are stopped in reverse start order. Each adapter's `stop()` is called with `shutdown_timeout_seconds`.
5. `PipelineRunner.stop()` is called. The runner removes middleware from the event bus and releases resources.
6. Storage is closed (flushes and releases SQLite resources).

Deliveries that complete during the drain window produce normal receipts and outcomes. Deliveries cancelled after the drain window are recorded as failures in diagnostics.

### v1 Diagnostics and Logging

During shutdown, the following is logged:

- **INFO:** `"Stopping MEDRE runtime {name} (timeout={timeout}s)"` — at the start of shutdown.
- **INFO:** `"Runtime stopping — accepting no new work"` — after `stop_accepting()`.
- **INFO:** `"In-flight work drained"` — when drain completes before timeout.
- **WARNING:** `"Drain timed out — {delivery_current} delivery, {replay_current} replay in-flight abandoned"` — when drain timeout expires with work still in flight.
- **INFO:** `"Adapter {transport}.{adapter_id} stopped"` — per adapter.
- **ERROR:** `"Error stopping adapter {transport}.{adapter_id}: {exc}"` — per adapter failure.
- **ERROR:** `"Error stopping pipeline runner: {exc}"` — if pipeline stop fails.
- **ERROR:** `"Error closing storage: {exc}"` — if storage close fails.
- **INFO:** `"Runtime stopped"` — on successful completion.

### What v1 does NOT implement (deferred to v2)

- **Per-adapter restart.** Only full runtime stop/start is supported. Individual adapters cannot be restarted independently.
- **Graceful connection drain.** Adapters do not drain pending transport-level operations (e.g., Matrix sync responses, Meshtastic pending packets) before disconnecting. The adapter's `stop()` cancels ongoing operations immediately.
- **Per-phase independent timeouts.** A single `shutdown_timeout_seconds` covers the overall stop sequence. The drain phase has its own `shutdown_drain_timeout_seconds`, but adapter stop and storage close share the global timeout.
- **Persistent route statistics.** `RouteStats` and `Diagnostician` counters are in-memory only and lost on shutdown.
- **Explicit replay cancellation during shutdown.** Replay relies on pipeline teardown; there is no separate replay shutdown hook.
- **Replay deduplication.** No deduplication on restart or during replay.
- **Exactly-once delivery guarantee.** Not provided.
- **Persistent queue.** Delivery state is in-memory only.
- **Distributed coordination.** Shutdown is local to the process.

See Contract 59 (Runtime Durability) for crash recovery expectations and Contract 60 (Runtime Cancellation) for detailed cancellation semantics.

## 11. Explicit Non-Goals

The following are explicitly out of scope for the current implementation:

- **Per-adapter restart.** The runtime does not support restarting a single adapter without shutting down the entire system.
- **Complex graceful drain.** There is no mechanism to wait for in-flight deliveries to complete with per-delivery timeouts, backoff, or partial completion tracking.
- **Hot restart / zero-downtime restart.** The runtime is a single-process application with no restart coordination.
- **State migration on shutdown.** In-memory state (route stats, diagnostic counters) is intentionally not persisted.

## 12. v2: Queue + Replay Coordination

v2 introduces `CapacityController` (see Contract 53, §15) as the central capacity manager. Shutdown now coordinates delivery drain, replay cancellation, and queue drain through this controller before adapter teardown begins.

**What v2 does not change:** No persistent in-flight recovery. No replay resume after shutdown. MEDRE remains best-effort — no exactly-once guarantees, no transactional delivery guarantees, no persistent queue. Radio transports remain probabilistic.

### 12.1 Replay Cancellation During Shutdown

When `MedreApp.stop()` is called:

1. `capacity_controller.stop_accepting()` is called. This sets `accepting_work = False`.
2. All subsequent `acquire_replay()` calls return `False` immediately, with the `replay_rejections` internal gauge incremented (maps to `outbound_failed` in `RuntimeAccounting`).
3. In-flight replay deliveries (those that already acquired a slot) continue executing until they complete or the drain timeout expires.
4. No new replay work is admitted after this point. The `ReplayEngine` does not have its own shutdown hook — it relies on `CapacityController` to gate new work and on the pipeline teardown to cancel any remaining tasks.

### 12.2 Replay Drain Timeout Participation

The drain loop (step 2 of shutdown) observes **both** delivery and replay in-flight counts via `capacity_controller.snapshot()`:

```python
drain_deadline = now + shutdown_drain_timeout_seconds
while now < drain_deadline:
    snap = capacity_controller.snapshot()
    if snap["delivery_current"] == 0 and snap["replay_current"] == 0:
        log("In-flight work drained")
        break
    await asyncio.sleep(0.1)
else:
    log warning with snap["delivery_current"], snap["replay_current"]
```

Replay work that completes within the drain window produces normal results. Replay work that does not complete is abandoned — no retry, no persistent recovery.

### 12.3 Queue Drain Before Adapter Teardown

The shutdown sequence in v2 is:

| Step | Action               | Detail                                                                                                       |
| ---- | -------------------- | ------------------------------------------------------------------------------------------------------------ |
| 1    | Stop accepting work  | `capacity_controller.stop_accepting()` — blocks new delivery and replay                                      |
| 2    | Drain in-flight work | Poll `capacity_controller.snapshot()` until both `delivery_current` and `replay_current` reach 0, or timeout |
| 3    | Signal shutdown      | `shutdown_event.set()` — notifies adapters and waiters                                                       |
| 4    | Stop adapters        | Reverse start order, each with `shutdown_timeout_seconds`                                                    |
| 5    | Stop pipeline runner | Remove middleware, release resources                                                                         |
| 6    | Close storage        | Flush and release SQLite resources                                                                           |

**Key change from v1:** In v1, the pipeline runner's `stop()` awaited in-flight deliveries independently. In v2, the drain happens at the `CapacityController` level _before_ adapters are stopped. This ensures that:

- Delivery capacity slots are released before adapters tear down their transport connections.
- Replay capacity slots are included in the drain check.
- The diagnostics snapshot captures the drain outcome with accurate in-flight counts.

### 12.4 Diagnostics Report Drain Outcome

The drain phase logs the outcome:

- **Successful drain:** `"In-flight work drained"` — both counters reached 0 before timeout.
- **Drain timeout:** `"Drain timed out — {delivery_current} delivery, {replay_current} replay in-flight abandoned"` — some work did not complete within the drain window. The `capacity_controller.snapshot()` provides the exact counts of abandoned work.

The runtime diagnostics snapshot (via `MedreApp.diagnostics_snapshot()`) includes CapacityController **internal gauge** names. These are semaphore-level counters inside `CapacityController`, distinct from the operator-facing `RuntimeAccounting` counters (`capacity_rejections`, `outbound_failed`, etc.).

```json
{
  "capacity": {
    "accepting_work": false,
    "delivery_current": 0,
    "delivery_limit": 64,
    "delivery_rejections": 3,
    "delivery_timeouts": 1,
    "replay_current": 0,
    "replay_limit": 32,
    "replay_rejections": 2,
    "replay_timeouts": 0
  },
  "shutdown_drain_timeout_seconds": 5.0
}
```

This snapshot is available after shutdown completes and can be inspected to determine how many deliveries were rejected or timed out during the shutdown sequence.

### 12.5 No Persistent In-Flight Recovery

In-flight deliveries and replay events that are abandoned at shutdown are **not** recovered on restart. There is no persistent in-flight queue, no replay resume mechanism, and no deduplication on restart. This is an explicit design decision:

- Delivery state is in-memory only.
- Restart begins with a clean in-flight state.
- Replay may be re-triggered manually by the operator, but it processes from storage (not from the abandoned in-flight set).
- Receipts written before shutdown are preserved in storage. Partially-completed deliveries are not retried.

## 13. Cross-References

| Topic                                                                         | Contract                           |
| ----------------------------------------------------------------------------- | ---------------------------------- |
| CapacityController, delivery/replay capacity bounds, exhaustion behavior      | Contract 53 (Resource Control)     |
| Runtime assembly, `RuntimeState` lifecycle, startup classification            | Contract 47 (Runtime Assembly)     |
| Durability semantics, what survives crash, process-local vs persisted         | Contract 59 (Runtime Durability)   |
| Cancellation semantics, CapacityController stop behavior, stop-during-startup | Contract 60 (Runtime Cancellation) |
| Persistence timing, WAL consistency, receipt durability                       | Contract 55 (Runtime Persistence)  |

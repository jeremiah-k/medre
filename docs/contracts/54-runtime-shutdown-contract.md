# Contract 54 — Runtime Shutdown Contract

**Status:** Partial Implementation
**Scope:** Shutdown ordering, in-flight work handling, persistence guarantees, and timeout behavior for the MEDRE runtime.
**Audience:** Runtime builders, adapter authors, operators.
**References:** Contract 47 (Runtime Assembly), Contract 48 (Runtime Observability), Contract 31 (Session Boundary), Contract 53 (Resource Control).


## 1. Shutdown Phases

Shutdown proceeds through five ordered phases. Each phase must complete (or time out) before the next begins.

| Phase | Name | Description |
|-------|------|-------------|
| 1 | **Signal** | `shutdown_event.set()` — notifies all waiters that shutdown has begun. New event ingestion stops. |
| 2 | **Stop accepting** | Adapters stop ingesting new events from their transports. In-flight receives are cancelled. |
| 3 | **Drain** | Pending deliveries in the pipeline are completed or abandoned per per-phase timeout. *(Not yet implemented.)* |
| 4 | **Persist** | Receipts, counters, and diagnostic state are flushed to durable storage. |
| 5 | **Release** | Transport connections, SDK clients, and file handles are released. |


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

| Category | Current Behavior | Target Behavior |
|----------|-----------------|-----------------|
| Events being received by adapters | Cancelled (task cancellation) | Cancelled; receipt not written |
| Events being routed by pipeline | Completed or cancelled | Completed within timeout |
| Events being delivered to adapters | **No drain** — `stop()` is called immediately | Drain with per-delivery timeout |
| Replay events in progress | Not tracked | Cancelled; receipts preserved for completed deliveries |

**What is drained vs abandoned:**

- **Drained:** Nothing currently. The runtime stops adapters immediately without waiting for pending deliveries to complete.
- **Abandoned:** All in-flight adapter deliveries, active sync loops, pending route executions.

A future implementation should add a drain phase between "stop accepting" and "persist" that completes pending deliveries up to a configurable timeout.


## 5. Timeout Behavior

### Per-phase max time

| Phase | Timeout | Current |
|-------|---------|---------|
| Adapter stop | `shutdown_timeout_seconds` (default from config) | Implemented |
| Pipeline runner stop | Same global timeout | Implemented |
| Storage close | Same global timeout | Implemented |
| Drain | N/A | Not implemented |

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

| Data | Storage | Persisted on shutdown? |
|------|---------|----------------------|
| Delivery receipts | SQLite / memory | Yes — written on every successful delivery |
| Event store (canonical events) | SQLite / memory | Yes — written on append |
| Route statistics | In-memory (RouteStats) | **No** — lost on shutdown |
| Diagnostic counters | In-memory (Diagnostician) | **No** — lost on shutdown |
| Adapter state (sync tokens, positions) | Per-adapter (SDK-managed) | Depends on adapter |
| Replay run results | Ephemeral (streamed) | No — replay results are not persisted |

Critical state (events, receipts) is written synchronously during normal operation, so shutdown does not need a separate flush step for these. In-memory counters and statistics are intentionally ephemeral.


## 9. Future Per-Adapter Restart

**Design direction only — not implemented.**

A future enhancement may support per-adapter restart without shutting down the entire runtime. The envisioned design:

- Each adapter manages its own lifecycle (`start()` / `stop()` / `restart()`).
- Restart does not affect other adapters or the pipeline.
- The runtime holds a reference to the adapter's task and can cancel and re-create it.
- Adapter restart preserves its configured routes and delivery plans.

This is not implemented in the current tranche. The current runtime only supports full start/stop cycles.


## 10. Current State

### What works

- `stop()` exists on `MedreApp` and stops subsystems in reverse dependency order.
- Adapters are stopped in reverse start order with per-adapter timeout.
- Individual adapter stop failures are logged and collected; a `RuntimeShutdownError` is raised if any subsystem fails.
- Pipeline runner has a `stop()` method.
- Storage has a `close()` method that flushes and releases resources.
- `shutdown_event` is set before adapter shutdown begins, allowing waiters to react.
- Signal handlers (SIGINT, SIGTERM) set the shutdown event in the runner.
- Task cancellation works: adapter receive loops and sync loops respond to cancellation.

### What does not exist yet

- **No drain phase.** Adapters are stopped immediately; pending deliveries are not awaited.
- **No per-adapter restart.** Only full runtime stop/start is supported.
- **No complex graceful drain implementation.** The shutdown does not wait for in-flight deliveries to complete before proceeding to the persist phase.
- **No explicit replay cancellation during shutdown.** Replay relies on pipeline teardown.
- **No per-phase independent timeouts.** A single `shutdown_timeout_seconds` covers the entire stop sequence.
- **No persistent route statistics.** `RouteStats` and `Diagnostician` counters are in-memory only.


## 11. Explicit Non-Goals

The following are explicitly out of scope for the current implementation:

- **Per-adapter restart.** The runtime does not support restarting a single adapter without shutting down the entire system.
- **Complex graceful drain.** There is no mechanism to wait for in-flight deliveries to complete with per-delivery timeouts, backoff, or partial completion tracking.
- **Hot restart / zero-downtime restart.** The runtime is a single-process application with no restart coordination.
- **State migration on shutdown.** In-memory state (route stats, diagnostic counters) is intentionally not persisted.

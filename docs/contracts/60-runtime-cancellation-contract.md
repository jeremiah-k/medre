# Contract 60 — Runtime Cancellation Contract

**Status:** Active
**Scope:** Cancellation semantics for the MEDRE runtime: CapacityController stop behavior, semaphore acquire behavior under shutdown, delivery and replay cancellation, stop-during-startup, idempotent stop, and explicit non-guarantees.
**Audience:** Runtime builders, adapter authors, operators.
**Tracks:** 9 (evidence consolidation and boundary enforcement)
**References:** Contract 47 (Runtime Assembly), Contract 53 (Resource Control), Contract 54 (Runtime Shutdown), Contract 59 (Runtime Durability), Contract 61 (Operational Evidence).
**Last reviewed:** 2026-05-25

Every agent or document that references MEDRE cancellation behavior, stop semantics, shutdown task cancellation, or capacity controller stop behavior must defer to this contract.

**Evidence separation (Track 9):** Cancellation behavior in this contract is backed by S-tier (simulated/fake) evidence from deterministic unit tests (`test_runtime_cancellation.py`, `test_runtime_hygiene.py`, `test_runtime_recovery.py`). No R-tier (real-live-runtime) evidence for cancellation under live transport conditions has been collected. Cancellation under real network latency, adapter reconnect during shutdown, and transport SDK cancellation propagation are NOT EXECUTED at R-tier. See Contract 61 §5.1 for evidence scores.

## 1. Scope

This contract specifies how the MEDRE runtime cancels work. It covers:

- What happens when `CapacityController.stop_accepting()` is called.
- How in-flight semaphore acquires behave during shutdown.
- How delivery and replay work is cancelled.
- What happens when `stop()` is called during startup.
- Idempotency of `stop()`.
- What cancellation does **not** guarantee.

## 2. RuntimeState and Cancellation

The `RuntimeState` enum has six states:

```text
INITIALIZED → STARTING → RUNNING → STOPPING → STOPPED
                                 ↘ FAILED
```

Cancellation is relevant in the `STOPPING` state. The runtime transitions from `RUNNING` to `STOPPING` when `stop()` is called. The `STOPPING` state covers the entire shutdown sequence (drain, adapter stop, pipeline stop, storage close).

The runtime transitions to `FAILED` if any subsystem fails during shutdown.

There are no substates within `STOPPING` for individual shutdown phases. Phase-level progress is observable through structured logging, not through state transitions.

## 3. CapacityController Cancellation

### 3.1 stop_accepting()

When `CapacityController.stop_accepting()` is called (during `MedreApp.stop()`):

1. `_accepting_work` is set to `False`.
2. All subsequent `acquire_delivery()` calls return `False` immediately. The `delivery_rejections` internal gauge is incremented (maps to `outbound_failed` in `RuntimeAccounting`).
3. All subsequent `acquire_replay()` calls return `False` immediately. The `replay_rejections` internal gauge is incremented (maps to `outbound_failed` in `RuntimeAccounting`).
4. In-flight work (slots already acquired) continues executing until completion or the drain timeout expires.
5. The controller does **not** cancel in-flight work directly. It gates new admissions.

### 3.2 Acquire Behavior Under Shutdown

The `acquire_delivery()` and `acquire_replay()` methods follow this flow:

```python
acquire():
  if not accepting_work:
    increment rejection counter
    return False                    # Fast reject — no semaphore interaction
  await semaphore.acquire(timeout=delivery_acquire_timeout_seconds)
    if not accepting_work (re-check):
      release semaphore             # Release the slot we just acquired
      increment rejection counter
      return False                  # Reject after acquiring — work stopped while waiting
    increment current counter
    return True                     # Proceed with work
  except TimeoutError:
    increment timeout counter
    return False                    # Timed out waiting for a slot
```

**Key properties:**

- If `stop_accepting()` is called while a coroutine is waiting on the semaphore, the coroutine will either acquire the slot and then immediately release it (re-check catches the stop), or time out. In either case, the coroutine does not proceed with work.
- There is no deadlock risk: the re-check after semaphore acquire ensures that slots are never held by a coroutine that should have been rejected.
- The `accepting_work` flag is a simple boolean, not a synchronization primitive. It is checked at two points: before the semaphore wait, and after the semaphore wait succeeds. This is sufficient for the single-threaded async model MEDRE uses.

### 3.3 What Stop Accepting Does Not Do

- It does **not** cancel in-flight deliveries or replay events.
- It does **not** release held semaphore slots. In-flight work that already acquired a slot continues to hold it until the work completes or is cancelled by the pipeline teardown.
- It does **not** close transport connections. That happens during adapter `stop()`.

## 4. Shutdown Cancellation Sequence

When `MedreApp.stop()` is called, cancellation proceeds through these ordered steps:

### Step 1: Stop Accepting New Work

```yaml
RuntimeState: RUNNING → STOPPING
capacity_controller.stop_accepting()   # No new deliveries or replay admitted
```

### Step 2: Drain In-Flight Work

```python
drain_deadline = now + shutdown_drain_timeout_seconds
while now < drain_deadline:
    snap = capacity_controller.snapshot()
    if snap.delivery_current == 0 and snap.replay_current == 0:
        break                          # All work drained
    await asyncio.sleep(0.1)           # Poll interval
else:
    log warning with remaining counts   # Drain timeout — work abandoned
```

During the drain phase:

- In-flight deliveries and replay events **continue executing**. They are not cancelled during the drain.
- If both `delivery_current` and `replay_current` reach zero before the deadline, the drain is successful.
- If the deadline expires with work remaining, the drain times out. The remaining work is abandoned — it will be cancelled when adapters are stopped (their tasks are cancelled) or when the pipeline runner is stopped.

### Step 3: Signal Shutdown

```text
shutdown_event.set()                   # Notifies adapters and waiters
```

The `shutdown_event` is an `asyncio.Event`. It is set after the drain phase (not before). Adapters that are waiting on this event will wake up and begin their own shutdown.

### Step 4: Stop Adapters

Adapters are stopped in **reverse start order**. Each adapter's `stop(timeout=...)` is called with `shutdown_timeout_seconds`. Adapter stop failures are logged but do not prevent other adapters from shutting down.

Adapter receive loops and sync loops respond to task cancellation (asyncio `CancelledError`). The adapter's `stop()` method should cancel its internal tasks and close transport connections.

### Step 5: Stop Pipeline Runner

`PipelineRunner.stop()` removes middleware from the event bus and releases resources.

### Step 6: Close Storage

`Storage.close()` flushes SQLite WAL buffers and releases the connection.

## 5. Cancellation During Delivery

### 5.1 Per-Target Delivery Cancellation

Each per-target delivery acquires a `CapacityController` slot in a `try/finally` block:

```python
async def _deliver_to_target(...):
    if not await capacity_controller.acquire_delivery():
        return DeliveryOutcome(status="permanent_failure", error="delivery_capacity_exceeded", failure_kind=CAPACITY_REJECTION)
# (or error="delivery_rejected_shutdown", failure_kind=SHUTDOWN_REJECTION if runtime has stopped accepting work)
    try:
        result = await adapter.deliver(payload)
    finally:
        await capacity_controller.release_delivery()
```

If the adapter's `deliver()` coroutine is cancelled (e.g., by task cancellation during adapter stop), the `CancelledError` propagates up through the `try/finally`, ensuring the slot is released via `release_delivery()` in the `finally` block.

### 5.2 Fan-Out Cancellation

When a delivery fans out to multiple targets, each target's delivery is an independent coroutine. If one target's delivery is cancelled, the others are **not** affected — each holds its own capacity slot and manages its own lifecycle.

### 5.3 No Cancellation of Already-Completed Deliveries

If a delivery completes (success or failure) before the drain timeout expires, it is not affected by shutdown. The delivery receipt is written to storage. The capacity slot is released normally.

## 6. Cancellation During Replay

### 6.1 Replay Capacity Gating

Replay deliveries acquire a replay slot via `acquire_replay()`:

```python
async def _stage_deliver(...):
    if not await capacity_controller.acquire_replay():
        return ReplayResult(status="error", error="replay_capacity_exceeded")
# (or error="replay_rejected_shutdown" if runtime has stopped accepting work)
    try:
        result = await pipeline_runner.handle_ingress(event)
    finally:
        await capacity_controller.release_replay()
```

The same cancellation semantics apply as for delivery: `CancelledError` propagates through the `finally` block, releasing the slot.

### 6.2 Replay Has No Shutdown Hook

The `ReplayEngine` does not have its own `stop()` method. Replay cancellation relies on:

1. `CapacityController.stop_accepting()` to gate new replay admissions.
2. Pipeline teardown to cancel any remaining replay delivery tasks.
3. The drain phase to observe `replay_current` reaching zero (or timeout).

### 6.3 Non-Delivery Replay Modes Are Not Cancelled

Replay modes that do not involve delivery (`RE_RENDER`, `RE_ROUTE`, `DRY_RUN`) do not acquire replay capacity slots. They are read-only operations that complete normally. They do not participate in the drain or cancellation logic.

## 7. Stop During Startup

### 7.1 Concurrent Stop While Adapters Are Starting

If `stop()` is called while the runtime is in `STARTING` state (adapters are still being started):

- `stop()` transitions the state to `STOPPING`.
- Already-started adapters are stopped in reverse order.
- Not-yet-started adapters are not started (the startup loop is sequential, so `stop()` waits for the current adapter to finish starting before proceeding with shutdown).

### 7.2 Idempotent Stop

`stop()` is idempotent:

- If the runtime is in `INITIALIZED` state (never started), `stop()` returns immediately without transitioning.
- If the runtime is in `STOPPED` state (already stopped), `stop()` returns immediately.
- If the runtime is in `STOPPING` state (concurrent `stop()` calls), the state is already `STOPPING` and the second call proceeds through the shutdown sequence. **Note:** The current implementation does not serialize concurrent `stop()` calls with a lock. Concurrent `stop()` invocations may interleave. The `RuntimeState` transition to `STOPPING` is atomic (single assignment), but the shutdown sequence itself is not mutex-guarded.

### 7.3 Startup Failure Cleanup

If startup fails (total failure or core subsystem error):

1. Already-started adapters are stopped in reverse order via `_cleanup_started_adapters()`.
2. Pipeline runner and storage are cleaned up via `_cleanup_core_resources()`.
3. State transitions to `FAILED`.
4. `RuntimeStartupError` is raised.
5. Callers do **not** need to call `stop()` after a startup failure — cleanup is performed before the exception propagates.

## 8. Task Hygiene

### 8.1 No Leaked Tasks After Stop

After a clean `stop()` completes (state → `STOPPED`), the following must hold:

- No asyncio tasks created by the runtime remain running.
- All adapter receive loops and sync loops have been cancelled.
- The pipeline runner has removed its middleware from the event bus.
- Storage has been closed.

### 8.2 Task Cancellation Pattern

Adapters and the pipeline runner use standard asyncio cancellation:

1. `task.cancel()` is called on the task.
2. The task's coroutine receives `CancelledError` at the next `await` point.
3. The coroutine's `finally` blocks execute (releasing capacity slots, closing connections).
4. The task completes with `CancelledError`.

This is the standard Python asyncio cancellation model. MEDRE does not use shielded tasks, timeout shields, or custom cancellation logic.

## 9. Explicit Non-Guarantees

The following are explicitly **not** guaranteed by the cancellation system:

- **Graceful transport drain.** Adapters cancel receive loops immediately. They do not wait for pending transport-level operations (e.g., Matrix sync responses, Meshtastic pending packets) to complete.
- **Atomic cancellation.** Cancellation proceeds through phases sequentially. A crash during any phase may leave the runtime in a partially-cancelled state.
- **Distributed cancellation.** Cancellation is local to the process. There is no mechanism to coordinate cancellation across multiple MEDRE instances.
- **Cancellation ordering within a phase.** Within the adapter stop phase, adapters are stopped sequentially (reverse start order), but there is no ordering guarantee between the adapter stop phase and the pipeline runner stop phase — they are sequential steps, not concurrent.
- **Per-delivery cancellation tracking.** The runtime does not track which individual deliveries were cancelled. It logs the count of abandoned deliveries at drain timeout, but not their identities.
- **Cancellation recovery.** Cancelled deliveries are not retried. Cancelled replay runs are not resumed. The operator must re-initiate replay manually if needed.

## 10. Test Coverage

Cancellation behavior is covered by the following test files:

| Test file                            | Coverage                                                                                                                                                                                                                                 |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/test_runtime_cancellation.py` | Repeated cancellation cycles, task leak checks, cancellation under load, shutdown during replay, shutdown during capacity wait, shutdown during delivery fanout, stop during startup, repeated stop races, cleanup timeout observability |
| `tests/test_runtime_recovery.py`     | Classification correctness for degraded/failed states, partial adapter startup, replay availability after restart, capacity reset on restart, SQLite persistence across restart                                                          |
| `tests/test_runtime_hygiene.py`      | Task hygiene after stop, adapter state transitions, runtime state transitions                                                                                                                                                            |

These tests use fake adapters only and do not require live transport dependencies.

## 11. Cross-References

| Topic                                                                     | Contract                          |
| ------------------------------------------------------------------------- | --------------------------------- |
| CapacityController, delivery/replay capacity bounds, acquire/release flow | Contract 53 (Resource Control)    |
| Shutdown ordering, drain phases, timeout behavior                         | Contract 54 (Runtime Shutdown)    |
| Durability semantics, what survives crash, process-local vs persisted     | Contract 59 (Runtime Durability)  |
| Runtime assembly, `RuntimeState` lifecycle, startup classification        | Contract 47 (Runtime Assembly)    |
| Persistence timing, WAL consistency, receipt durability                   | Contract 55 (Runtime Persistence) |

## 12. Outbox Interaction During Shutdown

### 12.1 Outbox Item Creation Before Shutdown

Outbox items are created in ``PipelineRunner._deliver_one`` after route/policy/loop/capacity acceptance and before the adapter delivery attempt.  If ``stop_accepting()`` has been called, the capacity controller rejects the delivery before outbox creation — no pending outbox item is left behind.

### 12.2 In-Flight Outbox Items at Shutdown

During the drain phase of shutdown:

- If a delivery completes within the drain window, the outbox item is updated normally (``sent``, ``queued``, ``retry_wait``, or ``dead_lettered``).
- If the drain timeout expires while an outbox item is ``in_progress``, the item retains its ``in_progress`` status with an expired lease.  On restart, the item is re-claimable by the RetryWorker.
- The shutdown does **not** cancel, delete, or modify outbox items directly — it relies on lease expiry for recovery.

### 12.3 RetryWorker Shutdown

When ``RetryWorker.stop()`` is called:

1. The shutdown event is set.
2. In-flight retry attempts that are already mid-processing continue to completion (they are not cancelled mid-delivery).
3. Items that were claimed but not yet processed have their lease released via ``release_outbox_claim`` (capacity rejection path) or retain their lease (which expires naturally).
4. The worker waits up to 5 seconds for the internal loop to exit.

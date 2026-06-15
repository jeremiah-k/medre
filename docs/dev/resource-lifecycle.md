# Resource Lifecycle

Runtime resource ownership, creation, teardown, and failure behavior for
contributors working on the MEDRE runtime.

## Resource Ownership Inventory

The table below covers every runtime resource that carries an explicit lifecycle
(open/create, close/stop/cancel). Resources that are plain data objects
(RuntimeAccounting, Route objects, config models) are omitted because they have
no teardown concerns.

### Core Runtime Resources

| Resource                      | Created by                              | Owner                                           | Teardown site                                                                                                                                                      | Idempotent?      |
| ----------------------------- | --------------------------------------- | ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------- |
| SQLiteStorage executor        | `_run_in_thread()` (lazy)               | `_SQLiteStorageBase._executor`                  | `close()` sets `_executor=None` then `asyncio.to_thread(executor.shutdown, wait=True)`                                                                             | Yes              |
| SQLiteStorage connection      | `initialize()`                          | `_SQLiteStorageBase._db`                        | `close()` sets `_db=None` before awaiting `db.close()`                                                                                                             | Yes              |
| RetryWorker task              | `start()` via `create_task`             | `RetryWorker._task`                             | `stop()` sets shutdown event, waits configurable timeout (standalone default 5 s; app-managed uses `runtime.shutdown_timeout_seconds`, default 10 s), then cancels | Yes              |
| PipelineRunner middleware     | `start()`                               | `PipelineRunner`                                | `stop()` removes middleware, sets `_running=False`                                                                                                                 | Yes              |
| CapacityController semaphores | constructor                             | `MedreApp._capacity_controller`                 | `stop_accepting()` gates new work; semaphores drain naturally                                                                                                      | N/A (gate)       |
| ReplayEngine cancel event     | constructor                             | `MedreApp._replay_engine`                       | `cancel()` sets event                                                                                                                                              | Yes              |
| Runtime shutdown event        | `RuntimeBuilder.build()`                | `MedreApp.shutdown_event`                       | `set()` in `stop()`                                                                                                                                                | Yes              |
| EventBuffer                   | `MedreApp.__post_init__()`              | `MedreApp._event_buffer`                        | None (GC, bounded deque)                                                                                                                                           | N/A              |
| Inflight delivery records     | `_deliver_single_target()` per delivery | `PipelineRunner._inflight_deliveries` dict      | `finally` block pops per delivery; `drain_abandoned_deliveries()` at shutdown                                                                                      | N/A              |
| Outbox lease renewal task     | `_deliver_single_target()` per delivery | local in `_deliver_single_target` finally block | `cancel()` + await in finally block                                                                                                                                | N/A              |
| Plugin shutdown               | plugin author                           | Plugin protocol                                 | `shutdown()` called if present                                                                                                                                     | Plugin-dependent |

### Adapter Resources

Adapters create transport-specific resources during `start()`. Each adapter
cleans up its session and background tasks in its own `stop(timeout)` method.

| Resource                                                        | Created by                      | Owner               | Teardown site                                                                               | Idempotent? |
| --------------------------------------------------------------- | ------------------------------- | ------------------- | ------------------------------------------------------------------------------------------- | ----------- |
| MatrixSession client, sync task, join tasks                     | `MatrixSession.start()`         | `MatrixSession`     | `stop()` cancels tasks, closes SDK client                                                   | Yes         |
| MeshtasticSession client, reconnect task                        | `MeshtasticSession.start()`     | `MeshtasticSession` | `stop()` cancels reconnect, calls `close_fn()`                                              | Yes         |
| MeshtasticAdapter drain task, background tasks, inbound futures | `MeshtasticAdapter.start()`     | adapter             | `stop()` clears `_started`, cancels drain, drains background tasks + futures, stops session | Yes         |
| MeshtasticOutboundQueue                                         | `MeshtasticAdapter` constructor | adapter             | No explicit close; items abandoned on drain task cancellation (by design)                   | N/A         |
| LxmfSession announce task, reconnect task, RNS transport        | `LxmfSession.start()`           | `LxmfSession`       | `stop()` cancels tasks                                                                      | Yes         |
| LxmfAdapter background tasks                                    | `LxmfAdapter.start()`           | adapter             | `stop()` drains background tasks, stops session                                             | Yes         |
| MeshCoreSession reconnect task, SDK client                      | `MeshCoreSession.start()`       | `MeshCoreSession`   | `stop()` cancels reconnect                                                                  | Yes         |
| MeshCoreAdapter background tasks                                | `MeshCoreAdapter.start()`       | adapter             | `stop()` drains background tasks, stops session                                             | Yes         |

## Shutdown Sequence

`MedreApp.stop()` in `src/medre/runtime/app.py` follows this fixed order.
Errors at any step are accumulated, not raised immediately, so later cleanup
always runs.

```text
 1. Idempotency guard        -- return if STOPPED, STOPPING, or INITIALIZED
 2. State -> STOPPING
 3. Stop accepting new work   -- capacity_controller.stop_accepting()
 4. Cancel replay engine      -- replay_engine.cancel()
 5. Stop retry worker         -- retry_worker.stop() (grace = runtime.shutdown_timeout_seconds, default 10 s; standalone constructor default is 5 s)
 6. Drain in-flight work      -- poll capacity until empty or deadline
 7. Persist abandoned evidence -- suppressed receipts for timed-out deliveries
 8. Set shutdown event        -- signals all adapters
 9. Stop adapters (pass 1)    -- reverse start order, runtime-enforced timeout
10. Stop adapters (pass 2)    -- never-started adapters
11. Stop pipeline runner
12. Close storage
13. Raise or succeed          -- RuntimeShutdownError if any errors collected
```

## Per-Resource Details

### SQLiteStorage

**Creation.** Two paths, chosen at import time by the `aiosqlite` availability
check:

- aiosqlite path: `await aiosqlite.connect(path)` in `initialize()`.
- Sync fallback path: `sqlite3.connect(path)` dispatched through a
  `ThreadPoolExecutor(max_workers=1)`, created lazily on first call to
  `_run_in_thread()`.

**Teardown.** `close()` is idempotent. An early-return guard
(`if self._closed: return`) prevents concurrent callers from racing.
On first call:

1. Sets `_closed = True` as an early gate for race safety; restored to
   `False` if the close I/O fails so a later `close()` can retry.
2. Saves `_db` to a local, then sets `self._db = None` **before** awaiting
   `db.close()`. This prevents concurrent callers from racing to close the
   same connection.
3. For the aiosqlite path: the close coroutine is wrapped in an explicit
   `asyncio.create_task(...)` and then awaited under
   `asyncio.shield(...)`. A strong reference to the close task is held
   by a local binding for the duration of the await. If a stray
   `CancelledError` arrives (e.g. from caller cancellation or an external
   `CancelledError`), the shielded close continues running to completion
   so aiosqlite can join its internal worker thread, and the cancellation
   is re-raised afterwards. Without the shield+task pattern the
   `CancelledError` would interrupt the close before aiosqlite's
   internal thread was joined, leaving the connection half-closed and
   triggering `ResourceWarning: <aiosqlite.core.Connection ...> was
deleted before being closed` in `__del__`. On any non-cancellation
   failure during the shielded close, `self._db = db` and `_closed` are
   restored before the exception is re-raised so a later `close()` call
   can retry. The inner `await close_task` uses `except BaseException`
   (not `except Exception`) so that `KeyboardInterrupt` / `SystemExit`
   cannot replace the triggering exception with a new one.
4. In the `finally` block: saves `_executor` to a local, sets
   `self._executor = None`, and calls
   `asyncio.to_thread(executor.shutdown, wait=True)`. The `wait=True`
   fully joins worker threads; the `to_thread` offload ensures the join
   does not block the event loop.

**Logging.** The `close()` method itself emits no logs. Debug-level logging
appears only in the error-recovery path if `close()` itself fails during a
failed `initialize()`.

**Test boundaries.** Idempotent close, concurrent close, and the
`_db = None` before await pattern are exercised in unit tests with fake
adapters and in-memory storage. No live/hardware validation claims apply.

### RetryWorker

**Creation.** `start()` calls `asyncio.create_task(self._run_loop())` if the
worker is enabled and no task already exists. The run loop polls on
`self._interval`, breaking immediately when `_shutdown_event` is set.

**Teardown.** `stop()` is idempotent (returns immediately if `_task is None`),
and is serialised by an internal `asyncio.Lock` so concurrent callers
cannot emit duplicate `retry_stopped` / `retry_abandoned` events:

1. Sets `_shutdown_event`, cooperatively signaling the loop to break.
2. **Polling-based wait, not `asyncio.wait_for`.** Polls `task.done()` at
   10 ms intervals until either the task finishes or the grace period
   expires. Polling is used because `asyncio.wait_for` cannot terminate
   a coroutine that catches and suppresses `asyncio.CancelledError` —
   the cancel is consumed by an inner `except` block and the await never
   raises, leaving `wait_for` to wait forever. Polling `task.done()` is
   the only reliable hard bound for cancellation-resistant coroutines.
3. **Forced cancellation.** If the cooperative stage times out, the task is
   `cancel()`-ed once and polled again for a second bounded grace
   period. (The same `asyncio.wait_for`-cannot-terminate rationale
   applies; the cancel grace is therefore also a poll loop, not a
   `wait_for`.)

Both stages use the `stop_timeout_seconds` grace. The **standalone
default** (when constructing `RetryWorker` directly) is **5.0 s**.
When the worker is created by `MedreApp.start()`, it is wired from
`config.runtime.shutdown_timeout_seconds` (the `runtime` section,
default **10.0 s**), not from any `retry` config field —
see the shutdown sequence below.

Outcomes:

- **Cancellation-responsive task** (the common case): the task finishes
  within the second grace period. `_task` is cleared,
  `state.running` is set to `False`, and a `retry_stopped` event is
  emitted.
- **Cancellation-resistant task** (rare; e.g. an adapter's stop
  suppresses cancellation or a long-blocking storage call refuses to
  release): the task is still alive after both grace periods. `_task`
  is cleared; the underlying coroutine is retained via `_abandoned_task`
  (it may still complete and clean itself up later), `state.abandoned`
  is set to `True`, `state.running` is set to `False` (the retained task
  may still be alive, but the worker is no longer considered running), a
  `retry_abandoned` event is emitted, and `stop()` returns without
  re-raising. While
  `state.abandoned` is `True`, subsequent `start()` calls are
  **refused** to prevent launching a duplicate worker over the same
  outbox. The caller (operator / supervisor) must inspect
  `state.abandoned` and either reset the worker (e.g. by waiting for
  the abandoned task to finish naturally) or shut the entire runtime
  down.
- **External cancellation of `stop()` itself** (e.g. `MedreApp.stop()`
  hits a shutdown timeout and cancels its inner cleanup work): the
  `asyncio.CancelledError` is caught inside the polling loop. Two
  sub-cases:
  - **Task already done at cancellation time** (race between the
    polling loop's cooperative check and the external cancel): the
    worker does **clean-stop** cleanup — `_task` is cleared,
    `state.running` is set to `False`, a `retry_stopped` event is
    emitted — and then the `CancelledError` is re-raised. This
    prevents the old leak of `_task` and `state.running=True` — the
    historical race where cancellation arrived a tick after the task
    finished naturally and neither was cleaned up.
  - **Task still alive**: the worker is marked abandoned
    (`state.abandoned = True`, `state.running` set to `False`; the
    retained task may still be alive), a `retry_abandoned` event with
    `reason="stop_cancelled"` is emitted, and the `CancelledError` is
    re-raised. This makes the "stop was cancelled" state
    distinguishable from "stop succeeded" and from "stop never called".

`stop()` will never hang indefinitely: even in the cancellation-
resistant case it returns within `2 * stop_timeout_seconds`.

The in-flight batch loop checks `_shutdown_event.is_set()` between items,
so a shutdown mid-batch completes the current item but skips the rest.

**Lease model.** Each cycle calls `storage.claim_due_outbox_items()` with a
lease of `interval * 1.5` seconds (minimum 30 s). There is no periodic lease
renewal heartbeat. Leases naturally expire if the worker crashes; the next
cycle reclaims them.

**Logging.** `start()` logs at INFO. `stop()` logs at INFO on clean stop
("RetryWorker stopped") and at WARNING on the cancellation-resistant
abandonment path ("RetryWorker task did not cancel within Xs;
abandoning"). There is no separate "cancelling" log line — the polling
loop moves directly from deadline expiry to `task.cancel()` and
continues polling. Emits structured events on each transition:
`retry_started` (on launch), `retry_stopped` (on clean stop), and
`retry_abandoned` (on the cancellation-resistant or stop-cancelled
path, with `stop_timeout_seconds` plus counters).

**Test boundaries.** Stop idempotency, timeout-then-cancel, cooperative
shutdown mid-batch, capacity release, concurrent stop serialization,
and external-cancellation-of-stop handling are covered in
`tests/test_retry_shutdown.py` using fake storage and in-memory adapters.

### MedreApp Adapter Stop Loop

**Scope.** Two passes: (1) started adapters in reverse start order, (2)
never-started adapters that still exist in `self.adapters`. Adapters already
in a terminal state (`STOPPED` or `FAILED`) are skipped in both passes.

**Timeout.** Every `adapter.stop(timeout)` call is driven by
`MedreApp._stop_adapter_with_deadline(...)`, a hard-bounded two-stage
helper that uses **polling, not `asyncio.wait_for`**, at every stage:

1. `stop_task = asyncio.create_task(adapter.stop(timeout=...))`
2. **Cooperative stage**: poll `stop_task.done()` at 10 ms intervals
   until the task finishes or the deadline expires. The
   `adapter.stop(timeout=...)` argument is the _adapter's_ cooperative
   timeout (double-layer enforcement with the runtime deadline).
3. **Forced cancellation stage**: on deadline expiry, `stop_task.cancel()`
   and poll again for a second bounded grace period. If the task is
   still alive after the cancel grace, it is **abandoned** (event loop
   reclaims it on shutdown) and the helper returns
   `("abandoned", exc, False)`.
4. **External cancellation**: if a `CancelledError` is delivered to
   the helper itself, the adapter's task is given one bounded cancel
   grace (also polling) and then the `CancelledError` is re-raised.

Polling is mandatory at every stage. A bare
`asyncio.wait_for(adapter.stop(...), timeout=...)` cannot bound
cancellation-resistant adapters: `wait_for` cancels the awaited task on
timeout and then waits for its cancellation/cleanup to finish, but if
`adapter.stop` suppresses `CancelledError` or blocks during its own
cleanup, `wait_for` can overrun the timeout or hang indefinitely.
Polling `task.done()` is the only reliable hard bound for such adapters.
The runtime deadline comes from `config.runtime.shutdown_timeout_seconds`
(default 10 s).

**Error recording.** Each failed stop appends `(adapter_id, exception)` to a
collector list. After all cleanup (adapters, pipeline, storage), if the list
is non-empty, `stop()` transitions to `FAILED` state and raises
`RuntimeShutdownError` with a summary string.

**Continuation guarantee.** The adapter stop loop never breaks early on
non-cancellation error. Pipeline runner stop and storage close always
execute, regardless of adapter stop failures or external cancellation
that arrives mid-stop.

**CancelledError policy.** Two distinct paths:

- **Normal `MedreApp.stop()`** (externally-cancelled shutdown): the
  `CancelledError` is **deferred** to a local variable and the loop
  breaks immediately. Pipeline runner stop and storage close still
  run; only after the cleanup is complete is the `CancelledError`
  re-raised to the caller. The runtime state is set to `FAILED`
  _before_ re-raising so a subsequent `stop()` call is not trapped by
  the `STOPPING` early-return guard.

  Because Python 3.11+ latches a task's cancellation state after a
  `CancelledError` is caught (the next `await` in the same task would
  re-raise immediately), the deferred-cancellation path calls
  `_drain_pending_cancellations()` which loops `current.uncancel()`
  while `current.cancelling() > 0` to remove all pending cancellation
  requests before `await self.pipeline_runner.stop()` and
  `await self.storage.close()`. `Task.uncancel()` decrements the
  cancellation count by one and returns the _remaining_ count (not the
  number removed), so a loop is required to drain more than one
  pending cancellation. The restore path then calls `current.cancel()`
  the same number of times to restore the cancel count before
  re-raising. This lets the cleanup awaits actually run while still
  propagating the original cancellation to the caller.

- **Startup best-effort cleanup** (`_cleanup_started_adapters`): a
  `CancelledError` from an adapter's `stop()` is **suppressed** so
  the caller's `_cleanup_core_resources()` (pipeline runner + storage
  close) still runs and the original startup failure is preserved.
  This is intentionally distinct from the normal-stop policy because
  startup failure is already an exceptional state, and the priority
  there is to leave the runtime in a clean state rather than to
  propagate the cancellation.

**Drain abandoned evidence.** Before stopping adapters, `stop()` polls
capacity until in-flight work reaches zero or the drain deadline expires. On
timeout, abandoned deliveries get persisted as `DeliveryReceipt` with
`status="suppressed"`, `failure_kind="shutdown_rejection"`,
`error="shutdown_drain_timeout"`. New deliveries rejected because
shutdown is underway produce `error="delivery_rejected_shutdown"`
with the same `failure_kind`.

**Test boundaries.** Timeout supervision, cancellation handling, storage-close
resilience, pipeline-stop resilience, reverse-stop-order preservation, and
RuntimeShutdownError message content are covered in
`tests/test_runtime_adapter_stop_supervision.py` and
`tests/test_startup_cleanup_stop_timeout.py`. Both files use
fake adapters with controlled stop timing. No live/hardware validation claims
apply.

### PipelineRunner

**Creation.** `start()` registers `_PipelineLoggingMiddleware` on the event
bus and sets `_running = True`.

**Teardown.** `stop()` removes middleware and sets `_running = False`. No
background tasks are owned directly by the runner itself. Per-delivery
lifecycle (inflight records, lease renewal tasks) is cleaned up in each
delivery's `finally` block, with `drain_abandoned_deliveries()` as the
shutdown safety net.

**Test boundaries.** Middleware registration and stop behavior are covered in
pipeline unit tests with fake adapters.

### CapacityController

**Creation.** Two `asyncio.Semaphore` instances (delivery, replay) constructed
at init time.

**Shutdown gate.** `stop_accepting()` sets an internal flag. Subsequent
acquire calls return `False` immediately, rejecting new work without blocking.

**Drain.** `MedreApp.stop()` polls `snapshot()` every 100 ms until both
semaphores report zero current holders, or the drain deadline expires.

**Test boundaries.** Capacity rejection, shutdown rejection, and drain timeout
behavior are covered in smoke drills and capacity tests with fake adapters.

### EventBuffer

**Creation.** Bounded `collections.deque(maxlen=256)` in `MedreApp.__post_init__`.

**Lifecycle.** Auto-evicts oldest entries when full. No explicit `close()`,
`flush()`, or `clear()`. Garbage-collected with the MedreApp object.

**Shutdown evidence.** The buffer continues to accept events during shutdown
(state transitions, adapter stop events). It is consumed by
`build_shutdown_evidence()` to classify shutdown outcome (graceful, timeout,
cancellation, etc.).

### Adapter Sessions and Background Tasks

Each transport adapter creates a session object during `start()` that owns
the SDK client, reconnection loop, and sync/announce tasks. The common
teardown pattern across all adapters:

1. Set a `_stop_requested` or `_closed` flag.
2. Cancel known named tasks (sync, reconnect, announce, drain).
3. Call `_drain_background_tasks(timeout)` to cancel and await any remaining
   tracked tasks.
4. Close the SDK client or transport connection.

Adapters track background tasks in a `set[asyncio.Task]` that grows as inbound
events arrive. The drain method cancels every tracked task and awaits it,
preventing orphaned coroutines.

**Test boundaries.** Adapter lifecycle is tested at the adapter-wrapper level
with mocked transport. Background task draining is tested per-adapter with
controlled timing. No live/hardware validation claims apply to transport SDK
task cleanup. Fake/synthetic test boundaries apply.

## Idempotency Summary

| Resource                            | Guard mechanism                                      | Safe to call twice?                                 |
| ----------------------------------- | ---------------------------------------------------- | --------------------------------------------------- |
| SQLiteStorage.close()               | `_closed` flag + `_db is None` + `_executor is None` | Yes                                                 |
| RetryWorker.stop()                  | `_task is None` check                                | Yes                                                 |
| MedreApp.stop()                     | state in `{STOPPED, STOPPING, INITIALIZED}`          | Yes (concurrent calls during STOPPING return early) |
| Adapter.stop()                      | Per-adapter `_started` / `_closed` flags             | Adapter-dependent; generally yes                    |
| PipelineRunner.stop()               | `_running` flag                                      | Yes                                                 |
| CapacityController.stop_accepting() | `_accepting_work` flag                               | Yes (no-op on second call)                          |

## Evidence and Logging Behavior

### What the runtime logs during shutdown

| Phase             | Level            | What is logged                                     |
| ----------------- | ---------------- | -------------------------------------------------- |
| Enter STOPPING    | INFO             | Runtime name, configured timeout                   |
| Replay cancelled  | INFO             | Replay engine cancelled, capacity stopped          |
| Retry worker stop | INFO             | Worker stopped (or WARNING on forced cancellation) |
| Drain complete    | INFO             | In-flight work drained                             |
| Drain timeout     | WARNING          | Count of abandoned deliveries and replays          |
| Per-adapter stop  | DEBUG/INFO/ERROR | Per-adapter lifecycle progress                     |
| Pipeline stopped  | INFO             | Pipeline runner stopped                            |
| Storage closed    | INFO             | Storage close complete                             |
| Final state       | INFO             | Runtime stopped, or ERROR with summary             |

### What the runtime persists during shutdown

- Suppressed receipts for drain-abandoned deliveries
  (`status="suppressed"`, `failure_kind="shutdown_rejection"`).
- Non-terminal outbox items are left as-is for resumable work on next startup.
  They are not cancelled or transitioned.
- Receipts and outbox rows in SQLite survive process exit.

### Shutdown evidence classification

`build_shutdown_evidence()` reads the event buffer, outbox counts, retry
state, and capacity state to produce a `ShutdownEvidence` record classified
as one of: `graceful_stop`, `cancellation`, `adapter_failure`,
`drain_timeout`, `shutdown_pending`, `stopped`, or `failed`.

## Test File Reference

| Test file                                        | Lines    | Covers                                                                                                       |
| ------------------------------------------------ | -------- | ------------------------------------------------------------------------------------------------------------ |
| `tests/test_runtime_adapter_stop_supervision.py` | 326      | Graceful shutdown: timeout, cancellation, ordering, error messages, storage/pipeline resilience              |
| `tests/test_startup_cleanup_stop_timeout.py`     | 215      | Startup failure cleanup: hung adapter stop, cancellation, multi-adapter cleanup, storage/pipeline resilience |
| `tests/test_retry_shutdown.py`                   | existing | RetryWorker stop idempotency, timeout-then-cancel, cooperative shutdown                                      |

All test files are under the 1500-line limit.

## See Also

- [testing.md](testing.md) -- test suite structure and conventions
- [adapter-authoring.md](adapter-authoring.md) -- writing transport adapters
- [../ops/troubleshooting.md](../ops/troubleshooting.md) -- operator-facing shutdown failure diagnosis
- [../ops/diagnostics-and-evidence.md](../ops/diagnostics-and-evidence.md) -- evidence provenance and report shapes

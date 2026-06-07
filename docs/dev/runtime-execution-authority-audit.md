# Runtime Execution Authority Audit

> **Classification:** Developer reference (derived from source audit, not normative)
> **Audience:** Runtime developers, code reviewers.
> **Authority:** `docs/spec/` pages are the normative specification. This document
> synthesizes runtime execution audit findings. If this document conflicts
> with a spec page, the spec is correct.

Core principle: runtime orchestration coordinates execution of already-authoritative
decisions. It is not lifecycle, planning, adapter, or persistence authority. The
runtime starts things, stops things, and coordinates their interaction. It does not
decide what to store, when a delivery is terminal, or what the pipeline should do.

## Task Ownership Hierarchy

Ownership flows downward. Each layer owns its own tasks and delegates to the layer
below. No layer reaches across ownership boundaries to manage tasks it does not own.

```text
CLI (run_commands.py)
  Chooses mode, invokes runtime. Does not own lifecycle semantics.
  Installs SIGINT/SIGTERM handlers that set shutdown_event.
  └─ MedreApp (runtime/app.py)
       Owns process lifecycle: start sequence, stop sequence,
       deferred cancellation, shutdown evidence.
       ├─ PipelineRunner (core/engine/pipeline/runner.py)
       │    Owns per-event execution: ingress, routing, planning,
       │    per-target delivery orchestration, outbox creation,
       │    lease renewal tasks, in-flight tracking.
       │    └─ TargetDeliveryService (core/engine/pipeline/target_delivery.py)
       │         Owns per-target delivery: rendering, adapter invoke,
       │         primary receipt construction, failure classification.
       ├─ RetryWorker (runtime/retry.py)
       │    Owns retry polling: claim due outbox items, reconstruct
       │    delivery context, call pipeline, track state.
       ├─ ReplayEngine (core/engine/replay/engine.py)
       │    Orchestrates re-processing. Initiates new work through
       │    the pipeline. Does not directly own storage writes.
       ├─ Adapters (adapters/*)
       │    Own transport resources and local tasks only.
       │    Do not own receipts, outbox, or lifecycle decisions.
       └─ Evidence / diagnostics
            Observes. Reads storage, builds projections.
            Does not own runtime tasks or storage writes.
```

The CLI installs signal handlers (`SIGINT`, `SIGTERM`) that call
`shutdown_event.set()` on the `MedreApp`. It does not reach into the
app's internals. The app owns the shutdown lifecycle from that signal.

## Runtime/Execution Component Inventory

Each component below is a runtime-execution concern: it starts, runs, and stops
under the `MedreApp` lifecycle. Storage-domain components (persistence authority)
are cataloged in [persistence-authority-audit.md](persistence-authority-audit.md).

### Component Detail Table

| Component                             | Source                                           | Owner                                               | Starts when                                                            | Stops when                                  | Storage write                                                                                                                         | Storage read                                                                            | Cancel behavior                                                                                                                | Shutdown behavior                                                                                                                                                                                                                                                                                     | Retry/recovery interaction                                                                                                       | Operator visibility                                                                                                             | Guarantee                                                                                                                                                                                                       |
| ------------------------------------- | ------------------------------------------------ | --------------------------------------------------- | ---------------------------------------------------------------------- | ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| MedreApp (runtime container)          | `runtime/app.py`                                 | CLI (creates, starts, stops)                        | `app.start()` after `builder.build()`                                  | `app.stop()` in reverse dependency order    | None directly                                                                                                                         | Seeds outbox counts on startup                                                          | Deferred: drains cancellations during cleanup, restores count after core cleanup completes                                     | 13-step shutdown (see [resource-lifecycle.md](resource-lifecycle.md#shutdown-sequence)): stop accepting, cancel replay, stop retry worker, drain, persist abandoned evidence, stop adapters, stop pipeline, close storage                                                                             | Starts retry worker, starts adapters. Recovery on restart: retry worker picks up `retry_wait` rows via `claim_due_outbox_items`  | Boot summary, diagnostic snapshot, runtime state enum, health classification                                                    | Ordered shutdown is attempted. Core cleanup proceeds through all phases and storage close is attempted; adapter/retry task completion is best-effort and abandonment is surfaced through state/events/evidence. |
| Bridge session / run-session          | `runtime/run_session/orchestration.py`           | `run_bridge_session()`                              | On invocation                                                          | On completion (stop after poll)             | Via `MedreApp` pipeline                                                                                                               | Reads receipts, native refs, events after stop                                          | Propagates from `MedreApp`                                                                                                     | Delegates to `MedreApp.stop()`                                                                                                                                                                                                                                                                        | Produces persisted SQLite evidence; does not interact with retry worker directly                                                 | JSON report, snapshot file, cross-linked commands                                                                               | Best-effort: poll-based receipt collection (3 s timeout)                                                                                                                                                        |
| Smoke runtime                         | `runtime/smoke.py`                               | `run_fake_bridge_smoke()`                           | On invocation                                                          | On completion                               | Via `MedreApp` pipeline                                                                                                               | Reads outcomes directly                                                                 | Propagates from `MedreApp`                                                                                                     | Delegates to `MedreApp.stop()`                                                                                                                                                                                                                                                                        | Not involved                                                                                                                     | JSON report, `medre smoke --json`                                                                                               | Diagnostic only: uses fake adapters, no real transport                                                                                                                                                          |
| PipelineRunner                        | `core/engine/pipeline/runner.py`                 | `MedreApp`                                          | `app.start()` step 2                                                   | `app.stop()` step 11                        | `append()`, `store_native_ref()`, `append_receipt()`, `create_outbox_item()`, outbox transitions                                      | `get()`, `list_receipts`, `count_outbox_by_status`                                      | `CancelledError` propagates; lease renewal task cancelled in finally block                                                     | `stop()` removes middleware, sets `_running=False`                                                                                                                                                                                                                                                    | Central delivery engine. Retry worker and replay both call `deliver_to_target()`                                                 | Phase snapshot, in-flight count, route stats                                                                                    | Guaranteed: each target processed independently; failure in one does not block others                                                                                                                           |
| TargetDeliveryService                 | `core/engine/pipeline/target_delivery.py`        | `PipelineRunner`                                    | Per-target during delivery                                             | Per-target after delivery                   | `append_receipt()`                                                                                                                    | Adapter lookup (in-memory)                                                              | `CancelledError` propagates                                                                                                    | No separate shutdown                                                                                                                                                                                                                                                                                  | Called per delivery by runner and retry worker                                                                                   | Per-target rendering evidence in receipt                                                                                        | Guaranteed: rendering and adapter invocation errors are normalized                                                                                                                                              |
| RetryWorker                           | `runtime/retry.py`                               | `MedreApp`                                          | `app.start()` step 2.5 (if `config.retry.enabled` and storage present) | `app.stop()` Phase 1                        | `mark_outbox_retry_wait`, `mark_outbox_sent`, `mark_outbox_dead_lettered`, `mark_outbox_abandoned`, `append_receipt()` (via pipeline) | `claim_due_outbox_items`, `get()`, `list_receipts_for_plan()`, `count_outbox_by_status` | Two-stage: cooperative (shutdown event + poll), then forced `cancel()` + poll. Abandonment if task survives both grace periods | `stop()` bounded by `stop_timeout_seconds`. May be **abandoned**: `state.abandoned=True`, `state.running=False`, retained abandoned task may still be alive. `state.running` describes managed RetryWorker lifecycle (no active `_task`). `abandoned=True` blocks restart until explicit state reset. | Core retry mechanism. Claims `retry_wait` and `pending` outbox rows. Dead-letters on exhaustion                                  | RetryWorkerState in snapshot, `retry_started/stopped/failed/abandoned` events, `retry_start_refused` event on abandoned restart | Best-effort: abandonment is a known outcome. Abandoned state blocks `start()` until operator intervention                                                                                                       |
| Recovery / orphan scanner             | `core/recovery/`                                 | `medre recover` CLI                                 | On CLI invocation                                                      | On CLI completion                           | None (read-only)                                                                                                                      | `canonical_events`, `delivery_receipts`, `delivery_outbox`                              | N/A (no background tasks)                                                                                                      | N/A                                                                                                                                                                                                                                                                                                   | Pure diagnostic. Classifies orphans and stale work. Does not fix them.                                                           | `medre recover` output                                                                                                          | Diagnostic only                                                                                                                                                                                                 |
| ReplayEngine                          | `core/engine/replay/engine.py`                   | `MedreApp` (wired by builder)                       | On `medre replay` invocation                                           | `cancel()` sets cancel event                | Creates outbox rows and receipts **through the pipeline** (not directly)                                                              | `canonical_events`, `event_relations`, `delivery_receipts`                              | `cancel_event.set()` checked between stages                                                                                    | `cancel()` from `app.stop()` step 4                                                                                                                                                                                                                                                                   | Initiates new work through the pipeline. `BEST_EFFORT` mode may produce new deliveries. Not a durable job system.                | `medre replay` output, `source='replay'` receipts                                                                               | Best-effort: crash during replay loses the run. No replay dedup.                                                                                                                                                |
| Adapter lifecycle (start/stop)        | `adapters/*/adapter.py`, `adapters/*/session.py` | `MedreApp` (start in sorted order, stop in reverse) | `app.start()` step 3                                                   | `app.stop()` steps 9-10                     | None directly (adapters report facts, pipeline persists)                                                                              | None                                                                                    | Adapter `stop()` may suppress `CancelledError`. App abandons adapter stop task if it survives cancel grace.                    | Two-stage deadline per adapter: cooperative poll, then forced cancel, then abandon. Abandoned tasks retained via `_abandoned_adapter_stop_tasks`                                                                                                                                                      | Not involved (adapters are transport, not retry)                                                                                 | Adapter state enum, start duration, health check, fake/live mode                                                                | Best-effort: adapter stop may be abandoned. Abandoned stop tasks run until event loop shuts down.                                                                                                               |
| Delayed native-ref queue (Meshtastic) | `adapters/meshtastic/adapter.py`                 | `MeshtasticAdapter`                                 | `_drain_task` started in `adapter.start()`                             | `adapter.stop()` cancels drain task         | Calls `ctx.record_outbound_native_ref` which calls `PipelineRunner._record_outbound_native_ref()`                                     | None                                                                                    | `cancel()` on drain task                                                                                                       | `stop()` cancels drain, awaits with timeout                                                                                                                                                                                                                                                           | Not involved (adapter-local queue)                                                                                               | Supplemental `sent` receipt bridges `queued` to `sent` evidence gap                                                             | Best-effort: items abandoned on drain task cancellation by design                                                                                                                                               |
| Outbound native ref callback          | `PipelineRunner._record_outbound_native_ref()`   | `PipelineRunner`                                    | Per queue-based delivery confirmation                                  | N/A (callback, not background task)         | `store_native_ref()`, `append_receipt()` (supplemental `sent`)                                                                        | `list_receipts_for_plan()` (for parent context)                                         | N/A                                                                                                                            | Not directly                                                                                                                                                                                                                                                                                          | Bridges evidence gap for queue-based adapters                                                                                    | Native refs queryable via `medre inspect`                                                                                       | Best-effort: callback failures are logged, never crash the adapter                                                                                                                                              |
| Evidence collection (during runtime)  | `core/evidence/`, `runtime/evidence/`            | Observes (no task ownership)                        | On demand (diagnostics, snapshot, report)                              | N/A                                         | None                                                                                                                                  | Reads all storage tables                                                                | N/A                                                                                                                            | N/A                                                                                                                                                                                                                                                                                                   | Read-only projections. If a report contradicts the receipt chain, the receipts are the authority.                                | `medre evidence`, `medre trace`, `medre inspect`                                                                                | Diagnostic only                                                                                                                                                                                                 |
| Outbox lease renewal task             | `PipelineRunner._start_outbox_lease_renewal()`   | `PipelineRunner` (local per-delivery)               | Per delivery in `_deliver_one()` after outbox creation                 | Cancelled in `_deliver_one()` finally block | `renew_outbox_lease()`                                                                                                                | None                                                                                    | `cancel()` + await in finally                                                                                                  | Short-lived (one per delivery). Cancelled when delivery completes.                                                                                                                                                                                                                                    | Extends lease during long adapter deliveries (radio transports)                                                                  | Not directly visible (internal)                                                                                                 | Best-effort: transient errors logged and retried on next cycle. Lease loss stops renewal.                                                                                                                       |
| CapacityController                    | `core/supervision/capacity.py`                   | `MedreApp` (wired by builder)                       | Constructor                                                            | `stop_accepting()` gates new work           | None                                                                                                                                  | In-flight counters (semaphore-based)                                                    | Semaphores drain naturally                                                                                                     | `stop_accepting()` at `app.stop()` step 3                                                                                                                                                                                                                                                             | Capacity rejection schedules outbox `retry_wait` with backoff                                                                    | `accepting_work`, capacity snapshot in diagnostics                                                                              | Guaranteed: stops accepting new work. Drain timeout determines what happens to in-flight.                                                                                                                       |
| Shutdown evidence persistence         | `MedreApp._persist_drain_abandoned_evidence()`   | `MedreApp`                                          | When drain deadline expires with in-flight deliveries                  | After persistence                           | `append_receipt()` with `status="suppressed"`, `failure_kind="shutdown_rejection"`                                                    | `drain_abandoned_deliveries()` from PipelineRunner                                      | Cancellation deferred: drain cancellations are drained, evidence persists, then cancelled re-raised                            | Core of shutdown reporting. Produces structured abandonment receipts.                                                                                                                                                                                                                                 | Creates receipts that are observable after restart. Abandoned deliveries are not automatically retried; they are evidence facts. | `medre inspect receipts` with `failure_kind="shutdown_rejection"`                                                               | Best-effort: persist failures are logged. If cancelled during persist, the cancellation is deferred.                                                                                                            |
| In-flight delivery tracking           | `PipelineRunner._inflight_deliveries`            | `PipelineRunner`                                    | Per delivery (when capacity controller present)                        | Per delivery (finally block pops)           | None                                                                                                                                  | Read by `drain_abandoned_deliveries()` at shutdown                                      | Untracked if cancelled before entry                                                                                            | Cleared by `drain_abandoned_deliveries()` at shutdown drain timeout                                                                                                                                                                                                                                   | Not involved                                                                                                                     | Visible in drain abandoned evidence                                                                                             | Best-effort: only tracked when capacity controller is wired                                                                                                                                                     |
| EventBuffer                           | `runtime/events.py`                              | `MedreApp.__post_init__()`                          | Constructor                                                            | GC (bounded deque)                          | None                                                                                                                                  | Read by snapshot                                                                        | N/A                                                                                                                            | N/A                                                                                                                                                                                                                                                                                                   | Not involved                                                                                                                     | Runtime events in snapshot                                                                                                      | Diagnostic only                                                                                                                                                                                                 |

### Guarantee Legend

| Guarantee       | Meaning                                                                                                                             |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Guaranteed      | The component completes its core responsibility. Failure raises to the caller.                                                      |
| Best-effort     | The component tries its core responsibility but may abandon it under time pressure or cancellation. The abandoned state is visible. |
| Diagnostic only | The component observes and reports. It does not drive execution or mutate storage.                                                  |
| Not guaranteed  | No assurance. Caller must not depend on completion.                                                                                 |

## Shutdown Semantics

### What shutdown guarantees

1. Every subsystem gets a stop call in reverse dependency order (adapters, pipeline,
   storage).
2. Retry worker gets a bounded stop with cooperative and forced-cancel stages.
3. In-flight deliveries are tracked and drained with a configurable timeout
   (`shutdown_drain_timeout_seconds`).
4. Abandoned deliveries receive structured `suppressed` receipts with
   `failure_kind="shutdown_rejection"` so operators can audit what was lost.
5. Pipeline runner middleware is removed.
6. Storage is closed (idempotent).
7. Deferred cancellation: if an external `CancelledError` arrives during shutdown,
   it is drained and re-raised after core cleanup (pipeline stop, storage close)
   completes.
8. Terminal state is set (`STOPPED` on success, `FAILED` on error or cancellation).

### What shutdown does not guarantee

1. Adapter stop completes within timeout. Adapter stop tasks may be **abandoned**
   if they suppress `CancelledError` and survive the two-stage deadline. Abandoned
   tasks run until the event loop shuts down.
2. All in-flight deliveries complete. The drain timeout is bounded. Deliveries
   still in progress after the deadline are abandoned.
3. Retry worker task terminates. The worker may be abandoned (state:
   `running=True, abandoned=True`). A subsequent `start()` call will refuse to
   launch.
4. Delayed native refs from Meshtastic queue drain. The drain task is cancelled;
   queued items without native IDs are lost.
5. Evidence persistence after drain timeout. If `append_receipt()` fails for an
   abandoned delivery, that delivery has no receipt record.

### What is preserved in storage

- All canonical events, event relations, delivery receipts, and native refs that
  were persisted before shutdown began.
- Outbox rows: `retry_wait` rows survive and are claimable on restart. `in_progress`
  rows with expired leases are re-claimable. `pending` rows are claimable.
- Drain abandoned evidence: `suppressed` receipts with `failure_kind="shutdown_rejection"`.
- All terminal outbox rows (`sent`, `dead_lettered`, `cancelled`, `abandoned`) are
  immutable and survive.

### What is retryable after restart

- `retry_wait` outbox rows: the retry worker claims them on its first cycle.
- `pending` outbox rows: claimable by the retry worker.
- `in_progress` rows with expired leases: re-claimable via `claim_due_outbox_items`.

### What is lost if only in-memory

- In-flight delivery tracking (`PipelineRunner._inflight_deliveries` dict).
- RetryWorker counters (processed, succeeded, failed, dead_lettered).
- CapacityController semaphore state.
- EventBuffer contents (bounded deque, not persisted).
- Boot summary, live health snapshot, adapter start timestamps.
- Replay run state (replay runs are not persisted).

### Evidence and reporting limitations

- `_persist_drain_abandoned_evidence()` is best-effort. If it is cancelled or
  storage is unavailable, abandoned deliveries may have no receipt.
- Shutdown receipts use `failure_kind="shutdown_rejection"`. This is a deliberate
  runtime-generated evidence fact, not an adapter or pipeline delivery result.
- The retry worker abandonment flag (`state.abandoned=True`) is in-memory only.
  After process exit, there is no persistent record that the worker was abandoned.
  Operators must check logs.

## Cancellation and Task Ownership Findings

### create_task / cancel / gather / wait_for patterns

The runtime uses three cancellation patterns:

1. **Deferred cancellation** (`MedreApp.stop()`): external `CancelledError` is
   caught, drained via `_drain_pending_cancellations()`, cleanup continues,
   then the cancellation is restored by calling `current.cancel()` the same
   number of times. This ensures pipeline stop and storage close always run.

2. **Two-stage deadline** (`_stop_adapter_with_deadline`, `RetryWorker.stop()`):
   cooperative poll at 10 ms intervals, then forced `cancel()` + second poll
   period. Uses polling instead of `asyncio.wait_for()` because `wait_for`
   cannot terminate a coroutine that suppresses `CancelledError`.

3. **Drain and restore** (`start()` startup cleanup): during failed startup,
   cancellations from per-adapter cleanup are drained, accumulated into a
   counter, and restored after all cleanup completes so the outer
   `CancelledError` handler can re-raise with the correct cancellation depth.

### Retry worker abandoned restart block

`RetryWorker.start()` refuses to launch when `state.abandoned` is `True`. This
prevents silently double-processing the outbox while an abandoned worker task is
still alive. The operator must either reset the worker or shut the entire runtime
down.

This is a runtime-generated safety guard, not a persistence guarantee. After
process exit and restart, the abandoned flag is gone and the worker starts fresh.
The real protection against double-processing is outbox lease semantics
(`claim_due_outbox_items` with `worker_id` and `lease_until`).

### Drain/restore cancellation pattern

`_drain_pending_cancellations()` loops `Task.uncancel()` while `Task.cancelling()`
is non-zero. This is required because a single `cancel()` call increments the
cancellation count, and a single `uncancel()` only decrements by one. Multiple
pending cancellation requests require multiple `uncancel()` calls to clear.

The pattern is used in:

- `MedreApp.stop()`: deferred cancellation during shutdown phases
- `MedreApp.start()` cleanup: startup cleanup after adapter start failure
- `_cleanup_core_resources()`: core cleanup drain/restore
- `_stop_adapter_with_deadline()`: does not drain; it re-raises `CancelledError` so callers handle drain/restore
- Caller-side drain/restore paths: accumulated cancellations restored after cleanup completes

### `_persist_drain_abandoned_evidence()` suppressed shutdown receipts

This method writes `suppressed` receipts with `failure_kind="shutdown_rejection"`
for in-flight deliveries that did not complete before the drain deadline. This is
a deliberate runtime-generated evidence fact. It is not an adapter delivery result,
a retry outcome, or a recovery action. It records that the runtime chose to stop
waiting for a delivery that was in progress.

The `failure_kind` value `"shutdown_rejection"` is defined in
`DeliveryFailureKind.SHUTDOWN_REJECTION` (`core/planning/delivery_plan.py`).

## Review Findings

Findings from the runtime execution audit, with classification and code
references. These are not bugs in all cases; they are areas where behavior is
subtle, undocumented, or fragile under edge conditions.

### No `docs/spec/runtime.md`

The runtime has no normative specification page. Runtime semantics are documented
across `resource-lifecycle.md`, `persistence-authority-audit.md`, and source code
docstrings, but there is no single spec page that defines the runtime's authority
boundary, startup/shutdown contracts, or guarantee levels.

**Classification:** Documentation gap (not a code defect).
**Impact:** New contributors must read source to understand runtime behavior.
**Recommendation:** Do not create `docs/spec/runtime.md` unless explicitly
directed. This audit document serves as the interim reference.

### Retry worker timeout behavior

The retry worker uses `asyncio.wait_for(self._shutdown_event.wait(), timeout=self._interval)`
in the polling loop. If the worker is processing a batch when shutdown is signaled,
the shutdown event is already set and `wait_for` returns immediately, so the worker
exits cleanly. But if the worker is in `await asyncio.wait_for()` when the cancel
arrives, the `TimeoutError` is caught and the loop continues. The shutdown event
check at the top of `_run_loop` catches this on the next iteration.

**Classification:** Correct but subtle. The two-stage stop in `stop()` handles the
case where the worker is unresponsive.
**Code:** `runtime/retry.py:486-501`
**Recommendation:** Add a cross-reference comment in `_run_loop` explaining
why `wait_for` is used instead of `sleep` (allows immediate shutdown when event is
already set).

### Suppressed shutdown receipts are runtime-generated facts

`_persist_drain_abandoned_evidence()` creates receipts with `source` from the
original delivery context (live, retry, or replay) but `failure_kind="shutdown_rejection"`.
The `source` column is not `"shutdown"`; it reflects where the delivery originated.
This is intentional (preserves delivery lineage) but could confuse operators who
expect `source` to indicate who wrote the receipt.

**Classification:** Design choice, not a bug.
**Code:** `runtime/app.py:1489-1547`
**Recommendation:** Add operator-facing note in evidence docs explaining
that `failure_kind="shutdown_rejection"` receipts have `source` from the original
delivery, not from the shutdown process.

### Cancellation drain/restore fragility

The drain/restore pattern relies on exactly counting cancellation requests and
restoring the same count. If any code path between drain and restore calls
`await` (which can re-raise `CancelledError`), the count may be wrong. The
current code is correct but tightly coupled to the exact call sequence.

**Classification:** Fragile-by-design. Correctness depends on no `await` between
drain and restore.
**Code:** `runtime/app.py:106-124`, `runtime/app.py:1288-1305`
**Recommendation:** Consider a context manager or decorator that automates
drain/restore to reduce the coupling surface.

### Recovery diagnostic vs operational wording

Recovery (`core/recovery/`, `medre recover`) is purely diagnostic. It classifies
outbox state but does not fix anything. Some variable and method names
(`RecoverySource.STARTUP_RECOVERY`, `RecoveryOwnershipAction`) use the word
"recovery" which implies repair, not observation. The `SNAPSHOT_DIAGNOSTICS`
enum value clarifies this but the naming is still potentially misleading.

**Classification:** Naming inconsistency.
**Code:** `core/recovery/recovery_source.py`, `core/recovery/models.py`
**Recommendation:** Vocabulary disambiguation. Consider renaming or adding
docstring clarification that "recovery" in this context means "identification and
classification", not "repair".

### Replay initiates new work through pipeline

Replay does not directly write storage. It calls `PipelineRunner.deliver_to_target()`,
which creates new outbox rows, receipts, and native refs through the normal pipeline
path. This means replay is not a read-only operation: `BEST_EFFORT` mode can produce
new deliveries, new receipts, and new native refs. Replay runs are not persisted; a
crash during replay loses the run.

**Classification:** Correct behavior, but often misunderstood.
**Code:** `core/engine/replay/engine.py`, `core/engine/replay/delivery.py`
**Recommendation:** This is documented in `persistence-authority-audit.md`
under "Replay Persistence Semantics". No code change needed.

### Test files near caps

Several test files are approaching the 1,500-line hard cap (see
[testing.md](testing.md#next-pr-candidates-near-the-cap)). Files near the cap
make it harder to add tests for runtime edge cases like cancellation drain/restore,
shutdown abandonment, and retry worker lifecycle.

**Classification:** Maintenance risk.
**Recommendation:** Split opportunistically per the procedure in
[testing.md](testing.md#splitting-procedure).

### Missing `tests/test_replay_delivery.py` — triaged: no gap, no file needed

There is no dedicated `tests/test_replay_delivery.py`, and none is needed.
The replay-to-pipeline delivery path and all three replay authority properties
are exercised by existing test files:

### Authority: replay initiates new work through the normal pipeline

- `test_replay_pipeline_integration.py` (585 lines, 11 tests): full
  ReplayEngine × PipelineRunner integration with fake adapters, exercising
  BEST_EFFORT delivery, DRY_RUN skip, route filtering, receipt traceability,
  loop prevention, and per-route attribution.
- `test_pipeline_live_replay_parity.py` (698 lines): proves the pipeline
  delivers semantically equivalent plans and receipts through both the live
  and replay paths, normalising away identity/timing fields.
- `test_replay_partial_failure.py`: replay through pipeline with capacity
  exhaustion and shutdown rejection.

### Authority: replay does not bypass lifecycle/persistence authority

- `test_persistence_authority_replay.py` (389 lines): dedicated file proving
  replay creates new receipt rows (source='replay', replay_run_id set), does
  not mutate existing rows; STRICT/DRY_RUN do not create receipts; attempt_number
  increments correctly.
- `test_replay_policy.py::test_replay_never_mutates_historical_event`:
  proves historical events are unchanged after replay.
- `test_replay_pipeline_integration.py::test_original_event_not_mutated`:
  stored event metadata unchanged after replay.
- `test_replay_routing_durability.py`: replay does not mutate storage.

### Authority: live/replay ownership metadata remain distinct

- `test_replay_bridge_conditions.py`: live delivery creates source='live',
  replay creates source='replay'; run_id isolation verified.
- `test_replay_after_stop_start.py`: replay creates new source='replay' rows
  without modifying existing live rows.
- 57+ test files across the suite verify source='replay' tagging and
  replay_run_id propagation (storage, lifecycle, evidence, trace, CLI).

**Classification:** No gap (triaged 2026-06-07).
**Recommendation:** Do not create `tests/test_replay_delivery.py`. Existing
split files provide comprehensive coverage of the replay-to-pipeline delivery
path. Adding a dedicated file would duplicate tests already present in
`test_replay_pipeline_integration.py`, `test_persistence_authority_replay.py`,
and `test_replay_bridge_conditions.py`.

## Constraints

This audit operates within the following constraints:

- **No compatibility shims.** The runtime does not branch on Python version,
  environment variables, or feature flags.
- **No public API commitments.** MEDRE is pre-release. Everything documented here
  is subject to change without notice.
- **No large framework.** The runtime is not a framework. It is a single-process,
  in-process orchestrator. No plugin loading, no dynamic dispatch, no DI container.
- **No runtime rewrite.** This audit does not recommend rewriting the runtime
  unless a real ownership bug is found. None was found.
- **No schema changes.** This audit does not require or imply any DDL changes,
  migrations, or schema version bumps.

## Cross-References

- **Resource lifecycle:** [resource-lifecycle.md](resource-lifecycle.md)
- **Persistence authority:** [persistence-authority-audit.md](persistence-authority-audit.md)
- **Lifecycle vocabulary:** [lifecycle-authority-audit.md](lifecycle-authority-audit.md)
- **Testing guide:** [testing.md](testing.md)
- **Testing resource warnings:** [TESTING_GUIDE.md](TESTING_GUIDE.md)
- **Normative delivery lifecycle:** [../spec/delivery-lifecycle.md](../spec/delivery-lifecycle.md)
- **Normative state machines:** [../spec/state-machines.md](../spec/state-machines.md)

## Follow-Up Recommendations

The following items should be addressed next, ordered by impact and feasibility.

1. **Retry worker abandoned-start durable visibility.** Structured
   `retry_start_refused` event emission is implemented (event buffer +
   snapshot). The `state.abandoned` flag remains in-memory only; after process
   exit there is no durable record that the worker was abandoned. A future
   improvement could persist an abandonment marker or add a startup diagnostic
   that detects orphaned `in_progress` outbox rows from a previous abandoned
   worker.

2. **Runtime cancellation doc cross-references.** The deferred cancellation,
   drain/restore, and two-stage deadline patterns are documented in source
   docstrings but not in any developer doc. Add cross-references from
   `resource-lifecycle.md` to the relevant source sections.

3. **Vocabulary disambiguation: "recovery" vs "repair".** The recovery module
   (`core/recovery/`) is read-only diagnostic, but its naming implies repair.
   Clarify in docstrings and operator docs that recovery identifies problems
   but does not fix them.

4. **Replay delivery test triage.** ~~Determine whether `test_replay_delivery.py`
   is needed as a dedicated file, or whether existing split files provide
   sufficient coverage of the replay-to-pipeline delivery path.~~ **Triaged
   2026-06-07:** no gap, no file needed. Existing files cover all three replay
   authority properties. See "Missing `tests/test_replay_delivery.py`" finding
   above for the full coverage map.

5. **Shutdown rejection receipt operator docs.** Add a note in operator docs
   explaining `failure_kind="shutdown_rejection"` receipts: what they mean,
   how to find them, and what to do about them.

6. **Cancellation drain/restore context manager.** Evaluate whether extracting
   the drain/restore pattern into a reusable context manager would reduce
   coupling surface in `app.py`.

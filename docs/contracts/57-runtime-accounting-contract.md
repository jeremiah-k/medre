# Contract 57 — Runtime Event Accounting

**Status:** Approved (Track 5)
**Module:** `medre.core.runtime.accounting`
**Depends on:** None (standalone, zero transport imports)

## Purpose

Provide process-local, bounded runtime event accounting counters that
make system behaviour **visible** without adding infrastructure.  These
counters are global process-level aggregates (not per-route) that
complement the existing per-route `RouteStats` and per-route
`ReplayMetrics`.

## Counter Catalog

| Counter                | Method                         | Meaning                                        |
|------------------------|--------------------------------|------------------------------------------------|
| `inbound_accepted`     | `record_inbound_accepted()`    | Inbound events accepted into the pipeline      |
| `outbound_attempts`    | `record_outbound_attempt()`    | Outbound delivery attempts (any outcome)       |
| `outbound_delivered`   | `record_outbound_delivered()`  | Outbound deliveries that succeeded             |
| `outbound_failed`      | `record_outbound_failed()`     | Outbound deliveries that failed                |
| `replay_processed`     | `record_replay_processed()`    | Replay events that completed all stages without unhandled exception |
| `replay_rejected`      | `record_replay_rejected()`     | Replay events rejected (missing, filter mismatch, or unhandled BEST_EFFORT error) |
| `loop_prevented`       | `record_loop_prevented()`      | Events blocked by the self-loop guard          |
| `capacity_rejections`  | `record_capacity_rejection()`  | Operations rejected by the capacity controller (both pipeline delivery and replay) |

### Retry Snapshot Counters

These counters appear under the `"retry"` key in the runtime diagnostic snapshot (not as `RuntimeAccounting` counters). They track RetryWorker activity:

| Counter | Meaning |
|---------|---------|
| `retry_processed` | Total retry attempts executed by the RetryWorker |
| `retry_succeeded` | Retry attempts that produced a successful delivery |
| `retry_failed` | Retry attempts that failed again (transient) |
| `retry_dead_lettered` | Retry attempts that exceeded `max_attempts` and were dead-lettered |

These are process-local like all `RuntimeAccounting` counters — they reset on restart. Durable retry state (which events have pending retries) persists in the `delivery_receipts` table via `next_retry_at` and `failure_kind` columns.

## Guarantees

### Process-local only
Counters live in a single `RuntimeAccounting` instance.  They are:
- **Not persisted.** Counter values are lost on process restart.
- **Not shared.** No cross-process synchronization, no IPC.
- **Not distributed.** No network calls, no coordination.

### Bounded memory
The `RuntimeCounters` dataclass holds exactly 8 integer fields.
Memory usage is **O(1)** regardless of how many events are recorded.
There are no unbounded dictionaries or growing lists.

### Deterministic snapshots
- `snapshot()` returns keys sorted alphabetically.
- Output is JSON-safe: all values are `int`, no secrets, no SDK objects.
- Repeated calls with no intervening mutations return identical output.
- Suitable for inclusion in future runtime diagnostics snapshots.

### Copy-on-write semantics
Each `record_*` call creates a new frozen `RuntimeCounters` instance.
The reference is replaced atomically (single attribute assignment).
Under the CPython GIL, concurrent reads and writes are safe without
explicit locking.

### Reset semantics
- `reset()` sets all counters to zero and returns the previous
  `RuntimeCounters` snapshot.
- A fresh `RuntimeAccounting()` instance has all counters at zero.
- `reset()` is idempotent: calling it on an already-reset instance
  returns an all-zero `RuntimeCounters`.

## API Surface

```python
from medre.core.runtime.accounting import RuntimeAccounting, RuntimeCounters

# Construction
acc = RuntimeAccounting()

# Recording (each increments exactly one counter by 1)
acc.record_inbound_accepted()
acc.record_outbound_attempt()
acc.record_outbound_delivered()
acc.record_outbound_failed()
acc.record_replay_processed()
acc.record_replay_rejected()
acc.record_loop_prevented()
acc.record_capacity_rejection()

# Reading
c: RuntimeCounters = acc.counters()   # frozen snapshot (zero-copy ref)
d: dict[str, int] = acc.snapshot()    # alphabetically sorted dict
d2: dict[str, int] = acc.to_dict()    # alias for snapshot()

# Reset
previous: RuntimeCounters = acc.reset()  # returns pre-reset values
```

## Non-persistence Contract

Counter values are **ephemeral**.  They exist only in memory for the
lifetime of the `RuntimeAccounting` instance.  Specifically:

1. Process restart → all counters reset to zero.
2. Instance replacement → old counters are garbage-collected.
3. No file I/O, no database writes, no network persistence.

Operators must not rely on counter continuity across restarts.  For
persistent metrics, use an external monitoring system (future).

## Operator Interpretation Guide

| Scenario | Meaning |
|----------|---------|
| `inbound_accepted` rising, `outbound_attempts` flat | Events accepted but not yet routed/delivered. May indicate routing gap or filtering. |
| `outbound_attempts` >> `outbound_delivered` + `outbound_failed` | Some attempts are in-flight or being skipped (check `loop_prevented`). |
| `loop_prevented` high relative to `outbound_attempts` | Self-loop guard firing frequently. Check route topology for cycles. |
| `capacity_rejections` rising | Capacity controller rejecting work. Consider increasing concurrency limits. |
| `replay_rejected` rising with `replay_processed` flat | Replay filter rejecting all events. Check replay request parameters. |
| All counters zero after uptime | Accounting not wired into the pipeline yet (future integration). |

## replay_processed vs replay_rejected Semantics

- **`replay_processed`** is incremented when a replay event completes all
  requested stages *without* an unhandled exception.  Individual stage
  failures (e.g. store check failed, no routes matched) still count as
  processed because the replay engine handled them gracefully.

- **`replay_rejected`** is incremented when a replay event is rejected
  *before* stage execution begins (event missing from storage, filter
  mismatch) or when an unhandled exception escapes in BEST_EFFORT mode.
  In non-BEST_EFFORT modes, unhandled exceptions propagate rather than
  incrementing this counter.

- **`capacity_rejections`** is incremented by both the pipeline (delivery
  capacity exhausted) and the replay engine (replay capacity exhausted).
  The counter is a global aggregate; use `CapacityController.snapshot()`
  to distinguish delivery vs replay rejections.

## Failure Taxonomy (DeliveryFailureKind)

The pipeline classifies delivery failures using `DeliveryFailureKind`:

| Kind | Trigger | Retryable |
|------|---------|-----------|
| `PLANNER_FAILURE` | Routing/planning error | No |
| `RENDERER_FAILURE` | Rendering error | No |
| `ADAPTER_TRANSIENT` | Timeout, connection reset | Yes |
| `ADAPTER_PERMANENT` | Business-logic rejection | No |
| `ADAPTER_MISSING` | Adapter ID not registered | No |
| `TARGET_NOT_FOUND` | Channel not found *(reserved — not currently emitted)* | No |
| `DEADLINE_EXCEEDED` | Plan deadline passed | No |
| `CAPACITY_REJECTION` | Capacity controller exhausted or timed out while accepting work | No |
| `SHUTDOWN_REJECTION` | Runtime shutdown cancelled delivery before capacity acquire | No |

`CAPACITY_REJECTION` is used when the capacity controller's semaphore is
exhausted but the controller is still accepting work.  `SHUTDOWN_REJECTION`
is used when the controller has called `stop_accepting()` (pipeline
shutdown in progress).

**Receipt non-persistence:** `CAPACITY_REJECTION` and `SHUTDOWN_REJECTION`
intentionally do **not** produce a persisted `DeliveryReceipt`. The event
never entered the delivery stage — the rejection occurs at the capacity
gate *before* any adapter interaction. Durable evidence of the rejection
is recorded via `RuntimeAccounting` counters and `RouteStats`, not via
`delivery_receipts`.

**Receipt traceability:** When receipts are created, each `DeliveryReceipt`
carries a `source` field (`"live"` or `"replay"`) and a nullable
`replay_run_id` field. These distinguish live delivery receipts from
replay delivery receipts at the storage layer. `RuntimeAccounting`
remains process-local and is not affected by receipt traceability fields.

## Relationship to Existing Metrics

| Module | Scope | Overlap |
|--------|-------|---------|
| `RouteStats` | Per-route | `delivered`, `failed`, `loop_prevented` |
| `ReplayMetrics` | Per-route replay | `events_processed`, `deliveries_failed` |
| `EventMetrics` | Per-kind pipeline | `ingressed`, `delivered`, `failed` |
| `RuntimeAccounting` | Global process | All eight counters above |

`RuntimeAccounting` is **additive**: it provides global aggregates that
the per-route/per-kind modules do not offer.  It does not replace or
duplicate them.

## Future Integration Points

The accounting module is designed for future consumption:

1. **Snapshot integration**: `RuntimeAccounting.snapshot()` output can be
   included in `RuntimeSnapshot.accounting` (new field) or composed into
   `build_diagnostics_snapshot()`.

2. **DiagnosticsCollector**: Add `set_accounting_snapshot()` or
   compose `RuntimeAccounting` alongside `RouteStats` and `ReplayMetrics`.

3. **Pipeline hooks**: Wire `RuntimeAccounting` into `PipelineRunner`
   via `PipelineConfig` (one new optional field) with minimal increment
   calls at ingress, delivery, and error points.

  4. **Capacity controller**: `record_capacity_rejection()` is wired into
     both `PipelineRunner._deliver_to_targets_inner` (delivery capacity
     exhausted) and `ReplayEngine._stage_deliver` (replay capacity
     exhausted).  The counter is incremented at the point of rejection.

## Testing Requirements

Tests must verify:
- Fresh instance has all counters at zero.
- Each `record_*` method increments exactly the target counter.
- Multiple increments accumulate correctly.
- `reset()` returns previous values and zeros all counters.
- `snapshot()` keys are alphabetically sorted.
- `snapshot()` output is JSON-serialisable (`json.dumps` succeeds).
- Memory is bounded: snapshot size is constant.
- `RuntimeCounters` is frozen (immutable).
- `counters()` returns a `RuntimeCounters` instance.

## Constraints

- **No transport imports**: The accounting module imports nothing from
  transport adapters or SDKs.
- **No redesign**: Does not modify `RouteStats`, `ReplayMetrics`,
  `CapacityController`, or `DiagnosticsCollector`.
- **No new infrastructure**: No HTTP endpoints, no Prometheus, no
  dashboards, no admin APIs.
- **No error suppression**: Does not catch or swallow exceptions from
  callers.

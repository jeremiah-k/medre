# Contract 57 — Runtime Event Accounting

**Status:** Approved (Wave 1, Track 5)
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
| `replay_processed`     | `record_replay_processed()`    | Replay events processed through the pipeline   |
| `replay_rejected`      | `record_replay_rejected()`     | Replay events rejected by filter/mode/policy   |
| `loop_prevented`       | `record_loop_prevented()`      | Events blocked by the self-loop guard          |
| `capacity_rejections`  | `record_capacity_rejection()`  | Operations rejected by the capacity controller |

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
- Suitable for inclusion in Wave 2 runtime diagnostics snapshots.

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
persistent metrics, use an external monitoring system (Wave 2+).

## Operator Interpretation Guide

| Scenario | Meaning |
|----------|---------|
| `inbound_accepted` rising, `outbound_attempts` flat | Events accepted but not yet routed/delivered. May indicate routing gap or filtering. |
| `outbound_attempts` >> `outbound_delivered` + `outbound_failed` | Some attempts are in-flight or being skipped (check `loop_prevented`). |
| `loop_prevented` high relative to `outbound_attempts` | Self-loop guard firing frequently. Check route topology for cycles. |
| `capacity_rejections` rising | Capacity controller rejecting work. Consider increasing concurrency limits. |
| `replay_rejected` rising with `replay_processed` flat | Replay filter rejecting all events. Check replay request parameters. |
| All counters zero after uptime | Accounting not wired into the pipeline yet (Wave 2 integration). |

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

## Wave 2 Integration Points

The accounting module is designed for Wave 2 consumption:

1. **Snapshot integration**: `RuntimeAccounting.snapshot()` output can be
   included in `RuntimeSnapshot.accounting` (new field) or composed into
   `build_diagnostics_snapshot()`.

2. **DiagnosticsCollector**: Add `set_accounting_snapshot()` or
   compose `RuntimeAccounting` alongside `RouteStats` and `ReplayMetrics`.

3. **Pipeline hooks**: Wire `RuntimeAccounting` into `PipelineRunner`
   via `PipelineConfig` (one new optional field) with minimal increment
   calls at ingress, delivery, and error points.

4. **Capacity controller**: Wire `record_capacity_rejection()` into
   `CapacityController.acquire_*()` rejection paths.

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

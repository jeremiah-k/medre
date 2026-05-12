# Contract 59 — Runtime Durability Contract

**Status:** Active
**Scope:** Durability semantics for the MEDRE runtime: what is durable, what is process-local, crash recovery expectations, boundedness guarantees, and explicit non-guarantees.
**Audience:** Runtime builders, adapter authors, operators.
**References:** Contract 47 (Runtime Assembly), Contract 53 (Resource Control), Contract 54 (Runtime Shutdown), Contract 55 (Runtime Persistence), Contract 60 (Runtime Cancellation).

Every agent or document that references MEDRE durability, crash recovery, what survives restart, or boundedness must defer to this contract and Contract 55 (Runtime Persistence).


## 1. Scope

This contract specifies the durability boundary of the MEDRE runtime. It distinguishes between:

- **Durable state** — survives process termination, crash, and restart.
- **Process-local state** — exists only within a running process; lost on crash or shutdown.
- **Bounded state** — resource usage is capped by the runtime to prevent unbounded growth.

This contract describes the current runtime's actual behavior. No new storage mechanisms or durability features are introduced.


## 2. Runtime Guarantees

### 2.1 Events Are Stored Before Delivery

Every normalized event that enters the pipeline is written to SQLite **before** delivery begins. If the runtime crashes after storing an event but before delivering it, the event exists in the database with no delivery receipt. The event was preserved; the delivery was not.

### 2.2 Delivery Receipts Are Written After Completion

A delivery receipt is written to SQLite after each delivery attempt completes (success or failure). If the runtime crashes during a delivery, no receipt is written for that attempt. The event remains in storage without a receipt.

### 2.3 SQLite WAL Mode Provides Crash Consistency

SQLite operates in WAL (Write-Ahead Logging) journal mode. Committed transactions are durable even if the process is killed without a clean shutdown (`kill -9`, OOM, power loss). SQLite's own crash recovery mechanism handles incomplete WAL frames on the next open.

### 2.4 Shutdown Completes Persisted Writes

During a clean shutdown, the `stop()` method:

1. Stops accepting new work via `CapacityController.stop_accepting()`.
2. Drains in-flight work up to `shutdown_drain_timeout_seconds`.
3. Stops adapters, pipeline runner, and storage in reverse dependency order.

Storage `close()` flushes SQLite WAL buffers. Deliveries that complete within the drain window produce normal receipts. See Contract 54 for full shutdown semantics.

### 2.5 Capacity Is Bounded

The runtime enforces two independent concurrency bounds via `CapacityController`:

| Resource | Bound | Mechanism |
|----------|-------|-----------|
| In-flight deliveries | `max_inflight_deliveries` (default 100) | Semaphore |
| In-flight replay events | `max_inflight_replay_events` (default 100) | Semaphore |
| Meshtastic outbound queue | `max_queue_size` (default 1024) | `deque(maxlen=...)` — drop-oldest |

These bounds prevent unbounded memory growth from concurrent operations or queue accumulation. See Contract 53 for full capacity semantics.

**No other adapter-level queue bounds exist.** Matrix, LXMF, and MeshCore adapters rely on the global `CapacityController` semaphore and their transport's own flow control.


## 3. Durable State

The following state survives process termination (crash, shutdown, or restart):

| State | Storage | Written When |
|-------|---------|--------------|
| Canonical events | SQLite | During pipeline store step, before delivery |
| Delivery receipts | SQLite | After each delivery attempt completes |
| Route attribution (`route_id` on receipts) | SQLite | With the delivery receipt |
| Native references (platform message IDs) | SQLite | With the delivery receipt |
| Replay run metadata and results | SQLite | After each replay run completes |
| Cross-adapter relationships | SQLite | During pipeline store step |
| Global runtime metadata (schema version) | SQLite | On first creation and migration |
| Matrix E2EE crypto keys | Filesystem (`{state}/adapters/{id}/matrix/store/`) | SDK-managed |
| LXMF identities | Filesystem (`{state}/adapters/{id}/lxmf/`) | Transport-managed |
| Log history | Filesystem (`{state}/logs/medre.log`) | Append-only |

### 3.1 Persistence Timing

- **Events:** Written synchronously during the pipeline store step. An event is durable before the pipeline begins delivery planning.
- **Receipts:** Written after the adapter's `deliver()` returns. The receipt reflects the outcome of that delivery attempt.
- **Crypto keys:** Written by the Matrix SDK (nio) according to its own persistence schedule. MEDRE does not control nio's write timing.

### 3.2 Single-Machine Persistence

MEDRE persists state to a local SQLite database and local filesystem. There is:

- No replication.
- No remote backup.
- No distributed coordination.
- No shared storage across MEDRE instances.

Operators are responsible for database backup, log rotation, and monitoring disk space (a full disk stops event persistence).


## 4. Process-Local State

The following state is **lost** on process termination (crash, shutdown, or restart):

| State | Nature | Impact of Loss |
|-------|--------|----------------|
| In-flight deliveries | Semaphore-tracked coroutines | No receipt, no retry, no recovery |
| Active replay runs | Async generator iterations | Must re-initiate manually |
| `CapacityController` counters (`delivery_timeouts`, `delivery_rejections`, etc.) | In-memory counters | Reset to zero on every startup |
| `RouteStats` per-route counters | In-memory counters | No historical route statistics |
| `RuntimeAccounting` counters | In-memory counters | Reset to zero on every startup |
| Adapter health / connection state | In-memory | Adapters reconnect from scratch on restart |
| `Diagnostician` counters | In-memory | Reset to zero on every startup |
| `BootSummary` | In-memory | Recomputed on next startup |

### 4.1 No Recovery of In-Flight Work

In-flight deliveries and replay events that are abandoned at shutdown or lost on crash are **not** recovered on restart. There is no persistent in-flight queue, no replay resume mechanism, and no deduplication on restart. Restart begins with a clean in-flight state.

### 4.2 Counters Reset on Every Startup

All `CapacityController`, `RouteStats`, `RuntimeAccounting`, and `Diagnostician` counters start at zero on every runtime startup. There is no mechanism to persist or restore these counters across restarts. Operators who need historical counter data must extract it before shutdown via `medre diagnostics` or the diagnostic snapshot.


## 5. Degraded-Runtime Semantics

### 5.1 Partial Adapter Startup

When some adapters fail to start but at least one succeeds, the runtime enters `RUNNING` state with **DEGRADED** health (see Contract 47, startup classification):

- Successfully started adapters operate normally.
- Failed adapters are logged with `adapter_id` attribution.
- The pipeline runner and storage are fully operational.
- The `BootSummary` records which adapters started and which failed.

The runtime does **not** attempt to restart failed adapters. Recovery requires a full runtime restart.

### 5.2 Total Startup Failure

If zero adapters start (including build failures), the runtime:

1. Cleans up any partially-started resources (pipeline runner, storage).
2. Sets state to `FAILED`.
3. Raises `RuntimeStartupError` with a summary.
4. Callers do **not** need to call `stop()` after a total failure.

### 5.3 Runtime During Capacity Exhaustion

When `CapacityController` reaches its semaphore limit:

- New delivery attempts wait up to `delivery_acquire_timeout_seconds`, then fail with `status="permanent_failure"` and `error="delivery_capacity_exceeded"`.
- No retry is attempted — capacity timeout is a backpressure signal, not a transient error.
- The runtime continues operating; existing in-flight work completes normally.
- Counters (`delivery_timeouts`, `delivery_rejections`) are incremented and visible via `snapshot()`.

The runtime does **not** degrade into a different operational mode under capacity pressure. It rejects new work and continues processing in-flight work.


## 6. Boundedness Guarantees

### 6.1 Memory Boundedness

| Resource | Bound | Default | Policy on Overflow |
|----------|-------|---------|-------------------|
| Concurrent deliveries | `max_inflight_deliveries` | 100 | Reject (permanent failure with diagnostics) |
| Concurrent replay deliveries | `max_inflight_replay_events` | 100 | Reject (error with diagnostics) |
| Meshtastic outbound queue | `max_queue_size` | 1024 | Drop-oldest |

**Unbounded by design:**
- Matrix, LXMF, MeshCore adapter internal buffers are not explicitly bounded by MEDRE. They rely on transport SDK behavior and the global capacity semaphore.
- Events stored in SQLite grow without bound. There is no event retention policy or automatic pruning.

### 6.2 Time Boundedness

| Timeout | Default | Controls |
|---------|---------|----------|
| `delivery_acquire_timeout_seconds` | 1.0 | Max wait for a delivery semaphore slot |
| `shutdown_drain_timeout_seconds` | 10.0 | Max wait for in-flight work to complete during shutdown |
| `shutdown_timeout_seconds` | 10 | Overall shutdown budget |

All timeouts are configurable via `[runtime.limits]` and `[runtime]`. See Contract 53 §14.1 for the full configuration schema.

### 6.3 No Boundedness for External Resources

MEDRE does not bound:
- Transport SDK memory usage (nio, meshtastic, reticulum SDKs manage their own memory).
- SQLite database file size (grows with event volume; no automatic vacuum).
- Log file size (append-only; no built-in rotation).
- OS-level resource consumption (file descriptors, socket buffers, serial port buffers).

Operators must monitor disk space and log file growth externally.


## 7. Crash Recovery

On hard crash (`kill -9`, OOM, power loss):

1. **No graceful shutdown.** No shutdown logs. No drain phase.
2. **SQLite database is preserved.** WAL mode provides crash consistency. Events and committed receipts survive.
3. **In-flight deliveries are lost.** Events that were stored but had no receipt written remain in the database as undelivered. They are not automatically retried.
4. **All process-local state is lost.** Counters, route stats, adapter connection state — all reset.
5. **Restart with the same config.** Adapters reconnect autonomously.
6. **Adapters may suppress stale messages** based on their `startup_backlog_suppress_seconds` setting.

To identify events that were stored but never delivered (orphaned by a crash):

```sql
SELECT e.event_id, e.source_adapter, e.created_at
FROM canonical_events e
LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
WHERE r.event_id IS NULL
ORDER BY e.created_at DESC;
```


## 8. Explicit Non-Guarantees

The following are explicitly **not** provided:

- **Exactly-once delivery.** MEDRE is best-effort. Delivery receipts may be duplicated on retry. Events may be delivered more than once.
- **Transactionality.** There is no transactional boundary across multiple adapter deliveries. A fan-out to 3 adapters may have 2 succeed and 1 fail.
- **Persistent in-flight recovery.** In-flight work is lost on crash or shutdown. No retry of abandoned deliveries.
- **Replay deduplication.** If the runtime restarts, replayed events may be delivered again.
- **Replay resume.** An interrupted replay run must be re-initiated manually.
- **Distributed durability.** State is local to the machine. No replication or consensus.
- **Persistence of in-memory counters.** All diagnostic, capacity, route, and accounting counters are zeroed on startup.
- **Database size bounding.** SQLite grows with event volume. No automatic pruning or retention policy.
- **Hot restart.** The runtime is a single-process application. No zero-downtime restart mechanism.
- **Per-adapter restart.** Individual adapters cannot be restarted without shutting down the entire runtime.


## 9. Cross-References

| Topic | Contract |
|-------|----------|
| CapacityController, delivery/replay capacity bounds, exhaustion behavior | Contract 53 (Resource Control) |
| Shutdown ordering, drain phases, in-flight work handling | Contract 54 (Runtime Shutdown) |
| Cancellation semantics, CapacityController stop behavior, stop-during-startup | Contract 60 (Runtime Cancellation) |
| Persistence timing, WAL consistency, receipt durability, storage schema | Contract 55 (Runtime Persistence) |
| Runtime assembly, `RuntimeState` lifecycle, startup classification | Contract 47 (Runtime Assembly) |
| Runtime observability, diagnostic snapshots | Contract 48 (Runtime Observability) |

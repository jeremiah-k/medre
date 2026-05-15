# Contract 59 — Runtime Durability Contract

**Status:** Active
**Scope:** Durability semantics for the MEDRE runtime: what is durable, what is process-local, crash recovery expectations, boundedness guarantees, and explicit non-guarantees. For **where** state is stored and **when** it is written, see Contract 55 (Runtime Persistence).
**Audience:** Runtime builders, adapter authors, operators.
**Tracks:** 9 (evidence consolidation and boundary enforcement)
**References:** Contract 47 (Runtime Assembly), Contract 53 (Resource Control), Contract 54 (Runtime Shutdown), Contract 55 (Runtime Persistence), Contract 60 (Runtime Cancellation), Contract 61 (Operational Evidence).

Every agent or document that references MEDRE **durability guarantees**, **crash recovery**, **what survives restart**, or **boundedness** must defer to this contract. For **storage locations**, **file formats**, and **write timing**, see Contract 55.

**Evidence separation (Track 9):** Durability claims in this contract are backed by S-tier (simulated/fake) evidence from deterministic unit tests. R-tier (real-live-runtime) evidence for crash recovery, sustained operation boundedness, and restart durability has NOT been collected. See Contract 61 §5.1 for current evidence scores per transport. Do not claim production durability without R-tier evidence per Contract 61 §6.


## 1. Scope

This contract specifies the **durability boundary** of the MEDRE runtime — what behavioral guarantees the runtime provides around state survival, crash recovery, and resource boundedness. It distinguishes between:

- **Durable state** — survives process termination, crash, and restart.
- **Process-local state** — exists only within a running process; lost on crash or shutdown.
- **Bounded state** — resource usage is capped by the runtime to prevent unbounded growth.

For the **storage locations** and **write timing** of persisted state (SQLite tables, file paths, WAL mode details), see Contract 55 (Runtime Persistence). This contract references those storage details but does not duplicate them.

This contract describes the current runtime's actual behavior. No new storage mechanisms or durability features are introduced.


## 2. Runtime Guarantees

### 2.1 Events Are Stored Before Delivery

Every normalized event that enters the pipeline is written to durable storage **before** delivery begins (see Contract 55 §4.1 for write ordering). If the runtime crashes after storing but before delivering, the event is preserved with no delivery receipt.

### 2.2 Delivery Receipts Are Written After Completion

A delivery receipt is written to durable storage after each delivery attempt completes (success or failure). If the runtime crashes during a delivery, no receipt is written for that attempt. The event remains in storage without a receipt.

### 2.3 Committed Transactions Survive Hard Crash

SQLite operates in WAL mode (see Contract 55 §2.1). Committed transactions are durable even if the process is killed without a clean shutdown (`kill -9`, OOM, power loss). SQLite's crash recovery mechanism handles incomplete WAL frames on the next open.

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

The following state survives process termination (crash, shutdown, or restart). Storage locations and write timing are in **Contract 55 §2** and **§4**.

| State | Survives Crash | Written When |
|-------|---------------|--------------|
| Canonical events | Yes | During pipeline store step, before delivery |
| Delivery receipts | Yes | After each delivery attempt completes |
| Receipt traceability (`source`, `replay_run_id` on receipts) | Yes | With the delivery receipt |
| Route attribution (`route_id` on receipts) | Yes | With the delivery receipt |
| Native references (platform message IDs) | Yes | With the delivery receipt |
| Cross-adapter relationships | Yes | During pipeline store step |
| Global runtime metadata (schema version) | Yes | On first creation and migration |
| Matrix E2EE crypto keys | Yes | SDK-managed (see Contract 55 §2.2) |
| LXMF identities | Yes | Transport-managed (see Contract 55 §2.2) |
| Log history | Yes (up to last flush) | Append-only |

### 3.1 Persistence Timing

Write timing and atomicity details are in **Contract 55 §4**. Key guarantee: events are durable before delivery begins; receipts are durable after delivery completes.

### 3.2 Single-Machine Persistence

MEDRE persists state to a local SQLite database and local filesystem (see Contract 55 §2.3). There is:

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
| ReplaySummary (completed replay results) | In-memory dataclass | Must re-run replay to regenerate |
| `CapacityController` internal gauges (`delivery_timeouts`, `delivery_rejections`, etc.) | In-memory counters | Reset to zero on every startup |
| `RouteStats` per-route counters | In-memory counters | No historical route statistics |
| `RuntimeAccounting` counters | In-memory counters | Reset to zero on every startup |
| Adapter health / connection state | In-memory | Adapters reconnect from scratch on restart |
| `Diagnostician` counters | In-memory | Reset to zero on every startup |
| `BootSummary` | In-memory | Recomputed on next startup |

### 4.1 No Recovery of In-Flight Work

In-flight deliveries and replay events that are abandoned at shutdown or lost on crash are **not** recovered on restart. There is no persistent in-flight queue, no replay resume mechanism, and no deduplication on restart. Restart begins with a clean in-flight state.

### 4.2 Counters Reset on Every Startup

All `CapacityController`, `RouteStats`, `RuntimeAccounting`, and `Diagnostician` counters start at zero on every runtime startup. There is no mechanism to persist or restore these counters across restarts. Operators who need historical counter data must extract it before shutdown via `medre diagnostics` or the diagnostic snapshot.


## 4.3 BEST_EFFORT Replay Storage Semantics

`ReplayMode.BEST_EFFORT` is the only replay mode that produces storage side effects. Its behavior:

- Every BEST_EFFORT delivery creates **new** `DeliveryReceipt` and `NativeMessageRef` records in storage. These are durable `DeliveryReceipt` records indistinguishable in schema from live receipts, but **distinguishable by origin** via the `source` and `replay_run_id` columns.
- `source` is set to `"replay"` on replay-produced receipts (live deliveries use `"live"`). `replay_run_id` carries the replay run's ID, enabling operators to trace which receipts originated from a specific replay run. `replay_run_id` is for operator tracing only.
- **Traceability is not deduplication.** Replay may still produce duplicate sends — replaying an event that was previously delivered will produce a second delivery attempt with no storage-level deduplication. Traceability means the operator can identify which receipts came from replay using `source` and `replay_run_id`. The `replay_run_id` field supports post-incident investigation and manual mitigation only; it does not prevent or detect duplicate sends at delivery time.
- **Native message refs created during replay are NOT tagged with `source` or `replay_run_id`.** The `NativeMessageRef` schema does not carry replay origin fields. Replay-produced native refs can be correlated to their replay origin through the associated `DeliveryReceipt` (which carries `source` and `replay_run_id`), then via the receipt's `delivery_plan_id` / `event_id` linkage to the native ref. This design avoids schema complexity on native refs while preserving full traceability through the receipt → native ref flow.
- `ReplaySummary` itself is **not durably persisted**. It is an in-memory dataclass returned to the caller. Process crash or restart loses it entirely; the replay must be re-run to regenerate the summary.
- **Duplicate-send risk** applies to all adapter transports. Replaying an event that was previously delivered will produce a second delivery attempt with no storage-level deduplication.
- `RuntimeAccounting` remains process-local and is reset on restart. Receipt traceability fields (`source`, `replay_run_id`) are stored in the durable `DeliveryReceipt` record and survive crashes.


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

- New delivery attempts wait up to `delivery_acquire_timeout_seconds`, then fail with `status="permanent_failure"` and `error="delivery_capacity_exceeded"` (or `error="delivery_rejected_shutdown"` if the runtime has stopped accepting work).
- No retry is attempted — capacity timeout is a backpressure signal, not a transient error.
- The runtime continues operating; existing in-flight work completes normally.
- Internal gauges (`delivery_timeouts`, `delivery_rejections`) are incremented and visible via `snapshot()`. Operator-facing counters (`capacity_rejections`, `outbound_failed`) are tracked in `RuntimeAccounting`.

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
- **Replay deduplication.** Replayed events may produce duplicate deliveries. BEST_EFFORT replay creates new `DeliveryReceipt` and `NativeMessageRef` records. Replay receipts are distinguishable from live receipts by `source`/`replay_run_id` (see §4.3), but no deduplication mechanism prevents duplicate sends.
- **Replay resume.** An interrupted replay run must be re-initiated manually. Completed `ReplaySummary` results are not durably persisted.
- **Replay run audit table.** There is no separate persistent replay run or audit table. Replay run traceability is available via `source` and `replay_run_id` columns on `delivery_receipts`, not via a dedicated audit store.
- **Distributed durability.** State is local to the machine. No replication or consensus.
- **Persistence of in-memory counters.** All diagnostic, capacity, route, and accounting counters are zeroed on startup.
- **Database size bounding.** SQLite grows with event volume. No automatic pruning or retention policy.
- **Hot restart.** The runtime is a single-process application. No zero-downtime restart mechanism.
- **Per-adapter restart.** Individual adapters cannot be restarted without shutting down the entire runtime.


## 9. Cross-References

| Topic | Contract |
|-------|----------|
| Storage locations, file paths, SQLite schema, write timing, WAL mode details | Contract 55 (Runtime Persistence) |
| CapacityController, delivery/replay capacity bounds, exhaustion behavior | Contract 53 (Resource Control) |
| Shutdown ordering, drain phases, in-flight work handling | Contract 54 (Runtime Shutdown) |
| Cancellation semantics, CapacityController stop behavior, stop-during-startup | Contract 60 (Runtime Cancellation) |
| Runtime assembly, `RuntimeState` lifecycle, startup classification | Contract 47 (Runtime Assembly) |
| Runtime observability, diagnostic snapshots | Contract 48 (Runtime Observability) |

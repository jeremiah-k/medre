# Contract 55 — Runtime Persistence Contract

**Status:** Active
**Scope:** Authoritative specification for what MEDRE runtime state is persisted, what is process-local, persistence timing semantics, crash consistency expectations, replay persistence guarantees, route attribution persistence guarantees, and observability persistence.
**Audience:** Runtime builders, adapter authors, operators, supervision implementors.
**References:** Contract 46 (Runtime Storage and Path Model), Contract 47 (Runtime Assembly), Contract 48 (Runtime Observability), Contract 49 (Routing and Bridge), Contract 51 (Route Attribution), Contract 53 (Resource Control), Contract 54 (Runtime Shutdown).

Every agent or document that references MEDRE persistence behavior, crash recovery expectations, what survives restart, or what is lost on process termination must defer to this contract.


## 1. Scope

This contract defines the persistence boundary for the MEDRE runtime. It establishes what state is durable (survives process termination and restart), what state is process-local (lost on crash or shutdown), and what timing guarantees apply to durable writes.

This is not a persistence design document. It describes the current runtime's actual persistence behavior. No new storage mechanisms are introduced by this contract.


## 2. Authoritative Persisted State

### 2.1 SQLite Database

The single SQLite database at `{state}/medre.sqlite` is the authoritative persisted state of the MEDRE runtime. It holds:

| Table/Area | Contents | Written When |
|------------|----------|--------------|
| Canonical events | Every normalized event that entered the pipeline | During the pipeline store step, before delivery begins |
| Delivery receipts | `DeliveryReceipt` records with status, attribution, retry lineage | After each delivery attempt completes (success or failure) |
| Native references | Platform-native message IDs and channel IDs | With the delivery receipt |
| Route attribution | `route_id` on `DeliveryReceipt` | With the delivery receipt |
| Replay state | Replay run metadata and results | After each replay run completes |
| Cross-adapter relationships | Links between events across adapters | During the pipeline store step |
| Global runtime metadata | Schema version, runtime identity | On first creation and migration |

**Key properties:**

- SQLite uses WAL (Write-Ahead Logging) journal mode. This provides good crash consistency: committed transactions are durable even if the process is killed without a clean shutdown.
- Events are stored **before** delivery begins. If the runtime crashes after storing an event but before delivering it, the event is in the database with no delivery receipt. The event was preserved; the delivery was not.
- Delivery receipts are written **after** each delivery attempt, not before. A receipt exists only if the delivery attempt completed far enough to produce a result.
- There are **no per-adapter databases**. All persisted state is in the single global database. Adapter-local filesystem state (section 2.2) is transport-owned.

### 2.2 Transport-Owned Persistent Files

The following files are persisted on disk but are owned by their respective transports, not by the MEDRE runtime core:

| Path | Owner | Survives Crash |
|------|-------|---------------|
| `{state}/adapters/{adapter_id}/matrix/store/` | Matrix SDK (nio crypto store: Olm/Megolm session keys, device keys) | Yes |
| `{state}/adapters/{adapter_id}/lxmf/` | LXMF/Reticulum (identity files) | Yes |
| `{state}/logs/medre.log` | Runtime logging | Yes (appended) |
| Config file (operator-managed) | Operator | Yes |

These survive crashes and restarts. The MEDRE runtime does not manage their consistency — the owning transport or SDK does.

### 2.3 What Persistence Means Here

"Persisted" means: the state exists on disk in a form that survives process termination, including hard kills (`kill -9`, OOM, power loss). It does not mean: replicated, backed up, or remotely stored. MEDRE persistence is single-machine, single-file (SQLite) plus transport-owned files. Operators are responsible for backup and disaster recovery.


## 3. Process-Local State (NOT Persisted)

The following runtime state is held in memory only. It is lost when the process terminates, whether by clean shutdown or hard crash.

### 3.1 RouteStats

`RouteStats` — per-route delivery counters (deliveries attempted, succeeded, failed, skipped, loop-prevented). These are process-local counters for in-flight observability. They are never written to SQLite or to disk.

### 3.2 CapacityController State

`CapacityController` state — semaphore counts, diagnostic gauges, and all counters:

| Counter | Nature |
|---------|--------|
| `delivery_current` | Process-local gauge |
| `delivery_limit` | Derived from config (re-created on startup) |
| `delivery_timeouts` | Process-local counter |
| `delivery_rejections` | Process-local counter |
| `replay_current` | Process-local gauge |
| `replay_limit` | Derived from config (re-created on startup) |
| `replay_timeouts` | Process-local counter |
| `replay_rejections` | Process-local counter |

All of these reset to zero on startup. No history is retained across restarts.

### 3.3 In-Flight Deliveries

Active adapter `deliver()` calls that have not yet completed. If the runtime crashes while deliveries are in-flight:

- No receipt is written for in-flight deliveries.
- The delivery is not retried on restart.
- The event is already stored in SQLite (it was stored before delivery began), but there is no receipt indicating the delivery outcome.
- Operators can identify these orphaned events by querying for events that have no corresponding delivery receipt.

In-flight deliveries are **not crash-recoverable**. This is an explicit non-guarantee.

### 3.4 Active Replay Runs

In-progress replay operations. If the runtime crashes during a replay run:

- The replay run is lost. It does not resume on restart.
- Any deliveries that completed before the crash produced receipts — those receipts are persisted.
- The remaining replay events are not retried automatically.

Replay requests are **not durable jobs**. They are ephemeral operations that run to completion or are lost on crash. There is no persistent replay queue, no replay resume mechanism, and no replay deduplication.

### 3.5 Adapter Health and Connection State

Current adapter health state (`healthy`, `degraded`, `failed`, `stopped`), connection state (`connected`, `disconnected`, `reconnecting`), and reconnect attempt counts. All ephemeral. Adapters rebuild their state from the transport on startup.

### 3.6 Pipeline Runner State

Which events are currently being processed by the pipeline runner (routing, planning, delivering). Ephemeral — the pipeline has no persistent work queue.

### 3.7 RoutingMetadata (In-Flight)

`RoutingMetadata.route_trace` on in-flight `CanonicalEvent` instances. This is populated during route matching and travels with the event through the pipeline. It is never written to SQLite. See Contract 51 §2.1.


## 4. Persistence Timing Semantics

### 4.1 Pipeline Write Ordering

The pipeline proceeds through these stages for each event:

```
ingest → normalize → store → route → plan → deliver → receipt
```

The **store** step writes the canonical event to SQLite before any delivery begins. This means:

- Events that entered the pipeline are always persisted, even if delivery never happens.
- Delivery receipts are written after each delivery attempt completes.
- A receipt with `status="sent"` or `status="confirmed"` proves the delivery attempt completed and the adapter reported success.
- An event with no receipt means delivery was never attempted or never completed.

### 4.2 Write Atomicity

SQLite transactions are atomic. An event write either completes fully or not at all. A receipt write is a separate transaction from the event write. This means:

- It is possible for an event to exist in the database with zero receipts (delivery never attempted).
- It is not possible for a receipt to exist without its corresponding event (receipts reference events by `event_id`).

### 4.3 Flush on Shutdown

During the Persist phase of shutdown (Contract 54 §1, Phase 4), the runtime ensures pending writes are flushed. After Phase 4 completes, all receipts and events that were produced before shutdown began are durable on disk.

This flush does **not** happen on hard crash. On hard crash, only transactions that were committed before the crash are preserved. WAL mode makes committed transactions robust against corruption.


## 5. Crash Consistency Expectations

### 5.1 Hard Crash (kill -9, OOM, power loss)

| What Happens | Result |
|-------------|--------|
| SQLite database | **Preserved.** WAL mode ensures committed transactions survive. The database may contain events without receipts. |
| In-flight deliveries | **Lost.** No receipts, no retry, no recovery. |
| Active replay runs | **Lost.** No resume, no durable queue. |
| Runtime counters (RouteStats, CapacityController) | **Lost.** All counters reset to zero on restart. |
| Adapter health/connection state | **Lost.** Adapters reconnect from scratch. |
| Transport-owned files (crypto stores, identities) | **Preserved.** These are managed by their respective SDKs/transports. |
| Log file | **Preserved.** Appended up to the point of crash. The last few lines may be lost if not flushed. |

### 5.2 Clean Shutdown

| What Happens | Result |
|-------------|--------|
| SQLite database | **Preserved.** Shutdown Phase 4 (Persist) flushes pending writes. |
| In-flight deliveries | **Drained or cancelled.** See Contract 54 §3. Drained deliveries produce receipts. Cancelled deliveries do not. |
| Active replay runs | **Cancelled.** Completed replay deliveries that produced receipts are preserved. Remaining replay events are lost. |
| Runtime counters | **Lost.** Not persisted, even on clean shutdown. |
| Adapter state | **Clean stop.** Each adapter stops in reverse start order. |

### 5.3 Database Integrity After Crash

SQLite with WAL mode is designed to survive hard crashes without database corruption. However, operators should verify integrity after a suspected crash:

```bash
sqlite3 {state}/medre.sqlite "PRAGMA integrity_check;"
```

If the integrity check fails, the database file may need to be restored from backup. Deleting the database and restarting creates a fresh database but loses all event history (see runtime-operation.md Recovery Procedures).


## 6. Replay Persistence Guarantees

Replay is an **ephemeral runtime operation**, not a durable job system.

| Property | Value |
|----------|-------|
| Replay request durability | Not persisted. Replay runs are initiated in-memory and lost on crash. |
| Replay queue | Does not exist. There is no persistent replay queue. |
| Replay resume after crash | Not supported. Replay must be re-initiated manually. |
| Replay deduplication | Not provided. Re-running replay may produce duplicate deliveries. |
| Replay receipt persistence | **Yes.** Delivery receipts produced by `BEST_EFFORT` replay are persisted to SQLite like any other receipt. |
| Replay route attribution persistence | **No.** `ReplayRouteAttribution` is an ephemeral result. It is not written to SQLite. |

**Operator implication:** If a replay run is interrupted by a crash or shutdown, the operator must re-run the replay manually. Completed deliveries from the interrupted run are already recorded (receipts in SQLite). The operator should inspect existing receipts before re-running to understand what was already delivered.


## 7. Route Attribution Persistence Guarantees

| Attribution Location | Persistence | Survives Crash |
|---------------------|-------------|---------------|
| `DeliveryReceipt.route_id` | Persisted in SQLite with the receipt | Yes |
| `RoutingMetadata.route_trace` (in-flight) | Process-local, on the CanonicalEvent object | No |
| `DeliveryOutcome.route_id` (pipeline-internal) | Process-local, pipeline result | No |
| `ReplayRouteAttribution.route_ids` | Process-local, replay result only | No |
| `RouteStats` per-route counters | Process-local, in-memory | No |

The only route attribution that survives a crash is the `route_id` on persisted `DeliveryReceipt` records. All other attribution is ephemeral.

**Operator implication:** To reconstruct what routes matched historical events, query delivery receipts by `route_id`. In-flight attribution at the time of crash is lost.


## 8. Observability Persistence

| Observability Data | Persistence | Survives Crash |
|-------------------|-------------|---------------|
| Structured log entries | Appended to `{state}/logs/medre.log` | Yes (up to last flush) |
| Diagnostic counters (`delivery_timeouts`, etc.) | Process-local only | No |
| Capacity gauges (`delivery_current`, etc.) | Process-local only | No |
| RouteStats per-route counters | Process-local only | No |
| `medre diagnostics` output | Ephemeral snapshot, never persisted | No |
| Adapter health states | Process-local only | No |

There is no persistent metrics store. Observability is split into two categories:

1. **Logs** — persistent, append-only, operator-managed for rotation.
2. **Counters and gauges** — ephemeral, process-local, reset on every startup.

**Operator implication:** If you need historical metrics (delivery rates over time, capacity timeout trends), you must implement external log aggregation and metric extraction. MEDRE does not retain runtime counters across restarts.


## 9. Operator Crash Recovery Matrix

### 9.1 What Survives Restart

| State | Location | Recovery |
|-------|----------|----------|
| Canonical events | SQLite `{state}/medre.sqlite` | Fully available. Query by time range, source adapter, event type. |
| Delivery receipts | SQLite | Fully available. Query by status, route, adapter, time range. |
| Native references | SQLite | Fully available. |
| Route attribution on receipts | SQLite (receipt `route_id`) | Fully available. |
| Replay run metadata (completed runs) | SQLite | Available for completed replay runs only. |
| Matrix crypto keys | `{state}/adapters/{id}/matrix/store/` | E2EE sessions resume without re-verification. |
| LXMF identities | `{state}/adapters/{id}/lxmf/` | Identity preserved. |
| Log history | `{state}/logs/medre.log` | Available for post-mortem analysis. |
| Configuration | Config file (operator-managed) | Unchanged. |

### 9.2 What Does NOT Survive Hard Crash

| State | Nature | Operator Impact |
|-------|--------|----------------|
| In-flight deliveries | Lost | Events exist in SQLite but have no receipt. Operator cannot distinguish "delivery was attempted but crashed" from "delivery was never attempted." |
| Active replay runs | Lost | Must be re-initiated manually. |
| Runtime counters (all) | Lost | No history of delivery timeouts, rejections, capacity pressure from prior run. |
| RouteStats | Lost | Per-route delivery counts reset. No historical route-level statistics. |
| CapacityController gauges | Lost | Current capacity usage resets. |
| Adapter health/connection state | Lost | Adapters reconnect from scratch. Startup backlog suppression may apply. |
| Pipeline work in progress | Lost | Events that were in the routing/planning/delivering pipeline stages are not retried. |

### 9.3 Startup After Crash — What to Expect

1. Runtime reads config and initializes storage.
2. SQLite integrity is implicitly checked on connection. WAL mode recovery is automatic.
3. Adapters start in deterministic order (Contract 47 §3).
4. Adapters reconnect to their transports. The `startup_backlog_suppress_seconds` setting controls how each adapter handles stale backlog from the transport.
5. Runtime counters begin at zero.
6. No automatic replay. No automatic re-delivery of in-flight events from the crashed run.
7. Events in SQLite without receipts are orphaned — they can be identified by querying for events with no matching receipt, but there is no built-in mechanism to re-deliver them.


## 10. Runtime Snapshot Semantics

Runtime snapshot (point-in-time capture of runtime state for supervision or diagnostics) is addressed by the runtime supervision and accounting track. This contract establishes the persistence baseline that snapshot mechanisms build upon:

- A snapshot captures process-local state (RouteStats, CapacityController gauges, adapter health) that is otherwise lost on crash.
- Snapshot durability depends on where the snapshot is stored. If stored in SQLite, it inherits SQLite's crash consistency. If logged, it inherits log persistence.
- Snapshot mechanisms do not change the fundamental persistence model described in this contract.

Snapshot implementation details are out of scope for this contract.


## 11. Degraded Runtime Examples

### 11.1 Database Full

If the SQLite database reaches filesystem limits:

1. New event writes fail. The pipeline logs errors.
2. Delivery receipts for events that were stored earlier may still succeed (the receipt table may be writable even if the event table is full, depending on page allocation).
3. The runtime continues running — adapters remain connected, in-flight deliveries may complete.
4. New events entering the pipeline are lost (cannot be stored).
5. **Operator action:** Free disk space or move the database to a larger volume. Restart the runtime after resolving the space issue.

### 11.2 Crypto Store Corruption

If the Matrix crypto store becomes corrupted:

1. E2EE sessions cannot be established. The Matrix adapter reports `encryption_error`.
2. Plaintext delivery may continue (if `encryption_mode="e2ee_optional"`).
3. If `encryption_mode="e2ee_required"`, the adapter fails all deliveries to encrypted rooms.
4. **Operator action:** Delete the crypto store directory and restart. New keys are established on next connect. **Previous E2EE session keys are lost** — devices must be re-verified.

### 11.3 Transport Disconnection (Persisted State Unaffected)

If a transport disconnects (radio unplugged, Matrix homeserver unreachable):

1. The adapter enters `degraded` or `failed` health state.
2. Inbound events stop. Outbound deliveries fail.
3. SQLite persistence is unaffected — the database continues to accept writes.
4. Other adapters continue operating independently.
5. **Operator action:** Restore transport connectivity. The adapter's reconnect policy attempts recovery autonomously. Check adapter health via `medre diagnostics`.


## 12. Startup Recovery Examples

### 12.1 Clean Restart After Clean Shutdown

```
INFO  medre.runtime: Loading config from /opt/medre/config.toml
INFO  medre.runtime: Storage opened: /opt/medre/state/medre.sqlite (12345 events, 6789 receipts)
INFO  medre.runtime: Starting 2 adapters
INFO  medre.adapters.matrix.bridge: adapter_starting transport=matrix adapter_id=bridge
INFO  medre.adapters.matrix.bridge: adapter_started transport=matrix adapter_id=bridge duration_ms=312
INFO  medre.adapters.meshtastic.radio: adapter_starting transport=meshtastic adapter_id=radio
INFO  medre.adapters.meshtastic.radio: adapter_started transport=meshtastic adapter_id=radio duration_ms=98
INFO  medre.runtime: Assembly complete: 2/2 adapters started in 410ms
INFO  medre.runtime: Resource limits: max_inflight_deliveries=100 max_inflight_replay=100 drain_timeout=10s
```

All counters at zero. All persisted state available. No recovery needed.

### 12.2 Restart After Hard Crash

```
INFO  medre.runtime: Loading config from /opt/medre/config.toml
INFO  medre.runtime: Storage opened: /opt/medre/state/medre.sqlite (12345 events, 6772 receipts)
INFO  medre.runtime: Starting 2 adapters
INFO  medre.adapters.matrix.bridge: adapter_starting transport=matrix adapter_id=bridge
INFO  medre.adapters.matrix.bridge: adapter_connected transport=matrix adapter_id=bridge
INFO  medre.adapters.matrix.bridge: adapter_started transport=matrix adapter_id=bridge duration_ms=534
INFO  medre.adapters.meshtastic.radio: adapter_starting transport=meshtastic adapter_id=radio
INFO  medre.adapters.meshtastic.radio: adapter_started transport=meshtastic adapter_id=radio duration_ms=102
INFO  medre.runtime: Assembly complete: 2/2 adapters started in 636ms
```

Note the receipt count (6772) is lower than what it would have been without the crash — 17 deliveries were in-flight at crash time and produced no receipts. The corresponding events exist in the database without receipts.

**Operator action after crash recovery:**

```sql
-- Find events that were stored but never delivered
SELECT e.event_id, e.source_adapter, e.created_at
FROM canonical_events e
LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
WHERE r.event_id IS NULL
ORDER BY e.created_at DESC;
```

This identifies orphaned events. The operator can then decide whether to replay them.

### 12.3 Restart After Crash with Database Integrity Issue

```
INFO  medre.runtime: Loading config from /opt/medre/config.toml
ERROR medre.runtime: Storage integrity check failed: database disk image is malformed
ERROR medre.runtime: Cannot open storage. Manual intervention required.
```

**Operator action:**

```bash
# Verify integrity
sqlite3 /opt/medre/state/medre.sqlite "PRAGMA integrity_check;"

# If corrupted, attempt recovery
sqlite3 /opt/medre/state/medre.sqlite ".recover" | sqlite3 /opt/medre/state/medre-recovered.sqlite

# Replace corrupted database with recovered version
mv /opt/medre/state/medre.sqlite /opt/medre/state/medre.sqlite.corrupted
mv /opt/medre/state/medre-recovered.sqlite /opt/medre/state/medre.sqlite

# Restart
medre run --config /opt/medre/config.toml
```

If recovery fails, delete the database and accept data loss. Crypto stores and identity files are unaffected (they are in separate directories).


## 13. Persistence Expectations Summary

| Question | Answer |
|----------|--------|
| Is event history preserved across restarts? | **Yes.** Events are in SQLite. |
| Are delivery receipts preserved across restarts? | **Yes.** Receipts are in SQLite. |
| Is route attribution on receipts preserved? | **Yes.** `route_id` is stored with the receipt. |
| Are runtime counters preserved across restarts? | **No.** All counters reset to zero. |
| Is in-flight work recoverable after crash? | **No.** No retry, no recovery, no receipt. |
| Are replay requests durable? | **No.** Replay is ephemeral, not a job queue. |
| Does replay deduplication exist? | **No.** Re-running replay may produce duplicates. |
| Are E2EE sessions preserved across restarts? | **Yes.** Crypto store is on disk. |
| Is transport identity preserved across restarts? | **Yes.** Identity files are on disk. |
| Are logs preserved across restarts? | **Yes.** Appended to log file. |
| Is there a persistent metrics store? | **No.** Counters are process-local only. |
| Does MEDRE backup its own database? | **No.** Operators are responsible for backup. |

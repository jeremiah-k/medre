# MEDRE Runtime Supervision Runbook

> Last updated: 2026-05-11
> Scope: Operator-facing supervision and persistence guidance for the MEDRE runtime
> Status: Pre-beta. Not production. Operational model is accurate to code; supervision tooling is in progress.

This runbook describes how operators supervise the MEDRE runtime, interpret persistence behavior, diagnose degraded states, and perform recovery. It is the operator-facing companion to Contract 55 (Runtime Persistence) and Contract 54 (Runtime Shutdown).


## 1. Runtime Supervision Overview

MEDRE is a single-process, multi-adapter runtime. Supervision is the operator's responsibility — there is no external health monitor, watchdog, or orchestrator built into MEDRE.

### 1.1 State Layers

MEDRE uses four distinct state layers. Understanding which layer you are looking at is essential for correct diagnosis.

| Layer | What It Tracks | Values | Where to Find It |
|-------|---------------|--------|-----------------|
| **RuntimeState** | Process lifecycle | `INITIALIZED`, `STARTING`, `RUNNING`, `STOPPING`, `STOPPED`, `FAILED` | `snapshot.lifecycle.runtime_state` |
| **RuntimeHealth** | Aggregate adapter health | `HEALTHY`, `DEGRADED`, `FAILED` | `snapshot.startup.startup_health.runtime_health` (startup-derived only) |
| **StartupOutcome** | One-time boot result | `SUCCESS`, `PARTIAL`, `TOTAL_FAILURE` | `snapshot.startup.boot_summary` |
| **AdapterState** | Per-adapter lifecycle | `INITIALIZING`, `READY`, `DEGRADED`, `BACKPRESSURED`, `DISCONNECTED`, `STOPPING`, `FAILED`, `STOPPED` | `snapshot.lifecycle.adapters.{adapter_id}` |

**Key point:** `RuntimeState` has no `DEGRADED` value. A runtime in `RUNNING` state can have `DEGRADED` health (some adapters up, some down). Degradation is a health concept, not a lifecycle concept.

**Health is not live-refreshed.** The `startup_health` value is computed once during startup and frozen. It does not reflect post-startup adapter failures. `live_health` is always `null` until active health polling is implemented.

### 1.2 What MEDRE Provides
- **Health classification** — pure functions (`classify_runtime_health`, `classify_adapter_failure_severity`, `runtime_supervision_snapshot`) that accept adapter states and return deterministic health/severity classifications. These can be called at any time with any adapter state values.
- **Startup health assessment** — adapter states are classified during startup and recorded in the boot summary.

**What MEDRE does NOT provide (not implemented in this tranche):**
- Active post-start failure detection — MEDRE does not automatically detect adapter failures after startup.
- Health refresh polling — adapter health is not periodically re-checked at runtime.
- Automatic runtime state transitions in response to adapter failures — the runtime does not actively transition to DEGRADED or FAILED when an adapter crashes; operators must diagnose and act on failures.

**Implication:** Operators must externally monitor adapter health (logs, diagnostics command, process supervision) and restart the runtime if an adapter failure requires action.

The operator supervises three categories of runtime state:

| Category | Nature | Source |
|----------|--------|--------|
| **Persisted state** | Durable (survives crash) | SQLite database, transport-owned files, logs |
| **Process-local state** | Ephemeral (lost on crash) | CapacityController gauges, RouteStats, adapter health, in-flight work |
| **Configuration** | Operator-managed | TOML config file |

Only persisted state survives a hard crash. Process-local state must be re-observed after restart.

### 1.3 Snapshot Provenance Metadata

The runtime snapshot (`medre diagnostics` output) carries explicit provenance metadata on each section. This tells you whether a value is a one-time startup snapshot, a process-local value, or live-refreshed — without needing to consult external documentation.

Each section has `scope` and `live_refresh` fields:

| Section | `scope` | `live_refresh` | What It Means for You |
|---------|---------|----------------|----------------------|
| `startup` | `"startup"` | `false` | Computed once during startup. **Does not change.** Contains boot summary, build failures, and startup health. |
| `health` | `"startup"` | `false` | Health assessment from startup. `live_health` is always `null`. **Does not reflect post-startup failures.** |
| `lifecycle` | `"process_local"` | `false` | Current in-process state at snapshot time. `runtime_state`, adapter lifecycle states, and uptime. |
| `diagnostics` | `"process_local"` | `true` | Event buffer grows during runtime. Most complete record of what happened after startup. |
| `routes.build_readiness` | `"build"` | `false` | Route states from build time. Frozen. |
| `routes.startup_readiness` | `"startup"` | `false` | Route states from startup time. Frozen. |
| `routes.stats` | (none) | (none) | Live delivery counters. Grows during runtime. |

Per-adapter entries in `adapters` carry a `provenance` field:

| Field | Provenance | Meaning |
|-------|-----------|---------|
| `adapters.{id}.health` | `"startup"` | Static health from build/startup. **Not refreshed.** |
| `adapters.{id}.provenance` | Always `"startup"` | Explicit marker that this data is startup-derived. |
| `lifecycle.adapters.{id}` | `"process_local"` | Current `AdapterState` at snapshot time. |

**Key distinction:** `adapters.{id}.health` and `lifecycle.adapters.{id}` can diverge after startup. An adapter may have `health: "healthy"` (startup) but `lifecycle.adapters.{id}: "failed"` (current). Always check `lifecycle.adapters` for current state.

**Where to look for what:**

| Question | Snapshot Path | Provenance |
|----------|--------------|------------|
| Did startup succeed? | `startup.boot_summary.startup_outcome` | startup |
| What's the overall health? | `startup.startup_health.runtime_health` | startup (not live!) |
| Which adapters are running? | `lifecycle.adapters.{id}` | process-local |
| Is adapter health current? | `adapters.{id}.provenance` → `"startup"` | startup (stale!) |
| Which routes are active? | `routes.eligibility.registered` | build |
| Which routes are degraded? | `routes.startup_readiness.degraded` | startup |
| What happened after startup? | `diagnostics.runtime_events.events[]` | process-local |


## 2. What to Monitor

### 2.1 Persisted State Health

| Signal | How to Check | What It Means |
|--------|-------------|---------------|
| Database size | `ls -lh {state}/medre.sqlite` | Growing database is normal (event accumulation). Sudden stops indicate write failures. |
| Database integrity | `sqlite3 {state}/medre.sqlite "PRAGMA integrity_check;"` | Returns `ok` if healthy. Any other result requires intervention. |
| Disk space | `df -h` on the volume holding `{state}` | Critical. Full disk stops all persistence. |
| Event/receipt counts | `sqlite3 {state}/medre.sqlite "SELECT COUNT(*) FROM canonical_events;"` | Events should exceed receipts (not all events are delivered). Receipts should grow over time. |

### 2.2 Process-Local State (Ephemeral)

| Signal | How to Check | What It Means |
|--------|-------------|---------------|
| Adapter health | `medre diagnostics` | `healthy`, `degraded`, `failed`, or `stopped` per adapter. |
| Capacity pressure | `medre diagnostics` → `capacity_rejections` | Growing rejections indicate delivery concurrency is insufficient. |
| Replay pressure | `medre diagnostics` → `capacity_rejections` | Growing rejections indicate replay concurrency is insufficient. |
| Log errors | `grep ERROR {state}/logs/medre.log` | Error frequency and patterns indicate systemic issues. |

All process-local state resets to zero on every restart. There is no persistent metrics store. If you need historical trends, implement external log aggregation.


## 3. Persistence-Aware Troubleshooting

### 3.1 "Did My Message Get Delivered?"

Check delivery receipts in SQLite:

```sql
-- Find receipts for events from a specific source
SELECT r.event_id, r.status, r.route_id, r.dest_adapter, r.attempt_number, r.source, r.replay_run_id, r.created_at
FROM delivery_receipts r
WHERE r.source_adapter = 'bot1'
ORDER BY r.created_at DESC
LIMIT 20;
```

Interpretation:

- `status = 'sent'` or `confirmed` → delivery attempt completed successfully.
- `status = 'failed'` → delivery attempt failed. Check `error` field.
- **No receipt exists** → the event was stored but delivery was never attempted or was interrupted by a crash.

### 3.2 "What Happened After a Crash?"

1. **Check logs** for the last entries before the crash:
   ```bash
   tail -100 {state}/logs/medre.log
   ```

2. **Verify database integrity:**
   ```bash
   sqlite3 {state}/medre.sqlite "PRAGMA integrity_check;"
   ```

3. **Find orphaned events** (stored but never delivered):
   ```sql
   SELECT e.event_id, e.source_adapter, e.created_at
   FROM canonical_events e
   LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
   WHERE r.event_id IS NULL
   ORDER BY e.created_at DESC
   LIMIT 50;
   ```

4. **Decide on replay:** If orphaned events need delivery, use replay with `DRY_RUN` first, then `BEST_EFFORT`. Expect possible duplicates.

### 3.3 "Is the Runtime Healthy Right Now?"

Check the runtime state layer first:

1. **RuntimeState** (`snapshot.lifecycle.runtime_state`):
   - `running` — the process is up. This does **not** mean all adapters are healthy.
   - `stopped` / `failed` — the process is not running.

2. **AdapterState** (`snapshot.lifecycle.adapters.{adapter_id}`):
   - `ready` — adapter is fully operational.
   - `degraded` — adapter is partially functional.
   - `backpressured` / `disconnected` — adapter is in a transitional degraded state.
   - `failed` — adapter has an unrecoverable failure.
   - `stopped` — adapter was shut down.

3. **RuntimeHealth** (`snapshot.startup.startup_health.runtime_health`):
   - `healthy` — all adapters were `ready` at startup.
   - `degraded` — some adapters were not `ready` at startup.
   - `failed` — no adapters were operational at startup.
   - **Important:** This reflects startup-time classification only. It is **not** live.

```bash
medre diagnostics
```

> **Health freshness note:** Adapter health values in the diagnostic snapshot are initialized at startup and are not automatically refreshed by post-start health polling. If an adapter fails after startup, the diagnostic snapshot may not reflect the failure until the runtime is restarted or the health state is explicitly refreshed. Monitor logs (`grep ERROR`) for real-time failure detection.

Then check capacity gauges:

- `delivery_current` near `delivery_limit` → sustained delivery pressure.
- `capacity_rejections` growing → delivery concurrency is insufficient.
- `replay_current` near `replay_limit` → sustained replay pressure.

### 3.4 "Runtime Is Running but Not Delivering"

1. Check adapter health — are adapters `healthy` and `connected`?
2. Check capacity counters — is `delivery_current` at `delivery_limit`?
3. Check for route configuration issues — do routes match the expected source/dest adapters?
4. Check logs for adapter errors — `grep "ERROR.*adapter_id" {state}/logs/medre.log`
5. Verify disk space — full disk prevents receipt writes.


## 4. Crash Recovery Procedures

### 4.1 Hard Crash Recovery (kill -9, OOM, Power Loss)

**What was lost:**
- All in-flight deliveries (no receipts, no retry).
- Active replay runs (no resume, must re-initiate).
- Runtime counters (all reset to zero).
- Adapter connection states (adapters reconnect from scratch).

**What survived:**
- All events stored before the crash (in SQLite).
- All receipts written before the crash (in SQLite).
- Crypto stores, identity files, and logs.

**Recovery steps:**

1. Verify the environment (disk space, transport connectivity):
   ```bash
   df -h
   ls -la /dev/ttyACM0  # for serial Meshtastic
   ```

2. Verify database integrity:
   ```bash
   sqlite3 {state}/medre.sqlite "PRAGMA integrity_check;"
   ```

3. Restart the runtime:
   ```bash
   medre run --config config.toml
   ```

4. Verify startup — all adapters should report `adapter_started`:
   ```bash
   grep "adapter_started" {state}/logs/medre.log | tail -5
   ```

5. Check for orphaned events if delivery continuity is critical:
   ```sql
   SELECT COUNT(*) FROM canonical_events e
   LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
   WHERE r.event_id IS NULL;
   ```

6. Replay orphaned events if needed (see §3.2).

### 4.2 Clean Shutdown Recovery

Clean shutdown drains in-flight deliveries and flushes persisted state. Recovery is simpler:

1. Restart the runtime:
   ```bash
   medre run --config config.toml
   ```

2. Verify startup. Counters begin at zero (normal — counters are not persisted).

3. No further action needed. Drained deliveries produced receipts. Cancelled deliveries are lost but were logged during shutdown.

### 4.3 Database Corruption Recovery

If `PRAGMA integrity_check` fails:

1. **Stop the runtime** if still running.
2. **Back up the corrupted database:**
   ```bash
   cp {state}/medre.sqlite {state}/medre.sqlite.corrupted
   ```
3. **Attempt recovery:**
   ```bash
   sqlite3 {state}/medre.sqlite ".recover" | sqlite3 {state}/medre-recovered.sqlite
   ```
4. **Replace the database:**
   ```bash
   mv {state}/medre.sqlite {state}/medre.sqlite.corrupted
   mv {state}/medre-recovered.sqlite {state}/medre.sqlite
   ```
5. **Restart the runtime.**
6. If recovery fails, delete the database and accept data loss:
   ```bash
   rm {state}/medre.sqlite
   medre run --config config.toml
   ```
   The runtime creates a fresh database. **All event history is lost.** Crypto stores and identity files are unaffected (separate directories).


## 5. Startup Verification Checklist

After any restart (planned or crash recovery), verify:

- [ ] Runtime process is running: `ps aux | grep medre`
- [ ] All adapters started: `grep "Assembly complete" {state}/logs/medre.log | tail -1`
- [ ] Database is accessible: `sqlite3 {state}/medre.sqlite "SELECT COUNT(*) FROM canonical_events;"`
- [ ] Adapters are connected: `medre diagnostics` shows `healthy` for all adapters
- [ ] No unexpected errors: `grep ERROR {state}/logs/medre.log | tail -10`
- [ ] Disk space is adequate: `df -h` on the state volume


## 6. Degraded Runtime Scenarios

### 6.1 One Adapter Failed, Others Running

MEDRE continues running with a partial adapter set. The failed adapter's events stop, but other adapters continue processing and bridging.

**Operator action:**

1. Check the failed adapter's error in logs.
2. If the failure is transport-related (connectivity, credentials), fix the underlying issue and restart the runtime.
3. There is no per-adapter restart — the entire runtime must restart.

### 6.2 Database Full

If the SQLite database reaches filesystem limits:

- New event writes fail.
- Existing events and receipts are readable.
- In-flight deliveries may still complete (receipts for stored events).
- The runtime continues but is functionally impaired.

**Operator action:**

1. Free disk space or move the database to a larger volume.
2. Restart the runtime after resolving the space issue.
3. Consider periodic event archival or pruning if event accumulation exceeds available storage.

### 6.3 High Delivery Pressure

When `capacity_rejections` is growing steadily:

1. Increase `max_inflight_deliveries` in `[runtime.limits]` if memory allows.
2. Reduce the number of active routes or source event rate.
3. For Meshtastic specifically, check if the radio channel throughput is the bottleneck.
4. Monitor after changes — counter resets on restart, so observe trends from the new baseline.


## 7. Replay and Crash Interaction

### 7.1 Replay Before a Crash

If a `BEST_EFFORT` replay was running when the runtime crashed:

- Deliveries that completed before the crash produced receipts — those are persisted.
- The replay run itself is lost — it does not resume on restart.
- Re-running the replay is safe but may produce duplicate deliveries (no replay deduplication).
- Replay-produced receipts are distinguishable from live receipts by `source='replay'` and carry `replay_run_id` for tracing. Use `WHERE source = 'replay'` to filter replay receipts.

### 7.2 Using Replay for Crash Recovery

Replay can re-deliver orphaned events after a crash:

1. Identify the time range of orphaned events:
   ```sql
   SELECT MIN(e.created_at), MAX(e.created_at) FROM canonical_events e
   LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
   WHERE r.event_id IS NULL;
   ```

2. Run `DRY_RUN` replay for that time range to verify route matching.
3. Run `BEST_EFFORT` replay if re-delivery is warranted.
4. Expect duplicate deliveries for any events that were delivered before the crash but whose receipts you did not query (receipts exist for those — check first).

### 7.3 Replay Is Not a Durable Job

Replay is a one-shot operation initiated by the operator or test harness. It is not a persistent queue, not a durable job, and not automatically retried. See Contract 55 §6.


## 8. Persistence Expectations for Operators

| Question | Answer |
|----------|--------|
| Do I lose event history on crash? | **No.** Events are in SQLite. |
| Do I lose delivery receipts on crash? | **No.** Receipts are in SQLite. |
| Do I lose E2EE sessions on crash? | **No.** Crypto store is on disk. |
| Do I lose runtime metrics on crash? | **Yes.** All counters reset on restart. |
| Do I lose in-flight deliveries on crash? | **Yes.** No retry, no recovery. |
| Do I need to manually replay after crash? | Only if orphaned events need delivery. Not automatic. |
| Does MEDRE back up its own database? | **No.** Operators handle backup. |
| Can I query historical delivery state? | **Yes.** Query receipts from SQLite. |
| Can I tell which receipts came from replay? | **Yes.** Query `WHERE source = 'replay'` on `delivery_receipts`. Use `replay_run_id` to group by replay run. |
| Can I see historical capacity metrics? | **No.** Counters are process-local only. Implement external monitoring. |


## 9. References

- Contract 55 — Runtime Persistence Contract (authoritative persistence semantics)
- Contract 54 — Runtime Shutdown Contract (shutdown ordering and drain)
- Contract 53 — Runtime Resource Control Contract (capacity limits and backpressure)
- Contract 51 — Route Attribution Contract (route attribution persistence)
- Contract 48 — Runtime Observability Contract (logging and diagnostics)
- Contract 46 — Runtime Storage and Path Model (filesystem layout)
- [Runtime Operation](runtime-operation.md) — general runtime operation
- [Bridge Operation](bridge-operation.md) — bridge delivery semantics
- [Configuration](configuration.md) — TOML configuration reference
- [Event Tracing](event-tracing.md) — operator guide for tracing events through
  pipeline lifecycle, timeline reports, SQL queries
- [Replay Operation](replay-operation.md) — replay modes, command shape,
  receipt interpretation, duplicate risk assessment
- [Bridge Recovery](bridge-recovery.md) — crash recovery procedures, orphan
  detection, recovery decision tree

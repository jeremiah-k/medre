# Bridge Recovery Runbook

> Last updated: 2026-05-14
> Scope: Bridge-specific recovery procedures — crash recovery, adapter failure, orphan detection
> Status: Pre-beta. Not production. All recovery is operator-initiated. No automatic remediation.
> Prerequisites: medre installed, runtime previously run with `[storage] backend = "sqlite"`.

This runbook provides step-by-step recovery procedures for bridge operators.
It covers crash recovery, adapter failure recovery, orphaned event detection,
and the decision tree for choosing the right recovery action.

**What recovery can do:**

- Identify events that were stored but never delivered (orphaned).
- Replay orphaned events through current routes.
- Verify database integrity after a crash.
- Assess adapter health and connectivity after restart.

**What recovery does NOT do:**

- Recover in-flight deliveries lost during crash (they are gone).
- Resume interrupted replay runs (they must be re-initiated).
- Automatically restart failed adapters (only full runtime restart).
- Deduplicate replay deliveries.
- Provide automatic retry scheduling.


## 0. Complete Incident Workflow (End-to-End)

This section describes a coherent end-to-end workflow that chains smoke,
trace, inspect, recover, and evidence into a single incident response
procedure. Use this when you suspect events were lost or not delivered
correctly.

**Caveat:** Traceability is not deduplication. This workflow shows you what
happened and lets you re-deliver, but it cannot tell you whether a duplicate
reached the remote side. BEST_EFFORT sends real messages. There is no final
ACK guarantee for radio transports. There is no active retry scheduler — every
step below is operator-initiated. Runtime events and counters are process-local
and reset on restart.

### Step 1: Verify Pipeline Health

```bash
# Smoke-test the pipeline with persistent storage so evidence survives exit.
# This uses fake adapters — it proves routing and receipt persistence, not
# real transport connectivity.
PYTHONPATH=src medre smoke --storage-path /tmp/medre-incident.db --json
```

Exit code 0 = pipeline healthy. If this fails, fix config or environment
before proceeding.

### Step 2: Trace the Suspect Event

```bash
# If you know the event_id from logs or a previous run:
medre trace event <event_id> --config my-bridge.toml

# With JSON for programmatic inspection:
medre trace event <event_id> --config my-bridge.toml --json
```

The trace output shows the full lifecycle: ingestion, routing, delivery
attempts, retry chains, and replay attribution. If no receipts exist, the
event is orphaned — proceed to Step 4.

### Step 3: Inspect Delivery Receipts

```bash
# All receipts for the event, including retry lineage and replay attribution
medre inspect receipts --event <event_id> --config my-bridge.toml

# Check native message refs to map transport-native IDs
medre inspect native-ref --adapter <name> --message <native_id> \
  --config my-bridge.toml
```

Look for:
- `source` field: `"live"` means original delivery, `"replay"` means
  re-delivered via replay engine.
- `replay_run_id`: groups receipts from the same replay run.
- `failure_kind`: tells you why delivery failed (if it did).
- `attempt_number` and `parent_receipt_id`: traces the retry chain.

### Step 4: Recover Orphaned or Failed Events

```bash
# Targeted recovery of a single event (DRY_RUN first):
medre recover --event <event_id> --dry-run --config my-bridge.toml

# If preview looks correct, execute:
medre recover --event <event_id> --config my-bridge.toml

# Or replay all orphaned events:
medre replay --mode DRY_RUN --config my-bridge.toml
medre replay --mode BEST_EFFORT --config my-bridge.toml
```

**Warning:** BEST_EFFORT sends real messages. Events that already have `sent`
receipts will be delivered again. Traceability is not deduplication — each
replay produces new outbound messages. There is no retry scheduler; replay
is a one-shot operator action.

### Step 5: Collect Evidence Bundle

```bash
# Full evidence for the incident, including the event and its receipts
medre evidence --event <event_id> --config my-bridge.toml --json \
  > incident-evidence.json

# If live health is also needed (starts real adapters):
medre evidence --event <event_id> --include-refresh-health \
  --config my-bridge.toml --json > incident-evidence-full.json
```

The evidence bundle includes config summary, route validation, diagnostics
snapshot, storage data (event, receipts, native refs), and optional live
health. Attach this to incident reports or bug filings.

### Workflow Summary

```
medre smoke --storage-path <db>
  → verifies pipeline, persists evidence
  ↓
medre trace event <id>
  → shows full event lifecycle
  ↓
medre inspect receipts --event <id>
  → delivery details, retry chains, replay attribution
  ↓
medre recover --event <id>   (dry-run first)
  → re-delivers orphaned event (BEST_EFFORT sends real messages)
  ↓
medre evidence --event <id> --json
  → collects full evidence bundle
```

See [Event Tracing](event-tracing.md) for trace command details,
[Replay Operation](replay-operation.md) for replay modes, and
[Bridge Evidence Bundle](bridge-evidence-bundle.md) for the full evidence
report shape.


## 1. Recovery Decision Tree

```
What happened?
│
├── Runtime crashed (kill -9, OOM, power loss)
│   ├── Is the database intact?
│   │   ├── Yes → Section 2: Crash Recovery
│   │   └── No → Section 5: Database Corruption Recovery
│   └── Unknown → Verify with: sqlite3 {state}/medre.sqlite "PRAGMA integrity_check;"
│
├── Adapter failed but runtime is still running
│   └── Section 3: Adapter Failure Recovery
│
├── Events were not delivered (suspected orphans)
│   └── Section 4: Orphan Detection and Replay
│
└── Runtime is running but not delivering
    ├── Check adapter health: medre diagnostics
    ├── Check capacity counters: delivery_current vs delivery_limit
    ├── Check route config: medre routes validate
    └── See [Runtime Supervision](runtime-supervision.md#34-runtime-is-running-but-not-delivering)
```


## 2. Crash Recovery

### 2.1 What Was Lost

On hard crash (kill -9, OOM, power loss):

| State | Survived? | Notes |
|-------|-----------|-------|
| Canonical events | **Yes** | Written to SQLite before delivery. Storage remains durable across crashes. |
| Delivery receipts | **Yes** | Written after each delivery attempt. SQLite persists. |
| Native message refs | **Yes** | Persisted in SQLite alongside receipts. |
| Receipt traceability (`source`, `replay_run_id`) | **Yes** | Stored on receipts in SQLite. Survives crash. |
| Matrix E2EE crypto keys | **Yes** | On disk under adapter state root |
| LXMF identity files | **Yes** | On disk under adapter state root |
| Logs (pre-crash) | **Yes** | Appended to `{log_dir}/medre.log` |
| In-flight deliveries | **No** | Lost — no receipt, no recovery |
| Active replay runs | **No** | Lost — must re-initiate manually |
| Runtime counters (accounting) | **No** | Process-local accounting resets after restart. All `RuntimeAccounting`, `CapacityController`, `RouteStats`, and `Diagnostician` counters reset to zero. |
| Adapter connection state | **No** | Adapters reconnect from scratch |

### 2.2 Crash Recovery Steps

```bash
# Step 1: Verify the environment
df -h                    # Check disk space
ls -la /dev/ttyACM0      # For serial Meshtastic adapters

# Step 2: Verify database integrity
sqlite3 {state}/medre.sqlite "PRAGMA integrity_check;"
# Expected output: "ok"

# Step 3: Restart the runtime
medre run --config config.toml

# Step 4: Verify startup — all adapters should report started
grep "Assembly complete" {state}/logs/medre.log | tail -1

# Step 5: Check for adapter errors
grep "adapter_failed" {state}/logs/medre.log | tail -5

# Step 6: Verify live health
medre diagnostics --refresh-health --config config.toml
```

### 2.3 Assess Orphaned Events After Crash

```bash
# Count orphaned events (stored but never delivered)
sqlite3 {state}/medre.sqlite "
  SELECT COUNT(*) FROM canonical_events e
  LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
  WHERE r.event_id IS NULL;
"

# List recent orphans with details
sqlite3 {state}/medre.sqlite "
  SELECT e.event_id, e.source_adapter, e.event_kind, e.created_at
  FROM canonical_events e
  LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
  WHERE r.event_id IS NULL
  ORDER BY e.created_at DESC
  LIMIT 20;
"
```

If orphans exist and delivery continuity is critical, proceed to Section 4.

### 2.4 Clean Shutdown Recovery

If the runtime was shut down cleanly (SIGTERM, SIGINT):

```bash
# Step 1: Restart
medre run --config config.toml

# Step 2: Verify startup
grep "Assembly complete" {state}/logs/medre.log | tail -1

# No further action needed. Drained deliveries produced receipts.
# Cancelled deliveries are lost but were logged during shutdown.
```


## 3. Adapter Failure Recovery

### 3.1 Symptoms

- Runtime is `RUNNING` but one or more adapters report `failed` or `degraded`
  health.
- Events from the failed adapter's transport are no longer being processed.
- Delivery receipts show `ADAPTER_PERMANENT` or repeated `ADAPTER_TRANSIENT`
  failures for the affected adapter.

### 3.2 Diagnosis

```bash
# Check adapter health
medre diagnostics --refresh-health --config config.toml

# Check logs for the specific adapter
grep "ERROR.*adapter_id=<adapter_id>" {state}/logs/medre.log | tail -20

# Check delivery receipts for failures
sqlite3 {state}/medre.sqlite "
  SELECT event_id, status, failure_kind, attempt_number, created_at
  FROM delivery_receipts
  WHERE target_adapter = '<adapter_id>'
    AND status = 'failed'
  ORDER BY created_at DESC
  LIMIT 20;
"
```

### 3.3 Recovery Steps

```bash
# Step 1: Identify the failure cause from diagnostics and logs
# Common causes:
#   - Network connectivity (Matrix homeserver unreachable)
#   - Serial device disconnected (Meshtastic USB unplugged)
#   - Authentication failure (expired Matrix access token)
#   - SDK dependency missing (adapter_kind="real" without SDK installed)

# Step 2: Fix the underlying cause
# Examples:
#   - Reconnect serial device
#   - Renew Matrix access token
#   - Install missing SDK: pip install -e ".[matrix]"

# Step 3: Restart the runtime (no per-adapter restart exists)
medre run --config config.toml

# Step 4: Verify the adapter started successfully
grep "adapter_started.*adapter_id=<adapter_id>" {state}/logs/medre.log | tail -1

# Step 5: Check for events that were missed during the outage
# See Section 4 for orphan detection
```

### 3.4 Adapter Failure Recovery Matrix

| Failure cause | Fix | Replay needed? |
|--------------|-----|----------------|
| Network outage | Restore network, restart runtime | Yes — events may have been received by other adapters but not delivered to the affected adapter |
| Serial device disconnected | Reconnect device, restart runtime | Yes — inbound events from the disconnected adapter were lost |
| Authentication failure | Renew credentials, restart runtime | No — events from other adapters were still processed |
| SDK dependency missing | Install SDK, restart runtime | No — events were never received |
| Adapter bug (crash loop) | Fix code or disable adapter, restart runtime | Partially — events from other adapters to this adapter's targets were lost |

**Key point:** There is no per-adapter restart. Only full runtime stop/start is
supported. All adapters restart together.


## 4. Orphan Detection and Replay

### 4.1 Orphan Detection SQL

Events are orphaned when they were stored in `canonical_events` but have no
corresponding entry in `delivery_receipts`. This happens when:

- The runtime crashed mid-delivery.
- Delivery was cancelled during shutdown.
- Route matching found no matching routes (by design — not an error).
- Loop prevention skipped delivery (by design — no receipt is written).

```sql
-- All orphaned events
SELECT
  e.event_id,
  e.source_adapter,
  e.event_kind,
  e.created_at
FROM canonical_events e
LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
WHERE r.event_id IS NULL
ORDER BY e.created_at DESC;

-- Orphaned events from a specific time window (e.g., around a known crash)
SELECT e.event_id, e.source_adapter, e.created_at
FROM canonical_events e
LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
WHERE r.event_id IS NULL
  AND e.created_at BETWEEN '2026-05-14T10:00:00Z' AND '2026-05-14T11:00:00Z'
ORDER BY e.created_at ASC;

-- Count of orphans by source adapter
SELECT e.source_adapter, COUNT(*) AS orphan_count
FROM canonical_events e
LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
WHERE r.event_id IS NULL
GROUP BY e.source_adapter;
```

### 4.2 Orphan vs. Expected-Undelivered

Not all events without receipts are truly orphaned:

| Scenario | Has receipt? | Action |
|----------|-------------|--------|
| Event stored, delivery in progress when crash occurred | No | Replay candidate |
| Event stored, no routes matched | No | Not an orphan — no routes were configured for this event's source |
| Event stored, loop prevented delivery | No | Not an orphan — loop prevention worked correctly |
| Event stored, capacity exceeded | No (permanent failure) | Replay candidate (after increasing capacity limits) |
| Event stored, delivery sent before crash | Yes (receipt written) | Not an orphan — check receipt status |

To distinguish genuine orphans from expected undelivered events:

```sql
-- Events with no matching routes (not orphans — by design)
SELECT e.event_id, e.source_adapter
FROM canonical_events e
LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
WHERE r.event_id IS NULL
  AND e.source_adapter NOT IN (
    SELECT DISTINCT source_adapter FROM delivery_receipts
  );
```

### 4.3 Replay Workflow for Orphans

After a crash, operators can replay orphan events to re-deliver them. Replay
is **manual** — there is no automatic retry scheduler, no background replay
daemon, and no resume mechanism. Each replay run is a one-shot operator action.

**Storage remains durable:** events, receipts, and native refs in SQLite
survive crashes. Only process-local accounting counters reset. This means
orphaned events are still in the database after restart, ready for manual
replay.

**Recommended workflow:** always run `DRY_RUN` first to preview what replay
would do, then `BEST_EFFORT` to execute. Replay is not dedupe — BEST_EFFORT
produces fresh receipts and sends real messages.

```bash
# Step 1: Count and review orphans (see SQL above)

# Step 2: Preview replay with DRY_RUN
medre replay --mode DRY_RUN --config my-bridge.toml

# Step 3: Review route attributions in DRY_RUN output
# - Which routes match?
# - Which target adapters will receive delivery?
# - How does this compare to what was expected?

# Step 4: Assess duplicate risk
sqlite3 {state}/medre.sqlite "
  SELECT e.event_id,
    (SELECT COUNT(*) FROM delivery_receipts r WHERE r.event_id = e.event_id AND r.source = 'live') AS live_count
  FROM canonical_events e
  LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
  WHERE r.event_id IS NULL
  ORDER BY e.created_at DESC;
";
# Events with live_count > 0 are not truly orphaned — they have receipts.
# This query catches the edge case where LEFT JOIN produces NULL from a
# different join condition.

# Step 5: Execute replay
medre replay --mode BEST_EFFORT --config my-bridge.toml

# Step 6: Verify replay results
medre trace replay <replay_run_id> --config my-bridge.toml

# Step 7: Check for remaining orphans
sqlite3 {state}/medre.sqlite "
  SELECT COUNT(*) FROM canonical_events e
  LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
  WHERE r.event_id IS NULL;
"
```

See [Replay Operation](replay-operation.md) for detailed replay mode
documentation and [Event Tracing](event-tracing.md) for tracing commands.


## 5. Database Corruption Recovery

If `PRAGMA integrity_check` returns anything other than `ok`:

```bash
# Step 1: Stop the runtime if still running
kill <pid>

# Step 2: Back up the corrupted database
cp {state}/medre.sqlite {state}/medre.sqlite.corrupted

# Step 3: Attempt recovery
sqlite3 {state}/medre.sqlite ".recover" | sqlite3 {state}/medre-recovered.sqlite

# Step 4: Verify the recovered database
sqlite3 {state}/medre-recovered.sqlite "PRAGMA integrity_check;"

# Step 5: If recovery succeeded, replace the database
mv {state}/medre.sqlite {state}/medre.sqlite.corrupted
mv {state}/medre-recovered.sqlite {state}/medre.sqlite

# Step 6: Restart the runtime
medre run --config config.toml

# Step 7: If recovery failed, accept data loss
rm {state}/medre.sqlite
medre run --config config.toml
# All event history is lost. Crypto stores and identity files are unaffected.
```

See [Runtime Supervision > Database Corruption Recovery](runtime-supervision.md#43-database-corruption-recovery)
for the authoritative procedure.


## 6. Recovery Commands Quick Reference

| Scenario | Command | Purpose |
|----------|---------|---------|
| Verify database integrity | `sqlite3 {state}/medre.sqlite "PRAGMA integrity_check;"` | Confirm SQLite is healthy |
| Restart runtime | `medre run --config config.toml` | Resume normal operation |
| Check adapter health | `medre diagnostics --refresh-health --config config.toml` | Live health snapshot |
| Count orphaned events | SQL: `SELECT COUNT(*) FROM canonical_events e LEFT JOIN delivery_receipts r ON e.event_id = r.event_id WHERE r.event_id IS NULL;` | Assess recovery scope |
| Preview replay | `medre replay --mode DRY_RUN --config my-bridge.toml` | See what replay would do |
| Execute replay | `medre replay --mode BEST_EFFORT --config my-bridge.toml` | Re-deliver orphaned events |
| Trace replay results | `medre trace replay <run_id> --config my-bridge.toml` | Inspect replay outcome |
| Trace a specific event | `medre trace event <event_id> --config my-bridge.toml` | Full event lifecycle |
| Recover a single event | `medre recover --event <event_id> --config my-bridge.toml` | Targeted recovery |
| Dry-run single event recovery | `medre recover --event <event_id> --dry-run --config my-bridge.toml` | Preview without side effects |
| Check recent errors | `grep ERROR {state}/logs/medre.log \| tail -20` | Scan for failure patterns |
| Verify startup | `grep "Assembly complete" {state}/logs/medre.log \| tail -1` | Confirm all adapters started |


## 7. Caveats

1. **No automatic retry.** Recovery is entirely operator-initiated. MEDRE does
   not automatically replay orphaned events, restart failed adapters, or retry
   failed deliveries after restart.

2. **No per-adapter restart.** Only full runtime stop/start is supported. When
   one adapter fails, all adapters must restart together.

3. **No deduplication.** Replay produces new outbound messages each time.
   Multiple BEST_EFFORT replays of the same events produce duplicates. This is
   by design.

4. **No active supervision.** There is no background health monitor, watchdog,
   or orchestrator. Operators must detect failures externally (logs, process
   supervisors, cron health checks).

 5. **Counters reset on restart.** All runtime counters (capacity_rejections,
   outbound_failed, RouteStats) reset to zero on every startup. There is no
   persistent metrics store.

6. **Single-machine only.** Recovery operates on the local SQLite database.
   There is no distributed coordination, shared state, or cross-instance
   recovery.

7. **No final ACK.** Radio transports (Meshtastic, MeshCore) are
   fire-and-forget. A `sent` receipt means the local radio accepted the packet,
   not that any remote node received it. Recovery cannot confirm radio delivery.

 8. **Replay is not a durable job.** Replay runs do not resume after crash.
    Completed deliveries from a crashed replay run are preserved (receipts in
    SQLite). Remaining events must be re-replayed manually. Always run
    DRY_RUN first to preview scope before BEST_EFFORT. Process-local
    accounting resets after restart; only SQLite data survives.

9. **Pre-beta.** Recovery commands, SQL queries, and decision tree may change
   before beta. Always verify against the current code.


## 8. Cross-References

- [Event Tracing](event-tracing.md) — tracing events and replay runs through
  the pipeline lifecycle, timeline reports, SQL queries.
- [Replay Operation](replay-operation.md) — replay modes, command shape,
  receipt interpretation, duplicate risk assessment.
- [Bridge Operation](bridge-operation.md) — delivery-state discipline,
  persistence of bridge state, per-transport semantics.
- [Bridge Failure Drills](bridge-failure-drills.md) — per-failure drill
  interpretation and inspect follow-up.
- [Runtime Operation](runtime-operation.md) — crash recovery procedures,
  persistence semantics, exit codes.
- [Runtime Supervision](runtime-supervision.md) — crash recovery procedures,
  persistence expectations, troubleshooting workflows.
- [Bridge Evidence Bundle](bridge-evidence-bundle.md) — collecting evidence as
  a pre-runtime package.

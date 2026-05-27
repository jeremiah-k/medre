# Recovery and Replay

How to recover from crashes, detect orphaned events, and re-process historical data through the replay engine.

All recovery is operator-initiated. There is no automatic remediation, no per-adapter restart, and no auto-healing. The RetryWorker handles transient failures automatically when enabled, but replay for orphaned events is always manual.

## Complete Incident Workflow

```bash
# 1. Verify pipeline health with persistent storage
medre smoke --storage-path /tmp/medre-incident.db --json

# 2. Inspect the suspect event
medre inspect event <event_id> --storage-path /tmp/medre-incident.db

# 3. Check delivery receipts
medre inspect receipts --event <event_id> --storage-path /tmp/medre-incident.db

# 4. Full investigation via inspect flags
medre inspect event <event_id> --timeline --evidence --recovery --storage-path /tmp/medre-incident.db

# 5. Preview replay (no side effects)
medre replay --mode DRY_RUN --config my-bridge.toml

# 6. Re-deliver orphaned events (sends real messages)
medre replay --mode BEST_EFFORT --config my-bridge.toml
```

## Recovery Decision Tree

```text
What happened?
│
├── Runtime crashed (kill -9, OOM, power loss)
│   ├── Is the database intact?
│   │   ├── Yes → Crash Recovery (below)
│   │   └── No → Database Corruption Recovery (below)
│   └── Unknown → Verify: sqlite3 {state}/medre.sqlite "PRAGMA integrity_check;"
│
├── Adapter failed but runtime is still running
│   └── Adapter Failure Recovery (below)
│
├── Events were not delivered (suspected orphans)
│   └── Orphan Detection and Replay (below)
│
└── Runtime is running but not delivering
    ├── Check adapter health: medre diagnostics --refresh-health
    ├── Check capacity: delivery_current vs delivery_limit
    └── Check route config: medre routes validate
```

## Crash Recovery

### What Was Lost

On hard crash (kill -9, OOM, power loss):

| State | Survived? | Notes |
|-------|-----------|-------|
| Canonical events | Yes | Written to SQLite before delivery |
| Delivery receipts | Yes | Written after each delivery attempt |
| Native message refs | Yes | Persisted in SQLite alongside receipts |
| Receipt traceability (`source`, `replay_run_id`) | Yes | Stored on receipts in SQLite |
| Matrix E2EE crypto keys | Yes | On disk under adapter state root |
| LXMF identity files | Yes | On disk under adapter state root |
| Logs (pre-crash) | Yes | Appended to `{log_dir}/medre.log` |
| In-flight deliveries | Partial | No receipt, but an `in_progress` outbox row may survive. Expired leases are reclaimable by RetryWorker. Deliveries without outbox rows are fully lost. |
| Active replay runs | No | Lost — must re-initiate manually |
| Runtime counters (accounting) | No | Process-local counters reset after restart |
| Adapter connection state | No | Adapters reconnect from scratch |

### Crash Recovery Steps

```bash
# 1. Verify environment
df -h                    # Check disk space
ls -la /dev/ttyACM0      # For serial Meshtastic adapters

# 2. Verify database integrity
sqlite3 {state}/medre.sqlite "PRAGMA integrity_check;"
# Expected output: "ok"

# 3. Restart the runtime
medre run --config config.toml

# 4. Verify startup — all adapters should report started
grep "Assembly complete" {state}/logs/medre.log | tail -1

# 5. Check for adapter errors
grep "adapter_failed" {state}/logs/medre.log | tail -5

# 6. Verify live health
medre diagnostics --refresh-health --config config.toml
```

### Assess Orphaned Events After Crash

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

### Clean Shutdown Recovery

If the runtime was shut down cleanly (SIGTERM, SIGINT):

```bash
# Restart
medre run --config config.toml

# Verify startup
grep "Assembly complete" {state}/logs/medre.log | tail -1

# No further action needed. Drained deliveries produced receipts.
# Cancelled deliveries are lost but were logged during shutdown.
```

## Adapter Failure Recovery

### Symptoms

- Runtime is `RUNNING` but one or more adapters report `failed` or `degraded` health.
- Events from the failed adapter's transport are no longer being processed.
- Delivery receipts show `ADAPTER_PERMANENT` or repeated `ADAPTER_TRANSIENT` failures for the affected adapter.

### Diagnosis

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

# Check for pending retries that the RetryWorker will handle
sqlite3 {state}/medre.sqlite "
  SELECT receipt_id, event_id, attempt_number, next_retry_at
  FROM delivery_receipts
  WHERE target_adapter = '<adapter_id>'
    AND status = 'failed'
    AND failure_kind = 'adapter_transient'
    AND next_retry_at IS NOT NULL
  ORDER BY next_retry_at ASC;
"
```

If `failure_kind='adapter_transient'` and `next_retry_at` is set, the RetryWorker will automatically retry when the adapter recovers (after runtime restart). For other failure kinds, manual replay is required.

### Recovery Steps

```bash
# 1. Identify the failure cause from diagnostics and logs
#    Common causes:
#      - Network connectivity (Matrix homeserver unreachable)
#      - Serial device disconnected (Meshtastic USB unplugged)
#      - Authentication failure (expired Matrix access token)
#      - SDK dependency missing (adapter_kind="real" without SDK installed)

# 2. Fix the underlying cause

# 3. Restart the runtime (no per-adapter restart exists)
medre run --config config.toml

# 4. Verify the adapter started successfully
grep "adapter_started.*adapter_id=<adapter_id>" {state}/logs/medre.log | tail -1

# 5. Check for events that were missed during the outage
#    See Orphan Detection below
```

### Adapter Failure Recovery Matrix

| Failure cause | Fix | Replay needed? |
|--------------|-----|---------------|
| Network outage | Restore network, restart runtime | Yes — events may have been received by other adapters but not delivered to the affected adapter |
| Serial device disconnected | Reconnect device, restart runtime | Yes — inbound events from the disconnected adapter were lost |
| Authentication failure | Renew credentials, restart runtime | No — events from other adapters were still processed |
| SDK dependency missing | Install SDK, restart runtime | No — events were never received |
| Adapter bug (crash loop) | Fix code or disable adapter, restart runtime | Partially — events from other adapters to this adapter's targets were lost |

There is no per-adapter restart. Only full runtime stop/start is supported. All adapters restart together.

## Orphan Detection and Replay

### Orphan Detection SQL

Events are orphaned when stored in `canonical_events` but have no corresponding entry in `delivery_receipts`.

```sql
-- All orphaned events
SELECT e.event_id, e.source_adapter, e.event_kind, e.created_at
FROM canonical_events e
LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
WHERE r.event_id IS NULL
ORDER BY e.created_at DESC;

-- Orphaned events from a specific time window
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

### Orphan vs. Expected-Undelivered

Not all events without receipts are truly orphaned:

| Scenario | Has receipt? | Action |
|----------|-------------|--------|
| Event stored, delivery in progress when crash occurred | No | Replay candidate |
| Event stored, no routes matched | No | Not an orphan — no routes were configured for this event's source |
| Event stored, loop prevented delivery | `suppressed` receipt | Not an orphan — loop prevention worked correctly |
| Event stored, capacity exceeded | `suppressed` receipt | Replay candidate (after increasing capacity limits) |
| Event stored, delivery sent before crash | Yes (receipt written) | Not an orphan — check receipt status |

### Replay Workflow for Orphans

```bash
# 1. Count and review orphans (see SQL above)

# 2. Preview replay with DRY_RUN
medre replay --mode DRY_RUN --config my-bridge.toml

# 3. Review route attributions in DRY_RUN output

# 4. Assess duplicate risk
sqlite3 {state}/medre.sqlite "
  SELECT e.event_id,
    (SELECT COUNT(*) FROM delivery_receipts r WHERE r.event_id = e.event_id AND r.source = 'live') AS live_count
  FROM canonical_events e
  LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
  WHERE r.event_id IS NULL
  ORDER BY e.created_at DESC;
"

# 5. Execute replay
medre replay --mode BEST_EFFORT --config my-bridge.toml

# 6. Verify replay results
medre inspect receipts --replay-run <replay_run_id> --storage-path /path/to/medre.sqlite

# 7. Check for remaining orphans
sqlite3 {state}/medre.sqlite "
  SELECT COUNT(*) FROM canonical_events e
  LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
  WHERE r.event_id IS NULL;
"
```

## Database Corruption Recovery

If `PRAGMA integrity_check` returns anything other than `ok`:

```bash
# 1. Stop the runtime if still running
kill <pid>

# 2. Back up the corrupted database
cp {state}/medre.sqlite {state}/medre.sqlite.corrupted

# 3. Attempt recovery
sqlite3 {state}/medre.sqlite ".recover" | sqlite3 {state}/medre-recovered.sqlite

# 4. Verify the recovered database
sqlite3 {state}/medre-recovered.sqlite "PRAGMA integrity_check;"

# 5. If recovery succeeded, replace the database
mv {state}/medre.sqlite {state}/medre.sqlite.corrupted
mv {state}/medre-recovered.sqlite {state}/medre.sqlite

# 6. Restart the runtime
medre run --config config.toml

# 7. If recovery failed, accept data loss
rm {state}/medre.sqlite
medre run --config config.toml
# All event history is lost. Crypto stores and identity files are unaffected.
```

## Replay Modes

| Mode | Routes? | Delivers? | Side effects | Use case |
|------|---------|-----------|-------------|----------|
| `DRY_RUN` | Yes | Skip (no delivery) | None | Preview what replay would do. First step before any BEST_EFFORT. |
| `RE_ROUTE` | Yes | No (read-only) | None | Re-evaluate route matching after a config change. No delivery. |
| `BEST_EFFORT` | Yes | Yes | Real adapter delivery | Re-deliver historical events. Sends real messages. Produces fresh storage receipts with `source='replay'`. |

Always run `DRY_RUN` or `RE_ROUTE` first. Only use `BEST_EFFORT` when you have verified the route matching preview and accept the duplicate delivery risk.

## Replay Command Shape

```bash
medre replay --mode <mode> [--event <event_id>] --config my-bridge.toml
```

| Flag | Required | Description |
|------|----------|-------------|
| `--mode` | Yes | One of: `DRY_RUN`, `RE_ROUTE`, `BEST_EFFORT` |
| `--event` | No | Specific event ID to replay. If omitted, replays all events in storage. |
| `--config` | Yes | Path to TOML config (must use SQLite storage) |

Additional flags:

| Flag | Description |
|------|-------------|
| `--from <timestamp>` | Replay events created after this ISO-8601 timestamp |
| `--to <timestamp>` | Replay events created before this ISO-8601 timestamp |
| `--route <route_id>` | Only replay events that match this route |

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Replay completed (may include partial failures in BEST_EFFORT) |
| 2 | Config error, no SQLite backend, or database not found |

## Replay Result Interpretation

### DRY_RUN Result

```json
{
  "mode": "DRY_RUN",
  "replay_run_id": "replay_preview_001",
  "events_processed": 10,
  "deliveries_attempted": 0,
  "deliveries_skipped": 10,
  "route_attributions": [
    {
      "event_id": "evt_abc123",
      "route_ids": ["bot-to-radio"],
      "target_adapters": ["radio"]
    }
  ]
}
```

Review `route_attributions` to verify which routes match each event and which target adapters would receive delivery before proceeding to BEST_EFFORT.

### RE_ROUTE Result

Compare `route_attributions` against previous delivery receipts to see what changed after a route config update. Events that previously matched one route but now match two will have fan-out delivery if replayed with BEST_EFFORT.

### BEST_EFFORT Result

```json
{
  "mode": "BEST_EFFORT",
  "replay_run_id": "replay_xyz789",
  "events_processed": 10,
  "deliveries_attempted": 8,
  "deliveries_sent": 7,
  "deliveries_failed": 1,
  "deliveries_skipped": 1,
  "errors": [
    {
      "event_id": "evt_def456",
      "error": "replay_capacity_exceeded"
    }
  ]
}
```

After BEST_EFFORT, inspect storage receipts:

```bash
medre inspect receipts --replay-run replay_xyz789 --storage-path /path/to/medre.sqlite
```

## Replay Receipts

BEST_EFFORT replay produces `DeliveryReceipt` records with these distinguishing fields:

```json
{
  "receipt_id": "rcpt_r1",
  "event_id": "evt_abc123",
  "target_adapter": "radio",
  "route_id": "bot-to-radio",
  "status": "sent",
  "source": "replay",
  "replay_run_id": "replay_xyz789",
  "attempt_number": 1,
  "parent_receipt_id": null,
  "created_at": "2026-05-14T11:00:00Z"
}
```

| Field | Value for replay | Purpose |
|-------|-----------------|---------|
| `source` | `"replay"` | Distinguishes replay deliveries from live deliveries |
| `replay_run_id` | Unique run identifier | Groups all receipts from the same replay run |

Key distinctions:

1. Replay receipts are distinguishable from live receipts via `source='replay'` and `replay_run_id`.
2. Replay is not dedupe — each BEST_EFFORT run produces fresh receipts regardless of existing live or replay receipts for the same event.
3. Native refs created during replay are not directly source-tagged. Correlate through the associated `DeliveryReceipt`'s `event_id` linkage.

### Querying Replay Receipts

```bash
# All receipts from a specific replay run
medre inspect receipts --replay-run <run_id> --storage-path /path/to/medre.sqlite
```

```sql
-- All replay receipts
SELECT event_id, target_adapter, status, replay_run_id
FROM delivery_receipts
WHERE source = 'replay'
ORDER BY created_at DESC;

-- Distinguish live from replay for a specific event
SELECT source, replay_run_id, status, target_adapter
FROM delivery_receipts
WHERE event_id = 'evt_abc123'
ORDER BY created_at ASC;

-- Group all receipts from one replay run (audit trail)
SELECT event_id, target_adapter, status, attempt_number
FROM delivery_receipts
WHERE source = 'replay' AND replay_run_id = 'replay_xyz789'
ORDER BY event_id;

-- Show all replay runs that touched this event
SELECT replay_run_id, COUNT(*) AS receipt_count
FROM delivery_receipts
WHERE event_id = 'evt_abc123' AND source = 'replay'
GROUP BY replay_run_id
ORDER BY replay_run_id;
```

## Duplicate Risk Assessment

Replay does not deduplicate. Every BEST_EFFORT replay produces new outbound messages on all matched targets.

### When Duplicates Occur

| Scenario | Risk level | Why |
|----------|-----------|-----|
| Replaying events that were never delivered | Low | No prior delivery exists |
| Replaying events that were delivered before a crash | Medium | Some events may have been delivered but have no receipt |
| Replaying events that have existing `sent` receipts | High | Events will be delivered again |
| Multiple BEST_EFFORT replays of the same events | High | Each run produces new deliveries |

### Assessing Risk Before Replay

```sql
-- How many events have existing live receipts (duplicate risk)?
SELECT COUNT(DISTINCT e.event_id)
FROM canonical_events e
JOIN delivery_receipts r ON e.event_id = r.event_id
WHERE r.source = 'live' AND r.status = 'sent';

-- Events with NO receipts (safe to replay)
SELECT e.event_id, e.source_adapter, e.created_at
FROM canonical_events e
LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
WHERE r.event_id IS NULL
ORDER BY e.created_at DESC;
```

### Mitigation Strategies

1. **Always DRY_RUN first.** Review route attributions before BEST_EFFORT.
2. **Query existing receipts.** Use the SQL above to assess how many events already have `sent` receipts.
3. **Scope replay narrowly.** Use `--event <id>` or `--from`/`--to` to replay only the events that need it.
4. **Accept duplicates for radio transports.** Meshtastic and MeshCore treat duplicate sends as normal operational practice.
5. **Warn for Matrix.** Matrix duplicates are rarer and more visible to users.

## Retry vs Replay

| | Retry (automatic) | Replay (manual) |
|---|---|---|
| **Trigger** | `ADAPTER_TRANSIENT` failures only | Operator-initiated via CLI |
| **Owner** | `RetryWorker` (background) | Operator |
| **Lineage** | `source='retry'`, linked via `parent_receipt_id`, same delivery chain | `source='replay'`, `replay_run_id`, new delivery execution |
| **Persistence** | Pending retry state (`next_retry_at`) survives restart | Receipts durable in SQLite. ReplaySummary is in-memory only. |
| **Duplicate risk** | None — same delivery attempt | High — new outbound messages, no dedup |
| **Bounded by** | `RetryPolicy` (max attempts, backoff) | Operator decides scope |
| **Opt-in** | Yes — requires `RetryPolicy` config | Always available |

### Retry States

| State | `status` | `next_retry_at` | `failure_kind` | Meaning |
|-------|---------|-----------------|----------------|---------|
| Pending retry | `failed` | Set (future time) | `adapter_transient` | RetryWorker will re-attempt |
| Exhausted | `dead_lettered` | `NULL` | `adapter_transient` | Max retries exceeded; manual intervention needed |
| Successful retry | `sent` or `confirmed` | `NULL` | `NULL` | Retry succeeded; check `parent_receipt_id` to trace back |

### When to Use Which

| Scenario | Use | Why |
|----------|-----|-----|
| Transient adapter failure | Retry (automatic) | RetryWorker handles this |
| Retry exhausted (dead-lettered) | Replay (manual) | After fixing the underlying cause |
| Event never delivered (orphaned by crash) | Replay (manual) or Retry (if outbox row exists) | If no outbox row, replay is the only option |
| Permanent failure | Replay (manual) | After fixing the underlying cause |
| Retry disabled (no RetryPolicy) | Replay (manual) | No RetryWorker running |

### Replay and Route-Level Retry Interaction

When BEST_EFFORT replay delivers to a route that has retry enabled, transient failures create due retry receipts in storage. The `medre replay` command never starts the RetryWorker (it builds but never calls `app.start()`). This means:

- Due retry receipts created during replay sit in storage unprocessed.
- If the operator later starts the runtime normally (`medre run`) with retry enabled, the RetryWorker will discover and process those receipts.
- This creates a cross-source retry chain: `source="replay"` to `source="retry"`, linked by `parent_receipt_id`.

After BEST_EFFORT replay, check for replay-created retry receipts:

```sql
SELECT receipt_id, event_id, status, next_retry_at, source, replay_run_id
FROM delivery_receipts
WHERE source = 'replay' AND next_retry_at IS NOT NULL;
```

If you see replay-created retry receipts and duplicate delivery is unacceptable, clear them:

```sql
UPDATE delivery_receipts SET next_retry_at = NULL
WHERE source = 'replay' AND next_retry_at IS NOT NULL;
```

## Recovery Commands Quick Reference

| Scenario | Command |
|----------|---------|
| Verify database integrity | `sqlite3 {state}/medre.sqlite "PRAGMA integrity_check;"` |
| Restart runtime | `medre run --config config.toml` |
| Check adapter health | `medre diagnostics --refresh-health --config config.toml` |
| Inspect an event | `medre inspect event <event_id> --storage-path <db>` |
| Inspect with timeline | `medre inspect event <event_id> --timeline --storage-path <db>` |
| Inspect with evidence | `medre inspect event <event_id> --evidence --storage-path <db>` |
| Inspect with recovery | `medre inspect event <event_id> --recovery --storage-path <db>` |
| Inspect delivery receipts | `medre inspect receipts --event <event_id> --storage-path <db>` |
| Inspect replay receipts | `medre inspect receipts --replay-run <run_id> --storage-path <db>` |
| Count orphaned events | SQL: `SELECT COUNT(*) FROM canonical_events e LEFT JOIN delivery_receipts r ON e.event_id = r.event_id WHERE r.event_id IS NULL;` |
| Preview replay | `medre replay --mode DRY_RUN --config my-bridge.toml` |
| Execute replay | `medre replay --mode BEST_EFFORT --config my-bridge.toml` |
| Check recent errors | `grep ERROR {state}/logs/medre.log \| tail -20` |
| Verify startup | `grep "Assembly complete" {state}/logs/medre.log \| tail -1` |

## Caveats

1. **No deduplication.** Each BEST_EFFORT replay produces new outbound messages.
2. **No automatic retry scheduling.** Replay is a one-shot operator action, not a durable job.
3. **No active supervision.** There is no background health monitor or watchdog beyond the RetryWorker.
4. **ReplaySummary is in-memory only.** Only BEST_EFFORT mode produces storage receipts. DRY_RUN and RE_ROUTE results exist only in CLI output.
5. **Counters reset on restart.** Process-local counters reset on every startup. Verify via SQLite queries, not counters.
6. **Single-machine only.** Replay operates on the local SQLite database. No distributed replay.
7. **No delivery order guarantee.** Replay processes events in storage order but delivery concurrency means outbound messages may arrive out of order.
8. **Radio transports are fire-and-forget.** A `sent` receipt means the local radio accepted the packet, not that the remote node received it.
9. **Shutdown during replay.** Completed events produce receipts; remaining events are lost. No automatic resume.
10. **No per-adapter restart.** Only full runtime stop/start is supported.

## See Also

- [diagnostics-and-evidence.md](diagnostics-and-evidence.md) — evidence provenance, bundle collection, report shapes
- [troubleshooting.md](troubleshooting.md) — failure drill interpretation, routing diagnostics

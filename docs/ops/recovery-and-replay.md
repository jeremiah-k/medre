# Recovery and Replay

How to recover from crashes, detect orphaned events, and re-process historical data through the replay engine.

All recovery is operator-initiated. There is no automatic remediation, no per-adapter restart, and no auto-healing. The RetryWorker handles transient failures automatically when enabled, but replay for orphaned events is always manual.

## Recovery Boundaries

Recovery operates within strict boundaries set by storage ownership:

- **Recovery never invents delivery success.** A `sent` receipt only exists because a real delivery attempt produced it. Orphan detection identifies events without receipts, but producing a receipt requires an actual delivery attempt through replay or retry.
- **Recovery never rewrites history.** Existing receipts, events, and native refs are immutable. Recovery can only produce new state: new delivery attempts through replay, new retry receipts through the RetryWorker, or reclaimed outbox rows through lease expiry.
- **Outbox reclaim is not lifecycle success.** Reclaiming an expired `in_progress` row or stale `queued` row restores operational work state. It does not imply the delivery succeeded. The outbox row must still complete the delivery pipeline and produce a receipt.
- **Orphan detection is bookkeeping.** The orphan query (events with no receipts) is a diagnostic observation. It does not create, modify, or delete any storage rows. Re-delivering orphans requires explicit operator action via replay.

## Complete Incident Workflow

```bash
# 1. Verify pipeline health (use a config with storage.backend = "sqlite")
medre smoke --config /tmp/medre-incident.toml --json

# 2. Inspect the suspect event
medre inspect event <event_id> --storage-path /tmp/medre-incident.db

# 3. Check delivery receipts
medre inspect receipts --event <event_id> --storage-path /tmp/medre-incident.db

# 4. Full investigation via inspect flags
medre inspect event <event_id> --timeline --evidence --recovery --storage-path /tmp/medre-incident.db

# 5. Preview replay (no side effects)
medre replay --mode dry_run --config my-bridge.toml

# 6. Re-deliver orphaned events (sends real messages)
medre replay --mode best_effort --config my-bridge.toml
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

| State                                            | Survived? | Notes                                                                                                                                                                 |
| ------------------------------------------------ | --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Canonical events                                 | Yes       | Written to SQLite before delivery                                                                                                                                     |
| Delivery receipts                                | Yes       | Written after each delivery attempt                                                                                                                                   |
| Native message refs                              | Yes       | Persisted in SQLite alongside receipts                                                                                                                                |
| Receipt traceability (`source`, `replay_run_id`) | Yes       | Stored on receipts in SQLite                                                                                                                                          |
| Matrix E2EE crypto keys                          | Yes       | On disk under adapter state root                                                                                                                                      |
| LXMF identity files                              | Yes       | On disk under adapter state root                                                                                                                                      |
| Logs (pre-crash)                                 | Yes       | Appended to `{log_dir}/medre.log`                                                                                                                                     |
| In-flight deliveries                             | Partial   | No receipt, but an `in_progress` outbox row may survive. Expired leases are reclaimable by `claim_due_outbox_items()`. Deliveries without outbox rows are fully lost. |
| Active replay runs                               | No        | Lost — must re-initiate manually                                                                                                                                      |
| Runtime counters (accounting)                    | No        | Process-local counters reset after restart                                                                                                                            |
| Adapter connection state                         | No        | Adapters reconnect from scratch                                                                                                                                       |

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
# Non-terminal outbox items were preserved as resumable work.
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

| Failure cause              | Fix                                          | Replay needed?                                                                                  |
| -------------------------- | -------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| Network outage             | Restore network, restart runtime             | Yes — events may have been received by other adapters but not delivered to the affected adapter |
| Serial device disconnected | Reconnect device, restart runtime            | Yes — inbound events from the disconnected adapter were lost                                    |
| Authentication failure     | Renew credentials, restart runtime           | No — events from other adapters were still processed                                            |
| SDK dependency missing     | Install SDK, restart runtime                 | No — events were never received                                                                 |
| Adapter bug (crash loop)   | Fix code or disable adapter, restart runtime | Partially — events from other adapters to this adapter's targets were lost                      |

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

| Scenario                                               | Has receipt?          | Action                                                            |
| ------------------------------------------------------ | --------------------- | ----------------------------------------------------------------- |
| Event stored, delivery in progress when crash occurred | No                    | Replay candidate                                                  |
| Event stored, no routes matched                        | No                    | Not an orphan — no routes were configured for this event's source |
| Event stored, loop prevented delivery                  | `suppressed` receipt  | Not an orphan — loop prevention worked correctly                  |
| Event stored, capacity exceeded                        | `suppressed` receipt  | Replay candidate (after increasing capacity limits)               |
| Event stored, delivery sent before crash               | Yes (receipt written) | Not an orphan — check receipt status                              |

### Replay Workflow for Orphans

```bash
# 1. Count and review orphans (see SQL above)

# 2. Preview replay with dry_run
medre replay --mode dry_run --config my-bridge.toml

# 3. Review route attributions in dry_run output

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
medre replay --mode best_effort --config my-bridge.toml

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

| Mode          | Stages                              | Delivers?          | Side effects          | Use case                                                                                                   |
| ------------- | ----------------------------------- | ------------------ | --------------------- | ---------------------------------------------------------------------------------------------------------- |
| `strict`      | store                               | No (validate only) | None                  | Validate events against current schema. No routing or delivery.                                            |
| `re_render`   | store, render                       | No (re-render)     | None                  | Re-run rendering for existing events. No delivery.                                                         |
| `re_route`    | store, route, plan                  | No (read-only)     | None                  | Re-evaluate route matching after a config change. No delivery.                                             |
| `dry_run`     | store, route, plan, render, deliver | Skip (no delivery) | None                  | Preview what replay would do. First step before any `best_effort`.                                         |
| `best_effort` | store, route, plan, render, deliver | Yes                | Real adapter delivery | Re-deliver historical events. Sends real messages. Produces fresh storage receipts with `source='replay'`. |

Always run `dry_run` or `re_route` first. Only use `best_effort` when you have verified the route matching preview and accept the duplicate delivery risk.

### Capability Filtering During Replay

`BEST_EFFORT` replay applies capability filtering before delivery, using the same `CapabilityDecisionResolver` as live delivery. Plans targeting adapters that lack capability for the event's kind or relation types are filtered out.

When `_filter_plans_by_capability` suppresses all plans for an event, the `ReplayResult` includes enriched output:

```json
{
  "status": "skipped",
  "error": "capability_suppressed: message.reacted not supported by target adapter(s)",
  "output": {
    "capability_suppressed_plans": [
      {
        "delivery_plan_id": "plan-001",
        "target_adapter": "radio",
        "capability_level": "unsupported",
        "capability_field": "reactions",
        "reason": "reactions unsupported"
      }
    ],
    "delivery_plan_ids": ["plan-001"],
    "replay_run_id": "replay_xyz789",
    "source": "replay"
  }
}
```

This output is carried in the in-memory `ReplayResult`. It is not persisted to storage unless a receipt is created through a different code path. If the process crashes before you inspect the replay output, this evidence is lost.

#### Capability Re-evaluation at Replay Time

Replay re-evaluates capabilities against the current adapter registry, not the adapter state at the time the original event was processed. If you add or remove adapters between the original delivery and the replay, capability decisions may differ. This is intentional: replay reflects what would happen with the current configuration.

## Replay Command Shape

```bash
medre replay --mode <mode> [--event <event_id>] --config my-bridge.toml
```

| Flag       | Required | Description                                                             |
| ---------- | -------- | ----------------------------------------------------------------------- |
| `--mode`   | Yes      | One of: `strict`, `re_render`, `re_route`, `dry_run`, `best_effort`     |
| `--event`  | No       | Specific event ID to replay. If omitted, replays all events in storage. |
| `--config` | Yes      | Path to TOML config (must use SQLite storage)                           |

Replay requires `--config` for route resolution and pipeline construction.
`--storage-path` is not supported for replay — it is reserved for read-only
inspection commands (`inspect`, `trace`, `evidence`).

Additional flags:

| Flag                              | Description                                       |
| --------------------------------- | ------------------------------------------------- |
| `--target-adapters ADAPTER [...]` | Only replay events targeting these adapter(s)     |
| `--route-ids ROUTE [...]`         | Only replay events that matched these route ID(s) |
| `--limit INT`                     | Maximum number of events to replay (default 100)  |
| `--json`                          | Output as JSON                                    |

### Exit Codes

| Code | Meaning                                                        |
| ---- | -------------------------------------------------------------- |
| 0    | Replay completed (may include partial failures in BEST_EFFORT) |
| 2    | Config error, no SQLite backend, or database not found         |

## Replay Result Interpretation

`events_replayed` counts replay result rows (one per `(event, stage)` pair),
not distinct event IDs. `events_scanned` is the distinct event count.

### dry_run Result

```json
{
  "mode": "dry_run",
  "run_id": "replay_preview_001",
  "events_scanned": 10,
  "events_replayed": 50,
  "skipped_count": 10,
  "failure_count": 0,
  "route_resolution_count": 8,
  "by_status": { "error": 0, "failed": 0, "passed": 40, "skipped": 10 },
  "by_stage": {
    "store": 10,
    "route": 10,
    "plan": 10,
    "render": 10,
    "deliver": 10
  },
  "by_route": {},
  "errors": [],
  "elapsed_ms": 12.3
}
```

Review `by_route` to verify which routes match each event and which target adapters would receive delivery before proceeding to `best_effort`.

### re_route Result

Compare `by_route` against previous delivery receipts to see what changed after a route config update. Events that previously matched one route but now match two will have fan-out delivery if replayed with `best_effort`.

### best_effort Result

```json
{
  "mode": "best_effort",
  "run_id": "replay_xyz789",
  "events_scanned": 10,
  "events_replayed": 50,
  "skipped_count": 0,
  "failure_count": 1,
  "route_resolution_count": 8,
  "by_status": { "error": 1, "failed": 0, "passed": 44, "skipped": 5 },
  "by_stage": {
    "store": 10,
    "route": 10,
    "plan": 10,
    "render": 10,
    "deliver": 10
  },
  "by_route": { "bot-to-radio": { "events": 10, "succeeded": 9, "failed": 1 } },
  "errors": ["replay_capacity_exceeded for event evt_def456"],
  "elapsed_ms": 45.6
}
```

After `best_effort`, inspect storage receipts:

```bash
medre inspect receipts --replay-run replay_xyz789 --storage-path /path/to/medre.sqlite
```

## Replay Receipts

`best_effort` replay produces `DeliveryReceipt` records with these distinguishing fields:

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

| Field           | Value for replay      | Purpose                                              |
| --------------- | --------------------- | ---------------------------------------------------- |
| `source`        | `"replay"`            | Distinguishes replay deliveries from live deliveries |
| `replay_run_id` | Unique run identifier | Groups all receipts from the same replay run         |

Key distinctions:

1. Replay receipts are distinguishable from live receipts via `source='replay'` and `replay_run_id`.
2. Replay is not dedupe — each `best_effort` run produces fresh receipts regardless of existing live or replay receipts for the same event.
3. Native refs created during replay are not directly source-tagged. Correlate through the associated `DeliveryReceipt`'s `event_id` linkage.
4. Replay produces the same deterministic `delivery_plan_id` as the original live delivery, because plan IDs are derived from `event_id`, `route_id`, `target_index`, and a stable target identity hash — not from Python object identity. This means the same event replayed multiple times produces the same `delivery_plan_id` values each time. Plan IDs are stable only when `event_id`, `route_id`, `target_index`/order, and the stable target identity hash are unchanged. If any of those inputs change (different route match, different target order, different adapter metadata), the plan ID changes.

### Live/Replay Plan Parity

Live delivery and replay planning produce semantically equivalent delivery plans for the same event and route configuration. The following fields are identical across live and replay paths:

- `plan_id` — deterministic via `stable_delivery_plan_id`
- `route_id` — same matched route
- `target_identity` — same stable JSON target identity
- `capability_level`, `capability_field`, `capability_reason` — same capability decision
- `primary_strategy.method` — same delivery strategy

The fields that intentionally differ are: `source` (`"live"` vs `"replay"`), `replay_run_id`, `receipt_id`, `parent_receipt_id`, `created_at`, and `adapter_message_id`. These differ by design for attribution and audit traceability.

### Receipt Parity Between Live and Replay

When comparing a live receipt to its replay counterpart for the same event and target, these fields match: `event_id`, `delivery_plan_id`, `target_adapter`, `target_channel`, `route_id`, `status`, `failure_kind`, `error` (for suppression reasons), `rendering_evidence` (strategy and capability level), and `next_retry_at` (when applicable).

To verify parity, compare receipts for the same event across live and replay:

```sql
-- Compare live and replay receipt fields for the same event
SELECT
  source,
  delivery_plan_id,
  target_adapter,
  target_channel,
  route_id,
  status,
  failure_kind
FROM delivery_receipts
WHERE event_id = 'evt_abc123'
ORDER BY source, created_at;
```

### Retry Lineage Preservation

Retry reconstruction preserves the original delivery's identity fields through the entire retry chain. Each retry attempt appends a new receipt row — earlier receipts are never overwritten.

Fields preserved across the retry chain:

| Field              | Preserved? | Notes                      |
| ------------------ | ---------- | -------------------------- |
| `delivery_plan_id` | Yes        | Same deterministic plan ID |
| `route_id`         | Yes        | From original delivery     |
| `target_adapter`   | Yes        | Frozen from original       |
| `target_channel`   | Yes        | Frozen from original       |
| `event_id`         | Yes        | Same canonical event       |

Each retry receipt links to the previous attempt via `parent_receipt_id`. When retries are exhausted, the final receipt has `status="dead_lettered"` with `next_retry_at=NULL`. The full chain is visible by following `parent_receipt_id` links:

```sql
-- Trace the full retry chain for a delivery plan
SELECT * FROM delivery_receipts WHERE delivery_plan_id = ? ORDER BY attempt_number;
```

### Suppressed Deliveries and the Retry Queue

Suppressed deliveries (status `"suppressed"` with failure kind `loop_suppressed`, `capability_suppressed`, or `policy_suppressed`) do not enter the retry queue. Suppressed receipts have `next_retry_at=NULL` and are never returned by `list_due_retry_receipts()`. This is by design: suppression indicates a guard fired to prevent delivery, not a transient failure that might resolve with retry.

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

Replay does not deduplicate. Every `best_effort` replay produces new outbound messages on all matched targets.

### When Duplicates Occur

| Scenario                                            | Risk level | Why                                                     |
| --------------------------------------------------- | ---------- | ------------------------------------------------------- |
| Replaying events that were never delivered          | Low        | No prior delivery exists                                |
| Replaying events that were delivered before a crash | Medium     | Some events may have been delivered but have no receipt |
| Replaying events that have existing `sent` receipts | High       | Events will be delivered again                          |
| Multiple `best_effort` replays of the same events   | High       | Each run produces new deliveries                        |

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

1. **Always `dry_run` first.** Review route attributions before `best_effort`.
2. **Query existing receipts.** Use the SQL above to assess how many events already have `sent` receipts.
3. **Scope replay narrowly.** Use `--event <id>` or `--limit` to replay only the events that need it.
4. **Accept duplicates for radio transports.** Meshtastic and MeshCore treat duplicate sends as normal operational practice.
5. **Warn for Matrix.** Matrix duplicates are rarer and more visible to users.

## Retry vs Replay

|                    | Retry (automatic)                                                     | Replay (manual)                                              |
| ------------------ | --------------------------------------------------------------------- | ------------------------------------------------------------ |
| **Trigger**        | `ADAPTER_TRANSIENT` failures only                                     | Operator-initiated via CLI                                   |
| **Owner**          | `RetryWorker` (background)                                            | Operator                                                     |
| **Lineage**        | `source='retry'`, linked via `parent_receipt_id`, same delivery chain | `source='replay'`, `replay_run_id`, new delivery execution   |
| **Persistence**    | Pending retry state (`next_retry_at`) survives restart                | Receipts durable in SQLite. ReplaySummary is in-memory only. |
| **Duplicate risk** | None — same delivery attempt                                          | High — new outbound messages, no dedup                       |
| **Bounded by**     | `RetryPolicy` (max attempts, backoff)                                 | Operator decides scope                                       |
| **Opt-in**         | Yes — requires `RetryPolicy` config                                   | Always available                                             |

### Retry Accountability

When retry is enabled, the system produces an auditable chain:

1. Initial failure creates a receipt with `status="failed"`, `failure_kind="adapter_transient"`, and `next_retry_at` set.
2. The RetryWorker discovers due receipts and re-attempts delivery.
3. Each retry appends a new receipt row with incremented `attempt_number` and `parent_receipt_id` linking to the previous attempt.
4. When retries are exhausted, the final receipt has `status="dead_lettered"`.

The full chain is queryable:

```sql
SELECT receipt_id, status, attempt_number, failure_kind, next_retry_at, created_at
FROM delivery_receipts
WHERE delivery_plan_id = '<plan_id>'
ORDER BY attempt_number;
```

### Outbox Accountability

The outbox tracks in-progress deliveries:

- A delivery starting creates an `in_progress` outbox row with an expiration lease.
- Delivery completion (success or failure) finalizes the outbox row.
- On crash recovery, expired `in_progress` rows are reclaimed by `claim_due_outbox_items()`.
- Outbox rows without corresponding receipts indicate deliveries that were lost before a receipt could be written.

### Resumable Shutdown Policy

When the runtime shuts down gracefully, pending retry receipts (those with `next_retry_at` set) and pending outbox items are not cancelled. They remain in storage as resumable work. On next startup:

- Due retry receipts are discovered and processed by the RetryWorker.
- Expired `in_progress` outbox rows are reclaimed by `claim_due_outbox_items()`.
- Stale `queued` outbox rows are reclaimed after the configured grace period.

This is an intentional design choice. Non-terminal outbox work (`pending`, `retry_wait`, `in_progress`, `queued`) is preserved, not cancelled. Cancellation is a distinct terminal state that requires explicit operator action.

The `ShutdownEvidence` record (available in the evidence bundle) reports:

- `resume_expected=True` when non-terminal outbox work was left at shutdown.
- `outbox_shutdown_policy="resumable"` to signal the resumable policy.
- `pending_outbox_counts` with per-status counts of preserved items.

Operators can inspect these fields to understand what work will resume after restart.

### Retry States

| State            | `status`        | `next_retry_at`   | `failure_kind`      | Meaning                                                  |
| ---------------- | --------------- | ----------------- | ------------------- | -------------------------------------------------------- |
| Pending retry    | `failed`        | Set (future time) | `adapter_transient` | RetryWorker will re-attempt                              |
| Exhausted        | `dead_lettered` | `NULL`            | `adapter_transient` | Max retries exceeded; manual intervention needed         |
| Successful retry | `sent`          | `NULL`            | `NULL`              | Retry succeeded; check `parent_receipt_id` to trace back |

### When to Use Which

| Scenario                                  | Use                                             | Why                                         |
| ----------------------------------------- | ----------------------------------------------- | ------------------------------------------- |
| Transient adapter failure                 | Retry (automatic)                               | RetryWorker handles this                    |
| Retry exhausted (dead-lettered)           | Replay (manual)                                 | After fixing the underlying cause           |
| Event never delivered (orphaned by crash) | Replay (manual) or Retry (if outbox row exists) | If no outbox row, replay is the only option |
| Permanent failure                         | Replay (manual)                                 | After fixing the underlying cause           |
| Retry disabled (no RetryPolicy)           | Replay (manual)                                 | No RetryWorker running                      |

### Replay and Route-Level Retry Interaction

When `best_effort` replay delivers to a route that has retry enabled, transient failures create due retry receipts in storage. The `medre replay` command never starts the RetryWorker (it builds but never calls `app.start()`). This means:

- Due retry receipts created during replay sit in storage unprocessed.
- If the operator later starts the runtime normally (`medre run`) with retry enabled, the RetryWorker will discover and process those receipts.
- This creates a cross-source retry chain: `source="replay"` to `source="retry"`, linked by `parent_receipt_id`.

After `best_effort` replay, check for replay-created retry receipts:

```sql
SELECT receipt_id, event_id, status, next_retry_at, source, replay_run_id
FROM delivery_receipts
WHERE source = 'replay' AND next_retry_at IS NOT NULL;
```

If replay-created retry receipts appear and duplicate delivery is a concern,
use query-time filtering or narrow the replay scope rather than mutating
receipt rows:

- Filter replay-origin rows at query time:
  ```sql
  SELECT * FROM delivery_receipts
  WHERE source <> 'replay' OR source IS NULL;
  ```
- Narrow replay scope before running replay (use `--route-ids`, `--target-adapters`, or `--limit`).
- Leave `next_retry_at` values intact — `delivery_receipts` rows are append-only evidence.

## Recovery Commands Quick Reference

| Scenario                  | Command                                                                                                                           |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Verify database integrity | `sqlite3 {state}/medre.sqlite "PRAGMA integrity_check;"`                                                                          |
| Restart runtime           | `medre run --config config.toml`                                                                                                  |
| Check adapter health      | `medre diagnostics --refresh-health --config config.toml`                                                                         |
| Inspect an event          | `medre inspect event <event_id> --storage-path <db>`                                                                              |
| Inspect with timeline     | `medre inspect event <event_id> --timeline --storage-path <db>`                                                                   |
| Inspect with evidence     | `medre inspect event <event_id> --evidence --storage-path <db>`                                                                   |
| Inspect with recovery     | `medre inspect event <event_id> --recovery --storage-path <db>`                                                                   |
| Inspect delivery receipts | `medre inspect receipts --event <event_id> --storage-path <db>`                                                                   |
| Inspect replay receipts   | `medre inspect receipts --replay-run <run_id> --storage-path <db>`                                                                |
| Count orphaned events     | SQL: `SELECT COUNT(*) FROM canonical_events e LEFT JOIN delivery_receipts r ON e.event_id = r.event_id WHERE r.event_id IS NULL;` |
| Preview replay            | `medre replay --mode dry_run --config my-bridge.toml`                                                                             |
| Execute replay            | `medre replay --mode best_effort --config my-bridge.toml`                                                                         |
| Check recent errors       | `grep ERROR {state}/logs/medre.log \| tail -20`                                                                                   |
| Verify startup            | `grep "Assembly complete" {state}/logs/medre.log \| tail -1`                                                                      |

## Caveats

1. **No deduplication.** Each `best_effort` replay produces new outbound messages.
2. **No automatic retry scheduling.** Replay is a one-shot operator action, not a durable job.
3. **No active supervision.** There is no background health monitor or watchdog beyond the RetryWorker.
4. **ReplaySummary is in-memory only.** Only `best_effort` mode produces storage receipts. `dry_run` and `re_route` results exist only in CLI output.
5. **Counters reset on restart.** Process-local counters reset on every startup. Verify via SQLite queries, not counters.
6. **Single-machine only.** Replay operates on the local SQLite database. No distributed replay.
7. **No delivery order guarantee.** Replay processes events in storage order but delivery concurrency means outbound messages may arrive out of order.
8. **Radio transports are fire-and-forget.** A `sent` receipt means the local radio accepted the packet, not that the remote node received it.
9. **Shutdown during replay.** Completed events produce receipts; remaining events are lost. No automatic resume.
10. **No per-adapter restart.** Only full runtime stop/start is supported.

## Replay and Live Delivery Separation

Replay and live delivery are isolated by the `source` field on receipts (`"live"`, `"retry"`, `"replay"`). This separation is enforced at correlation time:

### Queued Callback Source Selection

When a queue-based adapter callback arrives to confirm a queued delivery (queued-to-sent transition), the pipeline selects among matching queued receipts. The selection prefers non-replay candidates (`"live"` or `"retry"`) over `"replay"` candidates.

| Scenario                                              | Selected candidate                          | Log level |
| ----------------------------------------------------- | ------------------------------------------- | --------- |
| One live/retry candidate, no replay candidates        | Live/retry candidate                        | Debug     |
| Multiple live/retry candidates, same plan and channel | Latest live/retry candidate                 | Debug     |
| Only replay candidates available                      | None — correlation skipped (warning logged) | Warning   |
| No candidates at all                                  | No supplemental receipt created             | Debug     |

When only replay-sourced queued receipts are available, the pipeline skips correlation entirely and emits an operator-visible warning. No supplemental sent receipt is created, no outbox transition occurs. `OutboundNativeRefRecord` carries no trusted `source` / `replay_run_id` provenance, so replay-only queued receipts cannot be safely used for callback correlation without risking live recovery state mutation. This restriction may be relaxed in a future version when callback records carry trusted replay provenance.

### Uncorrelated Queued Outbox Items

When a queued outbox item has no receipt linkage, the reason-pending derivation flags it as awaiting callback correlation. The evidence output depends on what metadata is present:

- No `outbox_id` and no receipt linkage:

```text
Queued without queued receipt linkage — awaiting stale-grace reclaim or exact outbox_id + attempt_number callback correlation
```

- `outbox_id` present but missing `delivery_plan_id`:

```text
Queued with degraded plan metadata (missing delivery_plan_id) — awaiting stale-grace reclaim or exact outbox_id + attempt_number callback correlation
```

- `outbox_id` and `delivery_plan_id` present but no receipt:

```text
Queued in adapter-local queue — awaiting outbox_id + attempt_number callback correlation
```

Operators seeing these messages should check whether the adapter callback is expected to provide `outbox_id + attempt_number` linkage, or whether the stale-grace reclaim timer (`STALE_QUEUED_GRACE_SECONDS`, default 300 s) will eventually reclaim the item.

### Replay Does Not Mutate Live Recovery State

Replay execution (`medre replay`) produces its own receipts and outbox transitions, all tagged `source="replay"`. Replay does not modify existing live receipts, live outbox items, or live retry state. If a replay run creates due retry receipts (transient failure during `best_effort` mode), those retry receipts sit in storage unprocessed until the runtime starts normally with retry enabled.

## Convergence Diagnostics for Recovery

After a crash or unexpected shutdown, convergence diagnostics help assess the state of delivery targets. The evidence bundle for each event includes a `convergence_summary` that classifies every delivery target as `safe`, `degraded`, or `inconsistent`.

### Interpreting Convergence Severity

| Severity       | Meaning                                                 | Operator action                                                                         |
| -------------- | ------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `safe`         | Outbox and receipts agree. No action needed.            | None.                                                                                   |
| `degraded`     | Work stalled or mid-flight. Normal for recent events.   | Monitor. If degraded persists after startup recovery, investigate the specific target.  |
| `inconsistent` | State mismatch that cannot be explained by normal flow. | Investigate manually. Check outbox and receipt chain for the affected delivery_plan_id. |

### Checking Convergence After Crash

```bash
# Collect evidence for a specific event
medre inspect event <event_id> --evidence --storage-path /path/to/medre.sqlite

# The evidence output includes convergence_summary with per-target severity
# Look for "inconsistent" targets in the output
```

Convergence diagnostics are read-only. They do not repair state or block startup.

### Lifecycle Convergence After Recovery

The evidence bundle also includes a `lifecycle_convergence_report` with finer-grained findings about specific contradictions between outbox and receipt state. After recovery, check this report for:

- `terminal_receipt_nonterminal_outbox` or `terminal_outbox_nonterminal_receipt`: Status mismatches between the two state machines. These may be timing artifacts or need manual investigation.
- `retry_wait_missing_next_retry`: Outbox items stuck in `retry_wait` without valid retry timestamps.
- `stalled_delivery_plan`: Non-terminal outbox items that have not been updated within the stall threshold (default 1 hour).
- `attempt_count_regression` or `receipt_sequence_gap`: Receipt chain integrity issues.

All lifecycle convergence findings are detection-only. No automatic repair occurs. Operators use these findings to identify and manually address state discrepancies after recovery.

## See Also

- [diagnostics-and-evidence.md](diagnostics-and-evidence.md) — evidence provenance, bundle collection, report shapes
- [troubleshooting.md](troubleshooting.md) — failure drill interpretation, routing diagnostics

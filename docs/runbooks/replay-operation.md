# Replay Operation Runbook

> Last updated: 2026-05-16
> Scope: Operator-facing replay workflow — modes, commands, result interpretation, duplicate risk
> Status: Pre-beta. Not production. Replay is a one-shot operator action, not a durable job.
> Prerequisites: medre installed with `[dev]` extras, runtime previously run with `[storage] backend = "sqlite"`.

This runbook describes how to operate the MEDRE replay engine: re-processing
historical events through the pipeline. Replay is a lower-level supported
command for specialized recovery and verification scenarios. It is not part of
the preferred product path for day-to-day operation.

**For daily investigation**, start with `medre inspect event` and `medre
inspect receipts` (see the [Alpha Walkthrough](alpha-walkthrough.md)). Reach
for replay when you need to re-process events: recovering orphaned events
after a crash, verifying route changes against historical data, or
re-delivering events that were missed.

**What replay can do:**

- Re-evaluate which routes match historical events (`RE_ROUTE`, `DRY_RUN`).
- Re-deliver historical events through current routes and adapters
  (`BEST_EFFORT`).
- Produce traceable receipts with `source='replay'` and `replay_run_id` for
  audit.

**What replay does NOT do:**

- Deduplicate. Each BEST_EFFORT replay run produces new outbound messages.
- Resume after crash. Replay runs are not durable — they must be re-initiated.
- Provide exactly-once delivery semantics.
- Guarantee delivery order.


## 1. Replay Modes

| Mode | Routes? | Delivers? | Side effects | Use case |
|------|---------|-----------|-------------|----------|
| `DRY_RUN` | Yes | Skip (no delivery) | None | Preview what replay would do without side effects. First step before any BEST_EFFORT. |
| `RE_ROUTE` | Yes | No (read-only) | None | Re-evaluate route matching after a config change. Produces `ReplayRouteAttribution` showing which routes would match. No delivery. |
| `BEST_EFFORT` | Yes | Yes | Real adapter delivery | Re-deliver historical events. **BEST_EFFORT sends real messages** to adapters. Produces fresh storage receipts with `source='replay'` — replay is not dedupe; each run creates new receipts independently. Use with caution. |

**Operational rule:** Always run `DRY_RUN` or `RE_ROUTE` first. Only use
`BEST_EFFORT` when you have verified the route matching preview and accept the
duplicate delivery risk.


## 2. Command Shape

### 2.1 Replay Command

```bash
medre replay --mode <mode> [--event <event_id>] --config my-bridge.toml
```

| Flag | Required | Description |
|------|----------|-------------|
| `--mode` | Yes | One of: `DRY_RUN`, `RE_ROUTE`, `BEST_EFFORT` |
| `--event` | No | Specific event ID to replay. If omitted, replays all events in storage. |
| `--config` | Yes | Path to TOML config (must use SQLite storage) |

**Additional flags (not all may be implemented yet):**

| Flag | Description |
|------|-------------|
| `--from <timestamp>` | Replay events created after this ISO-8601 timestamp |
| `--to <timestamp>` | Replay events created before this ISO-8601 timestamp |
| `--route <route_id>` | Only replay events that match this route |

### 2.2 Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Replay completed (may include partial failures in BEST_EFFORT) |
| 2 | Config error, no SQLite backend, or database not found |

### 2.3 ReplaySummary

The `ReplaySummary` is the in-memory result returned after a replay run. It is
**not persisted to storage** — it exists only for the duration of the CLI
process. To inspect replay results after exit, query delivery receipts with
`source='replay'` and the specific `replay_run_id`.

```json
{
  "mode": "BEST_EFFORT",
  "replay_run_id": "replay_xyz789",
  "events_processed": 10,
  "deliveries_attempted": 8,
  "deliveries_sent": 7,
  "deliveries_failed": 1,
  "deliveries_skipped": 0,
  "errors": []
}
```

**Important:** `ReplaySummary` is in-memory only. Only `BEST_EFFORT` mode
produces storage receipts. `DRY_RUN` and `RE_ROUTE` produce no persistent
evidence — their results are printed to stdout and then discarded.


## 3. Replay Result Interpretation

### 3.1 DRY_RUN Result

```json
{
  "mode": "DRY_RUN",
  "replay_run_id": "replay_preview_001",
  "events_processed": 10,
  "deliveries_attempted": 0,
  "deliveries_sent": 0,
  "deliveries_failed": 0,
  "deliveries_skipped": 10,
  "route_attributions": [
    {
      "event_id": "evt_abc123",
      "route_ids": ["bot-to-radio"],
      "target_adapters": ["radio"]
    }
  ],
  "errors": []
}
```

**Interpretation:**

- `events_processed` — how many events were evaluated.
- `deliveries_skipped` — all deliveries were skipped (DRY_RUN behavior).
- `route_attributions` — which routes would match each event, and which target
  adapters would receive delivery. Review this to verify route matching before
  proceeding to `BEST_EFFORT`.

### 3.2 RE_ROUTE Result

```json
{
  "mode": "RE_ROUTE",
  "replay_run_id": "replay_reroute_001",
  "events_processed": 10,
  "deliveries_attempted": 0,
  "deliveries_sent": 0,
  "deliveries_failed": 0,
  "deliveries_skipped": 10,
  "route_attributions": [
    {
      "event_id": "evt_abc123",
      "route_ids": ["bot-to-radio", "bot-to-meshcore"],
      "target_adapters": ["radio", "meshcore"]
    }
  ],
  "errors": []
}
```

**Interpretation:**

- Compare `route_attributions` against previous delivery receipts to see what
  changed after a route config update.
- Events that previously matched one route but now match two will have fan-out
  delivery if replayed with `BEST_EFFORT`.
- Events that previously matched but no longer match a route will not be
  delivered.

### 3.3 BEST_EFFORT Result

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

**Interpretation:**

- `deliveries_attempted` — events that matched routes and were sent to adapters.
- `deliveries_sent` — adapters reported successful handoff.
- `deliveries_failed` — adapters reported failure or capacity was exceeded.
- `deliveries_skipped` — events that matched no routes or were loop-prevented.
- `errors` — individual event failures with details.

After `BEST_EFFORT`, inspect storage receipts:

```bash
medre inspect receipts --replay-run replay_xyz789 --storage-path /path/to/medre.sqlite
```

Or trace the full replay run:

```bash
medre trace replay replay_xyz789 --storage-path /path/to/medre.sqlite
```


## 4. Replay Receipts

### 4.1 Receipt Shape

BEST_EFFORT replay produces `DeliveryReceipt` records with these distinguishing
fields:

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

**Key distinctions:**

1. Replay receipts are distinguishable from live receipts via `source='replay'`
   and `replay_run_id`. These fields are set on every BEST_EFFORT receipt.
2. Replay is not dedupe — each BEST_EFFORT run produces fresh receipts
   regardless of existing live or replay receipts for the same event.
3. Native refs created during replay are **not** directly source-tagged. To
   correlate a native ref back to a replay run, join through the associated
   `DeliveryReceipt` (which carries `source` and `replay_run_id`), then via
   the receipt's `event_id` linkage to the native ref.

### 4.2 Querying Replay Receipts

```bash
# All receipts from a specific replay run
medre inspect receipts --replay-run <run_id> --storage-path /path/to/medre.sqlite

# SQL: all replay receipts
SELECT event_id, target_adapter, status, replay_run_id
FROM delivery_receipts
WHERE source = 'replay'
ORDER BY created_at DESC;

# SQL: receipts for a specific replay run
SELECT event_id, target_adapter, status, route_id
FROM delivery_receipts
WHERE source = 'replay' AND replay_run_id = '<run_id>'
ORDER BY event_id;
```


### 4.3 `source` vs `replay_run_id` Distinction

Replay receipts carry two key attribution fields. Understanding the distinction
is critical for incident investigation and duplicate risk assessment:

| Field | Purpose | Values |
|-------|---------|--------|
| `source` | **Origin classification** — was this delivery from the live pipeline, a RetryWorker retry, or a replay run? | `"live"`, `"retry"`, or `"replay"` |
| `replay_run_id` | **Run grouping** — which specific replay invocation produced this receipt? | Unique run ID string when `source == "replay"`, `null` when `source == "live"` or `source == "retry"` |

**Practical use:**

```sql
-- Distinguish live from replay deliveries for a specific event
SELECT source, replay_run_id, status, target_adapter
FROM delivery_receipts
WHERE event_id = 'evt_abc123'
ORDER BY created_at ASC;

-- Group all receipts from one replay run (audit trail)
SELECT event_id, target_adapter, status, attempt_number
FROM delivery_receipts
WHERE source = 'replay' AND replay_run_id = 'replay_xyz789'
ORDER BY event_id;
```

**Key points:**

1. `source='replay'` is the reliable way to filter replay-attributed receipts.
   Do not rely on `replay_run_id IS NOT NULL` alone — future features may
   populate this field for non-replay purposes.
2. `replay_run_id` groups receipts from a single `medre replay` invocation.
   Multiple BEST_EFFORT replays produce different `replay_run_id` values for
   the same events.
3. An event can have both `source='live'` and `source='replay'` receipts.
   This indicates duplicate delivery — the event was originally delivered
   through the live pipeline and then re-delivered via replay.

**Caveat:** Traceability is not deduplication. The `source` and
`replay_run_id` fields tell you where a receipt came from, but cannot tell
you whether the delivery actually reached the remote side. Radio transports
are fire-and-forget. There is no final ACK guarantee.

### 4.4 Stale Data Warning

Before running BEST_EFFORT replay, operators should check for existing live
receipts. Events with existing `source='live'` `sent` receipts will receive
duplicate deliveries if replayed with BEST_EFFORT.

```bash
# Check for events that already have live sent receipts
sqlite3 {state}/medre.sqlite "
  SELECT e.event_id,
    COUNT(CASE WHEN r.source = 'live' AND r.status = 'sent' THEN 1 END) AS live_sent
  FROM canonical_events e
  LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
  GROUP BY e.event_id
  HAVING live_sent > 0
  ORDER BY e.created_at DESC;
"
```

If this query returns events in your replay scope, you have stale data:
events that were already delivered. BEST_EFFORT replay will deliver them
again. Options:

1. **Accept duplicates** — radio transports treat duplicates as normal.
2. **Narrow scope** — use `--event <id>` to replay only truly orphaned events.
3. **Use RE_ROUTE** — re-evaluate route matching without side effects first.

There is no automatic stale data detection in the replay engine. The
operator is responsible for assessing duplicate risk before each BEST_EFFORT
run. The RetryWorker handles transient failures automatically when enabled, but does not trigger replay; replay is a one-shot operator
action. BEST_EFFORT sends real messages.

### 4.5 Replay Cancellation and Shutdown

If the runtime shuts down during an active BEST_EFFORT replay, the behavior
depends on what completed before shutdown:

- **Completed events** — events that were fully delivered before shutdown
  produce receipts that persist in SQLite. These receipts carry `source='replay'`
  and the `replay_run_id` as normal.
- **Remaining events** — events that had not yet been processed or were
  in-flight when shutdown occurred are lost. No receipts are written.
- **No automatic resume.** The operator must re-initiate a new replay run for
  the remaining events. There is no resume mechanism, no replay run audit
  table, and no persistent queue of pending replay events.
- **Partial results are persisted.** The receipts from the completed portion
  of the interrupted run are queryable via `source='replay'` and the
  `replay_run_id`. This allows the operator to determine which events were
  successfully replayed before the interruption.

To assess the scope of an interrupted replay:

```sql
-- What did the interrupted replay complete?
SELECT event_id, target_adapter, status
FROM delivery_receipts
WHERE source = 'replay' AND replay_run_id = '<interrupted_run_id>';

-- What events still need replay? (events in scope with no replay receipt)
SELECT e.event_id FROM canonical_events e
LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
  AND r.source = 'replay' AND r.replay_run_id = '<interrupted_run_id>'
WHERE r.event_id IS NULL
ORDER BY e.created_at ASC;
```

### 4.6 Replay Under Bridge Conditions

When replaying events that were originally delivered through a live bridge
(i.e., events that already have `source='live'` receipts), the following
applies:

- **Both live and replay receipts exist for the same `event_id`.** This is
  expected and correct. The `source` field distinguishes them: `source='live'`
  for the original delivery, `source='replay'` for the replay delivery.
- **Duplicate outbound messages occur.** The replay engine does not check
  for existing live receipts before re-delivering. BEST_EFFORT sends real
  messages regardless.
- **The same event may have multiple replay receipt sets** if BEST_EFFORT was
  run more than once. Each set has a different `replay_run_id`.
- **Native refs are shared.** Live and replay deliveries of the same event
  both create native refs. The native ref schema does not carry `source` or
  `replay_run_id` — correlate via the receipt's `event_id`.

This scenario is common during bridge recovery: the operator replays events
that were partially delivered before a crash, resulting in duplicate receipts
that together form the full delivery history.


## 5. Duplicate Risk Assessment

Replay does not deduplicate. This is by design — MEDRE does not have a
deduplication layer. Every `BEST_EFFORT` replay produces new outbound messages
on all matched targets.

### 5.1 When Duplicates Occur

| Scenario | Risk level | Why |
|----------|-----------|-----|
| Replaying events that were never delivered | Low | No prior delivery exists |
| Replaying events that were delivered before a crash | Medium | Some events may have been delivered before crash but have no receipt |
| Replaying events that have existing `sent` receipts | High | Events will be delivered again, producing duplicate messages |
| Multiple BEST_EFFORT replays of the same events | High | Each run produces new deliveries |

### 5.2 Assessing Risk Before Replay

```sql
-- How many events have existing live receipts (duplicate risk)?
SELECT COUNT(DISTINCT e.event_id)
FROM canonical_events e
JOIN delivery_receipts r ON e.event_id = r.event_id
WHERE r.source = 'live'
  AND r.status = 'sent';

-- Which events have both live and replay receipts already?
SELECT e.event_id,
  COUNT(CASE WHEN r.source = 'live' THEN 1 END) AS live_count,
  COUNT(CASE WHEN r.source = 'replay' THEN 1 END) AS replay_count
FROM canonical_events e
JOIN delivery_receipts r ON e.event_id = r.event_id
GROUP BY e.event_id
HAVING live_count > 0 AND replay_count > 0;

-- Events with NO receipts (safe to replay)
SELECT e.event_id, e.source_adapter, e.created_at
FROM canonical_events e
LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
WHERE r.event_id IS NULL
ORDER BY e.created_at DESC;
```

### 5.3 Mitigation Strategies

1. **Always DRY_RUN first.** Review route attributions before BEST_EFFORT.
2. **Query existing receipts.** Use the SQL above to assess how many events
   already have `sent` receipts.
3. **Scope replay narrowly.** Use `--event <id>` or `--from`/`--to` to replay
   only the events that need it.
4. **Accept duplicates for radio transports.** Meshtastic and MeshCore treat
   duplicate sends as normal operational practice. Duplicates are expected.
5. **Warn for Matrix.** Matrix duplicates are rarer and more visible to users.
   Consider whether replaying Matrix-targeted events is necessary.


## 6. Recovery Scenarios

### 6.1 Recovering Orphaned Events After Crash

```bash
# Step 1: Find orphaned events
sqlite3 {state}/medre.sqlite "
  SELECT COUNT(*) FROM canonical_events e
  LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
  WHERE r.event_id IS NULL;
"

# Step 2: Preview replay with DRY_RUN
medre replay --mode DRY_RUN --config my-bridge.toml

# Step 3: If preview looks correct, replay with BEST_EFFORT
medre replay --mode BEST_EFFORT --config my-bridge.toml

# Step 4: Verify replay results
medre trace replay <replay_run_id> --config my-bridge.toml
```

See [Bridge Recovery](bridge-recovery.md) for the full crash recovery workflow.

### 6.2 Verifying Route Changes Against Historical Data

```bash
# Step 1: After changing route config, re-route historical events
medre replay --mode RE_ROUTE --config my-bridge.toml

# Step 2: Review route attributions — which events now match different routes?

# Step 3: If re-delivery is warranted, use DRY_RUN to preview
medre replay --mode DRY_RUN --config my-bridge.toml

# Step 4: Proceed to BEST_EFFORT only if needed
```

### 6.3 Replaying a Single Event

```bash
# Target a specific event
medre replay --mode BEST_EFFORT --event evt_abc123 --config my-bridge.toml

# Verify the result
medre trace event evt_abc123 --config my-bridge.toml
```


## 7. Caveats

1. **No deduplication.** Each BEST_EFFORT replay produces new outbound messages.
   Multiple replays of the same event produce additional receipts each time.
   This is by design.

2. **No automatic retry.** Replay is a one-shot operator action. It is not a
   durable job, not automatically retried, and not resumed after crash. See
   Contract 55 §6.

3. **No active supervision.** There is no background scheduler running replays.
   All replay operations are operator-initiated.

4. **ReplaySummary is in-memory only.** The summary is not persisted. Only
   BEST_EFFORT mode produces storage receipts. DRY_RUN and RE_ROUTE results
   exist only in the CLI output.

 5. **Counters reset on restart.** Process-local counters (capacity_rejections)
     reset on every runtime restart. Replay results should be
    verified via SQLite queries, not counters.

 6. **Single-machine only.** Replay operates on the local SQLite database.
    There is no distributed replay or cross-instance coordination.

 7. **No delivery order guarantee.** Replay processes events in storage order
    but delivery concurrency means outbound messages may arrive out of order.

 8. **Radio transports are fire-and-forget.** A `sent` receipt for a replayed
    event means the local radio accepted the packet, not that the remote node
    received it.

 9. **Shutdown during replay.** If the runtime shuts down during an active
    BEST_EFFORT replay, completed events produce receipts; remaining events are
    lost. No automatic resume. See §4.5.

10. **Pre-beta.** Replay modes, CLI flags, receipt schemas, and result shapes
    may change before beta. Always verify against the current code.


## 8. Replay and Route-Level Retry Interaction

When BEST_EFFORT replay delivers to a route that has `[routes.<id>.retry]`
enabled, transient delivery failures create **due retry receipts** in storage.
This section documents what happens and what operators should expect.

### 8.1 What Happens During Replay

1. BEST_EFFORT replay delivers the event through the normal pipeline
   (`deliver_to_targets` with `source="replay"`).
2. If the adapter raises a transient error and the route has retry enabled,
   the pipeline records a `DeliveryReceipt` with:
   - `source = "replay"` — origin attribution is preserved
   - `replay_run_id = <run_id>` — groups with the replay run
   - `status = "failed"` — delivery failed
   - `next_retry_at = <computed backoff>` — retry is scheduled
   - `retry_max_attempts`, `retry_backoff_base`, etc. — policy metadata
3. The `ReplayResult` for the deliver stage shows `"passed"` because the
   delivery was *attempted*; the individual target failure is recorded in
   the delivery outcome envelope, not as a replay error.

### 8.2 The Replay Command Does NOT Start the RetryWorker

The `medre replay` command calls `RuntimeBuilder.build()` but **never calls
`app.start()`**. The RetryWorker is only started by `app.start()` when
`[retry] enabled = true` in the config. This means:

- Due retry receipts created during replay sit in storage unprocessed.
- No automatic retry occurs during or after the replay command.
- The replay command exits after writing receipts; it does not poll or wait.

### 8.3 Later Runtime Start Will Process Replay-Created Retry Receipts

If the operator later starts the runtime normally (`medre run`) with
`[retry] enabled = true`, the RetryWorker **will** discover and process any
due retry receipts — including those created during replay. This produces:

- A new receipt with `source = "retry"` (not "replay")
- `parent_receipt_id` linking back to the replay-origin failure receipt
- `attempt_number` incremented from the replay receipt's value

This creates a **cross-source retry chain**: `source="replay"` →
`source="retry"`. The `source` field changes because the retry attempt
originates from the RetryWorker, not from the replay engine. The
`parent_receipt_id` preserves the linkage.

### 8.4 Duplicate-Risk Implications

| Scenario | Risk | Explanation |
|----------|------|-------------|
| Replay succeeds, route has retry | No retry risk | Success receipt has `next_retry_at = null` |
| Replay fails transiently, route has retry, worker never starts | No retry delivery | Receipt has `next_retry_at` but no worker processes it |
| Replay fails transiently, route has retry, runtime starts later | **Retry delivery occurs** | Worker discovers the due receipt and re-attempts delivery |
| Replay fails transiently, route has retry, runtime starts, retry also fails transiently | **Multiple retries possible** | Worker retries up to `max_attempts`, each producing a receipt |

### 8.5 Recommended Operator Procedure

1. **Before BEST_EFFORT replay**, run `DRY_RUN` to verify route matching.
2. **Check which routes have retry enabled** in your config:
   ```bash
   grep -A5 '\[routes\.' my-bridge.toml | grep -B1 'retry'
   ```
3. **After BEST_EFFORT replay**, check for replay-created retry receipts:
   ```sql
   SELECT receipt_id, event_id, status, next_retry_at, source, replay_run_id
   FROM delivery_receipts
   WHERE source = 'replay' AND next_retry_at IS NOT NULL;
   ```
4. **If you see replay-created retry receipts**, decide:
   - **Accept**: Let the RetryWorker process them on next runtime start.
   - **Clear**: Manually clear `next_retry_at` to prevent automatic retry:
     ```sql
     UPDATE delivery_receipts SET next_retry_at = NULL
     WHERE source = 'replay' AND next_retry_at IS NOT NULL;
     ```
5. **Never start the runtime with retry enabled** immediately after a
   BEST_EFFORT replay without checking for replay-created retry receipts
   if duplicate delivery is unacceptable.


## 9. Cross-References

- [Event Tracing](event-tracing.md) — tracing events and replay runs through
  the pipeline lifecycle, timeline reports, SQL queries.
- [Bridge Recovery](bridge-recovery.md) — crash recovery procedures, orphan
  detection, recovery decision tree.
- [Bridge Operation](bridge-operation.md) — replay and route attribution,
  duplicate-send realities, per-transport semantics.
- [Bridge Failure Drills](bridge-failure-drills.md) — replay failure drills
  (duplicate risk, capacity exceeded).
- [Bridge Evidence Bundle](bridge-evidence-bundle.md) — evidence collection
  workflow using `medre evidence`.
- [Runtime Operation](runtime-operation.md) — diagnostics, inspect, exit codes,
  persistence semantics.
- [Runtime Supervision](runtime-supervision.md) — replay and crash interaction,
  using replay for crash recovery.

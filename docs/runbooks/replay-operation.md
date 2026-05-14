# Replay Operation Runbook

> Last updated: 2026-05-14
> Scope: Operator-facing replay workflow — modes, commands, result interpretation, duplicate risk
> Status: Pre-beta. Not production. Replay is a one-shot operator action, not a durable job.
> Prerequisites: medre installed with `[dev]` extras, runtime previously run with `[storage] backend = "sqlite"`.

This runbook describes how to operate the MEDRE replay engine: re-processing
historical events through the pipeline. Replay is the primary mechanism for
recovering orphaned events after a crash, verifying route changes against
historical data, and re-delivering events that were missed.

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
| `BEST_EFFORT` | Yes | Yes | Real adapter delivery | Re-deliver historical events. Produces storage receipts with `source='replay'`. Use with caution — this sends real messages. |

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
medre inspect receipts --replay-run replay_xyz789 --config my-bridge.toml
```

Or trace the full replay run:

```bash
medre trace replay replay_xyz789 --config my-bridge.toml
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

### 4.2 Querying Replay Receipts

```bash
# All receipts from a specific replay run
medre inspect receipts --replay-run <run_id> --config my-bridge.toml

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
| `source` | **Origin classification** — was this delivery from the live pipeline or from a replay run? | `"live"` or `"replay"` |
| `replay_run_id` | **Run grouping** — which specific replay invocation produced this receipt? | Unique run ID string when `source == "replay"`, `null` when `source == "live"` |

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
run. There is no active retry scheduler; replay is a one-shot operator
action. BEST_EFFORT sends real messages.


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

5. **Counters reset on restart.** Process-local counters (delivery_timeouts,
   replay_timeouts) reset on every runtime restart. Replay results should be
   verified via SQLite queries, not counters.

6. **Single-machine only.** Replay operates on the local SQLite database.
   There is no distributed replay or cross-instance coordination.

7. **No delivery order guarantee.** Replay processes events in storage order
   but delivery concurrency means outbound messages may arrive out of order.

8. **Radio transports are fire-and-forget.** A `sent` receipt for a replayed
   event means the local radio accepted the packet, not that the remote node
   received it.

9. **Pre-beta.** Replay modes, CLI flags, receipt schemas, and result shapes
   may change before beta. Always verify against the current code.


## 8. Cross-References

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

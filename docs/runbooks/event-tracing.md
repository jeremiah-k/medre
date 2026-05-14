# Event Tracing Runbook

> Last updated: 2026-05-14
> Scope: Operator guide for tracing events through the MEDRE pipeline lifecycle
> Status: Pre-beta. Not production. All tracing commands require SQLite storage.
> Prerequisites: medre installed, runtime previously run with `[storage] backend = "sqlite"`.

This runbook describes how to trace an event from ingestion through delivery,
inspect the timeline of what happened, and diagnose where things went wrong.
Tracing works against the persistent SQLite database — it cannot trace events
that only existed in memory.

**What tracing shows you:**

- The canonical event (source adapter, event kind, payload, timestamp).
- All delivery receipts for that event (status, target adapter, route, attempt
  number, failure kind, retry lineage).
- Native message refs mapping transport-native IDs to the canonical event.
- Replay receipts (if the event was replayed) with `source='replay'` attribution.
- A timeline report reconstructing the event's lifecycle from stored evidence.

**What tracing does NOT show you:**

- In-flight delivery state (lost on crash or shutdown — no receipt exists).
- Process-local counters (delivery_timeouts, RouteStats — reset on restart).
- Adapter health at the time of the event (not stored).
- Transport-level delivery confirmation beyond what the adapter reported.


## 1. Commands

### 1.1 Trace a Single Event

```bash
medre trace event <event_id> --config my-bridge.toml
```

This command queries the SQLite database for the canonical event, all delivery
receipts, and any native message refs associated with `<event_id>`. It prints a
timeline report to stdout.

**Output shape (JSON with `--json`):**

```json
{
  "event": {
    "event_id": "evt_abc123",
    "source_adapter": "bot",
    "event_kind": "message.text",
    "payload": { "text": "hello" },
    "created_at": "2026-05-14T10:30:00Z",
    "metadata": {
      "routing": {
        "route_trace": ["bot-to-radio"]
      }
    }
  },
  "receipts": [
    {
      "receipt_id": "rcpt_001",
      "target_adapter": "radio",
      "route_id": "bot-to-radio",
      "status": "sent",
      "failure_kind": null,
      "attempt_number": 1,
      "parent_receipt_id": null,
      "source": "live",
      "replay_run_id": null,
      "created_at": "2026-05-14T10:30:00.050Z"
    }
  ],
  "native_refs": [
    {
      "native_message_id": "fake_123",
      "native_channel_id": "general",
      "canonical_event_id": "evt_abc123",
      "adapter": "radio",
      "direction": "outbound"
    }
  ],
  "timeline": [
    {
      "timestamp": "2026-05-14T10:30:00.000Z",
      "phase": "ingestion",
      "description": "Event stored from source adapter 'bot'"
    },
    {
      "timestamp": "2026-05-14T10:30:00.010Z",
      "phase": "routing",
      "description": "Matched route 'bot-to-radio', planned delivery to adapter 'radio'"
    },
    {
      "timestamp": "2026-05-14T10:30:00.050Z",
      "phase": "delivery",
      "description": "Delivery to adapter 'radio': status=sent"
    }
  ]
}
```

### 1.2 Trace a Replay Run

```bash
medre trace replay <run_id> --config my-bridge.toml
```

This command queries all delivery receipts with `replay_run_id == <run_id>` and
reconstructs the replay timeline.

**Output shape (JSON with `--json`):**

```json
{
  "replay_run_id": "replay_xyz789",
  "receipts": [
    {
      "receipt_id": "rcpt_r1",
      "event_id": "evt_abc123",
      "target_adapter": "radio",
      "route_id": "bot-to-radio",
      "status": "sent",
      "source": "replay",
      "replay_run_id": "replay_xyz789",
      "attempt_number": 1,
      "created_at": "2026-05-14T11:00:00Z"
    }
  ],
  "summary": {
    "total_receipts": 1,
    "sent": 1,
    "failed": 0,
    "events_covered": 1,
    "duplicate_risk": "possible — check source='replay' receipts against live receipts for same events"
  }
}
```

### 1.3 Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Event/run found, report printed |
| 2 | Config error, no SQLite backend, or database not found |
| 1 | Event/run ID not found in the database |


## 2. Timeline Report Interpretation

### 2.1 Timeline Phases

The timeline report reconstructs these phases from stored evidence:

| Phase | Source | Description |
|-------|--------|-------------|
| `ingestion` | `canonical_events` table | Event stored from a source adapter |
| `routing` | Inferred from receipt `route_id` | Route matched, delivery planned |
| `delivery` | `delivery_receipts` table | Adapter delivery attempted; status recorded |
| `retry` | `parent_receipt_id` chain | Transient failure triggered a retry |
| `replay` | `source='replay'` receipts | Event re-delivered via replay engine |

### 2.2 Interpreting Timeline Gaps

Timeline phases are reconstructed from stored data. Gaps indicate:

- **No routing phase:** The event had no matching routes. Check route
  configuration and event source adapter.
- **No delivery phase:** Delivery was attempted but no receipt was written.
  This happens when the runtime crashes mid-delivery (in-flight delivery lost)
  or when delivery was skipped (loop prevention, capacity exceeded without a
  receipt).
- **Multiple delivery phases, same target:** Retry chain. Check
  `attempt_number` and `parent_receipt_id` to trace the lineage.
- **Multiple delivery phases, different targets:** Fan-out. Multiple routes
  or multiple targets matched the event.

### 2.3 Drill Timeline Evidence

The evidence bundle includes timeline evidence for specific failure scenarios
when `--event` or `--replay-run` is provided. Available drill timeline evidence
types:

| Evidence type | When it appears | What it tells you |
|--------------|----------------|-------------------|
| `replay_duplicate_risk` | BEST_EFFORT replay produces receipts for events that already have live receipts | Multiple outbound deliveries for the same event. Use `source` field to distinguish live from replay. |
| `adapter_transient_failure` | Transient adapter failure triggers retry chain | Check `attempt_number` progression and `parent_receipt_id` lineage. Each retry is a separate receipt. |
| `shutdown_rejection` | In-flight delivery cancelled during runtime shutdown | No receipt is written for rejected deliveries. Check `delivery_rejections` counter (process-local, lost on restart). |
| `degraded_live_health` | Adapter reports degraded/failed health after startup | Event may have been delivered to a degraded adapter. Check the adapter's `.error` field in live health output. |

To drill into these scenarios:

```bash
# Run a specific drill with persistent storage
PYTHONPATH=src medre smoke --drill replay_duplicate_risk \
  --storage-path /tmp/medre-trace.db --json

# Then trace the event
medre trace event <event_id> --config my-bridge.toml
# Or trace using the drill's SQLite directly
medre inspect receipts --event <event_id> --config my-bridge.toml
```


## 3. SQL Queries for Deep Tracing

When `medre trace` doesn't provide enough detail, query the SQLite database
directly. All queries assume `[storage] backend = "sqlite"` and a valid
database path.

### 3.1 Full Event History

```sql
-- All receipts for a specific event, including retry lineage
SELECT
  r.receipt_id,
  r.status,
  r.failure_kind,
  r.target_adapter,
  r.route_id,
  r.attempt_number,
  r.parent_receipt_id,
  r.source,
  r.replay_run_id,
  r.created_at
FROM delivery_receipts r
WHERE r.event_id = 'evt_abc123'
ORDER BY r.created_at ASC;
```

### 3.2 Retry Chain Reconstruction

```sql
-- Follow parent_receipt_id chain for a failed delivery
WITH RECURSIVE retry_chain AS (
  SELECT * FROM delivery_receipts WHERE receipt_id = 'rcpt_001'
  UNION ALL
  SELECT r.* FROM delivery_receipts r
  JOIN retry_chain rc ON r.parent_receipt_id = rc.receipt_id
)
SELECT receipt_id, attempt_number, status, failure_kind, created_at
FROM retry_chain
ORDER BY attempt_number ASC;
```

### 3.3 Orphaned Events (Stored but Never Delivered)

```sql
-- Events with no delivery receipts at all
SELECT
  e.event_id,
  e.source_adapter,
  e.event_kind,
  e.created_at
FROM canonical_events e
LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
WHERE r.event_id IS NULL
ORDER BY e.created_at DESC
LIMIT 50;
```

### 3.4 Replay Duplicate Risk Assessment

```sql
-- Events with both live and replay receipts (duplicate delivery risk)
SELECT
  e.event_id,
  COUNT(CASE WHEN r.source = 'live' THEN 1 END) AS live_deliveries,
  COUNT(CASE WHEN r.source = 'replay' THEN 1 END) AS replay_deliveries,
  GROUP_CONCAT(DISTINCT r.replay_run_id) AS replay_runs
FROM canonical_events e
JOIN delivery_receipts r ON e.event_id = r.event_id
GROUP BY e.event_id
HAVING live_deliveries > 0 AND replay_deliveries > 0
ORDER BY e.created_at DESC;
```

### 3.5 Events by Source Adapter and Time Range

```sql
-- Trace all events from a specific source in a time window
SELECT
  e.event_id,
  e.event_kind,
  e.created_at,
  (SELECT COUNT(*) FROM delivery_receipts r WHERE r.event_id = e.event_id) AS receipt_count,
  (SELECT COUNT(*) FROM delivery_receipts r WHERE r.event_id = e.event_id AND r.status = 'sent') AS sent_count,
  (SELECT COUNT(*) FROM delivery_receipts r WHERE r.event_id = e.event_id AND r.status = 'failed') AS failed_count
FROM canonical_events e
WHERE e.source_adapter = 'bot'
  AND e.created_at BETWEEN '2026-05-14T10:00:00Z' AND '2026-05-14T12:00:00Z'
ORDER BY e.created_at ASC;
```

### 3.6 Route-Level Delivery Summary

```sql
-- Delivery outcomes grouped by route
SELECT
  r.route_id,
  r.status,
  COUNT(*) AS count
FROM delivery_receipts r
GROUP BY r.route_id, r.status
ORDER BY r.route_id, r.status;
```


## 4. Tracing Workflows

### 4.1 "Did My Message Get Delivered?"

```bash
# Step 1: Trace the event
medre trace event <event_id> --config my-bridge.toml

# Step 2: If no receipts exist, check for orphans
sqlite3 {state}/medre.sqlite "
  SELECT e.event_id, e.source_adapter, e.created_at
  FROM canonical_events e
  LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
  WHERE r.event_id IS NULL AND e.event_id = '<event_id>';
"

# Step 3: If orphaned, consider replay. See [Replay Operation](replay-operation.md).
```

### 4.2 "Why Did Delivery Fail?"

```bash
# Step 1: Trace the event to get receipt details
medre trace event <event_id> --config my-bridge.toml --json

# Step 2: Look at failure_kind in the receipts
# Common failure kinds:
#   RENDERER_FAILURE     — event kind not handled by any renderer (permanent)
#   ADAPTER_PERMANENT    — adapter determined failure is not recoverable
#   ADAPTER_TRANSIENT    — temporary failure; check retry chain
#   DEADLINE_EXCEEDED    — delivery plan's absolute deadline passed
#   delivery_capacity_exceeded — too many concurrent deliveries
#   delivery_rejected_shutdown  — runtime shutting down

# Step 3: For transient failures, trace the retry chain
sqlite3 {state}/medre.sqlite "
  WITH RECURSIVE chain AS (
    SELECT * FROM delivery_receipts WHERE event_id = '<event_id>'
    UNION ALL
    SELECT r.* FROM delivery_receipts r
    JOIN chain c ON r.parent_receipt_id = c.receipt_id
  )
  SELECT receipt_id, attempt_number, status, failure_kind
  FROM chain ORDER BY attempt_number;
"
```

### 4.3 "Was This Event Replayed?"

```bash
# Step 1: Check for replay receipts
sqlite3 {state}/medre.sqlite "
  SELECT receipt_id, replay_run_id, status, target_adapter
  FROM delivery_receipts
  WHERE event_id = '<event_id>' AND source = 'replay';
"

# Step 2: If replay receipts exist, trace the full replay run
medre trace replay <replay_run_id> --config my-bridge.toml

# Step 3: Assess duplicate risk by counting all receipts for this event
sqlite3 {state}/medre.sqlite "
  SELECT source, COUNT(*) FROM delivery_receipts
  WHERE event_id = '<event_id>'
  GROUP BY source;
"
```


## 5. Limitations

1. **No in-flight visibility.** Events that are currently being delivered have
   no receipt yet. If the runtime crashes mid-delivery, no receipt is written.
   The event exists in `canonical_events` but has no corresponding entry in
   `delivery_receipts`.

2. **Counters reset on restart.** Process-local counters (delivery_timeouts,
   delivery_rejections, RouteStats) are not stored in SQLite. Timeline reports
   cannot reference counter values from before the last restart.

3. **No adapter health history.** Adapter health states at the time of delivery
   are not stored. The timeline cannot tell you whether the adapter was
   `healthy` or `degraded` when it accepted or rejected a delivery.

4. **Timeline is reconstructed, not recorded.** The timeline is assembled from
   stored events and receipts after the fact. MEDRE does not write a separate
   timeline log. Timestamps are the `created_at` values from the respective
   tables.

5. **Requires SQLite.** `medre trace` requires persistent SQLite storage.
   Events that only existed in memory (`backend = "memory"`) cannot be traced.

6. **Single-machine only.** Tracing covers events in the local SQLite database
   only. There is no distributed tracing across multiple MEDRE instances.

7. **No deduplication guidance.** The trace report shows you duplicate receipts
   but does not tell you which delivery actually reached its destination.
   Radio transports are fire-and-forget — `sent` means local acceptance, not
   remote receipt.

8. **Pre-beta.** CLI command shapes, output formats, and timeline phases may
   change before beta. Always verify against the current code.


## 6. Cross-References

- [Replay Operation](replay-operation.md) — replay modes, command shape,
  receipt interpretation, duplicate risk assessment.
- [Bridge Recovery](bridge-recovery.md) — crash recovery procedures, orphan
  detection, recovery decision tree.
- [Bridge Failure Drills](bridge-failure-drills.md) — per-failure drill
  interpretation and inspect follow-up.
- [Bridge Evidence Bundle](bridge-evidence-bundle.md) — collecting smoke, drill,
  and inspect outputs as a single evidence package.
- [Bridge Operation](bridge-operation.md) — delivery-state discipline,
  per-transport semantics, persistence of bridge state.
- [Runtime Operation](runtime-operation.md) — diagnostics, inspect, exit codes,
  persistence semantics.
- [Runtime Supervision](runtime-supervision.md) — crash recovery procedures,
  persistence expectations, troubleshooting workflows.

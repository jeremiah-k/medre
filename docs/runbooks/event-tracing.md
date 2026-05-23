# Event Tracing Runbook

> Last updated: 2026-05-16
> Scope: Operator guide for tracing events through the MEDRE pipeline lifecycle
> Status: Pre-beta. Not production. All tracing commands require SQLite storage.
> Prerequisites: medre installed, runtime previously run with `[storage] backend = "sqlite"`.

This runbook describes how to trace an event from ingestion through delivery,
inspect the timeline of what happened, and diagnose where things went wrong.
Tracing works against the persistent SQLite database, it cannot trace events
that only existed in memory.

**Inspect-first investigation path:** For day-to-day incident investigation,
`medre inspect event --timeline` is the preferred operator command. It
produces the same enriched timeline output as `medre trace event` within a
unified command surface. `medre inspect event --evidence` covers
`medre evidence --event`, and `medre inspect event --recovery` covers
`medre recover --event`. The `trace` command documented here is a specialized
command that remains available for standalone timeline output and scripting.
See the [Alpha Walkthrough](alpha-walkthrough.md) for the preferred product
path, and [Operator Command Surface](../architecture/operator-command-surface.md)
for the full command classification.

**What tracing shows you:**

- The canonical event (source adapter, event kind, payload, timestamp).
- All delivery receipts for that event (status, target adapter, route, attempt
  number, failure kind, retry lineage).
- Native message refs mapping transport-native IDs to the canonical event.
- Replay receipts (if the event was replayed) with `source='replay'` attribution
  and `replay_run_id` grouping. Trace output includes both live and replay
  receipts for the same `event_id` when applicable.
- Retry receipts with `source='retry'` attribution, `parent_receipt_id` linking to the original failure, and `attempt_number` showing the retry count.
- A timeline report reconstructing the event's lifecycle from stored evidence,
  showing both original delivery path and replay delivery path for replayed events.

**What tracing does NOT show you:**

- In-flight delivery state (lost on crash or shutdown — no receipt exists).
- Process-local counters (capacity_rejections, RouteStats — reset on restart).
- Adapter health at the time of the event (not stored).
- Transport-level delivery confirmation beyond what the adapter reported.

## 1. Commands

### 1.1 Trace a Single Event

```bash
# Human-readable output (default):
medre trace event <event_id> --config my-bridge.toml

# JSON for programmatic inspection:
medre trace event <event_id> --config my-bridge.toml --json
```

This command queries the SQLite database for the canonical event, all delivery
receipts, and any native message refs associated with `<event_id>`.

**Human-readable output (default, no `--json`):**

Without `--json`, the command prints a compact timeline summary to stdout:

```yaml
Event timeline: evt_abc123
  Kind:    message.text
  Source:  bot
  Entries: 5

  2026-05-14T10:30:00Z  [event] message.text from bot
  2026-05-14T10:30:00.010Z  [native_ref] outbound via radio: fake_123
  2026-05-14T10:30:00.050Z  [receipt] sent -> radio (attempt 1)
  2026-05-14T10:35:00Z  [receipt] failed -> radio (attempt 2)
  2026-05-14T10:35:10Z  [receipt] sent -> radio (attempt 3)
```

Entry types: `[event]`, `[receipt]`, `[native_ref]`, `[relation]`. For
receipts, the status and target adapter are shown alongside the attempt number.
For native refs, the direction, adapter, and native message ID are shown.

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

### 1.2 Receipt and Native-Ref Field Reference

The trace output includes receipts and native refs with the following fields.
Understanding these fields is essential for interpreting trace results and for
cross-referencing with evidence bundles ([Bridge Evidence Bundle](bridge-evidence-bundle.md)).

**DeliveryReceipt fields:**

| Field               | Type          | Description                                                                                                                                                          |
| ------------------- | ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `receipt_id`        | `str`         | Unique receipt identifier                                                                                                                                            |
| `event_id`          | `str`         | Canonical event this receipt belongs to                                                                                                                              |
| `target_adapter`    | `str`         | Adapter that received the delivery                                                                                                                                   |
| `route_id`          | `str`         | Route that matched the event                                                                                                                                         |
| `status`            | `str`         | `"sent"`, `"failed"`, or `"skipped"`                                                                                                                                 |
| `failure_kind`      | `str or null` | `"RENDERER_FAILURE"`, `"ADAPTER_PERMANENT"`, `"ADAPTER_TRANSIENT"`, `"DEADLINE_EXCEEDED"`, `"delivery_capacity_exceeded"`, `"delivery_rejected_shutdown"`, or `null` |
| `attempt_number`    | `int`         | 1 for first attempt, increments on retry                                                                                                                             |
| `parent_receipt_id` | `str or null` | Links to the previous receipt in a retry chain                                                                                                                       |
| `source`            | `str`         | `"live"` for original delivery, `"retry"` for RetryWorker-attempted delivery, `"replay"` for replay-attributed delivery                                              |
| `replay_run_id`     | `str or null` | Unique run ID when `source == "replay"`; groups all receipts from one replay run                                                                                     |
| `created_at`        | `str`         | ISO-8601 timestamp of receipt creation                                                                                                                               |

**NativeMessageRef fields:**

| Field                | Type          | Description                                                                    |
| -------------------- | ------------- | ------------------------------------------------------------------------------ |
| `native_message_id`  | `str`         | Transport-native message ID (e.g., Matrix event ID, Meshtastic packet ID)      |
| `native_channel_id`  | `str or null` | Transport-native channel or room ID                                            |
| `canonical_event_id` | `str`         | Links back to the canonical event                                              |
| `adapter`            | `str`         | Adapter that produced this mapping                                             |
| `direction`          | `str`         | `"inbound"` (ingested from transport) or `"outbound"` (delivered to transport) |

**Key distinction:** `source='live'` receipts are from normal pipeline
delivery. `source='retry'` receipts are from the RetryWorker re-attempting a transient failure (same delivery lineage, linked via `parent_receipt_id`). `source='replay'` receipts are from operator-initiated replay
(see [Replay Operation](replay-operation.md)). The `replay_run_id` field
groups all receipts from a single replay invocation, enabling audit of
which events were re-delivered in which run.

**Caveat:** Traceability is not deduplication. The trace report shows all
receipts including duplicates from replay, but cannot tell you which delivery
actually reached the remote side. Radio transports are fire-and-forget —
`sent` means local acceptance, not remote receipt. There is no final ACK
guarantee.

### 1.3 Trace a Replay Run

```bash
# Human-readable output (default):
medre trace replay <run_id> --config my-bridge.toml

# JSON for programmatic inspection:
medre trace replay <run_id> --config my-bridge.toml --json
```

This command queries all delivery receipts with `replay_run_id == <run_id>` and
reconstructs the replay timeline.

**Human-readable output (default, no `--json`):**

Without `--json`, the command prints a compact replay summary:

```yaml
Replay timeline: replay_xyz789
  Status:  complete
  Receipts: 3
  Events:  2

  2026-05-14T11:00:00Z  [receipt] sent -> radio (event: evt_abc123)
  2026-05-14T11:00:00Z  [event_summary] message.text from bot
  2026-05-14T11:00:01Z  [receipt] failed -> matrix (event: evt_def456)
  2026-05-14T11:00:01Z  [event_summary] message.text from radio
```

Entry types: `[receipt]` (status, target, event ID) and `[event_summary]`
(event kind, source adapter).

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
  },
  "timeline": [
    {
      "timestamp": "2026-05-14T11:00:00.000Z",
      "phase": "replay",
      "description": "Event evt_abc123 re-delivered via replay run replay_xyz789 to adapter 'radio': status=sent"
    }
  ]
}
```

**Replay trace output fields:**

| Field                             | Description                                                                                                 |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `replay_run_id`                   | Unique identifier for this replay run — matches `replay_run_id` on individual receipts                      |
| `receipts`                        | All delivery receipts produced by this replay run. Each has `source='replay'` and the same `replay_run_id`. |
| `summary.total_receipts`          | Total number of delivery receipts in this run                                                               |
| `summary.sent` / `summary.failed` | Count of successful/failed deliveries                                                                       |
| `summary.events_covered`          | Number of distinct events re-delivered                                                                      |
| `summary.duplicate_risk`          | Always present. Describes the risk that events already had live deliveries.                                 |
| `timeline`                        | Ordered replay lifecycle events, same shape as event trace timeline. Phase is always `"replay"`.            |

**Caveat:** The `duplicate_risk` field is informational only — it does not
prevent duplicates. BEST_EFFORT replay sends real messages regardless. There
RetryWorker handles transient delivery failures automatically when enabled, but does not re-deliver orphaned events; replay is operator-initiated and one-shot.
The RetryWorker handles transient delivery failures automatically when enabled (receipts carry `source="retry"`), but does not re-deliver orphaned events.
Runtime events are process-local — if the process crashed during replay,
completed deliveries are preserved (receipts in SQLite) but remaining events
must be re-replayed manually.

### 1.4 Exit Codes

| Code | Meaning                                                |
| ---- | ------------------------------------------------------ |
| 0    | Event/run found, report printed                        |
| 2    | Config error, no SQLite backend, or database not found |
| 1    | Event/run ID not found in the database                 |

### 1.5 Classification Vocabulary

The `trace`, `recover`, and `evidence` commands share a consistent set of
failure-kind values for classifying delivery outcomes. This vocabulary is used
in receipt `failure_kind` fields, recovery runbook classification, and
incident summaries within evidence bundles.

**Failure-kind values:**

| Value                | Category    | Meaning                                                           |
| -------------------- | ----------- | ----------------------------------------------------------------- |
| `adapter_transient`  | retryable   | Temporary failure (timeout, connection reset); eligible for retry |
| `adapter_permanent`  | permanent   | Non-recoverable adapter error; no retry                           |
| `adapter_missing`    | permanent   | Target adapter not registered at delivery time                    |
| `renderer_failure`   | permanent   | No renderer handled the event kind                                |
| `planner_failure`    | permanent   | Delivery planning error                                           |
| `capacity_rejection` | operational | Delivery rejected due to capacity limits                          |
| `shutdown_rejection` | operational | Delivery rejected during runtime shutdown                         |
| `deadline_exceeded`  | operational | Delivery plan deadline passed                                     |
| `unknown`            | unknown     | Unclassifiable failure                                            |

**Recovery categories** (used by `medre recover --event`):

| Category      | Includes                                                                      | Recommended next step                                          |
| ------------- | ----------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `retryable`   | `adapter_transient`                                                           | `medre replay --mode DRY_RUN`, then `BEST_EFFORT`              |
| `permanent`   | `adapter_permanent`, `adapter_missing`, `renderer_failure`, `planner_failure` | `medre trace event` and `medre inspect receipts` for diagnosis |
| `operational` | `capacity_rejection`, `shutdown_rejection`, `deadline_exceeded`               | `medre diagnostics`, `medre config check`                      |
| `unknown`     | `unknown`                                                                     | `medre trace event` for manual investigation                   |

This vocabulary is the same across `medre trace event` (receipt display),
`medre recover --event` (runbook classification and recommended commands),
and `medre evidence --event` (incident_summary section in the storage
section). See [Bridge Recovery](bridge-recovery.md) for the recovery workflow
and [Bridge Evidence Bundle](bridge-evidence-bundle.md) for the incident
summary shape.

## 2. Timeline Report Interpretation

### 2.1 Timeline Phases

The timeline report reconstructs these phases from stored evidence:

| Phase       | Source                           | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| ----------- | -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ingestion` | `canonical_events` table         | Event stored from a source adapter                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `routing`   | Inferred from receipt `route_id` | Route matched, delivery planned                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `delivery`  | `delivery_receipts` table        | Adapter delivery attempted; status recorded                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `retry`     | `parent_receipt_id` chain        | Transient failure triggered a retry (opt-in — requires `RetryPolicy`). Retry receipts linked by `parent_receipt_id`. Each retry attempt increments `attempt_number`. Retry receipts are distinguishable by `source="retry"`. If the RetryWorker cannot acquire delivery capacity, no new receipt is created; the original failed receipt is rescheduled for the next cycle.                                                                                     |
| `replay`    | `source='replay'` receipts       | Event re-delivered via replay engine. Replay receipts are identifiable by `source="replay"` and `replay_run_id`. Trace for a replayed event shows the original delivery path plus the replay delivery path. If replay delivery fails transiently on a route with retry enabled, the replay receipt will have `next_retry_at` set — the RetryWorker will process it on next runtime start (producing a `source="retry"` receipt linked via `parent_receipt_id`). |

### 2.2 Interpreting Timeline Gaps

Timeline phases are reconstructed from stored data. Gaps indicate:

- **No routing phase:** The event had no matching routes. Check route
  configuration and event source adapter.
- **No delivery phase:** No receipt exists for the event. This happens when
  the runtime crashes mid-delivery (in-flight delivery lost) or when delivery
  was never attempted (planner failure). Note: loop prevention, capacity
  exceeded, and shutdown rejection produce `status="suppressed"` receipts —
  those appear as a delivery phase, not as a gap.
- **Multiple delivery phases, same target:** Retry chain. Check
  `attempt_number` and `parent_receipt_id` to trace the lineage. Retry receipts have `source="retry"` and link back to the original failure via `parent_receipt_id`.
- **Multiple delivery phases, different targets:** Fan-out. Multiple routes
  or multiple targets matched the event.
- **Both `live` and `replay` phases for the same event:** The event was
  originally delivered through the live pipeline and later re-delivered via
  replay. Both phases are shown in the timeline. Use the `source` field on
  receipts to distinguish them: `source='live'` for original delivery,
  `source='replay'` for replay delivery. The `replay_run_id` groups receipts
  from the same replay invocation.

### 2.3 Drill Timeline Evidence

The evidence bundle includes timeline evidence for specific failure scenarios
when `--event` or `--replay-run` is provided. Available drill timeline evidence
types:

| Evidence type               | When it appears                                                                 | What it tells you                                                                                                                |
| --------------------------- | ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `replay_duplicate_risk`     | BEST_EFFORT replay produces receipts for events that already have live receipts | Multiple outbound deliveries for the same event. Use `source` field to distinguish live from replay.                             |
| `adapter_transient_failure` | Transient adapter failure triggers retry chain                                  | Check `attempt_number` progression and `parent_receipt_id` lineage. Each retry is a separate receipt.                            |
| `shutdown_rejection`        | In-flight delivery cancelled during runtime shutdown                            | `status="suppressed"` receipt persisted. Check `outbound_failed` counter (process-local, lost on restart) for additional signal. |
| `degraded_live_health`      | Adapter reports degraded/failed health after startup                            | Event may have been delivered to a degraded adapter. Check the adapter's `.error` field in live health output.                   |

To drill into these scenarios:

```bash
# Run a specific drill with persistent storage
PYTHONPATH=src medre smoke --drill replay_duplicate_risk \
  --storage-path /tmp/medre-trace.db --json

# Then inspect the event (preferred path):
medre inspect event <event_id> --timeline --storage-path /tmp/medre-trace.db
# Or inspect receipts directly:
medre inspect receipts --event <event_id> --storage-path /tmp/medre-trace.db
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

### 3.5 Replay-Created Retry Receipts

```sql
-- Replay receipts that have scheduled retries (will be processed by RetryWorker
-- on next runtime start if [retry] enabled = true).
SELECT
  r.receipt_id,
  r.event_id,
  r.status,
  r.next_retry_at,
  r.replay_run_id,
  r.retry_max_attempts,
  r.retry_backoff_base
FROM delivery_receipts r
WHERE r.source = 'replay'
  AND r.next_retry_at IS NOT NULL
ORDER BY r.next_retry_at ASC;
```

### 3.6 Events by Source Adapter and Time Range

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

### 3.7 Route-Level Delivery Summary

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
# Step 1: Inspect the event (preferred path)
medre inspect event <event_id> --timeline --storage-path /path/to/medre.sqlite

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
# Step 1: Inspect the event with timeline (preferred path)
medre inspect event <event_id> --timeline --storage-path /path/to/medre.sqlite

# Or use the specialized trace command for standalone JSON output:
# medre trace event <event_id> --storage-path /path/to/medre.sqlite --json

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

# Step 2: If replay receipts exist, inspect the full replay run
medre inspect replay <replay_run_id> --storage-path /path/to/medre.sqlite

# Or use the specialized trace command:
# medre trace replay <replay_run_id> --storage-path /path/to/medre.sqlite

# Step 3: Assess duplicate risk by counting all receipts for this event
sqlite3 {state}/medre.sqlite "
  SELECT source, COUNT(*) FROM delivery_receipts
  WHERE event_id = '<event_id>'
  GROUP BY source;
"
```

## 5. Trace Integrity Notes

This section documents the ordering and timestamping guarantees that underpin
trace output. Understanding these properties is essential for interpreting
timeline reports during incident investigation.

### 5.1 All Timestamps Are in UTC

Every `created_at` and `timestamp` field stored in SQLite uses ISO-8601 with
UTC timezone (`+00:00` or `Z`). The `_now_iso()` helper in the storage layer
produces timestamps via `datetime.now(timezone.utc).isoformat()`. Trace output
presents these timestamps as-is. There is no local timezone conversion.

### 5.2 Receipt Ordering Is by Sequence Number

Delivery receipts are ordered by their `sequence` column, which is an
`INTEGER PRIMARY KEY AUTOINCREMENT`. This ordering is deterministic and stable
across restarts:

- `list_receipts_for_event`: `ORDER BY sequence ASC`
- `list_receipts_by_replay_run`: `ORDER BY sequence ASC`
- `list_receipts_for_plan`: `ORDER BY attempt_number ASC, sequence ASC`

The `sequence` value is assigned at INSERT time by SQLite's auto-increment.
It reflects the true chronological order of receipt creation. Using `sequence`
instead of `created_at` avoids non-determinism when two receipts share the
same timestamp (which happens frequently in fast pipelines).

### 5.3 Replay Receipts Are Interleaved with Live Receipts

Replay receipts are stored in the same `delivery_receipts` table as live
receipts. They are interleaved in `sequence` order. A replay receipt will have
a higher `sequence` value than any live receipt that preceded it, because it
was inserted later. The `source` column (`"live"`, `"retry"`, or `"replay"`) distinguishes
origin. The `replay_run_id` column groups receipts from the same replay
invocation.

This means a timeline for an event that was both live-delivered and
replay-delivered shows receipts in chronological insertion order, not grouped
by source. Use the `source` field to filter:

```bash
# Show only live receipts:
medre inspect receipts --event evt_abc123 --storage-path /path/to/medre.sqlite | \
  python3 -c "import json,sys; [print(json.dumps(r)) for r in json.load(sys.stdin) if r.get('source')=='live']"
```

### 5.4 Events Are Sorted by Timestamp Then event_id

The event query (`medre trace event`, event listing) uses
`ORDER BY timestamp ASC, event_id ASC`. The `event_id` tiebreaker ensures
deterministic ordering when two events share the same timestamp. Events are
not sorted by `created_at` (the storage insertion time) but by `timestamp`
(the event's logical occurrence time, set by the source adapter).

### 5.5 Source Column Distinguishes Origin

The `source` column on `delivery_receipts` has three values:

| Value      | Meaning                                                                                                                                                                                                                                 |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `"live"`   | Delivery produced by the normal runtime pipeline during operation                                                                                                                                                                       |
| `"retry"`  | Delivery produced by the RetryWorker re-attempting a transient failure (opt-in — requires `RetryPolicy` configured). Retry receipts carry `parent_receipt_id` linking to the original failure receipt and incremented `attempt_number`. |
| `"replay"` | Delivery produced by an operator-initiated replay run                                                                                                                                                                                   |

Do not use `replay_run_id IS NOT NULL` to detect replay receipts. Future
features may populate `replay_run_id` for non-replay purposes. The `source`
column is the authoritative filter.

### 5.6 replay_run_id Groups Receipts from the Same Replay Run

Each `medre replay --mode BEST_EFFORT` invocation generates a unique
`replay_run_id`. All receipts produced by that invocation share the same
`replay_run_id` and have `source='replay'`. Multiple BEST_EFFORT runs of the
same events produce different `replay_run_id` values. To audit a specific
replay run:

```bash
medre trace replay replay_xyz789 --storage-path /path/to/medre.sqlite
```

### 5.7 Native Refs Are Ordered by created_at Then id

Native message refs for an event are ordered by `ORDER BY created_at ASC, id ASC`.
The `id` tiebreaker ensures deterministic ordering when multiple refs share the
same timestamp.

## 6. Long-Run Lineage Notes

This section documents lineage properties verified by the long-run evidence
integrity test suite (`tests/test_longrun_evidence_integrity.py`).

### 6.1 Repeated Replays Produce Distinct run_ids

Each `medre replay --mode BEST_EFFORT` invocation produces a unique
`replay_run_id`, even when replaying the same event multiple times. Replaying
event `p1-0` three times produces three distinct sets of receipts, each grouped
by a different `replay_run_id`. The trace for the event shows one live receipt
per target plus one replay receipt per target per run:

```yaml
Event: p1-0 (message.created) from mx
  Live deliveries:    2  (mesh, mc)
  Replay run 001:     2  (mesh, mc)
  Replay run 002:     2  (mesh, mc)
  Replay run 003:     2  (mesh, mc)
  Total receipts:     8
```

### 6.2 Interleaved Live and Replay Identifiable by Sequence

When live events are injected between replay runs, the sequence ordering
reflects the true insertion order. Receipts are not grouped by source; they are
interleaved in sequence order. Use the `source` and `replay_run_id` fields to
distinguish origin:

```text
seq 1-160:    original live receipts
seq 161-170:  phase-A live receipts (5 new events * 2 targets)
seq 171-176:  replay receipts (3 events * 2 targets, run_id=interleave-001)
seq 177-186:  phase-C live receipts (5 new events * 2 targets)
```

The `source` column (`"live"`, `"retry"`, or `"replay"`) is the authoritative filter. Do
not use `replay_run_id IS NOT NULL` to detect replay receipts.

### 6.3 Evidence Bundle Matches Trace Ordering

The evidence bundle collects receipts in `sequence ASC` order. This ordering
matches the timeline produced by `medre trace event`. When an event has both
live and replay receipts, the evidence bundle lists them in insertion order, not
grouped by source. Use `source` and `replay_run_id` to filter within the
bundle.

### 6.4 Counter Resets Are Process-Local, Not Lineage Events

Process-local counters (`RuntimeAccounting`, `RouteStats`, `CapacityController`)
reset to zero on every restart. These resets do not produce receipts, events,
or native refs. They are not visible in trace output or evidence bundles.
After a restart, new receipts continue the auto-increment sequence from where
the previous session left off. Counter values in `medre diagnostics` reflect
only the current process, not cumulative history.

## 7. Limitations

1. **No in-flight visibility.** Events that are currently being delivered have
   no receipt yet. If the runtime crashes mid-delivery, no receipt is written.
   The event exists in `canonical_events` but has no corresponding entry in
   `delivery_receipts`.

2. **Counters reset on restart.** Process-local counters (capacity_rejections,
   outbound_failed, RouteStats) are not stored in SQLite. Timeline reports
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

## 8. Cross-References

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

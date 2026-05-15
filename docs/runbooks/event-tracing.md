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
- Replay receipts (if the event was replayed) with `source='replay'` attribution
  and `replay_run_id` grouping. Trace output includes both live and replay
  receipts for the same `event_id` when applicable.
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

```
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

| Field | Type | Description |
|-------|------|-------------|
| `receipt_id` | `str` | Unique receipt identifier |
| `event_id` | `str` | Canonical event this receipt belongs to |
| `target_adapter` | `str` | Adapter that received the delivery |
| `route_id` | `str` | Route that matched the event |
| `status` | `str` | `"sent"`, `"failed"`, or `"skipped"` |
| `failure_kind` | `str or null` | `"RENDERER_FAILURE"`, `"ADAPTER_PERMANENT"`, `"ADAPTER_TRANSIENT"`, `"DEADLINE_EXCEEDED"`, `"delivery_capacity_exceeded"`, `"delivery_rejected_shutdown"`, or `null` |
| `attempt_number` | `int` | 1 for first attempt, increments on retry |
| `parent_receipt_id` | `str or null` | Links to the previous receipt in a retry chain |
| `source` | `str` | `"live"` for original delivery, `"replay"` for replay-attributed delivery |
| `replay_run_id` | `str or null` | Unique run ID when `source == "replay"`; groups all receipts from one replay run |
| `created_at` | `str` | ISO-8601 timestamp of receipt creation |

**NativeMessageRef fields:**

| Field | Type | Description |
|-------|------|-------------|
| `native_message_id` | `str` | Transport-native message ID (e.g., Matrix event ID, Meshtastic packet ID) |
| `native_channel_id` | `str or null` | Transport-native channel or room ID |
| `canonical_event_id` | `str` | Links back to the canonical event |
| `adapter` | `str` | Adapter that produced this mapping |
| `direction` | `str` | `"inbound"` (ingested from transport) or `"outbound"` (delivered to transport) |

**Key distinction:** `source='live'` receipts are from normal pipeline
delivery. `source='replay'` receipts are from operator-initiated replay
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

```
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

| Field | Description |
|-------|-------------|
| `replay_run_id` | Unique identifier for this replay run — matches `replay_run_id` on individual receipts |
| `receipts` | All delivery receipts produced by this replay run. Each has `source='replay'` and the same `replay_run_id`. |
| `summary.total_receipts` | Total number of delivery receipts in this run |
| `summary.sent` / `summary.failed` | Count of successful/failed deliveries |
| `summary.events_covered` | Number of distinct events re-delivered |
| `summary.duplicate_risk` | Always present. Describes the risk that events already had live deliveries. |
| `timeline` | Ordered replay lifecycle events, same shape as event trace timeline. Phase is always `"replay"`. |

**Caveat:** The `duplicate_risk` field is informational only — it does not
prevent duplicates. BEST_EFFORT replay sends real messages regardless. There
is no active retry scheduler; replay is operator-initiated and one-shot.
Runtime events are process-local — if the process crashed during replay,
completed deliveries are preserved (receipts in SQLite) but remaining events
must be re-replayed manually.
```

### 1.4 Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Event/run found, report printed |
| 2 | Config error, no SQLite backend, or database not found |
| 1 | Event/run ID not found in the database |


### 1.5 Classification Vocabulary

The `trace`, `recover`, and `evidence` commands share a consistent set of
failure-kind values for classifying delivery outcomes. This vocabulary is used
in receipt `failure_kind` fields, recovery runbook classification, and
incident summaries within evidence bundles.

**Failure-kind values:**

| Value | Category | Meaning |
|-------|----------|---------|
| `adapter_transient` | retryable | Temporary failure (timeout, connection reset); eligible for retry |
| `adapter_permanent` | permanent | Non-recoverable adapter error; no retry |
| `adapter_missing` | permanent | Target adapter not registered at delivery time |
| `renderer_failure` | permanent | No renderer handled the event kind |
| `planner_failure` | permanent | Delivery planning error |
| `capacity_rejection` | operational | Delivery rejected due to capacity limits |
| `shutdown_rejection` | operational | Delivery rejected during runtime shutdown |
| `deadline_exceeded` | operational | Delivery plan deadline passed |
| `unknown` | unknown | Unclassifiable failure |

**Recovery categories** (used by `medre recover --event`):

| Category | Includes | Recommended next step |
|----------|----------|----------------------|
| `retryable` | `adapter_transient` | `medre replay --mode DRY_RUN`, then `BEST_EFFORT` |
| `permanent` | `adapter_permanent`, `adapter_missing`, `renderer_failure`, `planner_failure` | `medre trace event` and `medre inspect receipts` for diagnosis |
| `operational` | `capacity_rejection`, `shutdown_rejection`, `deadline_exceeded` | `medre diagnostics`, `medre config check` |
| `unknown` | `unknown` | `medre trace event` for manual investigation |

This vocabulary is the same across `medre trace event` (receipt display),
`medre recover --event` (runbook classification and recommended commands),
and `medre evidence --event` (incident_summary section in the storage
section). See [Bridge Recovery](bridge-recovery.md) for the recovery workflow
and [Bridge Evidence Bundle](bridge-evidence-bundle.md) for the incident
summary shape.


## 2. Timeline Report Interpretation

### 2.1 Timeline Phases

The timeline report reconstructs these phases from stored evidence:

| Phase | Source | Description |
|-------|--------|-------------|
| `ingestion` | `canonical_events` table | Event stored from a source adapter |
| `routing` | Inferred from receipt `route_id` | Route matched, delivery planned |
| `delivery` | `delivery_receipts` table | Adapter delivery attempted; status recorded |
| `retry` | `parent_receipt_id` chain | Transient failure triggered a retry |
| `replay` | `source='replay'` receipts | Event re-delivered via replay engine. Replay receipts are identifiable by `source="replay"` and `replay_run_id`. Trace for a replayed event shows the original delivery path plus the replay delivery path. |

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

| Evidence type | When it appears | What it tells you |
|--------------|----------------|-------------------|
| `replay_duplicate_risk` | BEST_EFFORT replay produces receipts for events that already have live receipts | Multiple outbound deliveries for the same event. Use `source` field to distinguish live from replay. |
| `adapter_transient_failure` | Transient adapter failure triggers retry chain | Check `attempt_number` progression and `parent_receipt_id` lineage. Each retry is a separate receipt. |
| `shutdown_rejection` | In-flight delivery cancelled during runtime shutdown | No receipt is written for rejected deliveries. Check `outbound_failed` counter (process-local, lost on restart). |
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
was inserted later. The `source` column (`"live"` vs `"replay"`) distinguishes
origin. The `replay_run_id` column groups receipts from the same replay
invocation.

This means a timeline for an event that was both live-delivered and
replay-delivered shows receipts in chronological insertion order, not grouped
by source. Use the `source` field to filter:

```bash
# Show only live receipts:
medre inspect receipts --event evt_abc123 --config my-bridge.toml | \
  python3 -c "import json,sys; [print(json.dumps(r)) for r in json.load(sys.stdin) if r.get('source')=='live']"
```

### 5.4 Events Are Sorted by Timestamp Then event_id

The event query (`medre trace event`, event listing) uses
`ORDER BY timestamp ASC, event_id ASC`. The `event_id` tiebreaker ensures
deterministic ordering when two events share the same timestamp. Events are
not sorted by `created_at` (the storage insertion time) but by `timestamp`
(the event's logical occurrence time, set by the source adapter).

### 5.5 Source Column Distinguishes Origin

The `source` column on `delivery_receipts` has two values:

| Value | Meaning |
|-------|---------|
| `"live"` | Delivery produced by the normal runtime pipeline during operation |
| `"replay"` | Delivery produced by an operator-initiated replay run |

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
medre trace replay replay_xyz789 --config my-bridge.toml
```

### 5.7 Native Refs Are Ordered by created_at Then id

Native message refs for an event are ordered by `ORDER BY created_at ASC, id ASC`.
The `id` tiebreaker ensures deterministic ordering when multiple refs share the
same timestamp.


## 6. Limitations

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


## 7. Cross-References

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

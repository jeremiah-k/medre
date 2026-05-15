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
- Rely on the RetryWorker to automatically retry `ADAPTER_TRANSIENT` failures (bounded by `RetryPolicy`).

**What recovery does NOT do:**

- Recover in-flight deliveries lost during crash (they are gone).
- Resume interrupted replay runs (they must be re-initiated).
- Automatically restart failed adapters (only full runtime restart).
- Deduplicate replay deliveries.
- Auto-retry permanent failures (`ADAPTER_PERMANENT`, `RENDERER_FAILURE`, `PLANNER_FAILURE`, `DEADLINE_EXCEEDED`). Only `ADAPTER_TRANSIENT` failures are auto-retried by the RetryWorker.


## 0. Complete Incident Workflow (End-to-End)

This section describes a coherent end-to-end workflow that chains smoke,
trace, inspect, recover, and evidence into a single incident response
procedure. Use this when you suspect events were lost or not delivered
correctly.

**Caveat:** Traceability is not deduplication. This workflow shows you what
happened and lets you re-deliver, but it cannot tell you whether a duplicate
reached the remote side. BEST_EFFORT sends real messages. There is no final
ACK guarantee for radio transports. The RetryWorker is an opt-in background task for transient failures, but replay is operator-initiated — every
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
- `source` field: `"live"` means original delivery, `"retry"` means automatic
  RetryWorker re-attempt, `"replay"` means re-delivered via replay engine.
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
replay produces new outbound messages. RetryWorker is separate from replay:
it is opt-in (disabled by default), and only retries `adapter_transient`
failures that have a `RetryPolicy` configured. Replay remains a manual
operator action.

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

If `failure_kind='adapter_transient'` and `next_retry_at` is set, the RetryWorker will automatically retry when the adapter recovers (after runtime restart). For other failure kinds, manual replay is required (see Section 8).

**Retry state interpretation for adapter failures:**

```sql
-- Retry pending: will be auto-retried by RetryWorker
SELECT receipt_id, event_id, attempt_number, next_retry_at
FROM delivery_receipts
WHERE target_adapter = '<adapter_id>'
  AND status = 'failed'
  AND failure_kind = 'adapter_transient'
  AND next_retry_at IS NOT NULL;

-- Retry exhausted (dead-lettered): manual replay required
SELECT receipt_id, event_id, attempt_number
FROM delivery_receipts
WHERE target_adapter = '<adapter_id>'
  AND status = 'dead_lettered';

-- Successful retries (delivery eventually succeeded)
SELECT r.receipt_id, r.event_id, r.attempt_number, r.status, r.parent_receipt_id
FROM delivery_receipts r
WHERE r.target_adapter = '<adapter_id>'
  AND r.source = 'retry'
  AND r.status IN ('sent', 'confirmed');
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
is **manual** — the RetryWorker is an opt-in background task for transient failures (receipts with `source="retry"`), but replay for orphaned events is operator-initiated, there is no background replay
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


## 6. Investigating Incidents

This section provides concrete commands and expected output for common
investigation patterns. Use these when you need to understand what happened
during an incident rather than recover from one.

### 6.1 Correlating event_id, replay_run_id, receipt_id, and Native IDs

Every delivery produces a chain of identifiers. The canonical event has an
`event_id`; each delivery attempt produces a `receipt_id` linked to that event;
and each adapter interaction may produce a native message ref linking the
canonical event to a transport-native ID (Matrix event ID, Meshtastic packet
ID, etc.).

```bash
# Start from a known event_id:
medre trace event evt_abc123 --config my-bridge.toml

# Expected output (human-readable):
#   Event: evt_abc123 (message.text) from bot
#   Timeline (3 entries):
#
#     2026-05-14T10:30:00Z  [event] message.text from bot
#     2026-05-14T10:30:00.010Z  [native_ref] outbound via radio: fake_123
#     2026-05-14T10:30:00.050Z  [receipt] sent -> radio (attempt 1)
#
#   Summary:
#     Receipts: sent: 1
#     Native refs: 1
```

The timeline shows the correlation: `event_id` is the primary key. Each
`[receipt]` entry has a `receipt_id` (not shown in compact mode; use `--json`
to see it). Each `[native_ref]` entry links back to the event via
`canonical_event_id`. Replay receipts include `replay_run_id` grouping.

To see all identifiers explicitly:

```bash
medre inspect receipts --event evt_abc123 --config my-bridge.toml

# Expected output (JSON):
# [
#   {
#     "receipt_id": "rcpt_001",
#     "event_id": "evt_abc123",
#     "target_adapter": "radio",
#     "route_id": "bot-to-radio",
#     "status": "sent",
#     "source": "live",
#     "replay_run_id": null,
#     "attempt_number": 1,
#     "parent_receipt_id": null,
#     "created_at": "2026-05-14T10:30:00.050000+00:00"
#   }
# ]
```

### 6.2 Interpreting Replay Receipts vs Live Receipts

Replay receipts are interleaved with live and retry receipts in storage, ordered by
`sequence` (the auto-increment primary key).  The `source` column distinguishes
origin: `"live"` for pipeline deliveries, `"retry"` for RetryWorker-attempted
deliveries, `"replay"` for replay-attributed deliveries.

```bash
# An event that was delivered live and then replayed:
medre inspect receipts --event evt_abc123 --config my-bridge.toml

# Expected output (JSON, truncated):
# [
#   {
#     "receipt_id": "rcpt_001",
#     "event_id": "evt_abc123",
#     "status": "sent",
#     "source": "live",
#     "replay_run_id": null,
#     "sequence": 1,
#     ...
#   },
#   {
#     "receipt_id": "rcpt_r1",
#     "event_id": "evt_abc123",
#     "status": "sent",
#     "source": "replay",
#     "replay_run_id": "replay_xyz789",
#     "sequence": 42,
#     ...
#   }
# ]
```

Interpretation: the event was first delivered at `sequence=1` (live), then
re-delivered at `sequence=42` (replay run `replay_xyz789`).  Both deliveries
produced real outbound messages.  The `replay_run_id` groups all receipts from
the same replay invocation.

To isolate only replay receipts:

```bash
medre inspect receipts --replay-run replay_xyz789 --config my-bridge.toml
```

**Repeated replays of the same event are traceable through distinct run_ids.**
Each `medre replay --mode BEST_EFFORT` invocation produces a unique
`replay_run_id`, even when replaying the same event.  Replaying `evt_abc123`
twice produces two separate sets of replay receipts, each grouped by a different
`replay_run_id`.  Replay receipts from one run never overwrite or merge with
replay receipts from a previous run.  All replay receipts are additive: the
total receipt count grows with each replay invocation.  Use `replay_run_id` to
isolate a specific run:

```bash
# Show all replay runs that touched this event:
sqlite3 {state}/medre.sqlite "
  SELECT replay_run_id, COUNT(*) AS receipt_count
  FROM delivery_receipts
  WHERE event_id = 'evt_abc123' AND source = 'replay'
  GROUP BY replay_run_id
  ORDER BY replay_run_id;
"
```

Interpretation: the event was first delivered at `sequence=1` (live), then
re-delivered at `sequence=42` (replay run `replay_xyz789`). Both deliveries
produced real outbound messages. The `replay_run_id` groups all receipts from
the same replay invocation.

To isolate only replay receipts:

```bash
medre inspect receipts --replay-run replay_xyz789 --config my-bridge.toml
```

### 6.3 Interpreting Partial Evidence: Routing Succeeded but Delivery Failed

When routing matches a route but the adapter rejects the delivery, you see a
receipt with `status="failed"` and a `failure_kind` indicating the cause.

```bash
medre trace event evt_partial --config my-bridge.toml

# Expected output:
#   Event: evt_partial (message.text) from bot
#   Timeline (2 entries):
#
#     2026-05-14T10:30:00Z  [event] message.text from bot
#     2026-05-14T10:30:00.050Z  [receipt] failed -> radio (attempt 1) \
#       error=adapter_transient
#
#   Summary:
#     Receipts: failed: 1
#     Native refs: 0
```

Key indicators of partial evidence:
- A receipt exists with `route_id` populated (routing matched).
- `status="failed"` with a non-null `failure_kind` (delivery rejected).
- No native ref for the failed adapter (the adapter never produced a native
  message ID because delivery failed before that point).
- If `attempt_number > 1`, check `parent_receipt_id` to trace the retry chain.

```bash
# Check the full retry chain:
sqlite3 {state}/medre.sqlite "
  SELECT receipt_id, attempt_number, status, failure_kind, parent_receipt_id
  FROM delivery_receipts
  WHERE event_id = 'evt_partial'
  ORDER BY sequence ASC;
"

# Expected:
# rcpt_001|1|failed|adapter_transient|
# rcpt_002|2|failed|adapter_transient|rcpt_001
# rcpt_003|3|sent||rcpt_002
```

### 6.4 Interpreting Callback Ingress Evidence

When an event is ingested from a source adapter (inbound direction), there is
no `DeliveryOutcome` or delivery receipt for the ingestion itself. The event
exists in `canonical_events`, and any delivery receipts correspond to outbound
deliveries triggered by that event.

```bash
# An inbound event with successful outbound delivery:
medre trace event evt_inbound --config my-bridge.toml

# Expected output:
#   Event: evt_inbound (message.text) from radio
#   Timeline (3 entries):
#
#     2026-05-14T10:30:00Z  [event] message.text from radio
#     2026-05-14T10:30:00.010Z  [native_ref] inbound via radio: pkt_456
#     2026-05-14T10:30:00.050Z  [receipt] sent -> matrix (attempt 1)
```

The `[event]` entry is the ingestion. The `[native_ref]` with `direction:
inbound` shows the original transport-native message. The `[receipt]` shows the
outbound delivery to another adapter. There is no receipt for the inbound side
-- only the event and its native ref.

To identify inbound-only events (ingested but never delivered):

```bash
sqlite3 {state}/medre.sqlite "
  SELECT e.event_id, e.source_adapter,
    (SELECT COUNT(*) FROM delivery_receipts r WHERE r.event_id = e.event_id) AS receipt_count
  FROM canonical_events e
  WHERE (SELECT COUNT(*) FROM delivery_receipts r WHERE r.event_id = e.event_id) = 0
  ORDER BY e.created_at DESC
  LIMIT 20;
"
```

### 6.5 How Duplicate Suppression Appears in Storage

MEDRE does not have a deduplication layer, but loop prevention suppresses
delivery when an event would be routed back to its source adapter. In this
case, no receipt is written. The event exists in `canonical_events` with no
corresponding receipt for the loop-prevented adapter.

```bash
# An event where loop prevention suppressed delivery:
medre trace event evt_loop_prevented --config my-bridge.toml

# Expected output:
#   Event: evt_loop_prevented (message.text) from bot
#   Timeline (1 entry):
#
#     2026-05-14T10:30:00Z  [event] message.text from bot
#
#   Summary:
#     Receipts: none
#     Native refs: 0
```

No receipt was written because loop prevention prevented delivery. The event
has zero receipts. This is the same storage signature as an orphaned event.
To distinguish loop-prevented events from orphans, check whether the event's
source adapter matches any route's destination:

```bash
# Events with no receipts where the source adapter IS a route destination
# (likely loop-prevented, not orphaned):
sqlite3 {state}/medre.sqlite "
  SELECT e.event_id, e.source_adapter
  FROM canonical_events e
  LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
  WHERE r.event_id IS NULL
    AND e.source_adapter IN (
      SELECT DISTINCT json_extract(value, '$.dest') FROM ...
    )
  ORDER BY e.created_at DESC
  LIMIT 20;
"
```

Note: loop prevention is tracked in process-local `loop_prevented` counters
in the routing/accounting layer. These counters reset on restart. To check
loop prevention from storage alone, correlate the event's source adapter with
the route configuration.

### 6.6 Restart Boundaries: Previous Events in Storage, Counters Reset

After a restart, all events and receipts survive in SQLite. Process-local
counters (capacity rejections, outbound failed, route stats, loop-prevented)
reset to zero. This means:

1. Events from before the restart are still queryable via `medre trace` and
   `medre inspect`.
2. Receipts from before the restart are still in `delivery_receipts` with
   their original `sequence` values.
3. New receipts after restart continue the `sequence` auto-increment (no gap
   filling -- gaps indicate lost in-flight deliveries).
4. Counter-based diagnostics (`medre diagnostics`) reflect only post-restart
   state.

```bash
# Identify the restart boundary by looking for a gap in receipt timestamps:
sqlite3 {state}/medre.sqlite "
  SELECT r1.created_at AS before_restart,
         r2.created_at AS after_restart,
         (julianday(r2.created_at) - julianday(r1.created_at)) * 86400 AS gap_seconds
  FROM delivery_receipts r1
  JOIN delivery_receipts r2 ON r2.sequence = r1.sequence + 1
  WHERE (julianday(r2.created_at) - julianday(r1.created_at)) * 86400 > 60
  ORDER BY r1.sequence DESC
  LIMIT 5;
"

# Expected output (if restart happened):
# 2026-05-14T10:30:00.050Z|2026-05-14T11:15:00.010Z|2699.96
#
# A gap of ~45 minutes between consecutive receipts suggests a restart.
# Events created during the gap that have no receipts are likely lost
# in-flight deliveries.
```

To find events that were likely in-flight at the crash point:

```bash
sqlite3 {state}/medre.sqlite "
  SELECT e.event_id, e.created_at
  FROM canonical_events e
  LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
  WHERE r.event_id IS NULL
    AND e.created_at BETWEEN '2026-05-14T10:25:00Z' AND '2026-05-14T10:35:00Z'
  ORDER BY e.created_at ASC;
"
```


## 7. Retry vs Replay

MEDRE provides two mechanisms for re-delivering events that failed: **retry** (automatic, opt-in) and **replay** (manual). They serve different purposes and have different side effects.

**Key distinction:** Retry is delivery lineage — each attempt is linked by `parent_receipt_id`. Replay is a new bridge execution — receipts have `replay_run_id`, not `parent_receipt_id`.

### 7.1 Retry (Automatic, Same Delivery Lineage)

Retry is **opt-in** — disabled by default. The `RetryWorker` only runs when a `RetryPolicy` is configured. Without a `RetryPolicy`, transient failures are not automatically retried.

When a delivery fails with `failure_kind='adapter_transient'` and a `RetryPolicy` is configured, the RetryWorker automatically re-attempts delivery:

- **Trigger:** `ADAPTER_TRANSIENT` failures only (timeout, connection reset, `OSError` hierarchy).
- **Owner:** `RetryWorker` — a single-process background worker.
- **Lineage:** Each retry produces a new receipt with `source='retry'`, linked to the previous via `parent_receipt_id`, with incremented `attempt_number`. The delivery lineage is a single chain.
- **Persistence:** Pending retry state (`next_retry_at` on the failed receipt) survives process restart. The RetryWorker loads due receipts on its next cycle.
- **Bounded by:** `RetryPolicy` (max attempts, backoff). When max attempts are exceeded, the receipt is marked `dead_lettered`.
- **Native refs:** Only persisted on successful retry, not on the original failure.
- **No duplicate risk:** Retry continues the same delivery attempt. The adapter receives one message per successful retry, same as if the original had succeeded.
- **Frozen target metadata:** Retry uses the `target_adapter` and `target_channel` from the original failed receipt, not the current route configuration. Route config changes after the original failure do not affect in-flight retry targeting. The RetryWorker validates that the target adapter still exists at runtime before attempting delivery; if the adapter has been removed, the retry is dead-lettered.
- **Capacity rejection:** If the RetryWorker cannot acquire the delivery semaphore, it emits a `retry_failed` event and reschedules the receipt for the next worker interval. No new receipt is created — the original failed receipt remains due with its `next_retry_at` advanced by one backoff interval using the stored retry policy metadata. Capacity rejection does not advance `attempt_number` and does not count toward `RetryPolicy` exhaustion.

**Retry states an operator should distinguish:**

| State | `status` | `next_retry_at` | `failure_kind` | Meaning |
|-------|----------|-----------------|----------------|---------|
| Pending retry | `failed` | Set (future time) | `adapter_transient` | RetryWorker will re-attempt on next cycle |
| Exhausted | `dead_lettered` | `NULL` | `adapter_transient` | Max retries exceeded; manual intervention needed |
| Successful retry | `sent` or `confirmed` | `NULL` | `NULL` | A retry receipt succeeded; check `parent_receipt_id` to trace back |

### 7.2 Replay (Manual, New Bridge Execution)

When an operator invokes `medre replay --mode BEST_EFFORT`, events are re-delivered through a new execution:

- **Trigger:** Operator-initiated via CLI.
- **Owner:** Operator. No automatic scheduling.
- **Lineage:** Replay creates new receipts with `source='replay'` and a unique `replay_run_id`. These are new delivery attempts, not linked to the original via `parent_receipt_id`.
- **Persistence:** Replay results (receipts) are durable in SQLite. The `ReplaySummary` itself is not persisted.
- **Duplicate risk:** **High.** Replaying an event that was already delivered (including by a successful retry) produces a second outbound message with no deduplication. Traceability (via `source`/`replay_run_id`) lets the operator identify duplicates after the fact, but does not prevent them.

### 7.3 When to Use Which

| Scenario | Use | Why |
|----------|-----|-----|
| Transient adapter failure (timeout, connection reset) | **Retry** (automatic) | RetryWorker handles this. No operator action needed. |
| Retry exhausted (dead-lettered) | **Replay** (manual) | After fixing the underlying cause, replay the event. |
| Event never delivered (orphaned by crash) | **Replay** (manual) | No receipt exists, so retry has nothing to chain from. |
| Permanent failure | **Replay** (manual) | After fixing the underlying cause (e.g., auth, config). |
| Retry disabled (no RetryPolicy) | **Replay** (manual) | Without a RetryPolicy, the RetryWorker does not pick up transient failures. |

### 7.4 Checking Pending Retries

To see events that have pending retries (the RetryWorker will handle them):

```sql
SELECT receipt_id, event_id, target_adapter, attempt_number, next_retry_at
FROM delivery_receipts
WHERE status = 'failed'
  AND failure_kind = 'adapter_transient'
  AND next_retry_at IS NOT NULL
ORDER BY next_retry_at ASC;
```

If `next_retry_at` is in the past and the runtime is running, the RetryWorker should pick it up on its next cycle. If the runtime is stopped, these will be processed on restart.

## 8. Recovery Commands Quick Reference

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


## 9. Caveats

1. **Retry is limited to transient failures.** Only `ADAPTER_TRANSIENT` failures are auto-retried by the RetryWorker. Permanent failures (`ADAPTER_PERMANENT`, `RENDERER_FAILURE`, `PLANNER_FAILURE`, `DEADLINE_EXCEEDED`) require manual replay.

2. **No per-adapter restart.** Only full runtime stop/start is supported. When
   one adapter fails, all adapters must restart together.

3. **No deduplication.** Replay produces new outbound messages each time.
   Multiple BEST_EFFORT replays of the same events produce duplicates. This is
   by design. Retry does not produce duplicates (same delivery lineage).

4. **No active supervision.** There is no background health monitor, watchdog,
   or orchestrator beyond the RetryWorker. Operators must detect non-transient failures externally (logs, process
   supervisors, cron health checks).

  5. **Counters reset on restart.** All runtime counters (capacity_rejections,
    outbound_failed, RouteStats, retry counters) reset to zero on every startup. There is no
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


## 10. Cross-References

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

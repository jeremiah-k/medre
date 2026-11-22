# Operator Workflows

Commands assume an activated MEDRE environment; see [installation](install.md).
For checkout tests, sync core with `--extra dev`, then install the selected
adapter distributions. Core has no transport extras;
see [environment setup](../dev/environment.md) for sync behavior.

Day-to-day workflows for operating MEDRE: smoke testing, inspect-first investigation, evidence collection, event tracing, failure handling, and replay.

## Data Ownership and Immutability

Understanding what is permanent, what is mutable, and what is derived helps you interpret storage output correctly and avoid actions that cannot succeed.

### What is permanent and immutable

- **Canonical events** are never updated or deleted. Every event that entered the pipeline is preserved as an immutable fact. The event log is the definitive record of what happened.
- **Delivery receipts** are append-only. Each delivery attempt produces a new receipt row. Old receipt rows are never changed. Current delivery status is a projection: outbox-backed receipts are current only when an outbox row points at their `receipt_id`, while receipt-only delivery uses latest durable append order.
- **Native message refs** are idempotent correlation facts. They map native transport IDs to canonical events and are never updated or deleted.
- **Terminal outbox rows** (`sent`, `dead_lettered`, `cancelled`, `abandoned`) are immutable. Once an outbox item reaches a terminal status, it becomes permanent operational history.

### What is mutable

- **Non-terminal outbox rows** (`pending`, `in_progress`, `queued`, `retry_wait`) are mutable operational work state. Delivery workers claim, update, and transition these rows. On crash recovery, expired leases and stale queued rows are automatically reclaimed.
- **Plugin state** is scoped key-value storage owned by individual plugins.

### What is derived (not authoritative lifecycle state)

- **`medre inspect`, `medre trace`, `medre evidence`** read SQLite and present projections. These reports are convenient views, not separate sources of truth. If a report contradicts the receipt chain in `delivery_receipts`, the receipts are the authority.
- **Diagnostics snapshots** (`medre diagnostics`) are point-in-time readings of in-memory counters. Counters reset on every restart.
- **Convergence summaries** and **lifecycle convergence reports** are detection-only derived analyses. They flag potential inconsistencies but do not modify storage or represent lifecycle state themselves.

### What recovery can and cannot do

- Recovery identifies orphaned events (stored but never delivered) and stale outbox rows (crashed mid-delivery).
- Recovery never fabricates a delivery receipt. A `sent` receipt only exists because a real delivery attempt produced it.
- Recovery never rewrites existing receipts or events. It can only produce new state through actual delivery (via replay or retry).
- Orphan detection is a bookkeeping query, not a guarantee of successful re-delivery.

### Why deletion is not an operator workflow

All stored rows are either immutable facts (events, receipts, native refs, terminal outbox) or active work state (non-terminal outbox). There is no operator command to delete rows from the database because every row is evidence or active operational state. If the database grows too large, the supported path is to stop the runtime, back up the database, start a fresh database, and optionally replay critical events from the backup.

For the normative per-table ownership rules, see
[spec/storage.md §16](../spec/storage.md).

## Stale Storage Reset

The currently released schema version is 1, but the
layout (which tables, columns, and indexes exist, and which constraint
vocabularies are allowed) can change between builds. When you upgrade to a
newer core build, MEDRE automatically applies the known additive layout
migrations to the existing database at startup: new allowlisted tables,
columns, and indexes, and constraint widenings rebuilt under row-count and
foreign-key integrity checks. Changes outside that additive policy — renames,
removals, narrowed constraints — are still rejected at startup with a
`PreReleaseSchemaMismatchError` or `PreReleaseSchemaConstraintMismatchError`;
such a database is preserved for an explicit recovery decision. Use a
compatible previous build if appropriate, or back up and deliberately reset
when starting fresh is acceptable.

Before upgrading, keep a verified SQLite-aware backup. An older build can
refuse an upgraded layout even when both use version 1. To roll back,
restore the pre-upgrade backup with a build that accepts its catalog.
Keep the upgraded database separately if it contains work recorded since
the backup.

A backup is a point-in-time copy. A delivery that was pending when the
backup was taken may have completed natively afterward; restoring that
backup erases the completion record, so the restored build can send it
again. Before resuming a restored database, reconcile the provider and
radio effects that landed after the backup was taken, or prefer the
upgraded database when it holds work recorded since the backup.

For an exact destination that the restored evidence still shows as
`pending` or `retry_wait` with no reservation or claim, use
[`medre resolve` exact cancellation](exact-cancellation.md) to fence it
before starting the runtime: preview, verify the observed evidence, then
apply with the observed outbox ID, generation, status, and update version.
That command refuses `queued`, `in_progress`, uncertain, or accepted native
attempts. If the restored row is in one of those states, do not start the
restored build; reconcile the provider and radio outcome while it stays
offline, or keep the upgraded database authoritative.

### How to tell if your database is stale

Run `medre storage status --storage-path <path>` to assess the stored
catalog. Use `medre paths` to discover the resolved state directory, then
pass `{state}/medre.sqlite` as the storage path. The command applies the
same exact catalog validation and predecessor recognition that storage
startup applies, over one consistent read-only snapshot, and reports one
verdict: `current` (status HEALTHY), `supported_upgrade` (a recognized
additive predecessor), `unsupported`, `empty_new` (no schema objects; a
writable start creates the declared schema as new state), or `unavailable`
(the assessment could not read the database).

The assessment proves catalog compatibility only. It does not prove writable
access — permissions, concurrent locks, or later file changes can still
prevent a successful open — nor database integrity, remote availability, or
any behavior under concurrent changes. `unavailable` is a distinct verdict
from `unsupported` and carries no compatibility conclusion. Assessing a
database never writes application schema or data, migrates, or requests a
checkpoint. SQLite may create or update WAL bookkeeping sidecars (`-wal`,
`-shm`), even when the bridge is stopped. The read-only opener intentionally
observes committed WAL state rather than assuming the file is immutable.

If inspection refuses a recognized additive predecessor (including the older
operator-decision CHECK), **do not reset the database on that message**. Stop
all other MEDRE database users, preserve a backup if state matters, then
perform one normal writable MEDRE startup to apply the allowlisted atomic
migration. Read-only inspection cannot run that migration. Other, unsupported
layouts require a deliberate recovery or reset decision.

Alternatively, if MEDRE fails to start with an error like:

```text
PreReleaseSchemaMismatchError: Storage schema shape mismatch: table
'delivery_receipts': missing required columns ['confirmation_level'].
(database: /home/user/.local/state/medre/medre.sqlite) The database does not
match the supported catalog. MEDRE automatically applies known additive
layout migrations; a database requiring changes outside that policy
requires an explicit recovery or reset decision.
```

then the catalog is outside the additive migration policy. Preserve the
original database and choose a compatible previous build, a supported recovery
procedure if available, or an explicit reset. Reset is not required merely
because a database was created by an older release.

### Step-by-step reset procedure

**Reset freely only when you intend to discard the entire test bridge state.**
A new installation does not need to preserve an empty or disposable
SQLite database. Once you have sent real traffic, however, MEDRE may have
queued outbound deliveries awaiting a retry or native handoff confirmation.
Clearing the database drops those pending operations and the native IDs used to
correlate replies and avoid unsafe duplicate sends. It also discards historical
inspection data; this is an operator choice, not a requirement to archive
messages forever. Stop MEDRE and all other SQLite readers/writers before any
reset, inspect outstanding work if it matters, and keep a private archive only
when you actually need one. See
[inspect a stale store before reset](#inspect-a-stale-store-before-reset).
The failed-delivery recovery view does not enumerate queued work and may refuse
an incompatible schema.

1. **Resolve the database path.** `medre paths` shows the state directory;
   `medre storage status --storage-path PATH` inspects the schema. Use the
   actual SQLite file from your config, not an assumed default path.
2. **Keep and verify a backup if the state matters.** Skip archiving only when
   you intend to discard the entire disposable store. Otherwise, with all
   database users stopped, choose a **fresh private directory** and make a
   SQLite-aware backup before resetting:

   ```bash
   mkdir -m 700 /private/medre-reset-backup
   sqlite3 -readonly /path/to/medre.sqlite \
     ".backup '/private/medre-reset-backup/medre.sqlite'"
   sqlite3 -readonly /private/medre-reset-backup/medre.sqlite 'PRAGMA integrity_check;'
   ```

   Stop if any command fails or the integrity result is not exactly `ok`.
   SQLite's backup includes committed WAL content in the resulting database;
   do not copy the source WAL onto that snapshot. Copying only the main file is
   **not** a complete raw-file backup: committed data may remain in its WAL.
   A raw copy needs the matching main file and any `-wal`/`-shm` sidecars together.
   This preserves core state only; native identities, credentials and provider
   stores have separate [capture requirements](stopped-state-provider-inventory.md).

3. **Reset the disposable database deliberately.** With all users stopped,
   and either a verified backup from step 2 or an explicit decision to discard
   the state, reset without making another archive:

   ```bash
   medre storage reset --storage-path /path/to/medre.sqlite --yes
   ```

   If you want an additional timestamped copy beside the active database, use
   `--backup --yes` instead. That copy is outside the private backup directory
   and retains message content: protect and remove it separately when no
   longer needed. It includes existing sidecars but is a file-level backup,
   **not an online SQLite snapshot**. The reset command does not verify it or
   pause before deletion, so it does not replace step 2. Do not run it while
   another process is writing. Avoid hand-deleting SQLite files.

4. **Start MEDRE against the new empty store** with
   `medre run --config bridge.yaml`. Confirm its new schema using
   `medre storage status --storage-path PATH`.

### Inspect a stale store before reset

With MEDRE and its workers stopped, inspect a verified private copy of the
complete database and any WAL sidecars. The normal `inspect` and `recover`
commands validate the schema and can refuse an older store. SQLite's read-only
shell does not invoke MEDRE's schema initialization:

```bash
sqlite3 -readonly /private/medre-copy.sqlite <<'SQL'
PRAGMA table_info(delivery_outbox);
SELECT status, COUNT(*) AS items
FROM delivery_outbox
GROUP BY status
ORDER BY status;
SELECT outbox_id, event_id, target_adapter, status
FROM delivery_outbox
WHERE status IN ('pending', 'in_progress', 'queued', 'retry_wait')
ORDER BY outbox_id
LIMIT 100;
SELECT outbox_id, event_id, target_adapter, status
FROM delivery_outbox
WHERE status IN ('dead_lettered', 'abandoned')
ORDER BY outbox_id
LIMIT 100;
SELECT status, COUNT(*) AS items
FROM durable_ingress_work
GROUP BY status
ORDER BY status;
SELECT event_id, status, attempts
FROM durable_ingress_work
WHERE status IN ('pending', 'processing', 'failed')
ORDER BY event_id
LIMIT 100;
SELECT event_id, adapter, native_channel_id, native_message_id
FROM native_message_refs
ORDER BY event_id, adapter, native_message_id
LIMIT 100;
SQL
```

The counts cover every outbox and durable ingress row; sample limits do not
hide the presence of queued work. Pending/queued outbox and terminal problem
rows are sampled separately. Durable ingress can contain an admitted event
awaiting routing before any outbox row exists, so inspect both queues.
These outputs expose private identifiers, not message bodies. They do not
authorize retry or prove a reset safe. An empty sample does not certify that
native handoff state is safe
to discard. If the older schema lacks these tables or columns, use its matching
MEDRE revision against the copy instead of changing the schema by hand.

### What happens to the old data

Nothing from the old database is carried forward. The reset is total. Events, receipts, native refs, and outbox state from the old database are gone. The new database starts empty.

Stored message content can also support reply quotes when the destination
cannot represent a reply natively. Native IDs alone cannot reconstruct that
text. Relation fallback text and metadata may contain copies of an excerpt;
privacy is not determined by the canonical event body alone. Native SDK stores
and separate backups are outside a core database reset. See
[message content at the relay boundary](../spec/security-privacy.md#35-message-content-at-the-relay-boundary).

If you need old evidence, keep the complete backup set. Inspect a verified
copy without running MEDRE against it; if a WAL sidecar was backed up, leave
it beside the copied main file under the matching name. Never treat the
file-level reset backup as a live SQLite snapshot.

### When this does not apply

Use this workflow only when writable initialization rejects an unsupported
schema version or catalog (including missing or unexpected columns, constraints,
indexes, views or other objects), and you deliberately choose a fresh store.
An older build alone is not a reason to reset. A recognized additive
predecessor can be migrated at a normal writable open; a compatible database
needs no reset. Storage changes follow the
[versioning policy](../dev/versioning-policy.md).

## Inspect Outstanding Delivery Work

When delivery seems stuck, start with the read-only work-status summary
before touching any recovery command:

```bash
medre inspect work-status --storage-path /path/to/medre.sqlite
medre inspect work-status --adapter matrix --storage-path /path/to/medre.sqlite
medre inspect work-status --json --storage-path /path/to/medre.sqlite
```

The command reads one consistent snapshot of durable storage and reports,
per destination, how many current delivery generations are outstanding and
why: recorded unstarted work, scheduled future retry, reserved work, or
native queue admission. Unstarted includes pending work and retries not
classified as scheduled, including previously attempted work. These categories
and grace flags are not worker claimability checks. Each destination shows the oldest known anchor (or an
item with unknown age when none are usable), its precise age origin, the
selected delivery identity, and a read-only event-level inspection of its
recovery facts. The follow-up reads current state, not this snapshot. Match the
plan, channel and outbox ID; if the generation has changed, resample work
status. An event may have multiple deliveries. POSIX shells receive a quoted command;
Windows receives a JSON argument list (not shell code). Pass those arguments
to `medre`, preserving each item, for example from a Python subprocess.
The live comparison clock is sampled just after pinning the snapshot;
an explicit comparison time does not reconstruct a historical database.
Retry deadlines, lease expiry, queue grace and age anchors are compared as
instants. Equal-generation authority retains its shared stored-text tie-break.
Missing or unusable anchors give unknown age; an unusable retry deadline
does not prove it is due.
The view does not normalize retained data or change scheduler eligibility.
For noncanonical legacy timestamp spellings the current dispatch guards may
compare differently; inspect the exact delivery before acting. Reading the
view starts no adapter, opens no native profile, and submits, replays,
retires, or migrates nothing.

**Normal operation** looks like pending work draining to terminal states
between samples, with `outstanding` shrinking as deliveries complete:

```text
  Current generations: 9 — outstanding 2 (unstarted 1, scheduled 0,
reserved/active 1, native queue 0), uncertain outcome 0, terminal 7
```

**Disconnected destination or backoff** shows scheduled retries whose age
origin is the retry-due time, not the original admission, so a healthy
cooldown is distinguishable from stuck work:

```text
    matrix: outstanding 1 of 1 current (unstarted 0, scheduled 1, ...)
      oldest outstanding: retry scheduled at 2026-11-18T09:30:00+00:00 (due in at most 2400s)
```

**Uncertain outcomes** are labeled, never resolved into guesses. A reserved
generation can lack matching attempt evidence, have a missing, unusable
or expired lease, or both. Each flag is reported independently — the
view flags unusable queue timestamps separately from known stale queues. It
cannot prove a live native send or owner liveness, and says so:

```text
    chatmail: outstanding 1 of 1 current (unstarted 0, scheduled 0, reserved/active 1, ...)
      uncertainty: 1 reserved generation(s) carry no attempt evidence: whether a
      native send occurred is unknown
      next inspection (event-level, POSIX shell): medre inspect event --recovery ... -- EVENT
```

An `abandoned` generation is a settled local uncertain outcome, not
outstanding reserved work. A destination may show no outstanding work and
still have abandoned outcomes. Use the read-only SQL inventory above
(`status IN ('dead_lettered', 'abandoned')`) to locate affected event IDs,
then inspect each event with `medre inspect event --recovery`. The inventory
is only a retained-row sample. For a complete scoped candidate search, use
a read-only SQLite connection and page by outbox ID (bind the adapter, and
use the last returned ID for the next page):

```sql
SELECT outbox_id, event_id, delivery_plan_id, target_channel
FROM delivery_outbox
WHERE status = 'abandoned' AND target_adapter = :adapter
  AND outbox_id > :after
ORDER BY outbox_id LIMIT 100;
```

Start with an empty `:after`; omit the adapter predicate for all adapters.
Verify each candidate against the current event recovery runbook (including
outbox ID); a retained abandoned generation may have been superseded.
Resample if state changes.

Unknown is not zero: it describes facts a readable snapshot cannot establish.
An unavailable database produces a nonzero exit and no summary. `sent`
means local terminal acceptance, never independent recipient confirmation.

## Preferred Product Path

The recommended operator loop:

1. **Pre-flight**: `medre config check` and `medre routes validate`
2. **Optional smoke**: `medre smoke --config sample.yaml --storage-path smoke.sqlite3 --json`
3. **Inspect-first**: `medre inspect event` and `medre inspect receipts`
4. **Deeper investigation**: `--timeline`, `--evidence`, `--recovery` flags on inspect
5. **Replay only when needed**: `DRY_RUN` first, then `BEST_EFFORT`

> Replay is operator-initiated and one-shot. It does not continuously tail the event log. Each replay invocation processes stored events once and exits. Replay requires `--config`, not `--storage-path`.

## Smoke Test

The fastest way to confirm MEDRE works on your machine. No network, no credentials.

```bash
# Source checkout (in-memory storage, ephemeral)
medre smoke --config examples/configs/fake-bridge-smoke.yaml --json

# Source checkout (persist evidence without editing the config)
medre smoke --config examples/configs/fake-bridge-smoke.yaml \
  --storage-path /tmp/medre-smoke.db --json

# Installed package with a sample-contributing provider
medre config sample > /tmp/medre-walkthrough.yaml
medre smoke --config /tmp/medre-walkthrough.yaml \
  --storage-path /tmp/medre-walkthrough.db --json
```

Expected: JSON with `"status": "passed"`, an `event_id`, and delivery receipts.

### What Smoke Validates

- Pipeline builds and starts without errors.
- A fake adapter constructs a canonical event and injects it directly into
  the core pipeline; routing, rendering, delivery, and receipt accounting run.
- The fake destination adapter receives and acknowledges the delivery.
- Storage round-trips correctly.
- Diagnostics collected and reported.

### What Smoke Does Not Validate

- Native inbound codec decoding or real transport session callbacks; normal
  smoke bypasses these by injecting an already-canonical event.
- Any real network communication.
- Matrix SDK behaviour against a real homeserver.
- E2EE crypto operations.
- Meshtastic radio or serial communication.

Without `--storage-path`, the config selects the storage backend. Use
`--storage-path FILE` to persist smoke evidence in SQLite **without editing
YAML**, even when the configuration selects memory storage. Do not commit this
file to source control. A smoke report covers one selected source event and its
matched fanout, not all configured sources or live recipient delivery. For a
specific eligible source use `--source-adapter INSTANCE_ID`; for a mapped
conversation also pass `--source-context CONTEXT` (normal fake smoke only).

## Inspect-First Investigation

After any run (smoke, live, post-crash), start here. All inspect commands are read-only and use `--storage-path` to point at the database.

### Check the Event

```bash
medre inspect event <event_id> --storage-path /tmp/medre-walkthrough.db
```

Shows source adapter, event kind, payload, and timestamp.

### Check Delivery Receipts

```bash
medre inspect receipts --event <event_id> --storage-path /tmp/medre-walkthrough.db
```

Shows delivery receipts with target adapter, route, attempt number, and failure kind.

### Assemble a Timeline

```bash
medre inspect event <event_id> --timeline --storage-path /tmp/medre-walkthrough.db
```

Shows every stage the event passed through: ingestion, routing, delivery, retry, replay. Covers the same output as `medre trace event`.

### Collect a Full Evidence Bundle

```bash
medre inspect event <event_id> --evidence --storage-path /tmp/medre-walkthrough.db
```

Shows event, receipts, timeline, and incident summary. The recommended attachment format for bug reports. Covers `medre evidence --event`.

### Generate Recovery Guidance

```bash
medre inspect event <event_id> --recovery --storage-path /tmp/medre-walkthrough.db
```

Shows failure classification, affected routes, and recommended next steps. Covers `medre recover --event`.

### Combine Flags

```bash
medre inspect event <event_id> \
  --timeline --evidence --recovery \
  --storage-path /tmp/medre-walkthrough.db
```

The most detailed inspection available. Deterministic JSON, suitable for diffing or pasting into reports.

### What Inspect Does Not Do

- Does not start a runtime.
- Does not connect to any network service.
- Does not modify the database.
- Does not require any environment variables beyond the storage path.

## Event Tracing

`medre trace` assembles chronological timelines. For day-to-day use, `medre inspect event --timeline` is preferred — `trace` is available for standalone output and scripting.

### Trace a Single Event

```bash
# Human-readable
medre trace event <event_id> --storage-path /path/to/medre.db

# JSON
medre trace event <event_id> --storage-path /path/to/medre.db --json
```

### Trace a Replay Run

```bash
medre trace replay <run_id> --storage-path /path/to/medre.db
```

Replay trace status reflects durable execution state: `admitted` means the named
run owns at least one outbox generation but has not produced a receipt yet;
`active` means receipts exist while at least one admitted generation remains
non-terminal; `complete` means every represented outbox generation is terminal
(or the trace is receipt-only, with no outbox generations represented); and `partial` means referenced
canonical events could not all be loaded.

### Timeline Entry Types

| Entry type             | What it shows                                                                                           |
| ---------------------- | ------------------------------------------------------------------------------------------------------- |
| `event`                | Canonical event (kind, source adapter, timestamp)                                                       |
| `native_ref`           | Native transport references (Matrix event IDs, Meshtastic message IDs)                                  |
| `outbox_generation`    | Durable target admission/current outbox snapshot, including named replay origin before a receipt exists |
| `receipt`              | Immutable MEDRE delivery evidence (status, target adapter, attempt count)                               |
| `delivery_observation` | Later transport facts tied to one exact durable delivery attempt                                        |
| `relation`             | Relations to other events (replies, reactions)                                                          |

### Interpreting Timeline Gaps

- **No routing phase**: Event had no matching routes. Check route config.
- **No delivery phase**: No receipt exists. Runtime may have crashed mid-delivery, or delivery was never attempted. Loop prevention, capacity exceeded, and shutdown rejection produce `suppressed` receipts — those appear as a delivery phase.
- **Multiple delivery phases, same target**: Retry chain. Check `attempt_number` and `parent_receipt_id`.
- **Post-handoff observation after `sent`**: A transport reported a later fact;
  it does not rewrite the receipt or reopen the outbox lifecycle.
- **Multiple delivery phases, different targets**: Fan-out.
- **Multiple dispatch mechanisms**: Receipt `source` is the mechanism (`live`, `replay`, or `retry`); `replay_run_id` independently preserves replay origin. An event with durable replay admission but no receipt yet reports timeline source `none` rather than inventing a live dispatch.

### Receipt and Native-Ref Fields

**DeliveryReceipt:**

| Field               | Description                                                       |
| ------------------- | ----------------------------------------------------------------- |
| `receipt_id`        | Unique receipt identifier                                         |
| `event_id`          | Canonical event                                                   |
| `target_adapter`    | Adapter that received the delivery                                |
| `route_id`          | Route that matched the event                                      |
| `status`            | `sent`, `failed`, `suppressed`, etc.                              |
| `failure_kind`      | Failure classification or `null`                                  |
| `attempt_number`    | 1 for first attempt, increments on retry                          |
| `parent_receipt_id` | Links to previous receipt in retry chain                          |
| `source`            | `"live"`, `"retry"`, or `"replay"`                                |
| `replay_run_id`     | Named replay origin; preserved on later `source="retry"` attempts |

**NativeMessageRef:**

| Field               | Description                        |
| ------------------- | ---------------------------------- |
| `native_message_id` | Transport-native ID                |
| `native_channel_id` | Transport-native channel/room ID   |
| `resolves_to`       | Links to canonical event           |
| `adapter`           | Adapter that produced this mapping |
| `direction`         | `"inbound"` or `"outbound"`        |

### SQL Queries for Deep Tracing

```sql
-- All receipts for an event, including retry lineage
SELECT receipt_id, status, failure_kind, target_adapter, route_id,
       attempt_number, parent_receipt_id, source, replay_run_id, created_at
FROM delivery_receipts
WHERE event_id = 'evt_abc123'
ORDER BY created_at ASC;

-- Orphaned events (stored but never delivered)
SELECT e.event_id, e.source_adapter, e.event_kind, e.created_at
FROM canonical_events e
LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
WHERE r.event_id IS NULL
ORDER BY e.created_at DESC
LIMIT 50;

-- Replay duplicate risk assessment
SELECT e.event_id,
       COUNT(CASE WHEN r.source = 'live' AND r.receipt_kind = 'attempt' THEN 1 END) AS live_dispatches,
       COUNT(CASE WHEN r.replay_run_id IS NOT NULL THEN 1 END) AS replay_origin_receipts
FROM canonical_events e
JOIN delivery_receipts r ON e.event_id = r.event_id
GROUP BY e.event_id
HAVING live_dispatches > 0 AND replay_origin_receipts > 0;

-- Route-level delivery summary
SELECT route_id, status, COUNT(*) AS count
FROM delivery_receipts
GROUP BY route_id, status
ORDER BY route_id, status;

-- Retry chain reconstruction
WITH RECURSIVE chain AS (
  SELECT * FROM delivery_receipts WHERE receipt_id = 'rcpt_001'
  UNION ALL
  SELECT r.* FROM delivery_receipts r JOIN chain c ON r.parent_receipt_id = c.receipt_id
)
SELECT receipt_id, attempt_number, status, failure_kind
FROM chain ORDER BY attempt_number;
```

## Evidence Collection

When something goes wrong, `medre evidence` collects a storage-backed diagnostic bundle. Review its redacted output before sharing it.

### Offline Mode (From Storage)

```bash
medre evidence --storage-path /path/to/medre.db --json
```

Opens an existing database in read-only mode and derives event and delivery
evidence. It does not load configuration, construct a runtime or refresh health.

### Scope to a Specific Event

```bash
medre evidence --storage-path /path/to/medre.db --event <event_id> --json
medre evidence --storage-path /path/to/medre.db --replay-run <run_id> --json
```

### What Is in the Bundle

| Section                         | Contents                                                           |
| ------------------------------- | ------------------------------------------------------------------ |
| `sections.config_summary`       | Python config-backed collection: redacted config and paths         |
| `sections.route_validation`     | Python config-backed collection: route validation                  |
| `sections.diagnostics_snapshot` | Python config-backed collection: adapter state and counters        |
| `sections.live_health`          | Config-backed Python collection with `include_refresh_health=True` |
| `sections.storage`              | Event and delivery records from SQLite                             |

Top-level fields: `schema_version`, `medre_version`, `command`, `status`, `collected_at`, `config_source`, `errors`, `limitations`.

### Safety

- Access tokens and credentials are redacted.
- No message content — only metadata (event IDs, timestamps, adapter IDs, delivery status).
- No network addresses beyond homeserver URL.

Always review the JSON output before sharing. Look for any field containing your actual access token. If you find one, that is a bug.

## Delivery Failure Classification

### Receipt Statuses

| Status          | Kind      | Meaning                                                               |
| --------------- | --------- | --------------------------------------------------------------------- |
| `queued`        | attempt   | Enqueued for async delivery.                                          |
| `sent`          | attempt   | Adapter confirmed delivery.                                           |
| `failed`        | attempt   | Dispatch failed — check `failure_kind`; it may still be retryable.    |
| `dead_lettered` | lifecycle | Retry budget exhausted or delivery became permanently undeliverable.  |
| `cancelled`     | lifecycle | Delivery was explicitly cancelled.                                    |
| `abandoned`     | lifecycle | Durable execution was abandoned and must not be redispatched.         |
| `suppressed`    | lifecycle | Core/runtime suppressed delivery without recording transport success. |

### Failure Kinds

| Kind                    | Retryable | When                                                            |
| ----------------------- | --------- | --------------------------------------------------------------- |
| `adapter_transient`     | Yes       | Timeout, network error, connection reset                        |
| `adapter_permanent`     | No        | Malformed payload, business-logic rejection                     |
| `adapter_missing`       | No        | Target adapter not registered                                   |
| `planner_failure`       | No        | Routing/planning misconfiguration                               |
| `renderer_failure`      | No        | No renderer for event kind                                      |
| `deadline_exceeded`     | No        | Delivery plan deadline passed                                   |
| `capacity_rejection`    | No        | All in-flight slots occupied                                    |
| `shutdown_rejection`    | No        | Runtime shutdown cancelled delivery                             |
| `loop_suppressed`       | No        | Route-trace or self-loop prevention                             |
| `policy_suppressed`     | No        | Route-policy denial                                             |
| `capability_suppressed` | No        | Target adapter lacks capability for event kind or relation type |

Only `adapter_transient` is retryable.

### Common Failure Patterns

| Pattern                                              | Likely cause                                                  | What to check                                                    |
| ---------------------------------------------------- | ------------------------------------------------------------- | ---------------------------------------------------------------- |
| `failed` with `AdapterPermanentError`                | Bad room, forbidden                                           | Target room/channel exists, bot has access                       |
| `failed` with `AdapterSendError` (transient)         | Network/homeserver error                                      | Connectivity, homeserver health                                  |
| No receipt at all                                    | Never reached delivery stage                                  | Routing config, adapter health, pipeline logs                    |
| `suppressed` with `loop_suppressed`                  | Circular routes                                               | Route config for self-referencing adapters                       |
| `suppressed` with `policy_suppressed`                | Route-policy denial                                           | Route's `[policy]` section in config                             |
| `suppressed` or skipped with `capability_suppressed` | Target adapter lacks capability for the event's relation type | Transport profile capability declarations, `AdapterCapabilities` |
| Multiple receipts: `failed` then `sent`              | Transient failure + successful retry                          | `parent_receipt_id` chain                                        |
| `source='replay'` with `failed`                      | Replay re-delivery failed                                     | Target adapter healthy during replay                             |

## Replay Workflow

Replay is a lower-level tool for recovery and verification, not part of daily operation.

### When to Use Replay

- An adapter was offline during a critical window.
- You changed routing config and want to re-evaluate matching on historical events.
- You need to verify that a config change produces correct behaviour on past data.

### Replay Modes

| Mode          | Delivers? | Side effects  | Use case                     |
| ------------- | --------- | ------------- | ---------------------------- |
| `DRY_RUN`     | No        | None          | Preview                      |
| `RE_ROUTE`    | No        | None          | Re-evaluate route matching   |
| `BEST_EFFORT` | Yes       | Real delivery | Re-deliver historical events |

### Procedure

1. Dry run first:

   ```bash
   medre replay --mode DRY_RUN --config bridge.yaml --event <event_id> --json
   ```

2. Review output — check which routes match and which events would be re-delivered.

3. If preview is correct, best-effort replay:

   ```bash
   medre replay --mode BEST_EFFORT --config bridge.yaml --event <event_id> --json
   ```

4. Trace the replayed events:

   ```bash
   medre inspect event <event_id> --timeline --storage-path /path/to/medre.db
   ```

5. Check that replay-origin receipts have the expected `replay_run_id`; the initial dispatch uses `source='replay'` and any later RetryWorker attempts use `source='retry'`.

Replay requires a config file with declared routes and adapters. It is config-required and duplicate-risky — always run `DRY_RUN` first.

### Replay and Retry Interaction

`BEST_EFFORT` replay through a route with retry enabled creates an initial failure receipt with `source='replay'` and `replay_run_id`. The `medre replay` command does not start the RetryWorker. If the runtime is later started with `retry.enabled: true`, the worker discovers the durable outbox row and emits subsequent attempts with `source='retry'` while preserving the same `replay_run_id`.

## Specific Investigation Workflows

### "Delivered where?"

```bash
medre inspect event <event_id> --timeline --storage-path /path/to/medre.db
```

Look for receipt entries with `status: "sent"`. Each shows `target_adapter`, `target_channel`, `adapter_message_id`, and `route_id`.

### "Retried why?"

```bash
medre inspect event <event_id> --recovery --storage-path /path/to/medre.db
```

Look for multiple receipts with different `attempt_number` values linked by `parent_receipt_id`.

### "Suppressed why?"

Five types:

- **Loop suppressed**: `failure_kind: "loop_suppressed"` — route-trace or self-loop guard fired.
- **Policy suppressed**: `failure_kind: "policy_suppressed"` — route-policy denied the delivery. Adjust the route's `[policy]` section to resolve.
- **Capability suppressed**: `failure_kind: "capability_suppressed"` — the target adapter does not support the event's relation type (e.g. reactions, edits, replies). Check the transport profile capability declarations. The `suppression_reason`, `capability_field`, and `capability_level` fields in the receipt report dict identify which capability caused the suppression and at what level.
- **Outbox not owned**: `failure_kind: "outbox_not_owned"` — the durable outbox row was terminal or already active under another worker. No adapter delivery was attempted. This is a non-retryable runtime skip, not an adapter failure. Check the outbox state for the event to understand which worker or process already handled it.
- **Duplicate suppressed**: No receipt at all. The event was never stored (native-ref dedup at ingress). Check `RuntimeAccounting.loop_prevented` counters in diagnostics for aggregate counts.

### "Dead-lettered why?"

```bash
medre inspect event <event_id> --recovery --storage-path /path/to/medre.db
```

Look for `status: "dead_lettered"`. Trace `parent_receipt_id` back to the original failure. The event has exhausted its retry budget. Use `medre replay --mode BEST_EFFORT` if the underlying condition has resolved.

### "Queued locally but not RF-confirmed" (Meshtastic)

Meshtastic `sent` means local node sent the packet to the radio — not that any remote node received it. No transport-level confirmation is available. Check queue diagnostics:

```bash
medre evidence --storage-path /path/to/medre.db --json | jq '.sections.diagnostics_snapshot'
```

Look for `queue_total_sent`, `queue_total_failed`, `queue_total_rejected`.

### "Matrix E2EE blocked"

Check diagnostics for `undecryptable_event_count`. Non-zero indicates E2EE decryption
failures. Compare `megolm_recovery_attempts`, `megolm_recovery_successes`, and
`megolm_recovery_failures` to determine whether live missing-key recovery is being
accepted by the provider. Also inspect `cross_signing_chain_status`,
`cross_signing_current_device_self_signed`, and `cross_signing_reset_required`.
Cross-signing establishes MEDRE's own device identity but does not replace room-key
backup/import/export. If identity reset is required, back up the Matrix state directory
and prefer restoring matching state before using the explicit password-authenticated
reset command.

### "Meshtastic classifier ignored/dropped/deferred"

The packet classifier examines every inbound packet and decides: `relay`, `ignore`, `drop`, or `deferred`. Check counters:

| Counter                       | What it means                                                      |
| ----------------------------- | ------------------------------------------------------------------ |
| `classifier_packets_relayed`  | Proceeding to pipeline                                             |
| `classifier_packets_ignored`  | Skipped: ack/admin, telemetry, position, nodeinfo, DMs, empty text |
| `classifier_packets_dropped`  | Rejected: encrypted, malformed                                     |
| `classifier_packets_deferred` | Held for future: detection sensor, unknown portnum                 |

These are aggregate counters, not per-packet records. They reset on adapter restart.

## Attach Sanitized Output to Issues

The evidence bundle is designed to be safe to paste directly. Always review before sharing.

### Redaction Checklist

- Access tokens — search for `syt_`, `token`, `access_token`.
- User IDs — replace if you do not want them public.
- Room IDs — replace if private.
- Homeserver URLs — replace if internal-only.
- IP addresses — check the `diagnostics` section.
- Message content — verify no `body` or `content` fields contain actual messages.
- Meshtastic node info — redact if sensitive.

### Safe to Share

- `event_id`, `event_kind`, `source_adapter`
- Receipt status, target adapter, failure kind
- Native ref transport IDs
- Diagnostics health status
- `config_source`, `collected_at`, `evidence_level` timestamps

## Env-Only Fake Deployment

MEDRE can run entirely from environment variables without defining adapters or routes in YAML.

```yaml
# Minimal YAML
runtime:
  name: env-deployed

storage:
  backend: sqlite
  path: /var/medre/medre.db
```

```bash
# Matrix fake adapter
export MEDRE_ADAPTER__MATRIX_FAKE__TRANSPORT=matrix
export MEDRE_ADAPTER__MATRIX_FAKE__ADAPTER_KIND=fake
export MEDRE_ADAPTER__MATRIX_FAKE__HOMESERVER=https://matrix.example.test
export MEDRE_ADAPTER__MATRIX_FAKE__USER_ID=@bot:example.test
export MEDRE_ADAPTER__MATRIX_FAKE__ACCESS_TOKEN=fake-token

# Meshtastic fake adapter
export MEDRE_ADAPTER__RADIO_A__TRANSPORT=meshtastic
export MEDRE_ADAPTER__RADIO_A__ADAPTER_KIND=fake
export MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE=fake

# Route
export MEDRE_ROUTE__RADIO_TO_MATRIX__SOURCE_ADAPTERS=radio-a
export MEDRE_ROUTE__RADIO_TO_MATRIX__DEST_ADAPTERS=matrix-fake
export MEDRE_ROUTE__RADIO_TO_MATRIX__DIRECTIONALITY=source_to_dest
export MEDRE_ROUTE__RADIO_TO_MATRIX__ENABLED=true

# Run
medre run --config /path/to/config.yaml
```

Routes reference resolved adapter IDs (`radio-a`, `matrix-fake`), not env tokens (`RADIO_A`, `MATRIX_FAKE`).

### Environment Variable Rules

| Prefix                                             | Purpose                                |
| -------------------------------------------------- | -------------------------------------- |
| `MEDRE_ADAPTER__<TOKEN>__<FIELD>`                  | Runtime adapter config                 |
| `MEDRE_ROUTE__<TOKEN>__<FIELD>`                    | Runtime route config                   |
| `MEDRE_RETRY__<FIELD>`                             | Runtime retry config                   |
| `MATRIX_*`, `MESHTASTIC_*`, `MESHCORE_*`, `LXMF_*` | Pytest live-test convenience vars only |
| `MEDRE_MESHTASTIC_*`, etc.                         | Unsupported — rejected at startup      |

`MATRIX_*` variables are for pytest live-test convenience only. They are not read by `medre run`. To configure a Matrix adapter for runtime, use `MEDRE_ADAPTER__<TOKEN>__<FIELD>`.

## Matrix Live Session

If you have environment variables set for a Matrix adapter, you can validate MEDRE against a real homeserver.

### Prerequisites (Matrix Live)

- A Matrix homeserver (Synapse or Conduit) running and reachable.
- A bot account on that homeserver.
- The Matrix provider installed with its SDK; see [installation](install.md).

### Procedure — Matrix

1. Set environment variables:

```bash
export MEDRE_ADAPTER__MATRIX_PRIMARY__TRANSPORT=matrix
export MEDRE_ADAPTER__MATRIX_PRIMARY__HOMESERVER=http://localhost:8008
export MEDRE_ADAPTER__MATRIX_PRIMARY__USER_ID=@bot:localhost
export MEDRE_ADAPTER__MATRIX_PRIMARY__ACCESS_TOKEN="<matrix-access-token>"
export MEDRE_ADAPTER__MATRIX_PRIMARY__ROOM_ALLOWLIST="!abc123:localhost"
```

Do not commit these. Do not paste them into chat. Do not log them.

2. Start the runner:

```bash
medre run
```

You should see startup log lines confirming config loaded, pipeline started, and adapter connected. The key line is the adapter started message showing successful connection.

3. Validation checklist:
   - Send a message in the allowlisted room from a second Matrix account (not the bot).
   - Confirm the adapter receives it (check logs for errors).
   - Send a message through the adapter using `deliver()`.
   - Confirm it appears in the room via Element or another client.
   - Stop the runner with Ctrl+C. Confirm clean shutdown.

### Expected Output

Startup logs confirming config loaded, pipeline started, and adapter connected. Delivery receipts with genuine Synapse `event_id` values.

### Failure Modes (Matrix Live)

- Connection refused / timeout: homeserver unreachable, credentials invalid, or room does not exist. Verify the `MEDRE_ADAPTER__*` values.
- 401/403: access token expired or wrong user. Regenerate the token.
- Tests skipped: environment variables not set.

## Meshtastic Live Health Workflow

If you have a Meshtastic radio node accessible via TCP or serial, you can validate the Meshtastic adapter.

### Prerequisites — Meshtastic

- A Meshtastic radio node powered on and accessible via TCP (port 4403) or serial.
- The Meshtastic provider installed with its SDK; see [installation](install.md).
- The node on a non-critical channel (do not use emergency or default channel 0 for testing).

### Procedure — Meshtastic

1. Configure connection (TCP recommended):

```bash
# TCP mode
export MEDRE_ADAPTER__RADIO__CONNECTION_TYPE=tcp
export MEDRE_ADAPTER__RADIO__HOST=meshtastic.local

# Or serial mode
export MEDRE_ADAPTER__RADIO__CONNECTION_TYPE=serial
export MEDRE_ADAPTER__RADIO__SERIAL_PORT=/dev/ttyUSB0
```

1. Check storage evidence (receipts and delivery state from database):

```bash
medre evidence --storage-path /path/to/medre.db --json
```

2. Check live diagnostics (health output from running adapter):
   - `connected`: should be `true`
   - `node_info_present`: should be `true`
   - `last_received_at`: should be a recent timestamp

3. Send a test message from another Meshtastic node or the Meshtastic phone app.
4. Confirm the adapter receives it (check logs or storage).

### What Live Validates

- TCP or serial connection to the node.
- Pubsub callback registration for inbound messages.
- Outbound message queueing.
- Codec translation between Meshtastic packets and canonical events.

### What Live Does Not Validate

- Multi-hop delivery across the mesh.
- Telemetry, position, or nodeinfo portnum types (inbound is text only).
- Encryption or key management beyond firmware handling.
- Reliable delivery at the mesh layer.

### Failure Modes — Meshtastic

- Connection refused: node not reachable. Check network/serial connection.
- SDK import error: install the meshtastic extra.
- Adapter fails to start: verify the device path or hostname is correct.

## Diagnostics

### Build-Time Diagnostics

```bash
medre diagnostics
```

Builds runtime from config but does not start adapters, storage, or any I/O. Produces a pre-flight JSON snapshot.

### Live Health Refresh

```bash
medre diagnostics --refresh-health
```

Starts all enabled adapters, polls health once, prints snapshot with live health data, stops runtime cleanly. Starts real adapters — real Matrix adapters connect to homeservers, real Meshtastic adapters open serial/TCP ports.

### Interpreting Health

| Value      | Meaning                                                            |
| ---------- | ------------------------------------------------------------------ |
| `healthy`  | All started adapters report healthy                                |
| `degraded` | Some adapters report degraded/failed. Runtime may still be usable. |
| `failed`   | All adapters failed or health checks could not complete            |
| `unknown`  | No live health available                                           |

`startup_health` is frozen at startup time. `live_health` reflects the current state at refresh time. They can differ.

Failed health does not trigger automatic remediation. MEDRE does not restart adapters, routes, or the runtime based on health state.

## Delivery Outbox Inspection

The delivery outbox persists pending and retryable delivery work in SQLite. Operators can inspect outbox state to understand what deliveries are in progress or waiting.

### Check Outbox Counts

The runtime snapshot includes `outbox.counts` with per-status tallies.

### Query Outbox Items Directly

```bash
sqlite3 {state}/medre.sqlite "SELECT status, COUNT(*) FROM delivery_outbox GROUP BY status;"
```

### Outbox Statuses

| Status          | Meaning                                                                                        |
| --------------- | ---------------------------------------------------------------------------------------------- |
| `in_progress`   | Live pipeline delivery in progress. Not claimable by retry worker unless lease expires.        |
| `pending`       | Work exists but delivery attempt has not started.                                              |
| `retry_wait`    | Transient failure; will retry automatically when retry worker is enabled and item becomes due. |
| `queued`        | Accepted by adapter-local queue. After crash, this state is ambiguous.                         |
| `sent`          | Local send succeeded (terminal).                                                               |
| `dead_lettered` | Retries exhausted or permanent failure. Requires operator intervention.                        |
| `cancelled`     | Operator cancelled.                                                                            |
| `abandoned`     | Drain timeout during shutdown.                                                                 |

### Crash Recovery

Deliveries without a persisted outbox row are lost on crash; deliveries with
an outbox row survive and expired `in_progress` rows are reclaimed by the
RetryWorker after restart. `queued` rows are ambiguous — freshly queued rows
within the 300-second grace window are not reclaimed; stale ones are. The
canonical crash-recovery procedure lives in
[recovery-and-replay.md](recovery-and-replay.md).

## Matrix tx_id Deduplication

The Matrix adapter computes a deterministic `tx_id` (named `matrix_txn_id` internally) from `event_id + target_adapter + target_channel + room_id`. This `tx_id` is passed to the homeserver on every send attempt, including retries.

**What it does:** If the same `tx_id` is sent twice (e.g., a retry of a transient failure where the first attempt actually succeeded at the homeserver), the homeserver returns the original `event_id` instead of creating a duplicate event. This reduces duplicate retries.

**What it does not do:** This is not exactly-once delivery. The homeserver's deduplication window is finite. If the first attempt was lost before reaching the homeserver, the retry creates a new event (correct behaviour). There is no guarantee the homeserver remembers the `tx_id` across restarts or over long time windows.

Check the delivery receipt's metadata:

```json
{
  "adapter_message_id": "$mx_event_id",
  "metadata": {
    "matrix_txn_id": "medre_a1b2c3d4..."
  }
}
```

## Meshtastic Classifier Counters

The Meshtastic packet classifier examines every inbound packet and decides: `relay`, `ignore`, `drop`, or `deferred`. Check the aggregate counters:

```bash
medre evidence --storage-path /path/to/medre.db --json | jq '.sections.diagnostics_snapshot'
```

Primary counters:

| Counter                       | What it means                                                                  |
| ----------------------------- | ------------------------------------------------------------------------------ |
| `classifier_packets_seen`     | Total packets examined                                                         |
| `classifier_packets_relayed`  | Proceeding to pipeline                                                         |
| `classifier_packets_ignored`  | Skipped: ack/admin, telemetry, position, nodeinfo, direct messages, empty text |
| `classifier_packets_dropped`  | Rejected: encrypted packets, malformed payloads                                |
| `classifier_packets_deferred` | Held for future: detection sensor, unknown portnum, plugin-only                |

Sub-counters break down by reason:

| Sub-counter                                    | Classification reason                      |
| ---------------------------------------------- | ------------------------------------------ |
| `classifier_packets_malformed`                 | Dropped: no valid decoded payload          |
| `classifier_packets_encrypted_dropped`         | Dropped: packet is encrypted               |
| `classifier_packets_detection_sensor_deferred` | Deferred: detection sensor portnum         |
| `classifier_packets_dm_ignored`                | Ignored: direct message to a specific node |
| `classifier_packets_empty_text_ignored`        | Ignored: text message with empty body      |
| `classifier_packets_unknown_portnum_deferred`  | Deferred: unknown or custom portnum        |

These are aggregate counters, not per-packet records. They reset on adapter restart (in-memory only).

## Summary: Evidence Non-Guarantees

| Question                     | Answer                                                           | Evidence available?                 |
| ---------------------------- | ---------------------------------------------------------------- | ----------------------------------- |
| Delivered where?             | Receipt shows target adapter, channel, native message ID, route  | Yes (receipt + timeline)            |
| Retried why?                 | Receipt lineage shows failure kind, attempt number, retry policy | Yes (recovery context)              |
| Suppressed why (loop)?       | Route-trace or self-loop guard fired                             | Yes (receipt failure_kind)          |
| Suppressed why (policy)?     | Route-policy denial after route match                            | Yes (receipt failure_kind + reason) |
| Suppressed why (duplicate)?  | Native-ref dedup at ingress                                      | No receipt — counters only          |
| Dead-lettered why?           | Retry exhaustion after transient failures                        | Yes (receipt chain)                 |
| Queued but RF-confirmed?     | Meshtastic `sent` means local node only                          | Yes (queue stats, but no RF ack)    |
| Matrix tx_id used?           | Deterministic dedup reduces duplicates                           | Yes (receipt metadata)              |
| Matrix tx_id exactly-once?   | No — homeserver dedup window is finite                           | No — this is not guaranteed         |
| Matrix E2EE blocked?         | Undecryptable/key-request counters show recovery state           | Yes (E2EE diagnostics)              |
| Meshtastic classifier stats? | Aggregate inbound skip counts                                    | Yes (diagnostics classifier\_\*)    |
| Classifier stats per-packet? | No — aggregate only, reset on restart                            | No — in-memory counters only        |

## Quick Reference

```bash
# Pre-flight
medre config check --config config.yaml
medre routes validate --config config.yaml

# Smoke test (use a config with storage.backend: sqlite for persistence)
medre smoke --config config.yaml --json

# Inspect (primary path)
medre inspect event <event_id> --storage-path /tmp/medre.db
medre inspect receipts --event <event_id> --storage-path /tmp/medre.db

# Deeper investigation
medre inspect event <event_id> --timeline --storage-path /tmp/medre.db
medre inspect event <event_id> --evidence --storage-path /tmp/medre.db
medre inspect event <event_id> --recovery --storage-path /tmp/medre.db

# Evidence bundle
medre evidence --storage-path /tmp/medre.db --json

# Trace (specialized)
medre trace event <event_id> --storage-path /tmp/medre.db --json

# Replay (recovery only)
medre replay --mode DRY_RUN --config config.yaml --event <event_id> --json
medre replay --mode BEST_EFFORT --config config.yaml --event <event_id> --json

# Diagnostics
medre diagnostics
medre diagnostics --refresh-health
```

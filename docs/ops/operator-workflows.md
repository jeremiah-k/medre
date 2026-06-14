# Operator Workflows

Day-to-day workflows for operating MEDRE: smoke testing, inspect-first investigation, evidence collection, event tracing, failure handling, and replay.

## Data Ownership and Immutability

Understanding what is permanent, what is mutable, and what is derived helps you interpret storage output correctly and avoid actions that cannot succeed.

### What is permanent and immutable

- **Canonical events** are never updated or deleted. Every event that entered the pipeline is preserved as an immutable fact. The event log is the definitive record of what happened.
- **Delivery receipts** are append-only. Each delivery attempt produces a new receipt row. Old receipt rows are never changed. The current delivery status is a projection (latest receipt by sequence number), not a mutable field.
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

For the detailed per-table ownership audit, see [persistence-authority-audit.md](../dev/persistence-authority-audit.md).

## Pre-Release Storage Reset

MEDRE is prerelease software. The schema version is frozen at 1, but the column shape (which tables and columns exist) can change between builds. When you upgrade to a newer prerelease build that adds or renames columns, the existing database will be rejected at startup with a `PreReleaseSchemaMismatchError`. No automatic migration runs. You need to reset the database manually.

### How to tell if your database is stale

Run `medre storage status --storage-path <path>` to check the database path and schema state. Use `medre paths` to discover the resolved state directory, then pass `{state}/medre.sqlite` as the storage path. If the database was created by an older prerelease build, the command reports which tables have missing columns.

Alternatively, if MEDRE fails to start with an error like:

```text
PreReleaseSchemaMismatchError: Pre-release schema shape mismatch: table
'delivery_outbox' is missing required columns ['failure_kind_detail'].
(database: /home/user/.local/state/medre/medre.sqlite) The database was
likely created by an older pre-release build.  Please recreate the database
— no automatic migration is provided.
```

then the database is stale and needs a reset.

### Step-by-step reset procedure

1. **Find the database path.**

   ```bash
   medre storage status --storage-path ~/.local/state/medre/medre.sqlite
   ```

   The `--storage-path` flag is required. Use `medre paths` to see the resolved state directory if you don't know the database path.

2. **Back up the old database.**

   ```bash
   cp ~/.local/state/medre/medre.sqlite ~/.local/state/medre/medre.sqlite.bak
   ```

   Keep the backup if the old data has investigative value. The backup is a plain SQLite file you can query with `sqlite3`.

3. **Delete the database file.**

   ```bash
   rm ~/.local/state/medre/medre.sqlite
   rm ~/.local/state/medre/medre.sqlite-wal 2>/dev/null
   rm ~/.local/state/medre/medre.sqlite-shm 2>/dev/null
   ```

   Or use `medre storage reset --storage-path <path> --backup --yes`, which handles the backup and deletion:

   ```bash
   medre storage reset --storage-path ~/.local/state/medre/medre.sqlite --backup --yes
   ```

   This creates a timestamped `.bak-<timestamp>.db` copy and removes the original along with any WAL/SHM sidecar files. It does not start the runtime or create a new database.

4. **Rerun MEDRE.**

   ```bash
   medre run --config your-config.toml
   ```

   The next startup creates a fresh database with the current schema shape.

### What happens to the old data

Nothing from the old database is carried forward. The reset is total. Events, receipts, native refs, and outbox state from the old database are gone. The new database starts empty.

If you need data from the old database, query the backup file with `sqlite3` or `medre inspect --storage-path /path/to/backup.sqlite`.

### When this does not apply

This workflow only applies to prerelease builds where the column shape changed. After MEDRE makes its first release, schema migrations will be provided and this manual reset workflow will not be needed for compatible upgrades.

## Preferred Product Path

The recommended operator loop:

1. **Pre-flight**: `medre config check` and `medre routes validate`
2. **Optional smoke**: `medre smoke --config <sqlite-config> --json`
3. **Inspect-first**: `medre inspect event` and `medre inspect receipts`
4. **Deeper investigation**: `--timeline`, `--evidence`, `--recovery` flags on inspect
5. **Replay only when needed**: `DRY_RUN` first, then `BEST_EFFORT`

> Replay is operator-initiated and one-shot. It does not continuously tail the event log. Each replay invocation processes stored events once and exits. Replay requires `--config`, not `--storage-path`.

## Smoke Test

The fastest way to confirm MEDRE works on your machine. No network, no credentials.

```bash
# Source checkout (in-memory storage, ephemeral)
medre smoke --config examples/configs/fake-bridge-smoke.toml --json

# Source checkout (persistent SQLite — edit config to set storage.backend = "sqlite")
medre smoke --config /tmp/medre-sqlite.toml --json

# Installed package
medre config sample > /tmp/medre-walkthrough.toml
# Edit storage section: backend = "sqlite", path = "/tmp/medre-walkthrough.db"
medre smoke --config /tmp/medre-walkthrough.toml --json
```

Expected: JSON with `"status": "passed"`, an `event_id`, and delivery receipts.

### What Smoke Validates

- Pipeline builds and starts without errors.
- Canonical event flows through codec, renderer, and session stages.
- Fake adapter receives and acknowledges the event.
- Storage round-trips correctly.
- Diagnostics collected and reported.

### What Smoke Does Not Validate

- Any real network communication.
- Matrix SDK behaviour against a real homeserver.
- E2EE crypto operations.
- Meshtastic radio or serial communication.

Storage backend is determined by the config file. With `storage.backend = "memory"` (the default in shipped configs), evidence is discarded after the run. For persistent evidence, set `storage.backend = "sqlite"` with a `path` in the config.

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

### Timeline Entry Types

| Entry type   | What it shows                                                          |
| ------------ | ---------------------------------------------------------------------- |
| `event`      | Canonical event (kind, source adapter, timestamp)                      |
| `native_ref` | Native transport references (Matrix event IDs, Meshtastic message IDs) |
| `receipt`    | Delivery receipts (status, target adapter, attempt count)              |
| `relation`   | Relations to other events (replies, reactions)                         |

### Interpreting Timeline Gaps

- **No routing phase**: Event had no matching routes. Check route config.
- **No delivery phase**: No receipt exists. Runtime may have crashed mid-delivery, or delivery was never attempted. Loop prevention, capacity exceeded, and shutdown rejection produce `suppressed` receipts — those appear as a delivery phase.
- **Multiple delivery phases, same target**: Retry chain. Check `attempt_number` and `parent_receipt_id`.
- **Multiple delivery phases, different targets**: Fan-out.
- **Both `live` and `replay` phases**: Event was originally delivered and later re-delivered via replay. Use `source` field to distinguish.

### Receipt and Native-Ref Fields

**DeliveryReceipt:**

| Field               | Description                                                     |
| ------------------- | --------------------------------------------------------------- |
| `receipt_id`        | Unique receipt identifier                                       |
| `event_id`          | Canonical event                                                 |
| `target_adapter`    | Adapter that received the delivery                              |
| `route_id`          | Route that matched the event                                    |
| `status`            | `sent`, `failed`, `suppressed`, etc.                            |
| `failure_kind`      | Failure classification or `null`                                |
| `attempt_number`    | 1 for first attempt, increments on retry                        |
| `parent_receipt_id` | Links to previous receipt in retry chain                        |
| `source`            | `"live"`, `"retry"`, or `"replay"`                              |
| `replay_run_id`     | Groups receipts from one replay run (when `source == "replay"`) |

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
       COUNT(CASE WHEN r.source = 'live' THEN 1 END) AS live_deliveries,
       COUNT(CASE WHEN r.source = 'replay' THEN 1 END) AS replay_deliveries
FROM canonical_events e
JOIN delivery_receipts r ON e.event_id = r.event_id
GROUP BY e.event_id
HAVING live_deliveries > 0 AND replay_deliveries > 0;

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

When something goes wrong, `medre evidence` collects a diagnostic bundle. It is safe to paste into a GitHub issue — no secrets are included.

### Offline Mode (From Storage)

```bash
medre evidence --storage-path /path/to/medre.db --json
```

Opens the database in read-only mode, collects config, diagnostics, route information, and event data.

### Scope to a Specific Event

```bash
medre evidence --storage-path /path/to/medre.db --event <event_id> --json
medre evidence --storage-path /path/to/medre.db --replay-run <run_id> --json
```

### What Is in the Bundle

| Section                         | Contents                                                  |
| ------------------------------- | --------------------------------------------------------- |
| `sections.config_summary`       | Redacted runtime config and resolved paths                |
| `sections.route_validation`     | Route definitions and validation status                   |
| `sections.diagnostics_snapshot` | Per-adapter health and counters                           |
| `sections.live_health`          | Live adapter health (requires `--include-refresh-health`) |
| `sections.storage`              | Event and delivery records from SQLite                    |

Top-level fields: `schema_version`, `medre_version`, `command`, `status`, `collected_at`, `config_source`, `errors`, `limitations`.

### Safety

- Access tokens and credentials are redacted.
- No message content — only metadata (event IDs, timestamps, adapter IDs, delivery status).
- No network addresses beyond homeserver URL.

Always review the JSON output before sharing. Look for any field containing your actual access token. If you find one, that is a bug.

## Delivery Failure Classification

### Receipt Statuses

| Status          | Meaning                                                                |
| --------------- | ---------------------------------------------------------------------- |
| `queued`        | Enqueued for async delivery                                            |
| `sent`          | Adapter confirmed delivery                                             |
| `failed`        | Delivery failed — check `failure_kind`                                 |
| `dead_lettered` | All retries exhausted                                                  |
| `suppressed`    | Intentionally suppressed (loop prevention, policy, capacity, shutdown) |

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
   medre replay --mode DRY_RUN --config bridge.toml --event <event_id> --json
   ```

2. Review output — check which routes match and which events would be re-delivered.

3. If preview is correct, best-effort replay:

   ```bash
   medre replay --mode BEST_EFFORT --config bridge.toml --event <event_id> --json
   ```

4. Trace the replayed events:

   ```bash
   medre inspect event <event_id> --timeline --storage-path /path/to/medre.db
   ```

5. Check that replay receipts have `source='replay'` and the expected `replay_run_id`.

Replay requires a config file with declared routes and adapters. It is config-required and duplicate-risky — always run `DRY_RUN` first.

### Replay and Retry Interaction

`BEST_EFFORT` replay through a route with retry enabled will create retry receipts if delivery fails transiently. These carry `source='replay'` and `replay_run_id`. The `medre replay` command does not start the RetryWorker. If the runtime is later started with `[retry] enabled = true`, the worker will discover and process these receipts.

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

Check diagnostics for `undecryptable_event_count`. Non-zero indicates E2EE decryption failures. Common causes: crypto store missing room key, key rotation before bot received it, cross-signing not set up. MEDRE does not manage key distribution.

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

MEDRE can run entirely from environment variables without defining adapters or routes in TOML.

```toml
# Minimal TOML
[runtime]
name = "env-deployed"

[storage]
backend = "sqlite"
path = "/var/medre/medre.db"
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
medre run --config /path/to/config.toml
```

Routes reference resolved adapter IDs (`radio-a`, `matrix-fake`), not env tokens (`RADIO_A`, `MATRIX_FAKE`).

### Environment Variable Rules

| Prefix                                             | Purpose                                  |
| -------------------------------------------------- | ---------------------------------------- |
| `MEDRE_ADAPTER__<TOKEN>__<FIELD>`                  | Runtime adapter config                   |
| `MEDRE_ROUTE__<TOKEN>__<FIELD>`                    | Runtime route config                     |
| `MEDRE_RETRY__<FIELD>`                             | Runtime retry config                     |
| `MATRIX_*`, `MESHTASTIC_*`, `MESHCORE_*`, `LXMF_*` | Pytest live-test convenience vars only   |
| `MEDRE_MESHTASTIC_*`, etc.                         | Unsupported legacy — rejected at startup |

`MATRIX_*` variables are for pytest live-test convenience only. They are not read by `medre run`. To configure a Matrix adapter for runtime, use `MEDRE_ADAPTER__<TOKEN>__<FIELD>`.

## Matrix Live Session

If you have environment variables set for a Matrix adapter, you can validate MEDRE against a real homeserver.

### Prerequisites (Matrix Live)

- A Matrix homeserver (Synapse or Conduit) running and reachable.
- A bot account on that homeserver.
- The `matrix` extra installed: `pip install -e ".[matrix]"`.

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
- The `meshtastic` extra installed: `pip install -e ".[meshtastic]"`.
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

- Deliveries that never created an outbox row are lost on crash (no durable state).
- Deliveries with a persisted outbox row survive the crash.
- Expired `in_progress` rows become reclaimable by the RetryWorker after restart.
- `queued` outbox rows after a crash are ambiguous — the adapter may have sent the message before crashing or not. Freshly queued rows (within the 300-second grace window) are not reclaimed. Stale queued rows past the grace threshold are automatically reclaimed.

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
| Matrix E2EE blocked?         | Undecryptable events counted in diagnostics                      | Yes (undecryptable_event_count)     |
| Meshtastic classifier stats? | Aggregate inbound skip counts                                    | Yes (diagnostics classifier\_\*)    |
| Classifier stats per-packet? | No — aggregate only, reset on restart                            | No — in-memory counters only        |

## Quick Reference

```bash
# Pre-flight
medre config check --config config.toml
medre routes validate --config config.toml

# Smoke test (use a config with storage.backend = "sqlite" for persistence)
medre smoke --config config.toml --json

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
medre replay --mode DRY_RUN --config config.toml --event <event_id> --json
medre replay --mode BEST_EFFORT --config config.toml --event <event_id> --json

# Diagnostics
medre diagnostics
medre diagnostics --refresh-health
```

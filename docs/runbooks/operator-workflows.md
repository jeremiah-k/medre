# Operator Workflows

> Last updated: 2026-05-21
> Scope: Day-to-day MEDRE operation for a single operator
> Status: **Alpha. Not production. Not hardened. Not complete.** Everything here is subject to change without notice.

This runbook covers the practical side of running MEDRE: installing it, running a quick smoke test, validating against a real Matrix homeserver or Meshtastic node, collecting evidence when something goes wrong, and reading diagnostic output. It is written for a single operator on a single machine. It does not cover deployment, scaling, monitoring, or multi-node setups, because none of those exist yet.

If something has not been tested and confirmed working, this document says so. If something is known to be broken or missing, this document says that too.

For per-transport capability tracking, see `docs/STATUS.md`. That file is the single source of truth for what works, what is fake-tested, and what has live validation evidence.

Test developers should read `docs/dev/live-test-harness.md` for the live test patterns and conventions. This document is for operators.

## 1. Prerequisites

| Requirement                  | Details                                                                                   |
| ---------------------------- | ----------------------------------------------------------------------------------------- |
| Python                       | 3.11 or later (3.12 for LXMF)                                                             |
| pip                          | Recent enough to handle extras (`pip >= 21.3`)                                            |
| Git                          | For cloning the repo                                                                      |
| Matrix homeserver (optional) | Synapse or Conduit, local or reachable. Only needed for live Matrix sessions.             |
| Meshtastic node (optional)   | A real radio node accessible via TCP or serial. Only needed for live Meshtastic sessions. |

You do not need Docker for the basic workflow. You do not need any transport credentials to run the fake local smoke test.

## 2. Setup

### 2.1 Install

```bash
git clone <repo-url> && cd medre
pip install -e ".[dev]"
```

This installs MEDRE and all dev dependencies. For transport-specific live sessions, install the corresponding extras:

```bash
# Matrix (plaintext alpha)
pip install -e ".[matrix]"

# Matrix (E2EE text alpha, encrypted rooms)
pip install -e ".[matrix-e2e]"

# Meshtastic (real radio)
pip install -e ".[meshtastic]"
```

### 2.2 Verify the install

```bash
medre --help
```

If this prints a help message, the install worked. If it prints `command not found`, check that your virtualenv is active and the install completed without errors.

### Environment Variables

MEDRE uses different environment variable sets depending on context:

**Runtime config** (read by `medre run` and all config-backed commands):

- `MEDRE_ADAPTER__<TOKEN>__<FIELD>` — adapter instance config
- `MEDRE_ROUTE__<TOKEN>__<FIELD>` — route config
- `MEDRE_HOME`, `MEDRE_DB_PATH`, `MEDRE_LOG_LEVEL` — core runtime
- `MEDRE_RETRY__<FIELD>` — retry worker config

**Pytest live-test convenience vars** (read by `pytest -m live` only):

- `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, `MATRIX_ACCESS_TOKEN`, `MATRIX_ROOM_ID`
- `MESHTASTIC_CONNECTION_TYPE`, `MESHTASTIC_HOST`, `MESHTASTIC_SERIAL_PORT`
- `MESHCORE_CONNECTION_TYPE`, `MESHCORE_HOST`
- `LXMF_CONNECTION_TYPE`

**Unsupported legacy** (rejected at startup):

- `MEDRE_MATRIX_*`, `MEDRE_MESHTASTIC_*`, `MEDRE_MESHCORE_*`, `MEDRE_LXMF_*`

> **Important:** `MATRIX_*` variables are for pytest live-test convenience only.
> They are **not** read by `medre run`. To configure a Matrix adapter for
> runtime operation, use `MEDRE_ADAPTER__<TOKEN>__<FIELD>`.

## 3. End-to-End Fake Local Run Session

The fastest way to confirm MEDRE works on your machine. No network, no credentials, no external services.

### 3.1 Run the smoke test

```bash
PYTHONPATH=src medre smoke
```

This builds a pipeline with fake adapters, runs a message through it, and prints a summary. You should see output like:

```text
Smoke test: PASSED
  Evidence level: fake_bridge
  Events processed: 1
  ...
```

For machine-readable output:

```bash
PYTHONPATH=src medre smoke --json
```

The `evidence_level` field will say `fake_bridge`. That is honest. A fake smoke test proves the pipeline wiring works. It does not prove the Matrix adapter talks to a real homeserver.

### 3.2 What the smoke test validates

1. The pipeline builds and starts without errors.
2. A canonical event flows through the codec, renderer, and session stages.
3. The fake adapter receives and acknowledges the event.
4. Storage round-trips correctly.
5. Diagnostics are collected and reported.

### 3.3 What the smoke test does NOT validate

1. Any real network communication.
2. Matrix SDK behavior against a real homeserver.
3. E2EE crypto operations.
4. Meshtastic radio or serial communication.
5. Anything beyond a single event on a single fake adapter.

### 3.4 Full fake run-session walkthrough

For a more complete fake validation that exercises the run-session path:

1. Create a minimal config file pointing to fake adapters (no env vars needed):

```bash
cat > /tmp/medre-fake.toml <<'EOF'
[storage]
backend = "sqlite"
path = "/tmp/medre-fake.db"

[adapters.fake_a]
type = "fake"
adapter_id = "fake-source"

[adapters.fake_b]
type = "fake"
adapter_id = "fake-target"

[[routes]]
source = "fake-source"
target = "fake-target"
EOF
```

2. Run the pipeline:

```bash
PYTHONPATH=src medre run --config /tmp/medre-fake.toml
```

3. Check the database for stored events:

```bash
PYTHONPATH=src medre inspect event --storage-path /tmp/medre-fake.db --timeline
```

4. Collect evidence from the fake session:

```bash
PYTHONPATH=src medre evidence --storage-path /tmp/medre-fake.db --json
```

5. Verify the evidence bundle contains `config_source: "storage_path"` and `evidence_level: "fake_bridge"`.

This walkthrough confirms the full pipeline path (config load, adapter wire, codec, storage, diagnostics) without touching any network. The `config_source` field in the evidence report confirms where the data came from. See `docs/STATUS.md` for the "Fake lifecycle" capability row.

## Env-Only Fake Deployment

MEDRE can run entirely from environment variables without defining adapters or routes in TOML.

### Minimal TOML

The TOML config only needs runtime and storage basics:

```toml
[runtime]
name = "env-deployed"

[storage]
backend = "sqlite"
path = "/var/medre/medre.db"
```

### Env Adapter Creation

Create adapters using `MEDRE_ADAPTER__<TOKEN>__TRANSPORT`:

```bash
# Matrix adapter
export MEDRE_ADAPTER__MATRIX_FAKE__TRANSPORT=matrix
export MEDRE_ADAPTER__MATRIX_FAKE__ADAPTER_KIND=fake
export MEDRE_ADAPTER__MATRIX_FAKE__HOMESERVER=https://matrix.example.test
export MEDRE_ADAPTER__MATRIX_FAKE__USER_ID=@bot:example.test
export MEDRE_ADAPTER__MATRIX_FAKE__ACCESS_TOKEN=fake-token
export MEDRE_ADAPTER__MATRIX_FAKE__ROOM_ALLOWLIST=!room:example.test

# Meshtastic adapter
export MEDRE_ADAPTER__RADIO_A__TRANSPORT=meshtastic
export MEDRE_ADAPTER__RADIO_A__ADAPTER_KIND=fake
export MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE=fake
export MEDRE_ADAPTER__RADIO_A__MESHNET_NAME=RadioA
```

### Env Route Creation

Create routes using `MEDRE_ROUTE__<TOKEN>__<FIELD>`:

```bash
export MEDRE_ROUTE__RADIO_TO_MATRIX__SOURCE_ADAPTERS=radio-a
export MEDRE_ROUTE__RADIO_TO_MATRIX__DEST_ADAPTERS=matrix-fake
export MEDRE_ROUTE__RADIO_TO_MATRIX__DIRECTIONALITY=source_to_dest
export MEDRE_ROUTE__RADIO_TO_MATRIX__ENABLED=true
```

Routes reference resolved adapter IDs (`radio-a`, `matrix-fake`), not env tokens (`RADIO_A`, `MATRIX_FAKE`).

### Running

```bash
medre run --config /path/to/config.toml
```

### Inspecting

```bash
# Evidence bundle
medre evidence --config /path/to/config.toml --json

# Trace
medre trace event <EVENT_ID> --config /path/to/config.toml --json

# Inspect
medre inspect event <EVENT_ID> --config /path/to/config.toml
```

### Environment Variable Rules

| Prefix                                             | Purpose                                      |
| -------------------------------------------------- | -------------------------------------------- |
| `MEDRE_ADAPTER__<TOKEN>__<FIELD>`                  | Runtime adapter config                       |
| `MEDRE_ROUTE__<TOKEN>__<FIELD>`                    | Runtime route config                         |
| `MEDRE_RETRY__<FIELD>`                             | Runtime retry config                         |
| `MATRIX_*`, `MESHTASTIC_*`, `MESHCORE_*`, `LXMF_*` | Pytest live-test convenience vars only       |
| `MEDRE_MESHTASTIC_*`, etc.                         | **Unsupported legacy** — rejected at startup |

### Sharing Output

Use `--json` flags for machine-readable output. Sanitize logs and evidence bundles before sharing: all access tokens, secrets, and credentials are automatically redacted from evidence output, log files, and error messages.

## Reading Delivery Reliability Reports

MEDRE records every delivery attempt as a structured receipt. Operators can inspect delivery outcomes, retries, and suppressions through evidence bundles and trace timelines.

### Delivery Outcome Statuses

Each delivery attempt produces a `DeliveryOutcome` with one of these statuses:

| Status              | Meaning                                                                  |
| ------------------- | ------------------------------------------------------------------------ |
| `success`           | The adapter accepted the message and returned a native message ID.       |
| `queued`            | The adapter enqueued the message for async delivery.                     |
| `transient_failure` | A temporary error (timeout, connection reset). Retryable.                |
| `permanent_failure` | A non-retryable error (malformed payload, auth rejection).               |
| `skipped`           | Delivery was skipped (loop prevention, suppression, capacity rejection). |

### Receipt Statuses

Receipts persisted to storage have a finer-grained lifecycle:

| Status          | Meaning                                                               |
| --------------- | --------------------------------------------------------------------- |
| `accepted`      | Initial state — delivery plan accepted.                               |
| `queued`        | Enqueued for async delivery (queue-based transports).                 |
| `sent`          | Adapter confirmed delivery.                                           |
| `failed`        | Delivery attempt failed (check `failure_kind` for details).           |
| `dead_lettered` | All retry attempts exhausted — no further delivery will be attempted. |

### Failure Classification

The `failure_kind` field on receipts classifies failures:

| Kind                   | Retryable | When                                                                                                                                                                         |
| ---------------------- | --------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `adapter_transient`    | Yes       | Timeout, network error, connection reset                                                                                                                                     |
| `adapter_permanent`    | No        | Malformed payload, business-logic rejection                                                                                                                                  |
| `adapter_missing`      | No        | Target adapter not registered in the runtime                                                                                                                                 |
| `planner_failure`      | No        | Routing or planning misconfiguration                                                                                                                                         |
| `renderer_failure`     | No        | No renderer registered for the event kind                                                                                                                                    |
| `capacity_rejection`   | No        | All in-flight delivery slots occupied                                                                                                                                        |
| `duplicate_suppressed` | No        | Reserved — defined in the enum but not currently emitted as a receipt or outcome. Duplicate native-ref suppression happens before routing and returns an empty outcome list. |
| `loop_suppressed`      | No        | Route-trace or self-loop prevention blocked the delivery                                                                                                                     |

Only `adapter_transient` is retryable.

### Retry and Replay

- **Retries** are handled by `RetryWorker` — a background task that polls for
  transient-failure receipts and re-attempts delivery with exponential backoff.
  Retries are opt-in (`[retry] enabled = true` in config).
  Retry is opt-in and can be configured through `MEDRE_RETRY__` environment
  variables (or the `[retry]` TOML section). Retry mechanisms are documented
  and unit-tested but were not live-validated by this tranche.
- **Replay** is a separate mechanism that re-processes historical events through
  the pipeline. Replayed deliveries are tagged `source="replay"` with a
  `replay_run_id` for identification.

### Inspection

Use the CLI to inspect delivery details:

```bash
# Evidence bundle — includes receipt and native-ref summary:
medre evidence --config /path/to/config.toml --json

# Trace — chronological timeline for a specific event:
medre trace event <EVENT_ID> --config /path/to/config.toml --json

# Inspect — unified event details with receipts and native refs:
medre inspect event <EVENT_ID> --config /path/to/config.toml
```

Sanitized JSON example (`failure_kind` and `attempt_number` visible):

```json
{
  "receipt_id": "rcpt-...",
  "event_id": "evt-...",
  "route_id": "radio-to-matrix",
  "target_adapter": "matrix-fake",
  "status": "failed",
  "failure_kind": "adapter_transient",
  "attempt_number": 1,
  "error": "..."
}
```

No secrets or access tokens appear in evidence output.

## 4. Matrix Live Run Session

If you have `MEDRE_ADAPTER__<TOKEN>__*` variables set for a Matrix transport, you can validate MEDRE against a real homeserver. This section assumes you have already set up a homeserver and bot account. If you have not, the full setup instructions are in `docs/runbooks/matrix-alpha-operation.md`.

### 4.1 Set environment variables

```bash
export MEDRE_ADAPTER__MATRIX_PRIMARY__TRANSPORT=matrix
export MEDRE_ADAPTER__MATRIX_PRIMARY__HOMESERVER=http://localhost:8008
export MEDRE_ADAPTER__MATRIX_PRIMARY__USER_ID=@bot:localhost
export MEDRE_ADAPTER__MATRIX_PRIMARY__ACCESS_TOKEN=syt_xxxxxxxxxxxxx
export MEDRE_ADAPTER__MATRIX_PRIMARY__ROOM_ALLOWLIST="!abc123:localhost"
export MEDRE_ROUTE__PRIMARY_TO_MESH__SOURCE_ADAPTERS=matrix-primary
export MEDRE_ROUTE__PRIMARY_TO_MESH__DEST_ADAPTERS=meshtastic-radio
export MEDRE_ROUTE__PRIMARY_TO_MESH__DIRECTIONALITY=source_to_dest
export MEDRE_ROUTE__PRIMARY_TO_MESH__ENABLED=true
```

> **Note:** The `MATRIX_*` variables (`MATRIX_HOMESERVER`, `MATRIX_USER_ID`, etc.)
> are pytest live-test convenience vars. They are **not** read by `medre run`.
> Use `MEDRE_ADAPTER__<TOKEN>__<FIELD>` to configure Matrix adapters for runtime.

Do not commit these. Do not paste them into chat. Do not log them. They are credentials.

### 4.2 Start the runner

```bash
PYTHONPATH=src medre run
```

You should see startup log lines confirming config loaded, pipeline started, and adapter connected. The key line is:

```text
Matrix Operation Alpha running — awaiting shutdown signal
```

If you see that, the runner validated all env vars, initialized storage, started the pipeline, connected to the homeserver, and began the sync loop. Press Ctrl+C to stop.

### 4.3 Quick validation checklist

1. Send a message in the allowlisted room from a second Matrix account (not the bot).
2. Confirm the adapter receives it (check logs for any errors).
3. Send a message through the adapter using `deliver()`.
4. Confirm it appears in the room via Element or another client.
5. Stop the runner with Ctrl+C. Confirm clean shutdown.

If all five steps pass, the Matrix live path is working. If any step fails, see section 10 for diagnosis and `docs/runbooks/matrix-alpha-operation.md` for troubleshooting.

### 4.4 Matrix live evidence schema fields

When you collect evidence from a live Matrix session, the report includes these canonical fields:

| Field                            | Description                                                   |
| -------------------------------- | ------------------------------------------------------------- |
| `config_source`                  | How the bundle was collected (`"config"` or `"storage_path"`) |
| `collected_at`                   | ISO 8601 timestamp of collection                              |
| `evidence_level`                 | `fake_bridge` or `live_bridge` depending on adapter type      |
| `diagnostics.connected`          | Whether the nio client has an active connection               |
| `diagnostics.logged_in`          | Whether the client is authenticated                           |
| `diagnostics.sync_task_running`  | Whether the sync loop is active                               |
| `diagnostics.rooms_tracked`      | Number of rooms being tracked                                 |
| `diagnostics.delivery_attempts`  | Cumulative outbound delivery count                            |
| `diagnostics.delivery_successes` | Successful deliveries                                         |
| `diagnostics.delivery_failures`  | Failed deliveries                                             |

See `docs/runbooks/matrix-alpha-operation.md` section 9 for the full diagnostics schema.

## 5. Meshtastic Live Health Workflow

If you have `MESHTASTIC_*` environment variables set, you can validate the Meshtastic adapter against a real radio node. Fake mode is the default and recommended path. Real connectivity is opt-in for live validation.

### 5.1 Prerequisites

1. A Meshtastic radio node powered on and accessible via TCP (port 4403) or serial.
2. The `meshtastic` extra installed: `pip install -e ".[meshtastic]"`.
3. The node on a non-critical channel (do not use emergency or default channel 0 for testing).

### 5.2 Set environment variables

```bash
# TCP mode (recommended)
export MESHTASTIC_HOST="meshtastic.local"
export MESHTASTIC_CHANNEL_INDEX="0"

# Or serial mode
export MESHTASTIC_SERIAL_PORT="/dev/ttyUSB0"
export MESHTASTIC_CHANNEL_INDEX="0"
```

### 5.3 Start the Meshtastic runner

```bash
PYTHONPATH=src medre run --config /path/to/meshtastic-bridge.toml
```

Or use the adapter directly in a test script. See `docs/runbooks/meshtastic-alpha-operation.md` for the full wiring instructions.

### 5.4 Live health check

1. Confirm the adapter starts and reports `healthy`:

```bash
PYTHONPATH=src medre evidence --config /path/to/meshtastic-bridge.toml --include-refresh-health --json
```

2. Check the diagnostics section of the output:
   - `connected`: should be `true`
   - `node_info_present`: should be `true`
   - `last_received_at`: should be a recent timestamp

3. Send a test message from another Meshtastic node or the Meshtastic phone app.
4. Confirm the adapter receives it (check logs or storage).

### 5.5 What Meshtastic live validates

1. TCP or serial connection to the node.
2. Pubsub callback registration for inbound messages.
3. Outbound message queueing via `send_one`.
4. Codec translation between Meshtastic packets and canonical events.

### 5.6 What Meshtastic live does NOT validate

1. Multi-hop delivery across the mesh.
2. Telemetry, position, or nodeinfo portnum types (inbound processing is text only).
3. Encryption or key management beyond what the Meshtastic firmware handles.
4. Reliable delivery at the mesh layer.

See `docs/STATUS.md` for the current Meshtastic capability status. As of this writing, Meshtastic has fake-tested lifecycle and opt-in live test harness, but no recorded live validation evidence.

## 6. Collect Evidence

When something goes wrong, the `evidence` command collects a bundle of diagnostic data. The bundle is safe to paste into a GitHub issue. It does not contain secrets.

### 6.1 Offline mode (from storage)

If you have a storage path (SQLite database file) from a previous run:

```bash
PYTHONPATH=src medre evidence --storage-path /path/to/medre.db
```

This opens the database in read-only mode, collects config, diagnostics, route information, and event data, and prints a summary.

For JSON output:

```bash
PYTHONPATH=src medre evidence --storage-path /path/to/medre.db --json
```

### 6.2 Live mode (from config)

If you have a config file that points to a running setup:

```bash
PYTHONPATH=src medre evidence --config /path/to/medre.yaml
```

This starts a runtime, collects a full evidence bundle including live health checks, and then shuts down. The `--include-refresh-health` flag forces a fresh health check (incompatible with `--storage-path`).

### 6.3 What is in the bundle

| Section     | Contents                                          |
| ----------- | ------------------------------------------------- |
| Config      | Runtime configuration (secrets redacted)          |
| Diagnostics | Adapter health, counters, connection state        |
| Routes      | Configured routes and their status                |
| Events      | Event data if `--event-id` is specified           |
| Replay      | Replay run data if `--replay-run-id` is specified |

Use `--event-id` to scope the bundle to a specific event (includes native refs, receipts, and incident summary):

```bash
PYTHONPATH=src medre evidence --storage-path /path/to/medre.db --event-id <event_id>
```

Use `--replay-run-id` to scope the bundle to a replay run (includes replay receipt analysis):

```bash
PYTHONPATH=src medre evidence --storage-path /path/to/medre.db --replay-run-id <replay_run_id>
```

The `config_source` field tells you whether the bundle came from a config file or a storage path. The `collected_at` timestamp tells you when.

### 6.4 Safety

The evidence bundle is designed to be safe to share:

- Access tokens and credentials are redacted before inclusion.
- The bundle does not contain message content. It contains metadata: event IDs, timestamps, adapter IDs, delivery status.
- No network addresses beyond what is in the config (homeserver URL, which is not secret).

If you are unsure, review the JSON output before pasting it into an issue. Look for any field containing your actual access token. If you find one, that is a bug. Report it.

### 6.5 Canonical report schema fields

The evidence report is a single JSON object. These are the top-level keys you will see:

| Field             | Type            | Description                                                              |
| ----------------- | --------------- | ------------------------------------------------------------------------ |
| `schema_version`  | string          | Bundle schema version for forward compatibility                          |
| `medre_version`   | string          | Version of the MEDRE runtime that produced the bundle                    |
| `command`         | string          | Always `"evidence"`                                                      |
| `status`          | string          | Overall result: `"passed"`, `"partial"`, or `"error"`                    |
| `collected_at`    | string          | ISO 8601 timestamp when collection started                               |
| `generated_at`    | string          | ISO 8601 timestamp when the bundle was finalized                         |
| `config_source`   | string          | `"config"` or `"storage_path"`, depending on how the bundle was produced |
| `runtime_started` | boolean         | `true` if the runtime was booted for a live health check                 |
| `errors`          | array\<string\> | Accumulated error messages from any section                              |
| `limitations`     | array\<string\> | Known limitations or caveats about the collected data                    |
| `sections`        | object          | Nested data sections (see below)                                         |

The `sections` object contains the actual diagnostic data:

| Path                            | Description                                                                                                  |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `sections.config_summary`       | Redacted runtime config and resolved paths                                                                   |
| `sections.route_validation`     | Route definitions and validation status                                                                      |
| `sections.diagnostics_snapshot` | Per-adapter health and counters without starting the runtime                                                 |
| `sections.live_health`          | Live adapter health (requires `--include-refresh-health`)                                                    |
| `sections.storage`              | Event and delivery records from the SQLite store (scoped by `--event-id` or `--replay-run-id` when provided) |

## 7. Trace an Event

The `trace` command assembles a chronological timeline for a single event. It shows what happened to the event, when, and through which adapters.

### 7.1 Basic trace

```bash
PYTHONPATH=src medre trace event <event_id> --storage-path /path/to/medre.db
```

This prints a human-readable timeline. Each entry shows a timestamp, entry type, and relevant data.

For JSON output:

```bash
PYTHONPATH=src medre trace event <event_id> --storage-path /path/to/medre.db --json
```

### 7.2 Timeline entry types

| Entry type   | What it shows                                                          |
| ------------ | ---------------------------------------------------------------------- |
| `event`      | The canonical event itself (kind, source adapter, timestamp)           |
| `relation`   | Relations to other events (replies, reactions)                         |
| `native_ref` | Native transport references (Matrix event IDs, Meshtastic message IDs) |
| `receipt`    | Delivery receipts (status, target adapter, attempt count)              |

### 7.3 Interpreting the timeline

A healthy event trace looks something like this:

```text
Event: evt_abc123 (message.created) from matrix-alpha
Timeline (4 entries):

  2026-05-21T10:00:00Z  [event] message.created from matrix-alpha
  2026-05-21T10:00:00Z  [native_ref] inbound via matrix-alpha: $mx_event_id
  2026-05-21T10:00:01Z  [receipt] delivered to meshtastic-alpha
  2026-05-21T10:00:01Z  [native_ref] outbound via meshtastic-alpha: msg_456
```

An event that failed delivery will show a receipt with a failure status and an error message. See section 10 for reading failure details.

### 7.4 Trace replay

If an event was replayed (see `docs/runbooks/replay-operation.md`), the trace output includes replay receipts alongside live receipts. Replay receipts have `source='replay'` attribution and a `replay_run_id` grouping them together.

To see both live and replay delivery paths:

```bash
PYTHONPATH=src medre trace event <event_id> --config /path/to/bridge.toml --json
```

Look for receipt entries where `source` is `"replay"` vs `"live"`. The `replay_run_id` groups all receipts from a single replay invocation.

## 8. Trace Replay

Replay re-processes historical events through the pipeline. It is a specialized recovery and verification tool, not part of day-to-day operation.

### 8.1 When to use replay

- An adapter was offline during a critical window and events were not delivered.
- You changed routing config and want to re-evaluate which routes match historical events.
- You need to verify that a config change produces correct behavior on past data.

### 8.2 Replay modes

| Mode          | Delivers? | Side effects          | Use case                                       |
| ------------- | --------- | --------------------- | ---------------------------------------------- |
| `DRY_RUN`     | No        | None                  | Preview what replay would do                   |
| `RE_ROUTE`    | No        | None                  | Re-evaluate route matching after config change |
| `BEST_EFFORT` | Yes       | Real adapter delivery | Re-deliver historical events                   |

**Always run `DRY_RUN` first.** Only use `BEST_EFFORT` when you have verified the route matching preview and accept the duplicate delivery risk.

### 8.3 Replay workflow

1. Run a dry run to preview:

```bash
PYTHONPATH=src medre replay --mode DRY_RUN --config /path/to/bridge.toml
```

2. Review the output. Check which routes match and which events would be re-delivered.

3. If the preview looks correct, run best-effort replay:

```bash
PYTHONPATH=src medre replay --mode BEST_EFFORT --config /path/to/bridge.toml
```

4. Trace the replayed events to verify delivery:

```bash
PYTHONPATH=src medre trace event <event_id> --config /path/to/bridge.toml
```

5. Check that replay receipts have `source='replay'` and the expected `replay_run_id`.

See `docs/runbooks/replay-operation.md` for full replay documentation including mode details, risk assessment, and result interpretation.

## 9. Inspect Event

The `inspect` command queries stored data directly from a SQLite database. It is read-only and never modifies the database. For day-to-day investigation, `medre inspect event --timeline` is the preferred operator command.

### 9.1 Inspect a single event

```bash
PYTHONPATH=src medre inspect event <event_id> --storage-path /path/to/medre.db
```

This prints the canonical event as JSON. The output includes all fields: event ID, kind, source adapter, payload, timestamps, and metadata.

### 9.2 Inspect with --timeline

```bash
PYTHONPATH=src medre inspect event <event_id> --storage-path /path/to/medre.db --timeline
```

Adds a `timeline` section to the output with all chronological entries. This produces the same enriched timeline as `medre trace event` within a unified command surface.

### 9.3 Inspect with --evidence

```bash
PYTHONPATH=src medre inspect event <event_id> --storage-path /path/to/medre.db --evidence
```

Adds an `evidence` section with a full evidence bundle scoped to that event. This covers `medre evidence --event-id` in a single command.

### 9.4 Inspect with --recovery

```bash
PYTHONPATH=src medre inspect event <event_id> --storage-path /path/to/medre.db --recovery
```

Adds recovery context: receipt lineage, retry history, and replay attribution. Useful for understanding why an event was re-delivered or what recovery actions were taken.

### 9.5 Combine flags

```bash
PYTHONPATH=src medre inspect event <event_id> --storage-path /path/to/medre.db --timeline --evidence --recovery
```

This is the most detailed inspection available. It shows the event, its full timeline, the evidence bundle, and recovery context. The output is deterministic JSON, suitable for diffing or pasting into reports.

### 9.6 What inspect does NOT do

- It does not start a runtime.
- It does not connect to any network service.
- It does not modify the database.
- It does not require any environment variables beyond the storage path.

## 10. Interpreting Delivery Failures

When a message fails to reach its destination, the evidence is in the receipts and timeline entries. Here is how to read them.

### 10.1 Receipt status values

| Status      | Meaning                                                                        |
| ----------- | ------------------------------------------------------------------------------ |
| `delivered` | The target adapter confirmed successful delivery                               |
| `sent`      | The adapter sent the message but has not received transport-level confirmation |
| `failed`    | Delivery attempted and failed. Check the error message.                        |
| `pending`   | Delivery has not been attempted yet                                            |
| `skipped`   | Delivery was not attempted (e.g., route was degraded or disabled)              |

### 10.2 Reading a failure timeline

Trace the event (section 7) or inspect it with `--timeline --evidence` (section 9). Look for:

1. **The event entry.** Confirms the event exists and shows its kind and source.
2. **The receipt entries.** Show delivery attempts, statuses, and error messages.
3. **The evidence section.** Shows adapter health at the time of collection, which may reveal why delivery failed (adapter in `degraded` or `failed` state, for example).

### 10.3 Common failure patterns

| Pattern                                                     | Likely cause                                           | What to check                                                   |
| ----------------------------------------------------------- | ------------------------------------------------------ | --------------------------------------------------------------- |
| Receipt says `failed` with `AdapterPermanentError`          | Permanent delivery failure (bad room, forbidden, etc.) | Verify the target room/channel exists and the bot has access    |
| Receipt says `failed` with `AdapterSendError` (transient)   | Temporary network or homeserver error                  | Check network connectivity, homeserver health                   |
| No receipt at all                                           | Event never reached delivery stage                     | Check routing config, adapter health, pipeline logs             |
| Receipt says `skipped`                                      | Route was degraded or disabled                         | Check route status in evidence bundle                           |
| Multiple receipts with alternating `failed` and `delivered` | Intermittent failures during retry                     | Check network stability, adapter load                           |
| Receipt with `source='replay'` and `failed`                 | Replay re-delivery failed                              | Check that the target adapter was healthy during the replay run |
| Receipt with `attempt_number` > 1                           | Retried delivery                                       | Check the `parent_receipt_id` for the original failure reason   |

### 10.4 Incident summary

When filing an issue or asking for help, include:

1. The event ID.
2. The `medre trace event` output (or `medre inspect event --timeline --evidence` output).
3. The approximate time the failure occurred.
4. What you expected to happen vs. what actually happened.

Do not include your access token, password, or any credential. The evidence bundle redacts them, but double-check before pasting.

## 11. Attach Sanitized Output to Issues

When you file a GitHub issue, the evidence bundle is designed to be safe to paste directly. But always review before sharing. Here is the redaction checklist.

### 11.1 Redaction checklist

Review the JSON output line by line before pasting. Check for each of these:

- [ ] **Access tokens.** Search for `syt_`, `token`, `access_token`. The evidence bundle should redact these, but verify.
- [ ] **User IDs.** If you do not want your Matrix user ID public, replace `@bot:localhost` with `@redacted:example.com`.
- [ ] **Room IDs.** If the room is private, replace `!opaque:server` with `!redacted:server`.
- [ ] **Homeserver URLs.** These are not usually sensitive, but replace if yours is internal-only.
- [ ] **IP addresses.** The bundle should not contain IPs, but check the `diagnostics` section for any connection details.
- [ ] **Message content.** The bundle should contain only metadata, not message bodies. Verify that no `body` or `content` fields contain your actual messages.
- [ ] **Meshtastic node info.** Node numbers and hardware IDs may be present in diagnostics. Redact if you consider them sensitive.

### 11.2 What is safe to share

The following fields are safe and expected in issue reports:

- `event_id` (MEDRE canonical UUID)
- `event_kind` (e.g., `message.created`)
- `source_adapter` (e.g., `matrix-alpha`)
- `receipt.status` (e.g., `delivered`, `failed`)
- `receipt.target_adapter` (e.g., `meshtastic-alpha`)
- `native_ref` transport IDs (Matrix `$event_id`, Meshtastic message IDs)
- `diagnostics.health` (e.g., `healthy`, `degraded`)
- `config_source` and `collected_at` timestamps
- `evidence_level`

### 11.3 What to redact or omit

- Any field containing a value that starts with `syt_` or looks like a token.
- Your actual homeserver URL if it is internal or contains identifying information.
- Any field you are not sure about. When in doubt, redact.

## 12. Security

### 12.1 Token handling

- **Never print your access token.** The adapter's `__repr__` method redacts it. Your code might not.
- **Never commit credentials.** Not in `.env` files, not in scripts, not in config files checked into git.
- **Never paste credentials into chat or issues.** If you accidentally do, rotate the token immediately.
- **Unset env vars when done.** `unset MATRIX_ACCESS_TOKEN` after testing.

### 12.2 Safe-to-paste reports

The `evidence`, `trace`, and `inspect` commands produce output designed to be shareable. They redact secrets and include only metadata. But:

- Always review JSON output before sharing it. Look for anything that looks like a token or password.
- If you find a secret in the output, that is a bug. File an issue.
- The `collected_at` and `config_source` fields are metadata about the report itself, not secrets.

### 12.3 What is logged

The runner logs to stderr at INFO level. Logs include adapter IDs, room IDs, event IDs, and health status. They do not include access tokens (the adapter redacts them in `__repr__`). They may include error messages from the Matrix SDK, which could contain homeserver URLs but not credentials.

If you are sharing log output, review it first. Redact anything you are unsure about.

## 13. Alpha Status

This entire system is alpha software. Specific things that are not true:

1. **It is not production-ready.** Do not rely on it for anything important.
2. **It is not reliable.** Messages can be lost, duplicated, or silently dropped. There is no delivery guarantee.
3. **It is not hardened.** Error handling exists but is not comprehensive. Unexpected inputs may produce confusing errors or silent failures.
4. **It is not complete.** Many Matrix features are unsupported: reactions, edits, deletes, media, threads, presence, typing notifications, read receipts. See `docs/runbooks/matrix-alpha-operation.md` section 13 for the full list.
5. **It is not fast.** Performance has not been optimized. The sync loop is a single long-polling HTTP connection. Delivery is sequential within a single adapter.
6. **It is not documented completely.** This runbook covers the main workflows. Edge cases, error recovery, and advanced configuration are documented elsewhere or not at all.

For per-transport capability tracking, see `docs/STATUS.md`. That document tracks which capabilities are fake-tested, which have opt-in live tests, and which have recorded live validation evidence. No capability is marked "live-validated" without recorded evidence.

If you find a bug, file an issue with the evidence bundle (section 6) and the event trace (section 7). Include what you expected and what actually happened.

## 14. Unified Delivery Evidence Workflows

This section provides worked examples for the operator questions that the unified delivery evidence surface is designed to answer. All evidence is **best-effort** and **local-process scoped** — it reflects what the local MEDRE process observed.

### 14.1 "Delivered where?"

**Question:** Where did event `evt-abc123` end up?

```bash
medre inspect event evt-abc123 --storage-path /path/to/medre.db --timeline
```

Look for receipt entries with `status: "sent"`. Each receipt shows:

- `target_adapter` — which adapter received it
- `target_channel` — which channel/room
- `adapter_message_id` — the native message ID at the destination (e.g., Matrix `$event_id`, Meshtastic packet ID)
- `route_id` — which route triggered the delivery

Example output (sanitized):

```json
{
  "receipt_id": "rcpt-...",
  "event_id": "evt-abc123",
  "target_adapter": "matrix-primary",
  "target_channel": "!room:example.com",
  "status": "sent",
  "adapter_message_id": "$mx_event_id",
  "route_id": "radio-to-matrix",
  "attempt_number": 1,
  "source": "live"
}
```

This tells you: the event was delivered to `matrix-primary` in room `!room:example.com` via route `radio-to-matrix`, on the first attempt, during live operation.

### 14.2 "Retried why?"

**Question:** Why was event `evt-def456` retried?

```bash
medre inspect event evt-def456 --storage-path /path/to/medre.db --recovery
```

Look for multiple receipts with the same `event_id` but different `attempt_number` values. The `parent_receipt_id` links them into a retry lineage.

Example:

```json
{
  "receipt_id": "rcpt-001",
  "event_id": "evt-def456",
  "status": "failed",
  "failure_kind": "adapter_transient",
  "error": "connection reset",
  "attempt_number": 1,
  "next_retry_at": "2026-05-23T10:00:30Z",
  "source": "live"
}
```

```json
{
  "receipt_id": "rcpt-002",
  "event_id": "evt-def456",
  "status": "sent",
  "failure_kind": null,
  "attempt_number": 2,
  "parent_receipt_id": "rcpt-001",
  "source": "retry"
}
```

This tells you: the first attempt failed with a transient connection error, a retry was scheduled, and the second attempt succeeded. The `source: "retry"` confirms this was a RetryWorker-initiated attempt.

### 14.3 "Suppressed why?"

**Question:** Why was an event suppressed?

Two types of suppression exist:

**Loop suppressed** — visible in receipts:

```json
{
  "status": "skipped",
  "failure_kind": "loop_suppressed",
  "error": "loop_prevented: route already in route_trace"
}
```

This means the route-trace or self-loop guard prevented delivery.

**Duplicate suppressed** — silent at the receipt level. If an event was suppressed by native-ref dedup at ingress, there will be no receipt at all. The event was never stored. Check `RuntimeAccounting.loop_prevented` counters (in diagnostics) for the aggregate count. The `DUPLICATE_SUPPRESSED` failure kind is reserved but not currently emitted — the runtime does not safely persist the duplicate path without creating a new event.

### 14.4 "Dead-lettered why?"

**Question:** Why did event `evt-ghi789` end up dead-lettered?

```bash
medre inspect event evt-ghi789 --storage-path /path/to/medre.db --recovery
```

Look for a receipt with `status: "dead_lettered"`. Trace the `parent_receipt_id` chain back to the original failure.

Example:

```json
{
  "receipt_id": "rcpt-final",
  "event_id": "evt-ghi789",
  "status": "dead_lettered",
  "failure_kind": "adapter_transient",
  "error": "timeout",
  "attempt_number": 3,
  "retry_max_attempts": 3,
  "source": "retry"
}
```

This tells you: the event exhausted 3 retry attempts (all transient timeouts), and the pipeline will not retry further. The event is effectively dead — operators can use `medre replay --mode BEST_EFFORT` to attempt re-delivery if the underlying condition has resolved.

### 14.5 "Queued locally but not RF-confirmed" (Meshtastic)

**Question:** The Meshtastic receipt says `sent`, but did the remote node actually receive it?

**Answer:** No transport-level confirmation is available. The receipt statuses for Meshtastic mean:

- `queued` — the adapter's outbound queue accepted the message
- `sent` — the local Meshtastic node sent the packet to the radio

Neither status confirms RF delivery to any remote node. There is no Meshtastic acknowledgement mechanism exposed to MEDRE. Confirmed/ack semantics remain distinct and are not currently available from the adapter.

Check queue diagnostics for additional context:

```bash
medre evidence --config /path/to/config.toml --json | jq '.sections.diagnostics_snapshot'
```

Look for `queue_total_sent`, `queue_total_failed`, `queue_total_rejected` to understand queue-level throughput. `queue_total_rejected` indicates the queue was full and new messages were turned away.

### 14.6 "Matrix tx_id used"

**Question:** How does Matrix transaction ID deduplication work?

The Matrix adapter computes a deterministic `tx_id` (named `matrix_txn_id` internally) from `event_id + target_adapter + target_channel + room_id`. This `tx_id` is passed to the homeserver on every send attempt, including retries.

**What it does:** If the same `tx_id` is sent twice (e.g., a retry of a transient failure where the first attempt actually succeeded at the homeserver), the homeserver returns the original `event_id` instead of creating a duplicate event. This **reduces duplicate retries**.

**What it does NOT do:** This is not exactly-once delivery. The homeserver's deduplication window is finite. If the first attempt was lost before reaching the homeserver, the retry with the same `tx_id` will create a new event (correct behavior). If the homeserver processed the first attempt but the response was lost, the `tx_id` dedup prevents a duplicate (correct behavior). There is no guarantee the homeserver remembers the `tx_id` across restarts or over long time windows.

Check the delivery receipt's metadata for `matrix_txn_id`:

```json
{
  "adapter_message_id": "$mx_event_id",
  "metadata": {
    "matrix_txn_id": "medre_a1b2c3d4..."
  }
}
```

### 14.7 "Matrix E2EE blocked"

**Question:** Why are some Matrix inbound events not being processed in an encrypted room?

When Matrix E2EE is enabled and the bot cannot decrypt a MegolmEvent, the event is counted but its content is not processed. Check diagnostics:

```bash
medre evidence --config /path/to/config.toml --json | jq '.sections.diagnostics_snapshot'
```

Look for `undecryptable_event_count`. A non-zero value indicates E2EE decryption failures. Common causes:

- The bot's crypto store does not have the room key
- The room key was rotated before the bot received it
- Cross-signing or key backup is not set up

E2EE decryption is an upstream nio/vodozemac property. MEDRE does not manage key distribution, cross-signing, or key backup. See `docs/runbooks/matrix-alpha-operation.md` for E2EE setup requirements.

### 14.8 "Meshtastic classifier ignored/dropped/deferred"

**Question:** Why are Meshtastic inbound packets not being relayed?

The Meshtastic packet classifier examines every inbound packet and decides: `relay`, `ignore`, `drop`, or `deferred`. Check the aggregate counters:

```bash
medre evidence --config /path/to/config.toml --json | jq '.sections.diagnostics_snapshot'
```

Look for `classifier_packets_*` fields:

| Counter | What it means |
| --- | --- |
| `classifier_packets_seen` | Total packets examined |
| `classifier_packets_relayed` | Packets proceeding to the pipeline |
| `classifier_packets_ignored` | Skipped: ack/admin, telemetry, position, nodeinfo, direct messages, empty text |
| `classifier_packets_dropped` | Rejected: encrypted packets, malformed payloads |
| `classifier_packets_deferred` | Held for future: detection sensor, unknown portnum, plugin-only |

Sub-counters break down by reason:

| Sub-counter | Classification reason |
| --- | --- |
| `classifier_packets_malformed` | Dropped: no valid decoded payload |
| `classifier_packets_encrypted_dropped` | Dropped: packet is encrypted |
| `classifier_packets_detection_sensor_deferred` | Deferred: detection sensor portnum |
| `classifier_packets_dm_ignored` | Ignored: direct message to a specific node |
| `classifier_packets_empty_text_ignored` | Ignored: text message with empty body |
| `classifier_packets_unknown_portnum_deferred` | Deferred: unknown or custom portnum |

**Important:** These are **aggregate counters**, not per-packet records. They explain how many packets were classified and what aggregate decisions were made. They do **not** mean live validation — the classifier is a pure function that examines packet structure, not a real-time validator. They do **not** persist a record of every individual ignored, dropped, or deferred packet. Counters reset on adapter restart (in-memory only).

### 14.9 Summary: Evidence Non-Guarantees

| Question | Answer | Evidence available? |
| --- | --- | --- |
| Delivered where? | Receipt shows target adapter, channel, native message ID, route | Yes (receipt + timeline) |
| Retried why? | Receipt lineage shows failure kind, attempt number, retry policy | Yes (recovery context) |
| Suppressed why (loop)? | Route-trace or self-loop guard fired | Yes (receipt failure_kind) |
| Suppressed why (duplicate)? | Native-ref dedup at ingress | No receipt — counters only |
| Dead-lettered why? | Retry exhaustion after transient failures | Yes (receipt chain) |
| Queued but RF-confirmed? | Meshtastic `sent` means local node only | Yes (queue stats, but no RF ack) |
| Matrix tx_id used? | Deterministic dedup reduces duplicates | Yes (receipt metadata) |
| Matrix tx_id exactly-once? | No — homeserver dedup window is finite | No — this is not guaranteed |
| Matrix E2EE blocked? | Undecryptable events counted in diagnostics | Yes (undecryptable_event_count) |
| Meshtastic classifier stats? | Aggregate inbound skip counts | Yes (diagnostics classifier_*) |
| Classifier stats per-packet? | No — aggregate only, reset on restart | No — in-memory counters only |

## 15. Related Documentation

| Document                                      | What it covers                                                                 |
| --------------------------------------------- | ------------------------------------------------------------------------------ |
| `docs/STATUS.md`                              | Per-transport capability dashboard (the single source of truth for what works) |
| `docs/runbooks/matrix-alpha-operation.md`     | Full Matrix alpha operation guide (setup, validation, troubleshooting, E2EE)   |
| `docs/runbooks/matrix-live-smoke.md`          | Matrix live smoke test instructions                                            |
| `docs/runbooks/meshtastic-alpha-operation.md` | Full Meshtastic alpha operation guide                                          |
| `docs/runbooks/meshtastic-live-smoke.md`      | Meshtastic live smoke test instructions                                        |
| `docs/runbooks/meshcore-alpha-operation.md`   | Full MeshCore alpha operation guide                                            |
| `docs/runbooks/lxmf-alpha-operation.md`       | Full LXMF/Reticulum alpha operation guide                                      |
| `docs/runbooks/replay-operation.md`           | Replay modes, commands, and risk assessment                                    |
| `docs/runbooks/event-tracing.md`              | Detailed event tracing and timeline interpretation                             |
| `docs/dev/live-test-harness.md`               | Live test patterns and conventions for test developers                         |
| `docs/dev/TESTING_GUIDE.md`                   | General testing guide (tiers, style, patterns)                                 |

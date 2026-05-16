# Alpha Walkthrough Runbook

> Document version: 3
> Last updated: 2026-05-16

This runbook walks through the preferred product path for operating medre
at the alpha milestone. Every command is exact and copy-pasteable.

The flow is intentionally lean: validate config, optionally run local smoke,
then use inspect-first investigation to understand what happened. Replay is
available as a lower-level supported command when recovery is needed, but it
is not part of the daily operator path.

Prerequisites: Python >= 3.11, `pip install -e ".[dev]"`.

---

## Preferred Product Path

### Phase 0: Pre-flight validation

Before running anything, verify config and routes:

```bash
medre config check --config examples/configs/fake-bridge-smoke.toml
medre routes validate --config examples/configs/fake-bridge-smoke.toml
```

Expected: both exit 0 with no errors.

### Phase 1: Optional local smoke validation

`medre smoke` is optional local validation tooling. Use it to confirm the
pipeline works with fake adapters before committing to a live run. It seeds
a database you can inspect afterward.

```bash
medre smoke --config examples/configs/fake-bridge-smoke.toml \
  --storage-path /tmp/medre-alpha.db --json
```

Expected output: JSON object with `"status": "passed"`, an `event_id`, and
delivery receipts. The SQLite database at `/tmp/medre-alpha.db` now contains
the canonical event, delivery receipts, and native refs.

If you are validating a production config against real adapters, skip this
phase and go straight to `medre run`.

### Phase 2: Inspect-first investigation (preferred)

This is the core operator loop. After any run (smoke, live, or post-crash),
start here. Read-only commands use `--storage-path` to point directly at the
database. No config file needed.

**Check the event:**

```bash
medre inspect event <event_id> \
  --storage-path /tmp/medre-alpha.db
```

Replace `<event_id>` with the value from Phase 1. Expected output: the
canonical event record showing source adapter, kind, payload, and timestamp.

**Check delivery receipts:**

```bash
medre inspect receipts --event <event_id> \
  --storage-path /tmp/medre-alpha.db
```

Expected output: JSON array of delivery receipts, at least one with
`"status": "sent"`. Each receipt shows the target adapter, route, attempt
number, and failure kind if applicable.

For most day-to-day operation, these two commands are sufficient. If inspect
reveals something worth investigating further, proceed to Phase 3.

### Phase 3: Deeper investigation

Use these when `inspect` shows failures, unexpected routing, or you need a
full audit trail.

**Assemble a timeline:**

```bash
medre trace event <event_id> \
  --storage-path /tmp/medre-alpha.db --json
```

Expected output: JSON array of timeline entries. At least one entry with
`"entry_type": "receipt"`. The timeline shows every stage the event passed
through: ingestion, routing, delivery, retry, replay.

**Collect a full evidence bundle:**

```bash
medre evidence --event <event_id> \
  --storage-path /tmp/medre-alpha.db --json
```

Expected output: JSON evidence bundle with `"status": "passed"` or
`"status": "partial"`. The `storage` section contains the event, receipts,
timeline, and incident summary. This is the recommended attachment format
for bug reports.

### Phase 4: Replay (lower-level, only when needed)

Replay is a lower-level supported command for re-processing historical
events. It is not part of daily operation. Replay requires a config file
with declared routes and adapters to determine replay targets. It is
config-required and duplicate-risky.

**Create a replay config** pointing at the same SQLite database:

```bash
cat > /tmp/medre-alpha-replay.toml <<'EOF'
[runtime]
name = "alpha-replay"
shutdown_timeout_seconds = 10

[logging]
level = "WARNING"

[storage]
backend = "sqlite"
path = "/tmp/medre-alpha.db"

[adapters.matrix.fake_matrix]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "fake"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.meshtastic.fake_meshtastic]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "alpha-walkthrough"

[routes.mx_to_mesh]
source_adapters = ["fake_matrix"]
dest_adapters = ["fake_meshtastic"]
directionality = "source_to_dest"
enabled = true
EOF
```

**Dry run first** (no side effects):

```bash
medre replay --config /tmp/medre-alpha-replay.toml \
  --mode dry_run --event <event_id> --json
```

Expected output: JSON summary with `"mode": "dry_run"`,
`"events_scanned" >= 1`, `"events_replayed" >= 1`. No side effects.

**Best effort replay** (produces new outbound messages):

```bash
medre replay --config /tmp/medre-alpha-replay.toml \
  --mode best_effort --event <event_id> --json
```

Expected output: JSON summary with `"mode": "best_effort"` and replay
receipts with `source='replay'`.

> **Warning:** BEST_EFFORT replay incurs the same duplicate-send risk as all
> adapter transports. Without `--json`, the command prints a duplicate-risk
> warning to stderr. Replay receipts are distinguishable from live records by
> `source='replay'` and `replay_run_id`, but traceability is not deduplication.

Always run `dry_run` before `best_effort`. See [Replay
Operation](replay-operation.md) for the full replay workflow and duplicate
risk assessment.

---

## What This Proves

| Capability | Proven |
|-----------|--------|
| Config validation (check + routes validate) | Yes |
| Pipeline routing (source to target) | Yes |
| Canonical event storage (SQLite) | Yes |
| Delivery receipt recording | Yes |
| Inspect-first investigation (event + receipts) | Yes |
| Event tracing (timeline assembly) | Yes |
| Evidence bundle collection | Yes |
| Replay engine (dry_run and best_effort) | Yes |
| `--storage-path` for zero-config read-only commands | Yes |
| Config-file-driven replay | Yes |
| Replay duplicate-risk warning | Yes |

This path proves the software architecture works using fake adapters. It does
not prove any transport works with real hardware or real networks.

---

## Replay and Retry Notes

**Replay is manual and duplicate-risky.** Replay is a one-shot operator
action, not part of the preferred product path. There is no background
scheduler, no automatic resume, and no deduplication. Each `BEST_EFFORT`
replay produces new outbound messages. Always run `DRY_RUN` first. See
[Replay Operation](replay-operation.md) for the full replay workflow.

**Retry is a two-level opt-in.** Scheduled retry receipts are created only
when a route has retry enabled via its `[routes.<id>.retry]` section. Routes
without a retry section record transient failures without `next_retry_at`.
The `RetryWorker` that processes due retry receipts is controlled by the
`[retry]` section in the config.

**Replay and retry interaction.** BEST_EFFORT replay through a route with
`[routes.<id>.retry]` enabled will create retry receipts (`next_retry_at` set)
if delivery fails transiently. These receipts carry `source='replay'` and the
`replay_run_id`. The `medre replay` command does **not** start the
RetryWorker. If the runtime is later started with `[retry] enabled = true`,
the worker will discover and process these replay-created retry receipts,
producing `source='retry'` receipts linked via `parent_receipt_id`. See
[Replay Operation §8](replay-operation.md#8-replay-and-route-level-retry-interaction)
for the full interaction matrix and operator procedure.

---

## Test Suite

The alpha walkthrough test (`tests/test_alpha_walkthrough_cli.py`) exercises
these exact commands programmatically via `main([...])`, validating that the
documented flow works end-to-end:

```bash
PYTHONPATH=src pytest tests/test_alpha_walkthrough_cli.py -v
```

For function-level smoke tests, see `tests/test_alpha_walkthrough.py`.
For runtime-level replay/retry tests, see
`tests/test_alpha_walkthrough_runtime_retry_replay.py`.
For replay CLI surface tests, see `tests/test_cli_replay_surface.py`.

---

## Quick Reference

```bash
# Phase 0: Pre-flight
medre config check --config examples/configs/fake-bridge-smoke.toml
medre routes validate --config examples/configs/fake-bridge-smoke.toml

# Phase 1: Optional local smoke (seeds DB for inspection)
medre smoke --config examples/configs/fake-bridge-smoke.toml \
  --storage-path /tmp/medre-alpha.db --json

# Phase 2: Inspect-first investigation (copy event_id from smoke output)
medre inspect event <event_id> --storage-path /tmp/medre-alpha.db
medre inspect receipts --event <event_id> --storage-path /tmp/medre-alpha.db

# Phase 3: Deeper investigation (when inspect shows something)
medre trace event <event_id> --storage-path /tmp/medre-alpha.db --json
medre evidence --event <event_id> --storage-path /tmp/medre-alpha.db --json

# Phase 4: Replay (lower-level, config required, duplicate-risky)
medre replay --config /tmp/medre-alpha-replay.toml \
  --mode dry_run --event <event_id> --json
medre replay --config /tmp/medre-alpha-replay.toml \
  --mode best_effort --event <event_id> --json

# Full test suite
PYTHONPATH=src pytest -q

# Compile check
python -m compileall -q src tests
```

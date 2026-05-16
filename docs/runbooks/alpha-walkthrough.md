# Alpha Walkthrough Runbook

> Document version: 2
> Last updated: 2026-05-16

This runbook provides one coherent operator flow for exercising medre at the
alpha milestone. Every command is exact and copy-pasteable. Read-only commands
(inspect, trace, evidence) use `--storage-path` to point directly at the
database — no config-file ceremony required.

Prerequisites: Python >= 3.11, `pip install -e ".[dev]"`.

---

## Operator Flow

### Step 1: Run smoke to seed the database

```bash
medre smoke --config examples/configs/fake-bridge-smoke.toml \
  --storage-path /tmp/medre-alpha.db --json
```

Expected output: JSON object with `"status": "passed"`, an `event_id`, and
delivery receipts. The SQLite database at `/tmp/medre-alpha.db` now contains
the canonical event, delivery receipts, and native refs.

### Step 2: Inspect delivery receipts

```bash
medre inspect receipts --event <event_id> \
  --storage-path /tmp/medre-alpha.db
```

Replace `<event_id>` with the value from Step 1. Expected output: JSON array
of delivery receipts, at least one with `"status": "sent"`.

### Step 3: Trace the event timeline

```bash
medre trace event <event_id> \
  --storage-path /tmp/medre-alpha.db --json
```

Expected output: JSON array of timeline entries. At least one entry with
`"entry_type": "receipt"`.

### Step 4: Collect an evidence bundle

```bash
medre evidence --event <event_id> \
  --storage-path /tmp/medre-alpha.db --json
```

Expected output: JSON evidence bundle with `"status": "ok"` or
`"status": "partial"`. The `storage` section contains the event, receipts,
timeline, and incident summary.

### Step 5: Replay dry run (config required)

Replay requires a config file with declared routes and adapters to determine
replay targets. The config must point storage at the same SQLite database
used by the smoke command in Step 1.

Create a replay config:

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

Then run the dry run:

```bash
medre replay --config /tmp/medre-alpha-replay.toml \
  --mode dry_run --event <event_id> --json
```

Expected output: JSON summary with `"mode": "dry_run"`,
`"events_scanned" >= 1`, `"events_replayed" >= 1`. No side effects.

### Step 6: Replay best effort (config required)

```bash
medre replay --config /tmp/medre-alpha-replay.toml \
  --mode best_effort --event <event_id> --json
```

Expected output: JSON summary with `"mode": "best_effort"` and replay
receipts with `source='replay'`. This produces new outbound messages — always
run `dry_run` (Step 5) first.

> **Warning:** BEST_EFFORT replay incurs the same duplicate-send risk as all
> adapter transports. Without `--json`, the command prints a duplicate-risk
> warning to stderr. Replay receipts are distinguishable from live records by
> `source='replay'` and `replay_run_id`, but traceability is not deduplication.

---

## What This Proves

| Capability | Proven |
|-----------|--------|
| Pipeline routing (source to target) | Yes |
| Canonical event storage (SQLite) | Yes |
| Delivery receipt recording | Yes |
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
action — there is no background scheduler, no automatic resume, and no
deduplication. Each `BEST_EFFORT` replay produces new outbound messages.
Always run `DRY_RUN` first. See [Replay Operation](replay-operation.md) for
the full replay workflow.

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
# Alpha walkthrough (copy-paste in order)
medre smoke --config examples/configs/fake-bridge-smoke.toml \
  --storage-path /tmp/medre-alpha.db --json
# Copy event_id from output, then:
medre inspect receipts --event <event_id> --storage-path /tmp/medre-alpha.db
medre trace event <event_id> --storage-path /tmp/medre-alpha.db --json
medre evidence --event <event_id> --storage-path /tmp/medre-alpha.db --json
# Replay requires a config with SQLite storage (see Step 5)
medre replay --config /tmp/medre-alpha-replay.toml \
  --mode dry_run --event <event_id> --json
medre replay --config /tmp/medre-alpha-replay.toml \
  --mode best_effort --event <event_id> --json

# Full test suite
PYTHONPATH=src pytest -q

# Compile check
python -m compileall -q src tests
```

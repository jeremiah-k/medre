# Alpha Walkthrough Runbook

> Document version: 1
> Last updated: 2026-05-15

This runbook provides exact commands for exercising medre at the alpha
milestone. Each section covers a specific path from zero to evidence.

Prerequisites: Python >= 3.11, `pip install -e ".[dev]"`.

---

## Section 1: Fake-Only Path (No Docker, No Hardware)

This path exercises the full pipeline using fake adapters. No network access,
no SDK dependencies, no special hardware.

### 1.1 Validate configuration

```bash
# Check the built-in example config
medre config check --config examples/configs/fake-bridge-smoke.toml
```

Expected output: config is valid, lists adapters and routes found.

### 1.2 Run smoke test

```bash
# Quick pipeline validation with JSON output
medre smoke --json
```

The smoke command uses `examples/configs/fake-bridge-smoke.toml` by default.
It starts fake adapters, injects a test message through the full pipeline,
and reports results.

Expected output: JSON object with `"status": "passed"` and evidence results.

### 1.3 Run a one-shot smoke with persistent storage

```bash
# One-shot smoke: inject an event, collect evidence, write to SQLite, exit
medre smoke --config examples/configs/fake-bridge-smoke.toml \
  --storage-path /tmp/medre-alpha.db --json
```

`medre smoke` calls `run_fake_bridge_smoke()` internally: it injects one event
through the full pipeline, collects evidence, writes to the SQLite DB at
`--storage-path`, and exits. No waiting, no manual interruption needed.

> **Note:** `medre run` starts the runtime and waits for adapter callbacks
> (real or fake) but does **not** automatically inject a smoke event. For the
> alpha walkthrough, use `medre smoke` to get inspectable evidence in one
> command.

### 1.4 Inspect and trace events

After the smoke command from Section 1.3 completes, create a minimal config
pointing to the smoke database so that ``inspect`` and ``trace`` can find it:

```bash
cat > /tmp/medre-alpha-config.toml <<EOF
[runtime]
name = "alpha-inspect"

[storage]
backend = "sqlite"
path = "/tmp/medre-alpha.db"
EOF
```

Then inspect the event and build its timeline:

```bash
# Inspect a specific event by ID
medre inspect event <event_id> --config /tmp/medre-alpha-config.toml

# Build a chronological timeline for an event
medre trace event <event_id> --config /tmp/medre-alpha-config.toml

# Collect an evidence bundle (JSON)
medre evidence --config examples/configs/fake-bridge-smoke.toml --json
```

Replace `<event_id>` with an actual event ID from the smoke test JSON output.

### 1.5 Verify with the test suite

```bash
# Run the full unit test suite (no network, no hardware)
PYTHONPATH=src pytest -q
# Expected: 3200+ passed, live tests skipped by default
```

> **Note:** The alpha walkthrough test (`tests/test_alpha_walkthrough.py`)
> exercises these same CLI commands programmatically, validating that the
> commands documented above actually work end-to-end.

---

## Section 2: Docker Matrix Path

This path exercises the Matrix adapter against a containerized Synapse
homeserver. Requires Docker and the `mindroom-nio` SDK.

### 2.1 Install Matrix dependencies

```bash
pip install -e ".[matrix,dev]"
```

### 2.2 Validate Docker Matrix config

```bash
medre config check --config examples/configs/docker-matrix-bridge.toml
```

### 2.3 Run diagnostics

```bash
# Print adapter diagnostics without starting the full runtime
medre diagnostics --config examples/configs/docker-matrix-bridge.toml
```

### 2.4 Run Docker-gated integration test

```bash
# Requires a running Synapse Docker container
PYTHONPATH=src pytest tests/integration/test_synapse_bridge_smoke.py -m docker -v
```

This test exercises the Matrix SDK against a real Synapse instance. It verifies:
- SDK login and room join
- Outbound message delivery
- Sync loop operation
- Evidence bundle collection at the `docker_sdk_boundary` level

The test includes an xfail guard for the strict sync-loop check. If the
fallback sync strategy is used instead of the strict `sync_loop` method,
the test records an xfail (expected failure) to track progress.

---

## Section 3: Live Matrix Path (Placeholder)

This path is for future live testing against a real Matrix homeserver with
real credentials. It is documented here as a reference for what would be
required, not as a tested procedure at alpha.

### 3.1 Requirements

- A running Synapse homeserver with a registered bot account
- Environment variables:
  - `MATRIX_HOMESERVER` -- homeserver URL (e.g., `https://matrix.org`)
  - `MATRIX_USER_ID` -- bot user ID (e.g., `@bot:matrix.org`)
  - `MATRIX_ACCESS_TOKEN` -- access token for the bot account
  - `MATRIX_ROOM_ID` -- target room ID (e.g., `!roomid:matrix.org`)

### 3.2 Test to run

```bash
PYTHONPATH=src pytest tests/test_matrix_live.py -m live --tb=short
```

### 3.3 Expected evidence

- Test sends a text message to the specified room.
- Test verifies the message appears in the sync response.
- Test collects adapter diagnostics.
- Test is a smoke test: it proves start, send, and report against a real
  endpoint. It does not prove sustained throughput, reconnect resilience, or
  multi-hop delivery.
- The test includes an xfail guard for third-party inbound: if no message
  from a second account arrives within 30 seconds, the test records xfail
  rather than hard failure.

---

## Section 4: What Each Path Proves

### Fake-only path

| Capability | Proven |
|-----------|--------|
| Pipeline routing (source to target) | Yes |
| Canonical event storage (SQLite) | Yes |
| Delivery receipt recording | Yes |
| Event tracing (timeline assembly) | Yes |
| Evidence bundle collection | Yes |
| RetryWorker (opt-in) | Yes (unit tests) |
| Replay engine (three modes: DRY_RUN, RE_ROUTE, BEST_EFFORT) | Yes (unit tests) |
| CLI commands | Yes |
| Config validation | Yes |

The fake path proves the software architecture works. It does not prove
any transport works with real hardware or real networks.

**Replay is manual and duplicate-risky.** Replay is a one-shot operator
action — there is no background scheduler, no automatic resume, and no
deduplication. Each `BEST_EFFORT` replay produces new outbound messages.
Always run `DRY_RUN` or `RE_ROUTE` first. See
[Replay Operation](replay-operation.md) for the full replay workflow.

**Retry is a two-level opt-in.** Scheduled retry receipts are created only
when a route has retry enabled via its `[routes.<id>.retry]` section. Routes
without a retry section record transient failures without `next_retry_at`.
The `RetryWorker` that processes due retry receipts is controlled by the
`[retry]` section in the config:

1. **Route level** — when a route declares `[routes.<id>.retry]` with
   `enabled = true`, transient delivery failures produce a `failed` receipt
   with `next_retry_at` set and retry policy metadata persisted. Routes
   without a `[routes.<id>.retry]` section (or with `enabled = false`)
   record transient failures but do not schedule retries.
2. **Worker level** — the `[retry]` section controls whether the
   `RetryWorker` runs. `enabled = true` starts a background task that
   polls for due retry receipts and re-attempts delivery. If
   `enabled = false` (default), retry receipts accumulate in storage but
   are never processed automatically. They can be inspected with
   `medre inspect receipts`.

See `examples/configs/fake-retry-smoke.toml` for a config that enables
the retry worker with fake adapters.

### Docker Matrix path

| Capability | Proven |
|-----------|--------|
| Matrix SDK login | Yes |
| Outbound message delivery to Synapse | Yes |
| Sync loop (strict mode tracked via xfail) | Partial |
| Evidence collection at SDK boundary | Yes |
| Config-file-driven adapter assembly | Yes |

The Docker path proves the Matrix adapter works with the real SDK against a
real Synapse instance in a controlled environment. It does not prove sustained
reliability or edge-case handling.

### Live Matrix path

| Capability | Proven |
|-----------|--------|
| Real endpoint connectivity | Yes (smoke only) |
| Message delivery to production homeserver | Yes (smoke only) |
| Reconnect resilience | No |
| Sustained throughput | No |
| Third-party inbound | Unconfirmed (xfail guard) |

The live path proves a single message can travel from medre to a real
homeserver. Nothing more.

---

## Quick Reference

```bash
# Fake path (zero dependencies)
medre config check --config examples/configs/fake-bridge-smoke.toml
medre smoke --json
medre smoke --config examples/configs/fake-bridge-smoke.toml \
  --storage-path /tmp/medre-alpha.db --json
PYTHONPATH=src pytest -q

# Retry config (fake adapters, retry worker enabled)
medre config check --config examples/configs/fake-retry-smoke.toml

# Docker Matrix path
pip install -e ".[matrix,dev]"
medre config check --config examples/configs/docker-matrix-bridge.toml
medre diagnostics --config examples/configs/docker-matrix-bridge.toml
PYTHONPATH=src pytest tests/integration/test_synapse_bridge_smoke.py -m docker -v

# Live Matrix path (requires env vars)
PYTHONPATH=src pytest tests/test_matrix_live.py -m live --tb=short

# Compile check (verify no syntax errors)
python -m compileall -q src tests
```

# Local Matrix Live Validation with Docker Synapse

> Last updated: 2026-05-25 (Tranche 6 truth-surface update)
> Tranche 6 session (2026-05-25): Did NOT start Docker Synapse or execute live tests.
> This update adds evidence artifact capture commands, dependency/version section,
> and clarifies Docker SDK-boundary evidence scope.
> Baseline: HEAD 41a07c7, Python 3.12.3, medre 0.1.0.
> Evidence sub-classification: Docker SDK-boundary (local containerized Synapse).
> This validates SDK integration and adapter wiring, not external network behavior.

This guide covers setting up a local Matrix Synapse instance via Docker for
live-testing MEDRE's Matrix adapter without relying on an external homeserver.

## Prerequisites

- Docker Engine (24.0+)
- `docker compose` (v2)

## Starting Synapse

```bash
# Create a directory for Synapse config and data
mkdir -p ~/medre-synapse && cd ~/medre-synapse

# Generate the Synapse config
docker run --rm -e SYNAPSE_SERVER_NAME=matrix.local \
  -e SYNAPSE_REPORT_STATS=no \
  -v "$(pwd)/data:/data" \
  matrixdotorg/synapse:latest generate

# Edit data/homeserver.yaml to enable registration:
#   enable_registration: true
#   registration_shared_secret: <a-secret>

# Create a docker-compose.yml for the Synapse service
cat > compose.yaml << 'EOF'
services:
  synapse:
    image: matrixdotorg/synapse:latest
    container_name: medre-synapse
    ports:
      - "8008:8008"
    volumes:
      - ./data:/data
EOF

# Start Synapse
docker compose up -d
```

## Registering a Bot User

```bash
docker exec -it medre-synapse register_new_matrix_user \
  -u bot_user -p bot_password -c /data/homeserver.yaml \
  http://localhost:8008
```

## Creating a Room

Join or create a room using any Matrix client (Element, etc.) connected
to `http://localhost:8008`. Note the canonical room ID
(e.g. `!abc123:matrix.local`).

## Obtaining an Access Token

The `MATRIX_ACCESS_TOKEN` is a `syt_*` token obtained by logging in:

```bash
curl -X POST http://localhost:8008/_matrix/client/v3/login \
  -H "Content-Type: application/json" \
  -d '{"type":"m.login.password","user":"bot_user","password":"bot_password"}'
```

The response contains an `access_token` field. Copy that value (it starts
with `syt_`). Do not commit it to version control.

## Required Pytest Environment Variables

These are **live-test convenience vars** — they configure the pytest
live-test fixture, not the MEDRE runtime. The five variables below
are required. `MATRIX_*` variables are separate from and unrelated
to the unsupported legacy `MEDRE_MATRIX_*` runtime config vars:

```bash
export MATRIX_HOMESERVER=http://localhost:8008
export MATRIX_USER_ID=@bot_user:matrix.local
export MATRIX_ACCESS_TOKEN=syt_<token>
export MATRIX_ROOM_ID=!abc123:matrix.local
export MATRIX_LOCAL_SYNAPSE=1
```

## Running Matrix Live Tests

Matrix live tests are opt-in through the pytest `live` marker and
require explicit environment variable configuration.

### Required Environment Variables

```bash
export MATRIX_HOMESERVER=http://localhost:8008
export MATRIX_USER_ID=@bot_user:matrix.local
export MATRIX_ACCESS_TOKEN=syt_<token>
export MATRIX_ROOM_ID=!abc123:matrix.local
export MATRIX_LOCAL_SYNAPSE=1
```

### Running the Tests

Local Synapse tests additionally check `MATRIX_LOCAL_SYNAPSE=1`
so that they only run when the local Docker Synapse is deliberately
configured:

```bash
# Run only Matrix live tests against local Synapse:
export MATRIX_LOCAL_SYNAPSE=1
pytest tests/test_matrix_live.py -v -m live

# Or run all live tests across all transports:
export MATRIX_LOCAL_SYNAPSE=1
pytest -v -m live
```

### Identifying Results

- **Skipped tests**: Appear as `s` or `SKIP` in pytest output.
  These are normal — live tests are excluded by default (`addopts = "-m 'not live'"`).
- **Passed tests**: The adapter connected to Synapse, sent a message,
  and received a Matrix `$event_id`.
- **Failed tests**: A real failure after the gate is satisfied means the
  adapter or Synapse configuration needs attention.

## Collecting Evidence

After a live test run:

```bash
medre evidence --config /tmp/medre-live/medre.toml --json
```

## Dependency / Version Capture

Before running Docker Synapse live validation, capture the environment:

```bash
# Project baseline
python3 --version                        # Expected: Python 3.12.3
grep 'version = ' pyproject.toml         # Expected: version = "0.1.0"
git log --oneline -1                     # Expected: 41a07c7

# Matrix SDK
pip show mindroom-nio 2>/dev/null || echo "NOT INSTALLED"

# Docker
docker --version
docker compose version

# Synapse version (after starting)
docker exec medre-synapse python -m synapse.app.homeserver --version 2>/dev/null || echo "Synapse version unavailable"
```

## Evidence Artifact Capture

After a successful live test run, capture evidence for the repository:

```bash
# 1. Capture pytest output with verbose timestamps
pytest tests/test_matrix_live.py -v -m live 2>&1 | tee matrix-docker-live-$(date +%Y%m%d-%H%M%S).log

# 2. Capture diagnostics (if running medre runtime)
medre evidence --config /tmp/medre-live/medre.toml --json 2>/dev/null | tee matrix-docker-evidence-$(date +%Y%m%d).json || echo "Evidence command not applicable"

# 3. Record dependency versions
pip show mindroom-nio > matrix-docker-deps-$(date +%Y%m%d).txt 2>&1

# 4. Record Synapse info
docker exec medre-synapse python -m synapse.app.homeserver --version >> matrix-docker-deps-$(date +%Y%m%d).txt 2>&1 || true
```

**Evidence recording:** Copy relevant results into the Live Validation Evidence
table below and into `docs/runbooks/operational-evidence.md` §1.1b. Include:
date, Docker Synapse version, test result counts, duration, and any caveats.

## Cleanup

```bash
cd ~/medre-synapse
docker compose down -v
rm -rf data
```

## Notes

- This setup is **opt-in only**. CI does not require Docker by default.
- Matrix live tests use standard `pytest -m live` gating — they are always skipped
  unless `-m live` is passed, the required `MATRIX_*` environment
  variables are set, **and** `MATRIX_LOCAL_SYNAPSE=1` is exported.
- The `MATRIX_*` variables shown above (including `MATRIX_LOCAL_SYNAPSE`)
  are **pytest live-test convenience vars only**. They are not MEDRE runtime
  config. Runtime config always uses `MEDRE_ADAPTER__<TOKEN>__<FIELD>` and
  `MEDRE_ROUTE__<TOKEN>__<FIELD>`. Legacy `MEDRE_MATRIX_*` runtime config
  vars remain unsupported.
- `MATRIX_LOCAL_SYNAPSE=1` is an additional gate that ensures local
  Synapse tests only run when the operator has deliberately started the
  Docker Synapse instance.

## Live Validation Evidence

| Date       | Environment          | Result               | Duration |
| ---------- | -------------------- | -------------------- | -------- |
| 2026-05-22 | local Docker Synapse | 15 passed, 1 xfailed | 40.37s   |

**Details:**

- Command: `pytest tests/test_matrix_live.py -v -m live`
- Gate: `MATRIX_LOCAL_SYNAPSE=1`
- The single xfailed test (`test_inbound_message_received`) is expected — it requires a second Matrix user to send a message during the test window.
- The new local-Synapse-specific test `test_synapse_send_captures_event_id` passed.

**No tokens are recorded.** The `MATRIX_ACCESS_TOKEN` used during validation is intentionally omitted from this document.

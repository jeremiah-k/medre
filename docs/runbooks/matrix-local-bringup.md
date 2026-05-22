# Local Matrix Live Validation with Docker Synapse

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
export MEDRE_MATRIX_LOCAL_SYNAPSE=1
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
export MEDRE_MATRIX_LOCAL_SYNAPSE=1
```

### Running the Tests

Local Synapse tests additionally check `MEDRE_MATRIX_LOCAL_SYNAPSE=1`
so that they only run when the local Docker Synapse is deliberately
configured:

```bash
# Run only Matrix live tests against local Synapse:
export MEDRE_MATRIX_LOCAL_SYNAPSE=1
pytest tests/test_matrix_live.py -v -m live

# Or run all live tests across all transports:
export MEDRE_MATRIX_LOCAL_SYNAPSE=1
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
  variables are set, **and** `MEDRE_MATRIX_LOCAL_SYNAPSE=1` is exported.
- The `MATRIX_*` variables shown above are **pytest live-test convenience
  vars only**. They are not MEDRE runtime config. Runtime config always uses
  `MEDRE_ADAPTER__<TOKEN>__<FIELD>` and `MEDRE_ROUTE__<TOKEN>__<FIELD>`.
  Legacy `MEDRE_MATRIX_*` runtime config vars remain unsupported.
- `MEDRE_MATRIX_LOCAL_SYNAPSE=1` is an additional gate that ensures local
  Synapse tests only run when the operator has deliberately started the
  Docker Synapse instance.

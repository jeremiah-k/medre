# Local Matrix Live Validation with Docker Synapse

This guide covers setting up a local Matrix Synapse instance via Docker for
live-testing MEDRE's Matrix adapter without relying on an external homeserver.

## Prerequisites

- Docker Engine (24.0+)
- ``docker compose`` (v2)

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

# Start Synapse
docker compose up -d
```

## Registering a Bot User

```bash
docker exec -it synapse-synapse-1 register_new_matrix_user \
  -u bot_user -p bot_password -c /data/homeserver.yaml \
  http://localhost:8008
```

## Creating a Room

Join or create a room using any Matrix client (Element, etc.) connected
to ``http://localhost:8008``.  Note the canonical room ID
(e.g. ``!abc123:matrix.local``).

## Required Pytest Environment Variables

These are **live-test convenience vars** — they configure the pytest
live-test fixture, not the MEDRE runtime:

```bash
export MATRIX_HOMESERVER=http://localhost:8008
export MATRIX_USER_ID=@bot_user:matrix.local
export MATRIX_ACCESS_TOKEN=syt_<token>
export MATRIX_ROOM_ID=!abc123:matrix.local
```

## Running Matrix Live Tests

```bash
# Skip live tests unless explicitly enabled:
export MEDRE_MATRIX_LOCAL_SYNAPSE=1

# Run only Matrix live tests:
pytest tests/test_matrix_live.py -v -m live

# Or run all live tests:
pytest -v -m live
```

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

- This setup is **opt-in only**.  CI does not require Docker by default.
- Tests marked ``pytest.mark.live`` are skipped unless ``MEDRE_MATRIX_LOCAL_SYNAPSE=1`` is set.
- Docker availability is checked at test time; missing Docker skips gracefully.

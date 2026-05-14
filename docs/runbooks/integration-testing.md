# MEDRE Docker Integration Testing

## Overview

MEDRE supports Docker-based integration tests that exercise real adapters
against containerized services (Synapse for Matrix, meshtasticd for
Meshtastic).  These tests are **opt-in by default** — they are excluded
from the normal `pytest` run to keep the test suite fast and portable.

## Test Tiers

| Tier | Marker | When it runs | Requirements |
|---|---|---|---|
| Unit/fake | *(default)* | Every `pytest` run | None |
| Docker integration | `docker` | Explicit opt-in | Docker daemon |
| Live hardware | `live` | Explicit opt-in | Real devices/credentials |

## Quick Start

### Prerequisites

1. **Docker** running and accessible (`docker info` should succeed).
2. **MEDRE** installed with transport extras:
   ```bash
   pip install -e ".[matrix,meshtastic,dev]"
   ```

### Run Docker Integration Tests

```bash
# Run all Docker integration tests:
pytest tests/integration/ -m docker -v

# Run just Synapse tests:
pytest tests/integration/test_synapse_connectivity.py -m docker -v

# Run just meshtasticd tests:
pytest tests/integration/test_meshtasticd_connectivity.py -m docker -v

# Run ALL tests (unit + docker + live):
pytest -m ""
```

### Using Docker Compose (manual setup)

If you want to start services manually (e.g. for debugging):

```bash
# Start Synapse + meshtasticd:
docker compose -f docker-compose.integration.yaml up -d

# Wait for health checks to pass, then run tests:
pytest tests/integration/ -m docker -v

# Tear down:
docker compose -f docker-compose.integration.yaml down -v
```

**Note:** The `conftest.py` fixtures manage their own container lifecycle.
If you start services via Docker Compose, the fixtures will detect existing
containers and skip starting duplicates where possible.

### Using the CI Script Locally

```bash
bash scripts/ci/run-docker-integration.sh
```

This script checks prerequisites, ensures MEDRE is installed with extras,
and runs the full integration suite with a 13-minute timeout.

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `MEDRE_SKIP_DOCKER` | *(unset)* | Set to `1`/`true` to skip all Docker tests |
| `MEDRE_SYNAPSE_IMAGE` | `matrixdotorg/synapse:v1.149.0` | Synapse Docker image |
| `MEDRE_MESHTASTICD_IMAGE` | `meshtastic/meshtasticd:2.7.15` | meshtasticd Docker image |
| `MEDRE_SYNAPSE_PORT` | `8008` | Host port for Synapse |
| `MEDRE_MESHTASTICD_PORT` | `4403` | Host port for meshtasticd |
| `MEDRE_MESHTASTICD_HWID` | `11` | meshtasticd hardware ID |
| `MEDRE_DOCKER_READY_TIMEOUT` | `120` | Seconds to wait per service |
| `MEDRE_CI_ARTIFACT_DIR` | `.ci-artifacts/docker-integration` | Log/config output directory |

## What Tests Exist

### Synapse Connectivity (`test_synapse_connectivity.py`)

Tests the MEDRE Matrix adapter against a real Synapse homeserver:

1. **test_adapter_starts_against_synapse** — Adapter connects and reports running.
2. **test_health_check_reports_healthy** — health_check returns True.
3. **test_health_check_before_start_is_not_healthy** — health_check is False before start.
4. **test_send_text_message_to_synapse_room** — Sends m.text and gets a native_message_id.
5. **test_start_stop_idempotent** — Double start/stop is safe.

### Meshtasticd Connectivity (`test_meshtasticd_connectivity.py`)

Tests the MEDRE Meshtastic adapter against a simulated meshtasticd node:

1. **test_raw_tcp_interface_connects** — Raw mtjk TCPInterface connects.
2. **test_adapter_starts_and_reports_healthy** — Adapter lifecycle via session.
3. **test_adapter_start_stop_idempotent** — Double start/stop is safe.
4. **test_adapter_diagnostics_exposes_session_state** — diagnostics() returns metadata.

## CI Workflow

The `.github/workflows/docker-integration.yml` workflow runs on push/PR
to main/develop.  Key features:

- **Pinned images** for deterministic behavior.
- **Docker image caching** via GitHub Actions cache (avoids re-pulling ~500MB).
- **Docker Hub login** (optional secrets for higher rate limits).
- **Artifact upload** on failure for log inspection.
- **20-minute timeout** per run.

## Future Work (Not Yet Docker-Testable)

The following transports do not have official Docker images:

- **MeshCore** — No containerized simulator. Tests remain `live`-marker
  device tests.
- **LXMF/Reticulum** — No official Docker image. Could be containerized
  in a future tranche if a simulator is developed.

Cross-transport relay tests (Matrix ↔ Meshtastic through the full MEDRE
runtime) are planned for a future iteration once single-adapter
connectivity is proven stable.


## Docker SDK-Boundary Bridge Tests

### What This Tier Proves

Docker SDK-boundary bridge tests exercise the boundary between real adapter
SDK code and the MEDRE pipeline. They prove:

1. **Dependency resolution** — real SDK libraries load and initialize.
2. **Config loading** — adapter configs with real connection parameters
   parse and validate correctly.
3. **Adapter lifecycle** — real adapters start, connect to containerized
   services, and stop cleanly.
4. **SDK boundary** — the adapter-to-runtime boundary works with real SDK
   objects (no SDK objects leak across the boundary).
5. **Pipeline routing** — events flow through the real adapter code path
   into the pipeline.

### What This Tier Does NOT Prove

- **Live network connectivity** — services run on localhost via Docker.
- **Sustained throughput** — tests are smoke tests, not load tests.
- **Cross-transport relay** — a full Matrix-to-Meshtastic bridge through
  two real adapters is not yet tested end-to-end.
- **Network resilience** — no reconnection or failure recovery testing.

### Provenance Levels

| Tier | Environment | What it proves | Status |
|------|-------------|----------------|--------|
| Fake bridge | In-memory, fake adapters | Pipeline routing, rendering, receipts, accounting | **Proven** |
| Adapter-wrapper | Unit test, mocked transport | Adapter codec, renderer, session logic | **Proven** |
| Docker SDK-boundary | Container, real deps, loopback | Real SDK lifecycle, config, dependency resolution | **Proven** |
| Live network | Real endpoints | Actual connectivity, protocol compliance | **Not claimed** |

### Docker Bridge Example Config

An illustrative TOML config is provided at
`examples/configs/docker-bridge-smoke.toml`. This config has **placeholder
credentials** — it cannot be used directly with `medre run`. Docker
integration tests build configs programmatically from `conftest.py` fixtures
that auto-register users and allocate ports.

To validate the config's TOML structure and route shape without Docker:

```bash
PYTHONPATH=src pytest tests/test_example_configs.py::TestDockerBridgeSmoke -v
```

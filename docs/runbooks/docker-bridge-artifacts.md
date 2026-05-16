# Docker Bridge Artifact Collection

> Last updated: 2026-05-16
> Scope: Opt-in Docker Matrix <-> Meshtastic logged artifact run path
> Status: Pre-beta. Not production. Artifact collection is opt-in and does not affect default CI behavior.

## Overview

The Docker bridge artifact collector is an **opt-in** tool that runs Docker integration tests for Matrix <-> Meshtastic bridge scenarios and collects structured evidence into a timestamped run directory. It is not invoked by default CI — it requires explicit activation.

**What this proves:**
- Docker SDK-boundary validation: real adapter SDKs work against containerized services.
- Config-to-runtime path with real connection parameters.
- Adapter lifecycle correctness (start, health check, deliver, stop).

**What this does NOT prove:**
- No real external Matrix account is used or proven (container-local Synapse only).
- No real radio hardware is used or proven (container-local meshtasticd simulation only).
- No cross-transport bridge between two real adapters.
- No sustained throughput, reconnect resilience, or load evidence.
- Fire-and-forget delivery for radio transports.

## Usage

### Shell Script

```bash
# Default: matrix_to_meshtastic scenario
./scripts/ci/run-docker-bridge-artifacts.sh

# Specify scenario
./scripts/ci/run-docker-bridge-artifacts.sh bidirectional
```

### Python API

```python
from medre.runtime.docker_bridge_artifacts import collect_docker_bridge_artifacts

summary = collect_docker_bridge_artifacts(
    scenario="matrix_to_meshtastic",
    timeout_minutes=15,
)

print(f"Status: {summary['status']}")
print(f"Run directory: {summary['run_directory']}")
```

## Scenarios

| Scenario | Tests Included | Description |
|----------|---------------|-------------|
| `matrix_to_meshtastic` | Synapse connectivity, bridge smoke, run session | Matrix inbound via real nio SDK, outbound to fake target |
| `meshtastic_to_matrix` | meshtasticd connectivity, SDK bridge | Meshtastic outbound via real mtjk SDK |
| `bidirectional` | All of the above | Both directions exercised |

## Artifact Directory

Artifacts are written to:

```
.ci-artifacts/docker-bridge-runs/<ISO-timestamp>/
├── summary.json           # Structured evidence summary (always written)
├── pytest-stdout.log      # Pytest stdout capture
└── pytest-stderr.log      # Pytest stderr capture
```

Reuses the existing `MEDRE_CI_ARTIFACT_DIR` environment variable and `.ci-artifacts/docker-integration` convention from `tests/integration/conftest.py`.

## summary.json Shape

```json
{
  "status": "passed | failed | partial",
  "scenario": "matrix_to_meshtastic | meshtastic_to_matrix | bidirectional",
  "timestamp": "2026-05-16T12:00:00+00:00",
  "run_directory": "/path/to/.ci-artifacts/docker-bridge-runs/2026-05-16T12-00-00Z",
  "matrix": {
    "container": "matrixdotorg/synapse:v1.149.0",
    "room": "!roomid:localhost",
    "event_id": "$synapse_event_id",
    "ingress_path": "sync_loop | direct_on_room_message_fallback | null"
  },
  "meshtastic": {
    "daemon": "meshtastic/meshtasticd:2.7.15",
    "inbound": { "pubsub_proven": true },
    "outbound": { "packet_ids": ["42"] }
  },
  "medre": {
    "event_id": "canonical-event-id",
    "receipt": { "status": "sent" },
    "native_refs": [],
    "runtime": { "passed": 3, "failed": 0, "skipped": 0, "errors": 0 },
    "limitations": [
      "Docker containers run on localhost — not a real network environment",
      "No real external Matrix account proven (container-local Synapse only)",
      "No real radio hardware proven (container-local meshtasticd simulation only)",
      "..."
    ]
  },
  "logs": {
    "pytest_stdout": "...",
    "pytest_stderr": "..."
  },
  "config_snapshot": {
    "synapse_image": "matrixdotorg/synapse:v1.149.0",
    "synapse_port": "8008",
    "..."
  },
  "inspect_artifacts": ["/path/to/artifact.log"],
  "errors": []
}
```

**`summary.json` is always written**, even on failure. When tests fail, the file contains `status: "failed"` or `status: "partial"` with populated `errors` and `limitations` fields.

## Redaction

All tokens, passwords, API keys, and secrets are redacted from the summary using the project's existing `sanitize_for_log` and `sanitize_error` utilities (from `medre.observability.sanitization`). This includes:

- Matrix access tokens (`syt_...`)
- Password fields
- Secret/config key patterns
- SDK object repr strings

No secrets appear in any artifact file.

## Environment Variables

Reuses the existing Docker integration variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MEDRE_SYNAPSE_IMAGE` | `matrixdotorg/synapse:v1.149.0` | Synapse Docker image |
| `MEDRE_MESHTASTICD_IMAGE` | `meshtastic/meshtasticd:2.7.15` | meshtasticd Docker image |
| `MEDRE_SYNAPSE_PORT` | `8008` | Synapse HTTP port |
| `MEDRE_MESHTASTICD_PORT` | `4403` | meshtasticd TCP port |
| `MEDRE_MESHTASTICD_HWID` | `11` | meshtasticd hardware ID |
| `MEDRE_DOCKER_READY_TIMEOUT` | `120` | Seconds to wait per service |
| `MEDRE_CI_ARTIFACT_DIR` | `.ci-artifacts/docker-bridge-runs` | Artifact base directory |
| `MEDRE_SKIP_DOCKER` | unset | Skip all Docker tests when set |

## Docker Test Gating

Docker tests use the existing `pytest.mark.docker` marker and `MEDRE_SKIP_DOCKER` environment variable convention. When Docker is unavailable or `MEDRE_SKIP_DOCKER` is set, all Docker tests are skipped — they are not failed. This ensures the unit test suite is never broken by missing Docker.

The artifact collection tests in `tests/test_docker_bridge_artifacts.py` do **not** require Docker. They mock the pytest subprocess runner and test the artifact plan, redaction, and summary generation logic.

## No Default CI Requirement

This artifact path is **opt-in**:

- The shell script `scripts/ci/run-docker-bridge-artifacts.sh` is not called from any existing CI workflow.
- The Python module `medre.runtime.docker_bridge_artifacts` is not imported by any default CI path.
- The existing `scripts/ci/run-docker-integration.sh` and `.github/workflows/` remain unchanged.
- Default `pytest` runs are unaffected.

## Prerequisites

To run the artifact collector with real Docker tests:

```bash
# Install transport SDKs
pip install -e ".[matrix,meshtastic,dev]"

# Verify Docker is running
docker info

# Run
./scripts/ci/run-docker-bridge-artifacts.sh
```

Without Docker, the Python helper module and its tests still work for unit testing artifact generation logic.

## Testing

```bash
# Run artifact helper tests (no Docker required)
PYTHONPATH=src pytest tests/test_docker_bridge_artifacts.py -v

# Run Docker integration tests (Docker required, gated by markers)
PYTHONPATH=src pytest tests/integration/ -m docker -v

# Verify Docker tests are skipped without Docker
MEDRE_SKIP_DOCKER=1 PYTHONPATH=src pytest tests/integration/ -m docker -v
```

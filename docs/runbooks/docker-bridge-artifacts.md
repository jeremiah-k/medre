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
├── run-metadata.json      # Run parameters, images, timestamps (always written)
├── pytest-stdout.log      # Pytest stdout capture (always written)
├── pytest-stderr.log      # Pytest stderr capture (always written)
├── synapse.log            # Synapse container logs (Matrix scenarios)
├── meshtasticd.log        # meshtasticd container logs (Meshtastic scenarios)
├── config.toml            # Config snapshot used for this run
├── medre.log              # Runtime log (best-effort, may be absent)
├── receipts.json          # Delivery receipt snapshot (best-effort)
├── native-refs.json       # Inbound native message refs (best-effort)
├── inspect-timeline.json  # Per-event pipeline timeline (best-effort)
├── evidence.json          # Full bridge evidence bundle (best-effort)
└── final-snapshot.json    # Runtime shutdown snapshot (best-effort)
```

Reuses the existing `MEDRE_CI_ARTIFACT_DIR` environment variable and `.ci-artifacts/docker-integration` convention from `tests/integration/conftest.py`.

### Required files

These files are written on every run, including failed runs:

| File | Contents | Always present? |
|------|----------|-----------------|
| `summary.json` | Structured evidence summary: status, scenario, timestamps, per-transport results, limitations. See shape below. | Yes |
| `run-metadata.json` | Run parameters: scenario name, Docker image tags, ports, timeout, Python version, MEDRE version. | Yes |
| `pytest-stdout.log` | Captured stdout from the pytest subprocess. Includes test names, assertions, and the final summary line. | Yes |
| `pytest-stderr.log` | Captured stderr from the pytest subprocess. Includes log output and any tracebacks. | Yes |
| `synapse.log` | `docker logs` output from the Synapse container. Present for `matrix_to_meshtastic` and `bidirectional` scenarios. | For Matrix scenarios |
| `meshtasticd.log` | `docker logs` output from the meshtasticd container. Present for `meshtastic_to_matrix` and `bidirectional` scenarios. | For Meshtastic scenarios |
| `config.toml` | Copy of the TOML config used to configure adapters and routes for this run. | Yes |

### Best-effort files

These files may or may not be present depending on how far the run progressed. If missing, the reason is noted in `summary.json` under `limitations`.

| File | Contents | When present |
|------|----------|--------------|
| `medre.log` | Runtime log output from the MEDRE process. Present when the runtime started and wrote to its log file before the run ended. | Runtime initialized |
| `receipts.json` | JSON array of `DeliveryReceipt` objects persisted during the run. Present when at least one delivery was attempted. | Delivery attempted |
| `native-refs.json` | JSON array of `NativeMessageRef` objects recording inbound message provenance. Present when inbound events were processed and refs were stored. | Inbound refs recorded |
| `inspect-timeline.json` | Per-event pipeline timeline produced by the inspect subsystem. Present when events were fully processed through the pipeline. | Pipeline completed for at least one event |
| `evidence.json` | Full bridge evidence bundle (events, receipts, native refs, accounting). Present when the evidence collector ran successfully. | Evidence collection succeeded |
| `final-snapshot.json` | Runtime shutdown snapshot with final accounting counters, capacity gauges, and adapter state. Present when the runtime shut down gracefully. | Graceful shutdown |

When a best-effort file is absent, `summary.json` explains why in its `limitations` array. For example: `"medre.log not written: runtime exited before log initialization"` or `"receipts.json absent: no deliveries attempted before failure"`.

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

## Inspecting Artifact Bundles

After a run completes, the artifact directory contains everything needed to understand what happened. Start with `summary.json`, then dig into logs and best-effort files as needed.

### Quick summary inspection

```bash
RUN_DIR=$(ls -td .ci-artifacts/docker-bridge-runs/*/ | head -1)

# Overall status and scenario
python -c "import json; s=json.load(open('${RUN_DIR}summary.json')); print(s['status'], s['scenario'])"

# Limitations (always read these)
python -c "import json; s=json.load(open('${RUN_DIR}summary.json')); print('\n'.join(s['medre']['limitations']))"
```

### Log inspection

```bash
RUN_DIR=$(ls -td .ci-artifacts/docker-bridge-runs/*/ | head -1)

# Pytest output (test names, pass/fail counts, tracebacks)
less "${RUN_DIR}pytest-stdout.log"
less "${RUN_DIR}pytest-stderr.log"

# Synapse container output (for Matrix scenarios)
less "${RUN_DIR}synapse.log"

# meshtasticd container output (for Meshtastic scenarios)
less "${RUN_DIR}meshtasticd.log"

# Runtime log (if present)
less "${RUN_DIR}medre.log"
```

### Receipt and evidence inspection

```bash
RUN_DIR=$(ls -td .ci-artifacts/docker-bridge-runs/*/ | head -1)

# Delivery receipts (if present)
python -c "import json; rs=json.load(open('${RUN_DIR}receipts.json')); [print(r['status'], r.get('adapter_message_id','')) for r in rs]"

# Native message refs (if present)
python -c "import json; refs=json.load(open('${RUN_DIR}native-refs.json')); [print(r) for r in refs]"

# Full evidence bundle (if present)
python -m json.tool "${RUN_DIR}evidence.json" | less

# Pipeline timeline (if present)
python -m json.tool "${RUN_DIR}inspect-timeline.json" | less
```

### Missing best-effort files

Best-effort files are written when the corresponding subsystem ran successfully. If a file is missing, the run did not reach that stage. The `limitations` array in `summary.json` explains the absence. For example, if `receipts.json` is absent, no delivery was attempted before the run ended (likely an early failure in adapter initialization or container startup).

## Proof Boundaries

### Matrix to Meshtastic (`matrix_to_meshtastic`)

**What is proven:**
- Real `nio` SDK connects to a Docker-local Synapse homeserver.
- Inbound Matrix event arrives via the real sync loop (`ingress_path == "sync_loop"`), with fallback to `_on_room_message` if sync does not deliver within the timeout.
- `MatrixCodec` decodes the event into a `CanonicalEvent`.
- Pipeline routes the event and produces a `DeliveryReceipt` with a genuine Synapse `event_id`.
- `NativeMessageRef` records the Matrix provenance.
- Adapter lifecycle (start, health check, deliver, stop) works through real SDK code.

**What is NOT proven:**
- No real external Matrix account. Synapse runs in a Docker container on localhost.
- No real Meshtastic radio. The outbound target is a fake adapter; no packet leaves the machine.
- No final ACK guarantee. Radio delivery is fire-and-forget at the protocol level.
- No sustained throughput, reconnect resilience, or load behavior.
- Meshtastic inbound is simulated (via `simulate_inbound`), not received through real pubsub callbacks.

### Meshtastic to Matrix (`meshtastic_to_matrix`)

**What is proven:**
- Real `mtjk` SDK creates a `TCPInterface` to a Docker-local meshtasticd instance.
- Pubsub subscription is established and the SDK lifecycle (start, health, stop) works.
- Outbound delivery via real `sendText` returns a real packet ID.
- Adapter lifecycle works through real SDK code.

**What is NOT proven:**
- No real radio hardware. meshtasticd runs in simulation mode inside Docker.
- Inbound Meshtastic events use `simulate_inbound`, not real pubsub callbacks. Real inbound delivery through the pubsub path is unconfirmed.
- No real external Matrix account for the outbound Matrix target.
- No cross-transport bridge between two real adapters (outbound Matrix target is fake).
- No final ACK guarantee for radio delivery.
- No replay deduplication. Replay produces independent receipts.

### Bidirectional

Combines both scenarios above. All proven and unproven statements from both individual scenarios apply.

### What remains unproven (all scenarios)

- **No external Matrix account or homeserver.** Everything runs against Docker-local Synapse.
- **No real radio.** meshtasticd simulates LoRa behavior. No over-the-air packets.
- **No cross-transport bridge with two real adapters.** Bridge smoke routes real Matrix to a fake outbound adapter.
- **No final ACK guarantee.** Radio transports are fire-and-forget. `success=True` means the local SDK accepted the payload.
- **No replay deduplication.** Replayed events produce new receipts without dedup.
- **Meshtastic inbound may be simulated.** Real pubsub callback delivery is not confirmed.
- **No sustained throughput or reconnect resilience.** Tests are smoke tests, not reliability tests.

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

The script performs **fail-fast checks** on optional imports. It does not silently install anything. If a required import is missing, the script prints the install command and exits immediately.

### Required imports per scenario

| Scenario | Required imports | Install command |
|----------|-----------------|----------------|
| `matrix_to_meshtastic` | `import nio` (from `mindroom-nio`) | `pip install -e ".[matrix,meshtastic,dev]"` |
| `meshtastic_to_matrix` | `import meshtastic`, `from pubsub import pub` | `pip install -e ".[matrix,meshtastic,dev]"` |
| `bidirectional` | All of the above | `pip install -e ".[matrix,meshtastic,dev]"` |

### Setup

```bash
# Install transport SDKs and dev dependencies
pip install -e ".[matrix,meshtastic,dev]"

# Verify Docker is running
docker info

# Run
./scripts/ci/run-docker-bridge-artifacts.sh
```

If prerequisites are missing, the script prints an error like:

```
ERROR: Matrix SDK (mindroom-nio exposes 'nio') is not installed.

Install the required extras:
  pip install -e ".[matrix,meshtastic,dev]"

Then re-run this script.
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

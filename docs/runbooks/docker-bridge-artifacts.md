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
- Cross-adapter routing for `matrix_to_meshtastic`: real Matrix nio SDK ingress, PipelineRunner routing, real Meshtastic SDK outbound to meshtasticd.

**What this does NOT prove:**
- No real external Matrix account is used or proven (container-local Synapse only).
- No real radio hardware is used or proven (container-local meshtasticd simulation only).
- No automated queue draining, real pubsub Meshtastic inbound, or sustained throughput.

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
| `matrix_to_meshtastic` | Synapse connectivity, bridge smoke, run session | Real MatrixAdapter Synapse ingress via nio SDK, routed through PipelineRunner, real MeshtasticAdapter outbound to meshtasticd. Both `synapse.log` and `meshtasticd.log` required. |
| `meshtastic_to_matrix` | meshtasticd connectivity, SDK bridge | Meshtastic SDK lifecycle and outbound `sendText` proven. Inbound uses `simulate_inbound`/`wrapper_callback`, not real pubsub. No real external Matrix target. **Direction deferred** until cross-adapter Matrix outbound is proven. |
| `bidirectional` | All of the above | Both scenarios above combined. The `meshtastic_to_matrix` leg carries the same simulate_inbound limitations. |

## Artifact Directory

Artifacts are written to:

```
.ci-artifacts/docker-bridge-runs/<ISO-timestamp>/
├── summary.json           # Structured evidence summary (always written)
├── run-metadata.json      # Run parameters, images, timestamps (always written)
├── config.toml            # Config snapshot used for this run (always written)
├── synapse.log            # Synapse container logs (required for Matrix scenarios)
├── meshtasticd.log        # meshtasticd container logs (required for Meshtastic scenarios and matrix_to_meshtastic cross-adapter)
├── medre.log              # Runtime log (best-effort; absent when PipelineRunner is used instead of full MedreApp)
├── receipts.json          # Delivery receipt snapshot (best-effort)
├── native-refs.json       # Inbound native message refs (best-effort)
├── inspect-timeline.json  # Per-event pipeline timeline (best-effort)
├── evidence.json          # Full bridge evidence bundle (best-effort)
└── final-snapshot.json    # Runtime shutdown snapshot (best-effort; absent when PipelineRunner is used instead of full MedreApp)
```

Reuses the existing `MEDRE_CI_ARTIFACT_DIR` environment variable and `.ci-artifacts/docker-integration` convention from `tests/integration/conftest.py`.

### Required files

These files are written on every run, including failed runs. The exact list depends on the scenario, because only the relevant container logs are required.

**Scenario-aware required files:**

| Scenario | Required files |
|----------|---------------|
| `matrix_to_meshtastic` | `summary.json`, `run-metadata.json`, `config.toml`, `synapse.log`, `meshtasticd.log` |
| `meshtastic_to_matrix` | `summary.json`, `run-metadata.json`, `config.toml`, `meshtasticd.log` |
| `bidirectional` | `summary.json`, `run-metadata.json`, `config.toml`, `synapse.log`, `meshtasticd.log` |

For `matrix_to_meshtastic`, both `synapse.log` and `meshtasticd.log` are required because the scenario exercises the full cross-adapter path: real Synapse ingress through the nio SDK, PipelineRunner routing, and real Meshtastic adapter outbound delivery to meshtasticd. The run starts both containers and validates SDK-to-SDK event flow end to end.

**Always required (all scenarios):**

| File | Contents |
|------|----------|
| `summary.json` | Structured evidence summary: status, scenario, timestamps, per-transport results, limitations. See shape below. |
| `run-metadata.json` | Run parameters: scenario name, Docker image tags, ports, timeout, Python version, MEDRE version. |
| `config.toml` | Copy of the TOML config used to configure adapters and routes for this run. |

**Conditionally required:**

| File | Contents | When required |
|------|----------|---------------|
| `synapse.log` | `docker logs` output from the Synapse container. | `matrix_to_meshtastic`, `bidirectional` |
| `meshtasticd.log` | `docker logs` output from the meshtasticd container. | `matrix_to_meshtastic`, `meshtastic_to_matrix`, `bidirectional` |

**Legacy note:** Older runs (before scenario-aware plans) may have `pytest-stdout.log` and `pytest-stderr.log` as required files. Current runs capture pytest output inline in `summary.json` under `logs.pytest_stdout` and `logs.pytest_stderr`.

### Best-effort files

These files may or may not be present depending on how far the run progressed. If missing, the reason is noted in `summary.json` under `limitations`.

| File | Contents | When present |
|------|----------|--------------|
| `meshtasticd.log` | `docker logs` output from the meshtasticd container. Required for `matrix_to_meshtastic`, `meshtastic_to_matrix`, and `bidirectional` scenarios (see conditionally required table above). | meshtasticd container ran |
| `medre.log` | Runtime log output from the MEDRE process. **Absent when PipelineRunner is used instead of full MedreApp** (which is the common case for Docker bridge artifact runs). Present only when the full runtime started and wrote to its log file before the run ended (e.g., `test_synapse_run_session`). | Full MedreApp runtime initialized |
| `receipts.json` | JSON array of `DeliveryReceipt` objects persisted during the run. Present when at least one delivery was attempted. | Delivery attempted |
| `native-refs.json` | JSON array of `NativeMessageRef` objects recording inbound message provenance. Present when inbound events were processed and refs were stored. | Inbound refs recorded |
| `inspect-timeline.json` | Per-event pipeline timeline produced by the inspect subsystem. Present when events were fully processed through the pipeline. | Pipeline completed for at least one event |
| `evidence.json` | Full bridge evidence bundle (events, receipts, native refs, accounting). Present when the evidence collector ran successfully. | Evidence collection succeeded |
| `final-snapshot.json` | Runtime shutdown snapshot with final accounting counters, capacity gauges, and adapter state. **Absent when PipelineRunner is used instead of full MedreApp** (which is the common case for Docker bridge artifact runs). Present only when the full MedreApp runtime shut down gracefully. | Full MedreApp graceful shutdown |

When a best-effort file is absent, `summary.json` explains why in its `limitations` array. For example: `"medre.log not written: runtime exited before log initialization"` or `"receipts.json absent: no deliveries attempted before failure"`.

**PipelineRunner vs. MedreApp note:** Docker bridge artifact runs use `PipelineRunner` directly, not the full `MedreApp` runtime. This means `medre.log` and `final-snapshot.json` are typically absent. The pipeline runner does not initialize the runtime's log subsystem or produce shutdown snapshots. When present, these files come from runs that used `MedreApp` (e.g., `test_synapse_run_session`). Do not expect them from the standard artifact collector.

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

## Manual Inspection Walkthrough

This section walks through inspecting a `matrix_to_meshtastic` artifact bundle by hand. Use this when you want to understand exactly what a run did, step by step, without relying on automated tooling.

### Step 1: Install dependencies

```bash
pip install -e ".[matrix,meshtastic,dev]"
```

This pulls in the Matrix SDK (`mindroom-nio`), the Meshtastic SDK (`mtjk`), and dev tools. The script will fail fast with a clear message if anything is missing.

### Step 2: Run the artifact collector

```bash
./scripts/ci/run-docker-bridge-artifacts.sh matrix_to_meshtastic
```

This spins up Synapse and meshtasticd containers, runs the integration tests, and writes artifacts to a timestamped directory under `.ci-artifacts/docker-bridge-runs/`. The run takes a few minutes. When it finishes, note the run directory printed at the end.

### Step 3: Read the summary

```bash
RUN_DIR=$(ls -td .ci-artifacts/docker-bridge-runs/*/ | head -1)
cat "${RUN_DIR}summary.json" | python -m json.tool
```

The `status` field tells you the top-level result:

| Status | Meaning |
|--------|---------|
| `passed` | All tests passed. Evidence fields are populated. |
| `partial` | Some tests passed, some failed. Check `errors` and `limitations`. |
| `failed` | Tests did not pass. Evidence may be sparse. Read `errors` and logs. |

Look at `matrix.ingress_path` to see how the Matrix event arrived:

- `"sync_loop"` means the real nio sync callback fired. This is the ideal path.
- `"direct_on_room_message_fallback"` means sync did not deliver within the timeout window, so the test fell back to a direct callback. The test still passes, but this is a weaker signal.
- `null` means no Matrix ingress happened at all.

Look at `meshtastic.outbound` for the cross-adapter delivery result. If present, the `packet_ids` array shows real packet IDs returned by meshtasticd's `sendText` call.

### Step 4: Inspect the container logs

```bash
# Synapse log — shows the Matrix server processing the inbound event
less "${RUN_DIR}synapse.log"

# meshtasticd log — shows the Meshtastic daemon receiving the outbound packet
# This file is required for matrix_to_meshtastic (cross-adapter delivery)
less "${RUN_DIR}meshtasticd.log"
```

In `synapse.log`, look for the event ID matching `summary.json → matrix.event_id`. In `meshtasticd.log`, look for the packet ID matching `summary.json → meshtastic.outbound.packet_ids`.

### Step 5: Inspect the evidence files

These are best-effort files. They may be absent if the run failed early. When present, they tell you what the pipeline actually did.

```bash
# Delivery receipts — shows each outbound delivery attempt and its status
python -m json.tool "${RUN_DIR}receipts.json"

# Native message refs — records the Matrix provenance (event_id, room, sender)
python -m json.tool "${RUN_DIR}native-refs.json"

# Pipeline timeline — per-event trace through codec, router, renderer, adapter
python -m json.tool "${RUN_DIR}inspect-timeline.json"

# Full evidence bundle — everything above combined
python -m json.tool "${RUN_DIR}evidence.json"
```

### Step 6: Interpret the key fields

**`ingress_path`** in `summary.json → matrix`:

- `"sync_loop"`: The real nio SDK's `sync_forever` callback delivered the event. This proves the full Matrix adapter ingress path works against a real Synapse instance.
- `"direct_on_room_message_fallback"`: Sync did not fire in time. The test injected the event through `_on_room_message` instead. The event still went through `MatrixCodec` and the full pipeline, but the sync loop itself was not the delivery mechanism.

**`meshtastic_outbound_path`** (when present in evidence or receipts):

- `"manual_send_one_after_deliver"`: After the pipeline delivered the event through the Meshtastic adapter, a follow-up manual `sendText` confirmed the outbound path. This proves the real Meshtastic SDK successfully enqueued a packet to meshtasticd.

**`status`** field:

- `"passed"`: All assertions held. Evidence is consistent.
- `"partial"`: Some assertions passed, others did not. Read `errors` and `limitations` to understand which parts worked.
- `"failed"`: The run did not complete successfully. The artifact bundle still contains useful diagnostic information, especially in `pytest-stderr.log` and `synapse.log`.

## Proof Boundaries

### Matrix to Meshtastic (`matrix_to_meshtastic`)

**What is proven:**
- Real `nio` SDK connects to a Docker-local Synapse homeserver.
- Inbound Matrix event arrives via the real sync loop (`ingress_path == "sync_loop"`), with fallback to `_on_room_message` if sync does not deliver within the timeout.
- `MatrixCodec` decodes the event into a `CanonicalEvent`.
- `PipelineRunner` routes the canonical event to the real Meshtastic adapter.
- Real `mtjk` SDK delivers the outbound payload to a Docker-local meshtasticd instance via `sendText`, returning a real packet ID.
- `DeliveryReceipt` records the result with genuine Synapse `event_id` and Meshtastic `packet_id`.
- `NativeMessageRef` records the Matrix provenance.
- Adapter lifecycle (start, health check, deliver, stop) works through real SDK code for both Matrix and Meshtastic adapters.

**What is NOT proven:**
- No real external Matrix account. Synapse runs in a Docker container on localhost.
- No real LoRa radio. meshtasticd runs in simulation mode inside Docker. No over-the-air packets.
- No automated queue draining. The test exercises a single message, not sustained message flow.
- No real pubsub Meshtastic inbound. Meshtastic events are injected via `simulate_inbound`, not received through real pubsub callbacks.
- No sustained throughput, reconnect resilience, or load behavior.
- No final ACK guarantee. Radio delivery is fire-and-forget at the protocol level.

### Meshtastic to Matrix (`meshtastic_to_matrix`)

> **Status: Deferred.** This direction is not yet proven at the cross-adapter level. The scenario exercises real Meshtastic SDK lifecycle and outbound delivery, but does not prove real Meshtastic-to-Matrix bridge flow. See limitations below.

**What is proven:**
- Real `mtjk` SDK creates a `TCPInterface` to a Docker-local meshtasticd instance.
- Outbound delivery via real `sendText` returns a real packet ID.
- Adapter lifecycle (start, health, stop) works through real SDK code.
- Pubsub subscription is established at the SDK level.

**What is NOT proven:**
- No real radio hardware. meshtasticd runs in simulation mode inside Docker.
- Inbound Meshtastic events use `simulate_inbound` or `wrapper_callback`, not real pubsub callbacks from meshtasticd. Real inbound delivery through the pubsub path is unconfirmed.
- No real external Matrix account for the outbound Matrix target. The Matrix outbound adapter is not exercised in this scenario.
- No cross-transport bridge between two real adapters (Meshtastic inbound to Matrix outbound is not proven).
- No final ACK guarantee for radio delivery.
- No replay deduplication. Replay produces independent receipts.
- **This direction is deferred** until cross-adapter Matrix outbound is proven with a real Synapse homeserver.

### Bidirectional

Combines both scenarios above. All proven and unproven statements from both individual scenarios apply.

### What remains unproven (all scenarios)

- **No external Matrix account or homeserver.** Everything runs against Docker-local Synapse.
- **No real radio.** meshtasticd simulates LoRa behavior. No over-the-air packets.
- **No automated queue draining.** Tests exercise single-message paths, not sustained message flow through an outbound queue.
- **No real pubsub Meshtastic inbound.** Meshtastic events arrive through `simulate_inbound`, not real pubsub callbacks.
- **No final ACK guarantee.** Radio transports are fire-and-forget. `success=True` means the local SDK accepted the payload.
- **No replay deduplication.** Replayed events produce new receipts without dedup.
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

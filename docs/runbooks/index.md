# Runbook Index

> Last updated: 2026-05-21
> Status: **Pre-release / Alpha.** Runbooks are living documents. Check the individual runbook for its specific status and last-updated date.

This index lists all runbooks in `docs/runbooks/`. For per-transport capability status, see `docs/STATUS.md`.

## Transport Alpha Operation

These runbooks cover running a specific transport adapter against real infrastructure.

| Runbook                         | Title                              | Purpose                                                               | Audience | Prerequisites                                                        | Related files                                                 |
| ------------------------------- | ---------------------------------- | --------------------------------------------------------------------- | -------- | -------------------------------------------------------------------- | ------------------------------------------------------------- |
| `matrix-alpha-operation.md`     | Matrix Alpha Operation Runbook     | Run MEDRE against a real Matrix homeserver, including E2EE text alpha | Operator | Matrix homeserver, bot account, `.[matrix]` or `.[matrix-e2e]` extra | `tests/test_matrix_live.py`, `tests/test_matrix_e2ee_live.py` |
| `meshtastic-alpha-operation.md` | Meshtastic Alpha Operation Runbook | Run MEDRE against a real Meshtastic radio node (TCP or serial)        | Operator | Meshtastic node, `.[meshtastic]` extra                               | `tests/test_meshtastic_live.py`                               |
| `meshcore-alpha-operation.md`   | MeshCore Alpha Operation Runbook   | Run MEDRE against a real MeshCore radio node (TCP, serial, or BLE)    | Operator | MeshCore node, `meshcore` SDK                                        | `tests/test_meshcore_live.py`                                 |
| `lxmf-alpha-operation.md`       | LXMF Alpha Operation Runbook       | Run MEDRE against a real Reticulum/LXMF network                       | Operator | Reticulum instance, `lxmf` package                                   | `tests/test_lxmf_live.py`                                     |

## Transport Live Smoke Tests

Quick validation procedures for each transport against real infrastructure.

| Runbook                    | Title                              | Purpose                                             | Audience | Prerequisites                                  | Related files                   |
| -------------------------- | ---------------------------------- | --------------------------------------------------- | -------- | ---------------------------------------------- | ------------------------------- |
| `matrix-live-smoke.md`     | Matrix Live Smoke Test Runbook     | Quick live validation of Matrix adapter methods     | Operator | `MATRIX_*` env vars, `.[matrix]` extra         | `tests/test_matrix_live.py`     |
| `meshtastic-live-smoke.md` | Meshtastic Live Smoke Test Runbook | Quick live validation of Meshtastic adapter methods | Operator | `MESHTASTIC_*` env vars, `.[meshtastic]` extra | `tests/test_meshtastic_live.py` |
| `meshcore-live-smoke.md`   | MeshCore Live Smoke Test Runbook   | Quick live validation of MeshCore adapter methods   | Operator | `MESHCORE_*` env vars, `meshcore` SDK          | `tests/test_meshcore_live.py`   |
| `lxmf-live-smoke.md`       | LXMF Live Smoke Test Runbook       | Quick live validation of LXMF adapter methods       | Operator | Reticulum instance, `lxmf` package             | `tests/test_lxmf_live.py`       |

## Operator Workflows

Day-to-day operational procedures.

| Runbook                 | Title                     | Purpose                                                                            | Audience | Prerequisites                      | Related files                         |
| ----------------------- | ------------------------- | ---------------------------------------------------------------------------------- | -------- | ---------------------------------- | ------------------------------------- |
| `operator-workflows.md` | Operator Workflows        | End-to-end smoke tests, evidence collection, event tracing, failure interpretation | Operator | MEDRE installed                    | `docs/STATUS.md`                      |
| `event-tracing.md`      | Event Tracing Runbook     | Trace events through the pipeline lifecycle using `medre trace`                    | Operator | SQLite storage from a previous run | `docs/runbooks/replay-operation.md`   |
| `replay-operation.md`   | Replay Operation Runbook  | Re-process historical events (DRY_RUN, RE_ROUTE, BEST_EFFORT)                      | Operator | SQLite storage, TOML config        | `docs/runbooks/event-tracing.md`      |
| `alpha-walkthrough.md`  | Alpha Walkthrough Runbook | Guided walkthrough of the preferred product path                                   | Operator | MEDRE installed                    | `docs/runbooks/operator-workflows.md` |

## Evidence and Diagnostics

Procedures for collecting, interpreting, and sharing diagnostic data.

| Runbook                            | Title                             | Purpose                                                   | Audience            | Prerequisites                         | Related files                           |
| ---------------------------------- | --------------------------------- | --------------------------------------------------------- | ------------------- | ------------------------------------- | --------------------------------------- |
| `operational-evidence.md`          | Operational Evidence Runbook      | Collect and interpret operational evidence bundles        | Operator, Developer | Storage or config from a previous run | `docs/runbooks/operator-workflows.md`   |
| `live-operational-evidence.md`     | Live Operational Evidence Runbook | Evidence collection during live sessions                  | Operator            | Live transport running                | `docs/runbooks/operational-evidence.md` |
| `bridge-evidence-bundle.md`        | Bridge Evidence Bundle            | Evidence bundle format and field reference                | Developer           | None                                  | `src/medre/evidence/`                   |
| `fake-bridge-evidence-criteria.md` | Fake Bridge Evidence Criteria     | What constitutes valid evidence from fake bridge sessions | Developer           | None                                  | `tests/`                                |
| `fake-bridge-smoke-runbook.md`     | Fake Bridge Smoke Runbook         | Run the fake bridge smoke test and interpret results      | Operator, Developer | MEDRE installed                       | `tests/`                                |

## Bridge Operation

Procedures for operating MEDRE as a bridge between transports.

| Runbook                             | Title                                     | Purpose                                                | Audience | Prerequisites                          | Related files                                     |
| ----------------------------------- | ----------------------------------------- | ------------------------------------------------------ | -------- | -------------------------------------- | ------------------------------------------------- |
| `bridge-operation.md`               | Bridge Operation Runbook                  | Operate MEDRE as a bridge between two transports       | Operator | Two configured transports              | `docs/runbooks/live-matrix-meshtastic-bringup.md` |
| `bridge-recovery.md`                | Bridge Recovery Runbook                   | Recover from bridge failures                           | Operator | Previous bridge session                | `docs/runbooks/bridge-failure-drills.md`          |
| `bridge-failure-drills.md`          | Bridge Failure Drills Runbook             | Practice failure scenarios and recovery procedures     | Operator | MEDRE installed, configured transports | `docs/runbooks/bridge-recovery.md`                |
| `live-matrix-meshtastic-bringup.md` | Live Matrix to Meshtastic Bridge Bring-Up | Bring up a live bridge between Matrix and Meshtastic   | Operator | Both transports configured             | `docs/runbooks/bridge-operation.md`               |
| `docker-bridge-artifacts.md`        | Docker Bridge Artifact Collection         | Collect artifacts from Docker-based bridge deployments | Operator | Docker, running bridge container       | `docs/runbooks/bridge-operation.md`               |

## Runtime and Configuration

Configuration, runtime operation, and supervision.

| Runbook                  | Title                                   | Purpose                                               | Audience            | Prerequisites          | Related files                        |
| ------------------------ | --------------------------------------- | ----------------------------------------------------- | ------------------- | ---------------------- | ------------------------------------ |
| `configuration.md`       | MEDRE Configuration                     | Configuration file format, env vars, and defaults     | Operator, Developer | MEDRE installed        | `src/medre/config/`                  |
| `runtime-operation.md`   | MEDRE Runtime Operation                 | Runtime lifecycle, startup, shutdown, subsystems      | Operator            | MEDRE installed        | `src/medre/runtime/`                 |
| `runtime-supervision.md` | MEDRE Runtime Supervision Runbook       | Monitoring and supervising the MEDRE runtime process  | Operator            | Running MEDRE instance | `docs/runbooks/runtime-operation.md` |
| `secure-credentials.md`  | Secure Credential and Identity Handling | How MEDRE handles credentials, tokens, and identities | Operator, Developer | None                   | `src/medre/config/`                  |
| `container-operation.md` | Container Operation Runbook             | Run MEDRE in Docker containers                        | Operator            | Docker                 | `Dockerfile`, `docker-compose.yml`   |

## Installation and Setup

Getting MEDRE installed and ready.

| Runbook                    | Title                                    | Purpose                                                  | Audience            | Prerequisites     | Related files    |
| -------------------------- | ---------------------------------------- | -------------------------------------------------------- | ------------------- | ----------------- | ---------------- |
| `alpha-installation.md`    | Alpha Installation and First-Run Runbook | Install MEDRE and run the first smoke test               | Operator, Developer | Python 3.11+, Git | `pyproject.toml` |
| `developer-environment.md` | Developer Environment Setup Guide        | Set up a development environment for contributing        | Developer           | Python 3.11+, Git | `pyproject.toml` |
| `hardware-inventory.md`    | Hardware Inventory                       | Track hardware used for testing (radios, nodes, servers) | Operator            | None              | None             |
| `embedding-medre.md`       | Embedding MEDRE as a Library             | Use MEDRE as a library in other Python projects          | Developer           | MEDRE installed   | `src/medre/`     |

## Validation and Testing

Validation procedures, testing guides, and quality assurance.

| Runbook                    | Title                            | Purpose                                                       | Audience            | Prerequisites                | Related files                         |
| -------------------------- | -------------------------------- | ------------------------------------------------------------- | ------------------- | ---------------------------- | ------------------------------------- |
| `deployment-validation.md` | Deployment Validation Runbook    | Validate a MEDRE deployment is working correctly              | Operator            | Deployed MEDRE instance      | `docs/runbooks/runtime-operation.md`  |
| `integration-testing.md`   | MEDRE Docker Integration Testing | Run integration tests in Docker                               | Developer           | Docker                       | `tests/`                              |
| `beta-entry-validation.md` | Beta Entry Validation Runbook    | Validation criteria for promoting features from alpha to beta | Developer           | Test results from alpha      | `docs/STATUS.md`                      |
| `longrun-validation.md`    | Longrun Validation Runbook       | Sustained validation over extended periods                    | Operator            | Running MEDRE instance       | `docs/runbooks/soak-testing.md`       |
| `soak-testing.md`          | Soak Testing Runbook             | Long-duration soak testing procedures                         | Operator, Developer | Running MEDRE instance       | `docs/runbooks/longrun-validation.md` |
| `routing-correctness.md`   | Routing Correctness Runbook      | Verify that routing produces correct results                  | Developer           | Test data, configured routes | `tests/`                              |

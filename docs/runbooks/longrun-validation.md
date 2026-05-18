# Longrun Validation Runbook

> Last updated: 2026-05-12
> Tracks: 2, 3, 7, 8, 9
> Status: Procedures documented. No longrun evidence recorded. Deployment boundary enforcement and runtime duration fields added (Track 8/9).
> Scope: Evidence capture during extended (multi-minute to multi-hour) adapter operation. Distinct from `soak-testing.md` which covers soak harness infrastructure.
> Evidence schema: `docs/contracts/61-operational-evidence-contract.md`
> Related: `docs/runbooks/soak-testing.md`, `docs/runbooks/operational-evidence.md`, `docs/runbooks/live-operational-evidence.md`, `docs/runbooks/deployment-validation.md`

This runbook defines what to observe, record, and report when running MEDRE adapters for extended periods against real endpoints. It does not define soak harness infrastructure (see `soak-testing.md`). It defines the **evidence capture protocol** for longrun validation.

**Longrun validation is observational.** It does not assert on throughput, latency, or message ordering. It reports what happened over time.

## 1. Purpose

Longrun validation answers questions that short smoke tests cannot:

1. Does the adapter maintain stable health over minutes/hours of operation?
2. Do diagnostics counters drift (indicating leaks) or stabilize?
3. Does reconnect behavior recover cleanly after real network interruptions?
4. Does the adapter's memory, task count, and queue depth remain bounded?
5. Do crypto store operations remain stable across many E2EE operations (Matrix only)?

Short smoke tests prove the adapter works at a point in time. Longrun validation proves it works over time.

## 2. Terminology

| Term                     | Meaning                                                                                                                          |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| **Longrun**              | An adapter operating against a real endpoint for a configurable duration (30s–300s in CI, minutes to hours in manual operation). |
| **Observation interval** | How frequently diagnostics snapshots are captured (default: every 10 seconds).                                                   |
| **Health timeline**      | The sequence of health states (`healthy`, `degraded`, `failed`) over the longrun duration.                                       |
| **Diagnostics drift**    | Any diagnostic counter that grows without bound or does not stabilize.                                                           |
| **Evidence record**      | The structured output of a longrun session, stored per Contract 61.                                                              |

## 3. Longrun Evidence Fields

### 3.1 Universal Fields (all transports)

| Field                          | Required | Description                                                 |
| ------------------------------ | -------- | ----------------------------------------------------------- |
| `tier`                         | Yes      | Evidence tier per Contract 61 (typically `R` for longrun)   |
| `transport`                    | Yes      | Transport type (`matrix`, `meshtastic`, `meshcore`, `lxmf`) |
| `execution_date`               | Yes      | ISO date of execution                                       |
| `duration_seconds`             | Yes      | Total longrun duration                                      |
| `observation_interval_seconds` | Yes      | How often diagnostics were sampled                          |
| `health_timeline`              | Yes      | Sequence of health states with timestamps                   |
| `reconnect_count`              | Yes      | Number of reconnect cycles observed                         |
| `max_reconnect_attempts_seen`  | Yes      | Maximum consecutive reconnect attempts in any single cycle  |
| `messages_sent`                | Yes      | Total outbound messages during longrun                      |
| `messages_succeeded`           | Yes      | Outbound messages that completed successfully               |
| `messages_failed`              | Yes      | Outbound messages that failed permanently                   |
| `diagnostics_start`            | Yes      | Diagnostics snapshot at start of longrun                    |
| `diagnostics_end`              | Yes      | Diagnostics snapshot at end of longrun                      |
| `diagnostics_drift`            | Yes      | List of counters that drifted (grew without bound)          |
| `memory_trend`                 | Yes      | Memory usage trend (stable, growing, decreasing)            |
| `task_count_trend`             | Yes      | Background task count trend                                 |
| `caveats_observed`             | Yes      | Any unexpected behavior during longrun                      |
| `operator`                     | Yes      | Who ran the longrun (automated / operator name)             |

### 3.2 Matrix-Specific Longrun Fields

| Field                            | Required    | Description                                                      |
| -------------------------------- | ----------- | ---------------------------------------------------------------- |
| `sync_error_count`               | Yes         | Number of sync errors during longrun                             |
| `undecryptable_event_trend`      | Yes         | How undecryptable_event_count changed over time                  |
| `encrypted_room_count_stable`    | Yes         | Whether encrypted_room_count remained constant                   |
| `crypto_store_loaded_throughout` | Yes         | Whether crypto_store_loaded remained True throughout (E2EE mode) |
| `e2ee_operation_count`           | Conditional | Number of encrypted send/decrypt operations (E2EE mode)          |

### 3.3 Meshtastic-Specific Longrun Fields

| Field                        | Required | Description                                      |
| ---------------------------- | -------- | ------------------------------------------------ |
| `serial_disconnect_count`    | Yes      | Number of serial/TCP disconnect events           |
| `reconnect_success_count`    | Yes      | Number of successful reconnect recoveries        |
| `reconnect_failure_count`    | Yes      | Number of reconnect cycles that exhausted budget |
| `inbound_packet_count`       | Yes      | Total inbound packets received                   |
| `outbound_queue_depth_max`   | Yes      | Maximum outbound queue depth observed            |
| `outbound_queue_depth_final` | Yes      | Outbound queue depth at end of longrun           |
| `node_id_stable`             | Yes      | Whether node_id remained constant throughout     |

## 4. Longrun Procedures

### 4.1 Matrix Longrun Procedure

**Prerequisites:** Same as Matrix live smoke (see `live-operational-evidence.md` §1.1).

**Command:**

```bash
# Install SDK
pip install -e ".[matrix]"

# Set env vars (same as smoke test)
export MATRIX_HOMESERVER="https://matrix.example.com"
export MATRIX_USER_ID="@bot:example.com"
export MATRIX_ACCESS_TOKEN="syt_..."
export MATRIX_ROOM_ID="!room:example.com"

# Run Matrix soak (same infrastructure as soak-testing.md Tier 2)
SOAK_DURATION_SECONDS=120 pytest tests/test_soak.py::TestMatrixSoak -m live -v -s

# Record results in operational-evidence.md and this document.
```

**What to observe during the run:**

1. **Health stability:** Adapter should remain `healthy` throughout unless a real network interruption occurs.
2. **Sync error count:** Should remain low or zero on a stable homeserver.
3. **Diagnostics snapshot at start and end:** Compare `connected`, `sync_task_running`, `reconnect_attempts`, `inbound_published`, `inbound_suppressed_*` fields.
4. **Memory trend:** Monitor RSS or process memory. Should stabilize, not grow indefinitely.
5. **Background task count:** Should remain constant (no task leaks).

#### §4.1 NOT EXECUTED (current machine)

| Field | Value |
| **Resolution** | Set Matrix env vars, run soak command, record evidence. |

**Historical soak evidence status:** NOT EXECUTED for all transports. See `operational-evidence.md` §1.4, §2.2.

### 4.2 Matrix E2EE Longrun Procedure

**Prerequisites:** Same as Matrix E2EE smoke plus `.[matrix-e2e]` installed.

**Additional observations:**

1. **Crypto store loaded:** Should remain `True` throughout. If it drops to `False`, that indicates a crypto store error.
2. **Undecryptable events:** Count should not grow unboundedly. Some undecryptable events are expected from devices that left the room or whose keys are unavailable.
3. **E2EE operation count:** Track encrypted sends and decryptions to confirm the crypto path is exercised.

#### §4.2 NOT EXECUTED (current machine)

| Field | Value |

### 4.3 Meshtastic Longrun Procedure

**Prerequisites:** Same as Meshtastic live smoke (see `live-operational-evidence.md` §2.1).

**Command:**

```bash
# Install SDK
pip install -e ".[meshtastic]"

# Set env vars
export MESHTASTIC_CONNECTION_TYPE="serial"
export MESHTASTIC_SERIAL_PORT="/dev/ttyACM0"

# Run Meshtastic soak
SOAK_DURATION_SECONDS=120 pytest tests/test_soak.py::TestMeshtasticSoak -m live -v -s
```

**What to observe during the run:**

1. **Connection stability:** `connected` should remain `True`. If it drops, observe reconnect behavior.
2. **Serial disconnect/reconnect:** If the USB cable is physically disturbed, observe the reconnect loop.
3. **Queue depth:** `queue_pending` should not grow unboundedly. If it does, `send_one` is not keeping up with `deliver` rate.
4. **Inbound packet count:** Should increment as radio traffic is received.
5. **Node ID stability:** Should remain constant throughout.

#### NOT EXECUTED (current machine)

| Field              | Value                                                                     |
| ------------------ | ------------------------------------------------------------------------- |
| **Execution date** | NOT EXECUTED                                                              |
| **Reason**         | No Meshtastic radio hardware connected.                                   |
| **Resolution**     | Connect Meshtastic node, set env vars, run soak command, record evidence. |

### 4.4 Dry-Run Longrun (No Hardware Required)

The soak harness infrastructure can be validated without real endpoints:

```bash
# Tier 1: Harness validation (no hardware needed)
pytest tests/test_soak_harness.py tests/test_soak_config_builder.py -v

# Increase iterations for longer dry run
SOAK_HARNESS_ITERATIONS=100 pytest tests/test_soak_harness.py -v
```

This validates the soak infrastructure (start/stop cycles, state cleanup, queue depths, iteration stability) but produces S-tier (simulated) evidence only.

## 5. Evidence Recording Template

When a longrun completes, record the following in this section:

```markdown
### Longrun Evidence: [Transport] — [Date]

| Field                        | Value                                 |
| ---------------------------- | ------------------------------------- |
| tier                         | R (or S for dry-run)                  |
| transport                    | matrix / meshtastic / meshcore / lxmf |
| execution_date               | YYYY-MM-DD                            |
| duration_seconds             | (value)                               |
| observation_interval_seconds | (value, default 10)                   |
| operator                     | (name or "automated")                 |
| medre_commit                 | (hash)                                |
| python_version               | (version)                             |
| environment                  | (description)                         |
| health_timeline              | (sequence of states with timestamps)  |
| reconnect_count              | (count)                               |
| max_reconnect_attempts_seen  | (count)                               |
| messages_sent                | (count)                               |
| messages_succeeded           | (count)                               |
| messages_failed              | (count)                               |
| diagnostics_start            | (snapshot)                            |
| diagnostics_end              | (snapshot)                            |
| diagnostics_drift            | (none, or list of drifted counters)   |
| memory_trend                 | stable / growing / decreasing         |
| task_count_trend             | stable / growing / decreasing         |
| caveats_observed             | (none, or description)                |
```

**No longrun evidence has been recorded yet.** All entries below are placeholders.

### 5.1 Matrix Longrun Evidence

| Field      | Value                                                       |
| ---------- | ----------------------------------------------------------- |
| **tier**   | NOT EXECUTED                                                |
| **reason** | No Matrix homeserver credentials configured on this machine |

### 5.2 Meshtastic Longrun Evidence

| Field      | Value                                                  |
| ---------- | ------------------------------------------------------ |
| **tier**   | NOT EXECUTED                                           |
| **reason** | No Meshtastic radio hardware connected to this machine |

### 5.3 MeshCore Longrun Evidence

| Field      | Value                                                                   |
| ---------- | ----------------------------------------------------------------------- |
| **tier**   | NOT EXECUTED                                                            |
| **reason** | No MeshCore hardware available. No soak test class exists for MeshCore. |

### 5.4 LXMF Longrun Evidence

| Field      | Value                                                               |
| ---------- | ------------------------------------------------------------------- |
| **tier**   | NOT EXECUTED                                                        |
| **reason** | No Reticulum network available. No soak test class exists for LXMF. |

### 5.5 Dry-Run Harness Evidence

| Field          | Value                                                                                                 |
| -------------- | ----------------------------------------------------------------------------------------------------- |
| **tier**       | NOT EXECUTED (not run during this session)                                                            |
| **test files** | `tests/test_soak_harness.py`, `tests/test_soak_config_builder.py`                                     |
| **resolution** | Run `pytest tests/test_soak_harness.py tests/test_soak_config_builder.py -v` and record results here. |

## 6. Longrun vs. Soak Testing Distinction

| Aspect     | Soak testing (`soak-testing.md`)       | Longrun validation (this document)            |
| ---------- | -------------------------------------- | --------------------------------------------- |
| Focus      | Harness infrastructure and procedures  | Evidence capture and recording                |
| Tiers      | CI dry-run → manual soak → live soak   | S-tier dry-run → R-tier live longrun          |
| Test files | `test_soak.py`, `test_soak_harness.py` | Same files, different evidence protocol       |
| Output     | Pass/fail + console output             | Structured evidence record per Contract 61    |
| Ownership  | Soak harness is infrastructure         | Longrun evidence is operational documentation |

Both documents reference the same test infrastructure. `soak-testing.md` defines how to run soaks. This document defines what evidence to capture and how to record it.

## 7. Cross-References

| Document                                                 | Relationship                                                |
| -------------------------------------------------------- | ----------------------------------------------------------- |
| `docs/contracts/61-operational-evidence-contract.md`     | Evidence schema and classification                          |
| `docs/runbooks/operational-evidence.md`                  | Primary evidence recording (links to longrun evidence here) |
| `docs/runbooks/live-operational-evidence.md`             | Short-duration live procedures                              |
| `docs/runbooks/soak-testing.md`                          | Soak harness infrastructure and procedures                  |
| `docs/contracts/37-transport-maturity-classification.md` | Transport maturity uses longrun evidence                    |
| `docs/contracts/39-operational-risk-register.md`         | Risks informed by longrun evidence gaps                     |
| `docs/contracts/59-runtime-durability-contract.md`       | Durability claims require longrun evidence                  |
| `docs/runbooks/deployment-validation.md`                 | Deployment boundary validation (Track 8/9)                  |
| `docs/contracts/60-runtime-cancellation-contract.md`     | Cancellation during longrun shutdown                        |

## 8. Deployment and Boundedness Observation Fields (Track 8/9)

Longrun evidence must include deployment and boundedness observations per Contract 61 §3.6:

| Field                          | Required | Description                                                                  |
| ------------------------------ | -------- | ---------------------------------------------------------------------------- |
| `deployment_mode`              | Yes      | `container` (MEDRE_HOME set) or `host` (XDG mode)                            |
| `runtime_duration_seconds`     | Yes      | Wall-clock duration of the longrun session (already in §3.1)                 |
| `boundedness_confirmed`        | Yes      | Whether all bounded resources stayed within limits during the entire longrun |
| `reconnect_events`             | Yes      | Number of reconnect events (already in §3.1)                                 |
| `restart_events`               | Yes      | Number of adapter restart events during longrun, or 0                        |
| `deployment_path_verified`     | Yes      | Whether runtime paths remained valid throughout longrun                      |
| `adapter_state_isolation_held` | Yes      | Whether adapter state roots remained isolated throughout longrun             |

All fields above are NOT EXECUTED for all transports. No longrun evidence has been recorded.

## 9. Unresolved Risks

| Risk                                          | Status                         | Mitigation                                                                                           |
| --------------------------------------------- | ------------------------------ | ---------------------------------------------------------------------------------------------------- |
| No longrun evidence for any transport         | NOT EXECUTED                   | Run longrun procedures against real endpoints, record evidence per §5.                               |
| Memory drift not measured                     | NOT EXECUTED                   | Add psutil RSS capture at observation intervals. Requires psutil installed.                          |
| Unbounded SQLite growth during longrun        | Theoretical (Contract 59 §6.1) | Monitor database file size during longrun. No automatic pruning.                                     |
| Longrun reconnect behavior unobserved         | NOT EXECUTED                   | Longrun procedures include reconnect observation fields. Requires network interruption during run.   |
| Adapter resource leaks over extended duration | Not validated                  | Longrun evidence would reveal task leaks, counter drift, or memory growth. Requires R-tier evidence. |
| Container longrun not tested                  | NOT EXECUTED                   | Container deployment longrun requires live container runtime. No container evidence exists.          |

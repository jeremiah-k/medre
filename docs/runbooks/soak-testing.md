# Soak Testing Runbook

> Last updated: 2026-05-11
> Status: Procedures defined. No live soak evidence recorded yet.
> Related: `docs/runbooks/operational-evidence.md`, `tests/test_soak.py`, Contract 59 (Runtime Durability), Contract 60 (Runtime Cancellation)

This document defines three tiers of soak testing for MEDRE, from CI-friendly
dry runs to manual extended soaks against live endpoints. Soak tests prove that
adapters maintain session health, reconnect correctly, and do not leak resources
over sustained operation.

**Soak tests are observational.** They do not assert on throughput, latency, or
message ordering. They report what happened.


## 1. Terminology

| Term | Meaning |
|------|---------|
| **Dry run** | Uses `SoakRuntime` harness with fake adapters. No hardware or credentials needed. |
| **Manual soak** | Uses real transport endpoint. Duration configurable (30–300 s). Operator-supervised. |
| **Live soak** | Same as manual soak but executed against production-adjacent infrastructure. |
| **SOAK_DURATION_SECONDS** | Environment variable controlling soak duration. Default 30 s, hard cap 300 s. |


## 2. Tier 1: Short CI-Friendly Dry Run

This tier uses the soak **harness** (`tests/test_soak_harness.py`) with fake
adapters. It validates the soak infrastructure itself — start/stop cycles,
state cleanup, queue depths, iteration stability. No transport SDK, hardware,
or credentials required.

### 2.1 When to run

- Every CI pipeline
- Every PR that touches runtime, adapter, or soak code
- Before any live soak attempt

### 2.2 Procedure

```bash
# Ensure dev dependencies are installed
pip install -e ".[dev]"

# Run harness tests (default: 50 iterations)
pytest tests/test_soak_harness.py tests/test_soak_config_builder.py -v

# Optional: increase iterations for longer dry run
SOAK_HARNESS_ITERATIONS=100 pytest tests/test_soak_harness.py -v
```

### 2.3 Expected results

| Check | Expected |
|-------|----------|
| Start/stop cycles (10x) | All pass, no leaked tasks |
| State clean after each cycle | All state cleared between cycles |
| Queue depths within limits | No unbounded growth |
| N iterations no degradation | All iterations complete within bounds |

### 2.4 Evidence to record

Record in `docs/runbooks/operational-evidence.md` under a new subsection:

```
### Soak Harness Evidence
| Field | Value |
|-------|-------|
| Test files | tests/test_soak_harness.py, tests/test_soak_config_builder.py |
| Last execution date | (date) |
| SOAK_HARNESS_ITERATIONS | (value, default 50) |
| Passed / Failed | (result) |
```


## 3. Tier 2: Manual Longer Soak (With Real Endpoint)

This tier uses `tests/test_soak.py` against a real transport endpoint. It
requires the transport SDK installed, valid credentials, and a reachable
endpoint (homeserver, radio, or device).

### 3.1 Matrix Soak

```bash
# Install Matrix SDK
pip install -e ".[matrix]"

# Set required environment variables
export MATRIX_HOMESERVER="https://matrix.example.com"
export MATRIX_USER_ID="@bot:example.com"
export MATRIX_ACCESS_TOKEN="syt_..."
export MATRIX_ROOM_ID="!room:example.com"

# Set soak duration (default 30, max 300)
export SOAK_DURATION_SECONDS=60

# Run Matrix soak
pytest tests/test_soak.py::TestMatrixSoak -m live -v -s
```

**What the Matrix soak tests verify:**

1. `test_connect_and_maintain_health` — Session stays healthy for the full duration.
2. `test_send_periodic_messages` — Sends 1 message per 10 seconds. Verifies
   `event_id` returned for each send.
3. `test_reconnect_recovery` — Triggers disconnect, observes reconnect to healthy.
4. Reports health timeline, reconnect attempts, message send/succeed counts.

### 3.2 Meshtastic Soak

```bash
# Install Meshtastic SDK
pip install -e ".[meshtastic]"

# Set required environment variables
# For serial:
export MESHTASTIC_CONNECTION_TYPE="serial"
export MESHTASTIC_SERIAL_PORT="/dev/ttyACM0"

# For TCP:
export MESHTASTIC_CONNECTION_TYPE="tcp"
export MESHTASTIC_HOST="192.168.1.100"
export MESHTASTIC_PORT="4403"

# Optional: channel index (default 0)
export MESHTASTIC_CHANNEL_INDEX="0"

# Set soak duration
export SOAK_DURATION_SECONDS=60

# Run Meshtastic soak
pytest tests/test_soak.py::TestMeshtasticSoak -m live -v -s
```

**What the Meshtastic soak tests verify:**

1. `test_connect_and_maintain_health` — Radio connection stays healthy.
2. `test_send_periodic_text_packets` — Sends 1 text per 10 seconds. Verifies
   `MeshPacket` returned with populated `id`.
3. `test_reconnect_recovery` — Triggers disconnect, observes reconnect.
4. Reports health timeline, reconnect attempts, inbound packet observations.

### 3.3 MeshCore Soak

No soak test class exists for MeshCore yet. When hardware becomes available:

1. Create `tests/test_soak.py::TestMeshCoreSoak` following the Matrix/Meshtastic
   pattern.
2. Set `MESHCORE_CONNECTION_TYPE`, `MESHCORE_HOST` (or `MESHCORE_SERIAL_PORT`).
3. Run with `SOAK_DURATION_SECONDS=60 pytest tests/test_soak.py::TestMeshCoreSoak -m live -v -s`.

### 3.4 LXMF Soak

No soak test class exists for LXMF yet. When a Reticulum network becomes
available:

1. Create `tests/test_soak.py::TestLXMFSoak` following the Matrix/Meshtastic
   pattern.
2. Set `LXMF_CONNECTION_TYPE`, `LXMF_IDENTITY_PATH`.
3. Run with `SOAK_DURATION_SECONDS=60 pytest tests/test_soak.py::TestLXMFSoak -m live -v -s`.

### 3.5 Evidence to record

For each transport soak, record in `docs/runbooks/operational-evidence.md`:

```
### N.M Soak Test Evidence
| Field | Value |
|-------|-------|
| Test class | tests/test_soak.py::TestXxxSoak |
| Last execution date | (date) |
| SOAK_DURATION_SECONDS | (value) |
| Messages sent | (count) |
| Messages succeeded | (count) |
| Max reconnect attempts seen | (count) |
| Session health throughout | (description) |
| Caveats observed | (description or "none") |
```


## 4. Tier 3: Live Soak (Production-Adjacent)

A live soak is a manual soak executed in an environment that closely
resembles the target deployment. The procedures are identical to Tier 2,
but the **expectations and evidence requirements** are stricter.

### 4.1 When to run

- Before declaring any transport "beta-ready" for external operators
- After significant changes to reconnect, health, or session code
- When validating against a new firmware version or SDK release

### 4.2 Additional evidence requirements

Beyond the Tier 2 evidence, a live soak must record:

| Field | Required |
|-------|----------|
| Exact MEDRE commit hash | Yes |
| Transport SDK version (`pip show <package>`) | Yes |
| Firmware version (for hardware transports) | Yes |
| Network topology description | Yes (e.g., "single radio, USB-serial, firmware 2.7.19") |
| Observed message delivery | Yes (event_ids or packet_ids) |
| Error log excerpts (if any errors) | Yes |
| Resource usage observations | Desired (memory, open files, task count) |

### 4.3 Duration guidance

| Scope | Recommended duration |
|-------|---------------------|
| Minimum live soak | 60 seconds (`SOAK_DURATION_SECONDS=60`) |
| Standard live soak | 120 seconds (`SOAK_DURATION_SECONDS=120`) |
| Extended live soak | 300 seconds (`SOAK_DURATION_SECONDS=300`) |

The 300-second hard cap in `test_soak.py` prevents accidental indefinite runs.
For longer observations, use the MEDRE runtime directly with
`medre run --config <path>` and monitor health diagnostics.


## 5. Safety Constraints

All soak tests enforce these constraints (defined in `tests/test_soak.py`):

1. **Duration hard cap:** 300 seconds maximum, regardless of env var.
2. **Send rate limit:** 1 message per 10 seconds maximum.
3. **No destructive operations:** No room creation, channel changes, firmware
   updates, or admin commands.
4. **No media or encryption:** Only plaintext text messages are sent.
5. **No cross-transport testing:** Each soak test targets a single transport.
6. **Observational only:** Tests report what happened; they do not enforce
   throughput or latency targets.


## 6. Current Soak Evidence Status

| Transport | Tier 1 (dry run) | Tier 2 (manual soak) | Tier 3 (live soak) |
|-----------|-------------------|----------------------|---------------------|
| Matrix | NOT EXECUTED | NOT EXECUTED | NOT EXECUTED |
| Meshtastic | NOT EXECUTED | NOT EXECUTED | NOT EXECUTED |
| MeshCore | NOT EXECUTED | No test class exists | No test class exists |
| LXMF | NOT EXECUTED | No test class exists | No test class exists |
| Harness | NOT EXECUTED | N/A (this IS the harness tier) | N/A |

All entries marked NOT EXECUTED should be resolved as hardware and
credentials become available. See `docs/runbooks/operational-evidence.md` §7
for reasoning and required commands.


## 7. Soak Observations and Contracts

Soak tests exercise the runtime over sustained periods. The following contracts define the guarantees and non-guarantees that soak tests may validate:

| Topic | Contract | Soak-relevant guarantee |
|-------|----------|------------------------|
| Capacity boundedness | Contract 53 | `max_inflight_deliveries` and `max_inflight_replay_events` prevent unbounded memory growth |
| Shutdown drain | Contract 54 | In-flight work is drained (or abandoned) within `shutdown_drain_timeout_seconds` |
| Crash durability | Contract 59 | SQLite events and receipts survive hard crash; in-flight work is lost |
| Cancellation under load | Contract 60 | `CapacityController.stop_accepting()` gates new work; drain polls `snapshot()` |
| Counter resets on restart | Contract 59 | All process-local counters reset to zero on every startup |

Soak tests are **observational** — they do not assert on throughput, latency, or ordering. They report whether the runtime maintained its guarantees over the soak duration.


## 8. Relationship to Other Documents

| Document | Relationship |
|----------|-------------|
| `docs/runbooks/operational-evidence.md` | Where soak evidence is recorded |
| `docs/runbooks/beta-entry-validation.md` | Beta gate (soak is desired, not blocking) |
| `docs/runbooks/developer-environment.md` | Clean-env procedure (prerequisite) |
| `tests/test_soak.py` | Live soak test implementation |
| `tests/test_soak_harness.py` | Dry run soak harness tests |
| `tests/test_soak_config_builder.py` | Soak config builder tests |

# Contract 61 — Operational Evidence Contract

> Contract version: 3
> Last updated: 2026-05-13
> Track: 1 (Transport Maturity Evidence), Track 2 (Live Operational Evidence), Track 7 (Live Evidence Documentation), Track 8 (Deployment Boundary Enforcement), Track 9 (Evidence Consolidation)
> Supersedes: Contract 61 v2 (2026-05-12). Adds evidence lifecycle metadata pattern (§8) — evidence_type, confidence, verified_at, verification_scope, environment. Pilot only; does not alter H/C/S/R tiers or existing fields.
> Status: Active contract. Defines the schema, classification, and recording protocol for all operational evidence.
> References: Contract 32 (Beta Readiness), Contract 37 (Transport Maturity), Contract 39 (Risk Register), Contract 48 (Observability), Contract 59 (Durability), Contract 60 (Cancellation).

This contract defines the authoritative schema for operational evidence in MEDRE. Every document that records, references, or consumes live test evidence must comply with this contract.


## 1. Purpose

Operational evidence is the factual record of how MEDRE adapters behave against real transport endpoints. This contract ensures:

1. Evidence is classified honestly (historical vs current vs simulated vs real-live).
2. Evidence fields are consistent and reproducible.
3. Absence of evidence is explicitly documented, never implied.
4. No document claims reliability, durability, or correctness not backed by recorded evidence.


## 2. Evidence Classification

All operational evidence must be classified into exactly one of the following tiers. The tier determines what claims can be derived from the evidence.

### 2.1 Tier Definitions

| Tier | Label | Meaning | Allowed claims |
|------|-------|---------|----------------|
| **H** | Historical | Recorded during a prior development phase. Not re-confirmed against the current codebase. May be stale if adapter code has changed since recording. | "On date D, behavior X was observed." No claim about current behavior. |
| **C** | Current-tranche | Recorded against the current codebase during the active development tranche. Reproducible by re-running the same command at the same commit. | "At commit H, behavior X is confirmed." |
| **S** | Simulated / Fake-runtime | Recorded using `FakeAdapter`, mock objects, or simulated transport. No real network or hardware involved. | "The adapter's internal logic produces X when given input Y." No claim about real endpoint behavior. |
| **R** | Real-live-runtime | Recorded against a real transport endpoint with real network/hardware. Requires env vars, SDK, and physical or network access. | "Against real endpoint E, behavior X was observed under conditions Y." |

### 2.2 Classification Rules

1. **Every evidence table entry must include a `tier` field** with value `H`, `C`, `S`, or `R`.
2. **Historical evidence** must include the original recording date. It must not be presented as current.
3. **Simulated evidence** must never be used to support claims about real transport behavior.
4. **Real-live-runtime evidence** is the only tier that supports claims about production-adjacent behavior.
5. **NOT EXECUTED** is not a tier. It is an explicit statement that no evidence of any tier exists for that field. Every NOT EXECUTED entry must include a `reason` field.

### 2.3 Tier Transitions

Historical evidence (`H`) may be upgraded to current-tranche (`C`) or real-live-runtime (`R`) by re-running the corresponding test at the current commit. The upgrade must include the new date, commit, and full evidence fields.

Simulated evidence (`S`) may never be upgraded to `R` without a real endpoint run.


## 3. Required Evidence Fields

### 3.1 Universal Fields (all transports, all evidence types)

| Field | Required | Description |
|-------|----------|-------------|
| `tier` | Yes | Evidence tier: H, C, S, or R |
| `test_file` | Yes | Path to the test file that produced this evidence |
| `execution_date` | Yes | ISO date of execution, or `NOT EXECUTED` |
| `executor` | Yes | Who/what ran the test (e.g., "Live agent (automated)", "Operator manual") |
| `medre_commit` | Yes | Git commit hash of the MEDRE codebase under test |
| `python_version` | Yes | Python version used |
| `environment` | Yes | Description of execution environment |
| `total_tests` | Yes | Number of tests run |
| `passed` / `failed` / `skipped` | Yes | Test result counts |
| `duration` | Yes | Wall-clock duration of the test run |
| `caveats_observed` | Yes | Any unexpected behavior, bugs found, or limitations observed |
| `not_executed_reason` | Conditional | Required when `execution_date` is `NOT EXECUTED`. Explains why. |

### 3.2 Matrix-Specific Fields

| Field | Required | Description |
|-------|----------|-------------|
| `homeserver` | Yes | Matrix homeserver URL used |
| `room_id` | Yes | Room ID tested against |
| `room_encryption_status` | Yes | Encrypted or unencrypted |
| `encryption_mode` | Yes | Config encryption_mode value |
| `mindroom_nio_version` | Yes | Installed nio package version |
| `e2ee_deps_version` | Conditional | Required if encryption_mode != `plaintext` |
| `sync_start_latency_ms` | Yes | Time from `start()` to first sync event, or NOT EXECUTED |
| `outbound_send_latency_ms` | Yes | Time for `room_send` to return event_id, or NOT EXECUTED |
| `restart_preserves_state` | Yes | Whether stop→start cycle preserves login state |
| `reconnect_behavior` | Yes | Observed reconnect behavior: degraded→healthy budget, or NOT EXECUTED |
| `diagnostics_snapshot_fields` | Yes | List of fields present in `diagnostics()` output |
| `self_echo_suppression` | Yes | Whether own messages are correctly suppressed |
| `e2ee_store_reuse` | Conditional | Required for E2EE mode. Whether crypto store loads across restarts. |
| `third_party_inbound` | Yes | Whether third-party inbound was tested and confirmed |
| `undecryptable_events` | Conditional | Required for E2EE mode. Count of undecryptable events observed. |
| `repeated_start_stop_cycles` | Yes (v2) | Number of start/stop cycles completed, or NOT EXECUTED |
| `replay_restart_recovery` | Yes (v2) | Observations from restart after offline gap, or NOT EXECUTED |
| `long_running_sync_observation` | Yes (v2) | Soak/sustained sync evidence: duration, reconnect count, health, or NOT EXECUTED |
| `room_state_boundedness` | Yes (v2) | Whether room count stayed within `_MAX_ROOM_STATES` (10,000), or NOT EXECUTED |
| `diagnostics_snapshot_at_start` | Yes (v2) | Full `diagnostics()` dict captured immediately after start |
| `diagnostics_snapshot_at_end` | Yes (v2) | Full `diagnostics()` dict captured at end of observation |
| `runtime_duration_seconds` | Yes (v2) | Wall-clock duration of the observation session, or NOT EXECUTED |

### 3.3 Meshtastic-Specific Fields

| Field | Required | Description |
|-------|----------|-------------|
| `connection_type` | Yes | serial, tcp, or ble |
| `node_hardware` | Yes | Hardware model of the Meshtastic node |
| `firmware_version` | Yes | Node firmware version |
| `node_id` | Yes | Node identifier (e.g., `!25d6e474`) |
| `channel_index` | Yes | Channel index used for testing |
| `channel_name` | Yes | Channel name (e.g., LONG_FAST) |
| `mtjk_version` | Yes | Installed mtjk package version |
| `serial_reconnect` | Yes | Whether serial disconnect/reconnect was tested, and result |
| `outbound_send_one` | Yes | Whether MEDRE `send_one` path was exercised against real hardware |
| `outbound_packet_id_unique` | Yes | Whether outbound packet IDs were unique across sends |
| `inbound_pubsub_callback` | Yes | Whether pubsub callback fired on packet reception |
| `inbound_second_node` | Yes | Whether inbound from a second node was tested |
| `diagnostics_snapshot_fields` | Yes | List of fields present in `diagnostics()` output |
| `destructive_operations` | Yes | Whether any destructive operations were performed (must be "None") |
| `repeated_start_stop_cycles` | Yes (v2) | Number of start/stop cycles completed, or NOT EXECUTED |
| `serial_reconnect_degraded` | Yes (v2) | Observed health transitions during serial reconnect, or NOT EXECUTED |
| `outbound_degraded_behavior` | Yes (v2) | Observed transient retry / permanent failure behavior, or NOT EXECUTED |
| `long_running_runtime_observation` | Yes (v2) | Sustained runtime evidence: duration, connection stability, queue stats, or NOT EXECUTED |
| `hardware_firmware_snapshot` | Yes (v2) | Full hardware/firmware field capture per §2.11 of live procedures |
| `diagnostics_snapshot_at_start` | Yes (v2) | Full `diagnostics()` dict captured immediately after start |
| `diagnostics_snapshot_at_end` | Yes (v2) | Full `diagnostics()` dict captured at end of observation |
| `runtime_duration_seconds` | Yes (v2) | Wall-clock duration of the observation session, or NOT EXECUTED |
| `connection_establishment_time_ms` | Yes (v2) | Time from `start()` to `connected == True`, or NOT EXECUTED |

### 3.4 MeshCore-Specific Fields

| Field | Required | Description |
|-------|----------|-------------|
| `connection_type` | Yes | tcp, serial, or ble |
| `node_hardware` | Yes | Hardware model |
| `sdk_version` | Yes | meshcore SDK version |
| `meshcore_port` | Yes | Port used (default 4000) |
| `adapter_lifecycle` | Yes | start→health→stop result |
| `send_text_result` | Yes | Result of send_text against real node |
| `inbound_callback` | Yes | Whether inbound events were received |

### 3.5 LXMF/Reticulum-Specific Fields

| Field | Required | Description |
|-------|----------|-------------|
| `connection_type` | Yes | reticulum |
| `rns_version` | Yes | RNS package version |
| `lxmf_version` | Yes | LXMF package version |
| `identity_source` | Yes | loaded or generated |
| `delivery_state_progression` | Yes | Observed state transitions (OUTBOUND → DELIVERED) |
| `propagation_node` | Yes | Whether store-and-forward was tested |

### 3.6 Common Runtime Observation Fields (v2)

These fields apply to all transports when recording extended runtime observations (soak, longrun, or sustained operation).

| Field | Required | Description |
|-------|----------|-------------|
| `observation_type` | Yes | `smoke`, `soak`, `longrun`, `sustained`, or `manual` |
| `observation_duration_seconds` | Yes | Wall-clock duration of the observation |
| `health_transitions_observed` | Yes | List of health state transitions observed (e.g., `["healthy", "degraded", "healthy"]`) |
| `reconnect_events` | Yes | Number of reconnect events during observation, or NOT EXECUTED |
| `diagnostics_drift` | Yes | Delta of key diagnostics fields between start and end snapshots |
| `memory_rss_start_bytes` | Conditional | Process RSS at observation start (if `psutil` available) |
| `memory_rss_end_bytes` | Conditional | Process RSS at observation end (if `psutil` available) |
| `boundedness_confirmed` | Yes (v2) | Whether all bounded resources stayed within limits during observation |


### 3.7 Deployment and Boundary Enforcement Evidence Fields (Track 8/9)

These fields record evidence from deployment boundary enforcement testing. They apply to deployment validation, container operation, and runtime boundary checks.

| Field | Required | Description |
|-------|----------|-------------|
| `deployment_helpers_transport_agnostic` | Yes | Whether deployment helpers (runner, config) have no SDK imports or instantiation. Verified by `tests/test_deployment_boundaries.py`, `tests/test_runtime_deployment_boundaries.py`. |
| `cli_no_direct_sdk_instantiation` | Yes | Whether CLI module has no top-level SDK imports and uses dynamic probing only. Verified by boundary tests. |
| `snapshot_export_sdk_free` | Yes | Whether snapshot and export modules have no transport SDK coupling. Verified by boundary tests. |
| `clean_env_no_live_sdks` | Yes | Whether clean-env test files import no transport SDKs. Verified by boundary tests. |
| `soak_fake_only_unless_live_marked` | Yes | Whether fake-only soak files have no SDK imports and live soak files carry `pytest.mark.live`. Verified by boundary tests. |
| `no_live_tests_by_default` | Yes | Whether `pyproject.toml` has `addopts = "-m 'not live'"`. Verified by boundary tests. |
| `runtime_no_direct_adapter_imports` | Yes | Whether runtime helpers (app, builder) import only adapter config dataclasses and base classes, not adapter runtime modules. Verified by `tests/test_runtime_deployment_boundaries.py`. |
| `boundary_tests_pass` | Yes | Whether all boundary enforcement tests pass at the current commit. |
| `boundary_test_date` | Yes | ISO date when boundary tests were last run, or NOT EXECUTED |
| `boundary_test_commit` | Yes | Git commit hash at which boundary tests were run, or NOT EXECUTED |

All fields above are S-tier evidence (deterministic test pass/fail). They do not require live endpoints.


## 4. Evidence Recording Protocol

### 4.1 When to Record

Evidence must be recorded:

1. After every live test execution (pass or fail).
2. After every soak or longrun execution.
3. After every manual operational validation session.
4. When updating transport maturity classification (Contract 37).

### 4.2 Where to Record

| Evidence type | Primary location | Secondary reference |
|---------------|-----------------|---------------------|
| Live smoke test results | `docs/runbooks/operational-evidence.md` §1–§4 | `docs/runbooks/live-operational-evidence.md` |
| Longrun validation results | `docs/runbooks/longrun-validation.md` | `docs/runbooks/operational-evidence.md` |
| Transport maturity | `docs/contracts/37-transport-maturity-classification.md` | Contract 61 §5 |
| Risk register updates | `docs/contracts/39-operational-risk-register.md` | — |

### 4.3 How to Record

1. Fill every required field from §3. Do not leave any field blank.
2. If a field cannot be filled because the test was not executed, set the field to `NOT EXECUTED` and fill `not_executed_reason`.
3. Include the `tier` classification for every entry.
4. Record caveats exactly as observed — do not minimize, reinterpret, or omit.
5. Do not fabricate, extrapolate, or infer evidence from unit test results.

### 4.4 Evidence Freshness

| Tier | Maximum staleness before re-confirmation required |
|------|--------------------------------------------------|
| H | No limit (historical record) |
| C | Must be re-confirmed if adapter code changes |
| S | Must be re-confirmed if adapter interface changes |
| R | Must be re-confirmed if adapter or SDK version changes |

## 5. Transport Evidence Maturity Score

Each transport's evidence maturity is scored based on the evidence collected:

| Score | Meaning |
|-------|---------|
| **0** | No evidence of any tier. All fields NOT EXECUTED. |
| **1** | Simulated (S) evidence only. Unit tests pass. |
| **2** | Historical (H) real-live evidence exists but is stale. |
| **3** | Current-tranche (C) real-live evidence exists for smoke tests only. |
| **4** | Current-tranche (C) real-live evidence exists for smoke + soak + diagnostics. |
| **5** | Current-tranche (C) real-live evidence exists for all required fields. |

### 5.1 Current Scores (as of 2026-05-12, v2)

| Transport | Evidence Score | Justification |
|-----------|---------------|---------------|
| Matrix | 3 | Historical R-tier smoke evidence (2026-05-10, 13/13 passed), historical R-tier E2EE evidence (7/7 passed, 3.73s). Current C-tier deterministic evidence (3237 pass). v2 fields (repeated start/stop cycles, replay/restart/recovery, long-running sync, room-state boundedness, diagnostics snapshots, runtime durations) all NOT EXECUTED. Soak, third-party inbound, sustained diagnostics NOT EXECUTED. |
| Meshtastic | 3 | Historical R-tier smoke evidence (2026-05-10, 10/10 passed, 34.47s). Current C-tier deterministic evidence. v2 fields (repeated start/stop cycles, serial reconnect/degraded, outbound degraded behavior, long-running runtime, hardware/firmware snapshot, connection establishment time) all NOT EXECUTED. Soak, second-node inbound, send_one NOT EXECUTED. |
| MeshCore | 1 | S-tier unit test evidence only. All R-tier fields NOT EXECUTED. No hardware available. |
| LXMF | 1 | S-tier unit test evidence only. All R-tier fields NOT EXECUTED. No Reticulum network available. |


## 6. Prohibited Claims

The following claims are prohibited without explicit R-tier evidence:

1. "Transport X reliably delivers messages." — Requires R-tier soak evidence.
2. "Transport X recovers from network failures." — Requires R-tier reconnect evidence.
3. "Transport X E2EE is secure." — E2EE security is an upstream nio/vodozemac property, not a MEDRE claim.
4. "Transport X is production-ready." — No transport qualifies. See Contract 37 §6.
5. "Messages are delivered in order." — No evidence supports ordering claims.
6. "Delivery latency is bounded by X ms." — Requires R-tier evidence with timing measurements.
7. "Repeated start/stop is safe in production." — Requires R-tier start/stop cycle evidence. (v2)
8. "Boundedness guarantees hold under load." — Requires R-tier sustained operation evidence. (v2)

### 6.1 Honest Claims Allowed

| Claim | Minimum evidence required |
|-------|--------------------------|
| "Adapter passes unit tests against mocks." | S-tier, any commit |
| "Adapter connected to real endpoint on date D." | H-tier or R-tier with date |
| "Encrypted send produced event_id in encrypted room." | R-tier E2EE evidence |
| "Radio send returned MeshPacket with populated id." | R-tier Meshtastic evidence |
| "Crypto store loads across restarts." | R-tier E2EE restart evidence |
| "No transport has live evidence." | NOT EXECUTED entries for all fields |
| "Boundedness logic is implemented with limit X." | S-tier deterministic test + source reference (v2) |
| "Repeated start/stop cycle tests pass against mocks." | S-tier deterministic test (v2) |


## 7. Relationship to Other Documents

| Document | Relationship |
|----------|-------------|
| `docs/runbooks/operational-evidence.md` | Primary evidence recording location. Must comply with this contract's schema. |
| `docs/runbooks/live-operational-evidence.md` | Detailed live procedures for Matrix and Meshtastic. Uses this contract's field definitions. |
| `docs/runbooks/longrun-validation.md` | Longrun evidence capture procedures. Uses this contract's field definitions. |
| `docs/contracts/32-beta-readiness-checklist.md` | §1.3.2 references evidence status. Must align with this contract's classification. |
| `docs/contracts/37-transport-maturity-classification.md` | Maturity tiers use evidence scores from this contract §5. |
| `docs/contracts/39-operational-risk-register.md` | Risk ratings informed by evidence gaps identified via this contract. |
| `docs/contracts/48-runtime-observability-contract.md` | Defines diagnostics fields referenced in `diagnostics_snapshot_fields`. |
| `docs/contracts/59-runtime-durability-contract.md` | Durability claims must be backed by evidence per this contract. |
| `docs/contracts/60-runtime-cancellation-contract.md` | Cancellation claims must be backed by evidence per this contract. |
| `docs/runbooks/soak-testing.md` | Soak harness infrastructure. Produces evidence that must comply with this contract's §3.6. |
| `docs/runbooks/deployment-validation.md` | Deployment boundary validation (Track 8/9). Produces evidence per §3.7. |
| `docs/runbooks/container-operation.md` | Container operation evidence (Track 8/9). Produces evidence per §3.7. |
| `tests/test_deployment_boundaries.py` | Deployment boundary enforcement tests. Results recorded per §3.7. |
| `tests/test_runtime_deployment_boundaries.py` | Runtime-level boundary enforcement tests. Results recorded per §3.7. |


## 8. Evidence Lifecycle Metadata (Pilot)

> **Status:** Pilot. Optional pattern. Does not replace or modify the H/C/S/R tier system (§2). Augments it with verification-freshness tracking so evidence cannot silently overclaim validation currency.

### 8.1 Purpose

Evidence tier (§2) classifies *provenance* — where evidence came from. Lifecycle metadata classifies *verification status and freshness* — when it was last checked and how broadly. Together they prevent stale evidence from being presented as current validation.

### 8.2 Fields

| Field | Values | Description |
|-------|--------|-------------|
| `evidence_type` | `tested` \| `observed` \| `inferred` \| `planned` | Nature of the evidence. `tested`: produced by an automated test suite. `observed`: recorded by manual operator action or live observation. `inferred`: derived from other evidence or reasoning, not directly verified against the system. `planned`: aspirational — no backing evidence exists yet. |
| `confidence` | `high` \| `medium` \| `low` | Qualitative confidence. `high`: directly verified, recent, broad scope. `medium`: verified but scope-limited, aging, or single-point. `low`: indirect, extrapolated, or substantial uncertainty. |
| `verified_at` | ISO date or `never` | Date the evidence was last directly verified. Use `never` for `planned` evidence. |
| `verification_scope` | Free text | What was actually checked. E.g., "unit tests only, no live endpoint"; "live smoke test, single outbound send, no reconnect"; "full soak, 4h, 3 reconnect cycles". |
| `environment` | Free text | Where verified. E.g., "dev laptop, Python 3.12, serial /dev/ttyACM0"; "CI (GitHub Actions)"; "NOT EXECUTED". |

### 8.3 Relationship to H/C/S/R Tiers

Lifecycle metadata is **orthogonal** to tier classification:

| Tier + lifecycle combination | Example |
|------------------------------|---------|
| C-tier, `tested`, `high` | Current-tranche unit tests passing at HEAD |
| H-tier, `tested`, `medium` | Historical live tests that passed but may be stale |
| R-tier, `observed`, `medium` | Live smoke test observed by operator, single scenario |
| S-tier, `tested`, `high` | Mock-based tests confirming internal logic |
| Any tier, `inferred`, `low` | Claim derived from proxy evidence, not directly checked |
| Any tier, `planned`, N/A | No evidence yet; `verified_at: never` |

### 8.4 Usage Pattern

Attach a lifecycle metadata block to any evidence section or table:

```markdown
**Evidence lifecycle** (Contract 61 §8):

| Field | Value |
|-------|-------|
| evidence_type | tested |
| confidence | medium |
| verified_at | 2026-05-10 |
| verification_scope | Live smoke test, single homeserver, no soak |
| environment | Dev laptop, sk.community homeserver, Python 3.12 |
```

When multiple evidence entries have different lifecycle status, use one block per entry or a summary covering the weakest link.


## 9. Changelog

| Date | Version | Change |
|------|---------|--------|
| 2026-05-12 | v1 | Contract 61 created. Formalizes evidence schema from operational-evidence.md. Defines 4 evidence tiers, required fields per transport, evidence maturity scores, prohibited claims. |
| 2026-05-12 | v2 | Tracks 1/2/7/8/9 consolidation. Added: Matrix v2 fields (§3.2: `repeated_start_stop_cycles`, `replay_restart_recovery`, `long_running_sync_observation`, `room_state_boundedness`, `diagnostics_snapshot_at_start/end`, `runtime_duration_seconds`). Meshtastic v2 fields (§3.3: `repeated_start_stop_cycles`, `serial_reconnect_degraded`, `outbound_degraded_behavior`, `long_running_runtime_observation`, `hardware_firmware_snapshot`, `diagnostics_snapshot_at_start/end`, `runtime_duration_seconds`, `connection_establishment_time_ms`). Common runtime observation fields (§3.6). Deployment and boundary enforcement evidence fields (§3.7: Track 8/9). Updated prohibited claims (§6). Updated transport scores with v2 field status (§5.1). Added Contract 60 reference. |
| 2026-05-13 | v3 | Added evidence lifecycle metadata pattern (§8). Pilot-only: defines evidence_type (tested/observed/inferred/planned), confidence, verified_at, verification_scope, environment. Orthogonal to H/C/S/R tier system. No existing fields or tiers modified. |

# Contract 61 — Operational Evidence Contract

> Contract version: 1
> Last updated: 2026-05-12
> Track: 2 (Transport Maturity Evidence), Track 3 (Live Operational Evidence), Track 7 (Live Evidence Documentation)
> Supersedes: Nothing. Formalizes evidence schema referenced by `docs/runbooks/operational-evidence.md`.
> Status: Active contract. Defines the schema, classification, and recording protocol for all operational evidence.
> References: Contract 32 (Beta Readiness), Contract 37 (Transport Maturity), Contract 39 (Risk Register), Contract 48 (Observability), Contract 59 (Durability).

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

### 5.1 Current Scores (as of 2026-05-12)

| Transport | Evidence Score | Justification |
|-----------|---------------|---------------|
| Matrix | 3 | Historical R-tier smoke evidence (2026-05-10), current C-tier deterministic evidence (3237 pass). Soak, third-party inbound, and sustained diagnostics NOT EXECUTED. |
| Meshtastic | 3 | Historical R-tier smoke evidence (2026-05-10), current C-tier deterministic evidence. Soak, second-node inbound, serial reconnect, send_one NOT EXECUTED. |
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

### 6.1 Honest Claims Allowed

| Claim | Minimum evidence required |
|-------|--------------------------|
| "Adapter passes unit tests against mocks." | S-tier, any commit |
| "Adapter connected to real endpoint on date D." | H-tier or R-tier with date |
| "Encrypted send produced event_id in encrypted room." | R-tier E2EE evidence |
| "Radio send returned MeshPacket with populated id." | R-tier Meshtastic evidence |
| "Crypto store loads across restarts." | R-tier E2EE restart evidence |
| "No transport has live evidence." | NOT EXECUTED entries for all fields |


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


## 8. Changelog

| Date | Change |
|------|--------|
| 2026-05-12 | Contract 61 created. Formalizes evidence schema from operational-evidence.md. Defines 4 evidence tiers, required fields per transport, evidence maturity scores, prohibited claims. |

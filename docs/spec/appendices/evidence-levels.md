# Evidence Levels

Evidence provenance tiers for classifying test and validation results.

---

## 1. Tier Definitions

All operational evidence is classified into one of four tiers. The tier
determines what claims can be derived from the evidence.

| Tier  | Label            | Meaning                                                                                                   | Allowed Claims                                                                                       |
| ----- | ---------------- | --------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| **H** | Historical       | Recorded during a prior development phase. Not re-confirmed against the current codebase. May be stale.   | "On date D, behavior X was observed." No claim about current behavior.                               |
| **C** | Current          | Recorded against the current codebase at the current commit. Reproducible by re-running the same command. | "At commit H, behavior X is confirmed."                                                              |
| **S** | Simulated / Fake | Recorded using `FakeAdapter`, mock objects, or simulated transport. No real network or hardware involved. | "The adapter's internal logic produces X when given input Y." No claim about real endpoint behavior. |
| **R** | Real-live        | Recorded against a real transport endpoint with real network/hardware.                                    | "Against real endpoint E, behavior X was observed under conditions Y."                               |

## 2. Classification Rules

1. Every evidence entry must include a `tier` field with value `H`, `C`, `S`, or `R`.
2. Historical evidence must include the original recording date. It must not be presented as current.
3. Simulated evidence must never be used to support claims about real transport behavior.
4. Real-live evidence is the only tier that supports claims about production-adjacent behavior.
5. `NOT EXECUTED` is not a tier. It is an explicit statement that no evidence of any tier exists. Every `NOT EXECUTED` entry must include a `reason` field.

## 3. Tier Transitions

- Historical evidence (`H`) may be upgraded to current (`C`) or real-live (`R`) by re-running the corresponding test at the current commit. The upgrade must include the new date, commit, and full evidence fields.
- Simulated evidence (`S`) may never be upgraded to `R` without a real endpoint run.

## 4. Evidence Sub-Classification

Real-live evidence (`R`) has sub-classifications for different testing environments:

| Sub-classification      | Meaning                                                                                                                                                                                |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Docker SDK-boundary** | Tested against a local Docker container (e.g., Docker Synapse). Validates SDK integration and adapter wiring but not external network behavior, federation, or real-world rate limits. |
| **External live**       | Tested against a real external service (e.g., a real Matrix homeserver on the internet).                                                                                               |
| **Hardware**            | Tested against real physical hardware (e.g., a Meshtastic radio, a MeshCore node).                                                                                                     |

## 5. Required Evidence Fields

### Universal Fields (all transports, all evidence types)

| Field                           | Required | Description                                       |
| ------------------------------- | -------- | ------------------------------------------------- |
| `tier`                          | Yes      | Evidence tier: H, C, S, or R                      |
| `test_file`                     | Yes      | Path to the test file that produced this evidence |
| `execution_date`                | Yes      | ISO date of execution, or `NOT EXECUTED`          |
| `executor`                      | Yes      | Who/what ran the test                             |
| `medre_commit`                  | Yes      | Git commit hash of the MEDRE codebase under test  |
| `python_version`                | Yes      | Python version used                               |
| `environment`                   | Yes      | Description of execution environment              |
| `total_tests`                   | Yes      | Number of tests run                               |
| `passed` / `failed` / `skipped` | Yes      | Test result counts                                |
| `duration`                      | Yes      | Wall-clock duration of the test run               |
| `caveats_observed`              | Yes      | Any unexpected behavior observed                  |

## 6. Capability Status Definitions

| Status                    | Meaning                                                                                                               |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `not started`             | No implementation exists. No tests. No code.                                                                          |
| `designed`                | There is a spec, contract, or design document. No working code yet.                                                   |
| `fake-tested`             | Works with fake/mock adapters. Unit tests pass. No real network traffic. Proves pipeline wiring, not SDK integration. |
| `opt-in live test exists` | A test harness exists, gated by environment variables. Not yet run against a real transport with recorded results.    |
| `live-validated`          | Tested against a real transport with results recorded in the repository.                                              |
| `blocked`                 | A known blocker prevents progress.                                                                                    |

## 7. Policy

No capability is marked `live-validated` unless there is recorded live evidence
in the repository. No `ready` labels. No aspirational statuses. If it has not
been tested and confirmed, it says so.

## 8. Pipeline Behaviour Evidence Coverage

The following pipeline behaviours have S-tier test coverage (fake adapters,
deterministic tests) now covered:

| Behaviour                                              | Test module(s)                                                                 | Tier |
| ------------------------------------------------------ | ------------------------------------------------------------------------------ | ---- |
| Deterministic plan IDs via `stable_delivery_plan_id`   | `test_pipeline_live_replay_parity.py`                                          | S    |
| Live/replay plan parity (plan_id, strategy, caps)      | `test_pipeline_live_replay_parity.py`                                          | S    |
| Live/replay receipt parity (status, failure, evidence) | `test_pipeline_live_replay_parity.py`                                          | S    |
| Repeated replay produces identical plan IDs            | `test_pipeline_live_replay_parity.py`                                          | S    |
| Capability skip does not call adapter send             | `test_pipeline_suppression_no_send.py`                                         | S    |
| Loop suppression does not call adapter send            | `test_pipeline_suppression_no_send.py`                                         | S    |
| Suppressed receipts distinct from failed sends         | `test_pipeline_suppression_no_send.py`                                         | S    |
| Suppressed deliveries do not enter retry queue         | `test_pipeline_suppression_no_send.py`, `test_receipt_lineage_retry_parity.py` | S    |
| Retry reconstruction preserves plan/route/target       | `test_receipt_lineage_retry_parity.py`                                         | S    |
| Retry attempts append evidence, not overwrite          | `test_receipt_lineage_retry_parity.py`                                         | S    |
| Retry exhaustion produces dead_lettered evidence       | `test_receipt_lineage_retry_parity.py`                                         | S    |
| Native refs persisted and used in replay               | `test_pipeline_native_ref_loop_prevention.py`                                  | S    |
| Loop suppression evidence includes all fields          | `test_pipeline_native_ref_loop_prevention.py`                                  | S    |
| Operator diagnostics cover all pipeline stages         | `test_evidence_operator_diagnostics.py`                                        | S    |

None of these behaviours have R-tier (live endpoint) validation.

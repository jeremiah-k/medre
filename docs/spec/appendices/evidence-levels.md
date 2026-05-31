# Evidence Levels

Evidence provenance tiers for classifying test and validation results.

---

## 1. Tier Definitions

All operational evidence is classified into one of six tiers. The tier
determines what claims can be derived from the evidence. The runtime emits
these tier labels as machine-readable strings when tier-tagged evidence is
available.

| Tier label       | Legacy code | Meaning                                                                                                                                                                       | Allowed Claims                                                                                                                |
| ---------------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| **historical**   | H           | Recorded during a prior development phase. Not re-confirmed against the current codebase. May be stale.                                                                       | "On date D, behavior X was observed." No claim about current behavior.                                                        |
| **conformance**  | C           | Recorded against the current codebase at the current commit. Reproducible by re-running the same command.                                                                     | "At commit H, behavior X is confirmed."                                                                                       |
| **synthetic**    | S           | Recorded using `FakeAdapter`, mock objects, or simulated transport. No real network or hardware involved.                                                                     | "The adapter's internal logic produces X when given input Y." No claim about real endpoint behavior.                          |
| **docker**       | (was R)     | Tested against a local Docker container (e.g., Docker Synapse). Validates SDK integration and adapter wiring but not external network, federation, or real-world rate limits. | "SDK integration works in a containerized environment." Docker evidence does not prove external network or hardware behavior. |
| **live_service** | (was R)     | Recorded against a real external transport service over the network (e.g., a real Matrix homeserver, a real Reticulum LXMF router).                                           | "Against real endpoint E, behavior X was observed under conditions Y."                                                        |
| **hardware**     | (was R)     | Recorded against real physical hardware connected via serial, TCP, or BLE (e.g., a Meshtastic radio, a MeshCore node).                                                        | "Against physical device D, behavior X was observed under conditions Y."                                                      |

The legacy codes H, C, S, R are accepted as shorthand in existing evidence tables for historic / conformance / synthetic / runtime contexts. New evidence entries should use the full tier labels.

## 2. Classification Rules

1. Every evidence entry must include a `tier` field with one of the six tier labels (or the corresponding legacy code).
2. Historical evidence must include the original recording date. It must not be presented as current.
3. Synthetic evidence must never be used to support claims about real transport behavior.
4. Docker evidence validates SDK integration and adapter wiring. It does not prove external network behavior, federation, or hardware operation. Docker is not hardware.
5. Only `live_service` and `hardware` tiers support claims about production-adjacent behavior. Both require actual execution against real endpoints.
6. `NOT EXECUTED` (or `not_executed`) is not a tier. It is an explicit statement that no evidence of any tier exists. Every `NOT EXECUTED` entry must include a `reason` field.
7. Storage-only evidence (receipts and outbox rows in SQLite) does not constitute live or hardware validation. Stored data proves what was recorded, not what was validated against a real endpoint.

## 3. Tier Transitions

- Historical evidence (`historical`) may be upgraded to `conformance`, `live_service`, or `hardware` by re-running the corresponding test at the current commit. The upgrade must include the new date, commit, and full evidence fields.
- Synthetic evidence (`synthetic`) may never be upgraded to `docker`, `live_service`, or `hardware` without a real endpoint or device run.
- Docker evidence (`docker`) may not be upgraded to `live_service` or `hardware` without testing against an external service or physical device respectively.

## 4. Storage-Only Evidence Caveat

Evidence stored in the SQLite database (receipts, outbox items, native refs) is a record of what the runtime observed and recorded. Stored evidence alone does not constitute validation of any tier. For example:

- A receipt with `status="sent"` recorded during a synthetic (fake adapter) run proves the pipeline wiring works, not that a real transport accepted the message.
- An outbox row with a `live_service` source value indicates the delivery was attempted against a real service, but the stored record itself is not a substitute for live validation evidence that includes the execution date, commit, environment, and test outcomes.
- Storage contents cannot be used to claim `live_service` or `hardware` tier validation. Those claims require recorded live test evidence with full metadata.

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
| `synthetic-tested`        | Works with fake/mock adapters. Unit tests pass. No real network traffic. Proves pipeline wiring, not SDK integration. |
| `conformance-tested`      | Tested against the current codebase with deterministic fixtures. Reproducible at the same commit.                     |
| `docker-validated`        | Tested against a local Docker container with real SDK dependencies. Not external network or hardware.                 |
| `opt-in live test exists` | A test harness exists, gated by environment variables. Not yet run against a real transport with recorded results.    |
| `live-validated`          | Tested against a real transport (`live_service` or `hardware` tier) with results recorded in the repository.          |
| `blocked`                 | A known blocker prevents progress.                                                                                    |

## 7. Policy

No capability is marked `live-validated` unless there is recorded live evidence
in the repository. No `ready` labels. No aspirational statuses. If it has not
been tested and confirmed, it says so.

## 8. Pipeline Behaviour Evidence Coverage

The following pipeline behaviours have synthetic-tier test coverage (fake
adapters, deterministic tests) now covered:

| Behaviour                                              | Test module(s)                                                                 | Tier      |
| ------------------------------------------------------ | ------------------------------------------------------------------------------ | --------- |
| Deterministic plan IDs via `stable_delivery_plan_id`   | `test_pipeline_live_replay_parity.py`                                          | synthetic |
| Live/replay plan parity (plan_id, strategy, caps)      | `test_pipeline_live_replay_parity.py`                                          | synthetic |
| Live/replay receipt parity (status, failure, evidence) | `test_pipeline_live_replay_parity.py`                                          | synthetic |
| Repeated replay produces identical plan IDs            | `test_pipeline_live_replay_parity.py`                                          | synthetic |
| Capability skip does not call adapter send             | `test_pipeline_suppression_no_send.py`                                         | synthetic |
| Loop suppression does not call adapter send            | `test_pipeline_suppression_no_send.py`                                         | synthetic |
| Suppressed receipts distinct from failed sends         | `test_pipeline_suppression_no_send.py`                                         | synthetic |
| Suppressed deliveries do not enter retry queue         | `test_pipeline_suppression_no_send.py`, `test_receipt_lineage_retry_parity.py` | synthetic |
| Retry reconstruction preserves plan/route/target       | `test_receipt_lineage_retry_parity.py`                                         | synthetic |
| Retry attempts append evidence, not overwrite          | `test_receipt_lineage_retry_parity.py`                                         | synthetic |
| Retry exhaustion produces dead_lettered evidence       | `test_receipt_lineage_retry_parity.py`                                         | synthetic |
| Native refs persisted and used in replay               | `test_pipeline_native_ref_loop_prevention.py`                                  | synthetic |
| Loop suppression evidence includes all fields          | `test_pipeline_native_ref_loop_prevention.py`                                  | synthetic |
| Operator diagnostics cover all pipeline stages         | `test_evidence_operator_diagnostics.py`                                        | synthetic |

None of these behaviours have `live_service` or `hardware` tier validation.

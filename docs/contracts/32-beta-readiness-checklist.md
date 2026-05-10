# Beta Readiness Checklist

> Contract version: 1
> Last updated: 2026-05-09
> Track: 9 (Transport Capability Contracts)
> Supersedes: Nothing. Consolidates beta criteria from contract 28 sections 4, 5, 6.
> Status: Checklist. Defines what must be true before beta release.

This document is the beta readiness checklist for the MEDRE framework. It defines three tiers: must-have before beta, should-have before beta, and explicitly deferred. It records per-transport beta blockers, live-test requirements, docs/runbook requirements, and packaging/dependency requirements.

This is a checklist document. No new features, transports, or runtime redesign are proposed. Items marked "must-have" are blocking. Items marked "should-have" are strongly recommended but not blocking. Items marked "deferred" are explicitly out of scope for beta.


## 1. Must-Have Before Beta

These items are blocking. Beta cannot ship without them.

### 1.1 Cross-Transport Must-Haves

| # | Item | Current Status | Gap |
|---|------|---------------|-----|
| M1 | All four adapters pass unit test suite | Passing | None. |
| M2 | All four adapters have live test harness files | Present (`test_matrix_live.py`, `test_meshtastic_live.py`, `test_meshcore_live.py`, `test_lxmf_live.py`) | Harnesses exist but have not been run against real endpoints in a recorded, reproducible way. |
| M3 | All four adapters have fake mode implementations | Present (`FakeMatrixAdapter`, `FakeMeshtasticAdapter`, `FakeMeshCoreAdapter`, `FakeLxmfAdapter`) | None. |
| M4 | Diagnostics contract documented (contract 29) | Done | None. |
| M5 | Delivery result contract documented (contract 30) | Done | None. |
| M6 | Session boundary contract documented (contract 31) | Done | None. |
| M7 | No secrets leak through any diagnostic path | Verified (contract 27, section 3.3) | None. |
| M8 | No SDK objects leak through any adapter boundary | Verified (contract 27, section 5.3) | None. |
| M9 | Metadata namespacing enforced across all adapters | Verified (contract 27, section 5.1) | None. |
| M10 | Delivery receipt pipeline unit-tested | Tested (`tests/test_delivery.py`) | Not validated against real network. |

### 1.2 Per-Transport Must-Haves

| # | Transport | Item | Current Status | Gap |
|---|-----------|------|---------------|-----|
| M11 | Matrix | Live smoke test run against real homeserver | Harness exists, not recorded | Run and record results in runbook. |
| M12 | Matrix | E2EE live smoke test run | Harness exists, not recorded | Run and record. |
| M13 | Meshtastic | Live smoke test run against real radio | Harness exists, not recorded | Run and record. |
| M14 | MeshCore | Live smoke test run against real radio | Harness exists, not recorded | Run and record. |
| M15 | LXMF | Live smoke test run against real Reticulum | Harness exists, not recorded | Run and record. |
| M16 | Matrix | Inbound reception confirmed from live test | Not confirmed (self-message suppression tested, third-party inbound not confirmed) | Add inbound reception test to live harness. |


## 2. Should-Have Before Beta

These items are strongly recommended. Beta can ship without them, but their absence should be documented as a known limitation.

### 2.1 Cross-Transport Should-Haves

| # | Item | Current Status | Gap |
|---|------|---------------|-----|
| S1 | Reconnect resilience tested under real network failure | No live test exercises reconnect | Add reconnect resilience test for at least one transport (Matrix is the easiest to test). |
| S2 | Sustained throughput smoke test | No load/stress testing exists | Add sustained send test for at least Matrix. |
| S3 | Delivery receipt pipeline validated against real network | Unit-tested only | Run delivery receipt recording during live test. |
| S4 | "Diagnostics not authoritative state" caveat documented in all adapter diagnostics methods | Implied by "read-only snapshot" language, not explicitly called out everywhere | Add explicit docstring language. |
| S5 | All runbooks updated with live test results | Runbooks exist but contain no recorded results | Record results after running live harnesses. |
| S6 | Token/identity secure storage recommendations documented | Not documented | Add recommendations to relevant runbooks. |

### 2.2 Per-Transport Should-Haves

| # | Transport | Item | Current Status | Gap |
|---|-----------|------|---------------|-----|
| S7 | Matrix | Access token handling recommendations documented | Env var only, no rotation/refresh docs | Document in `matrix-alpha-operation.md`. |
| S8 | Meshtastic | `deliver()` returns packet ID instead of `None` | Returns `None` (queued) | If SDK provides packet ID on send, plumb it through. Low priority, the queue worker does produce a result. |
| S9 | MeshCore | BLE connection mode tested | Constructor exists, not tested | Test BLE mode or document as unsupported for beta. |
| S10 | LXMF | Delivery state progression to "delivered" confirmed in live test | State model implemented, progression not confirmed | Add state progression test. |
| S11 | LXMF | Identity file protection documented | 64-byte private key file, no secure storage | Document file permission requirements. |


## 3. Explicitly Deferred

These items are out of scope for beta. They are recorded here to prevent scope creep.

| # | Item | Deferred Because | Notes |
|---|------|-----------------|-------|
| D1 | Reactions, edits, deletes, attachments, media | Feature expansion beyond text messaging | Not required for text-only beta. |
| D2 | New transports (Signal, Discord, IRC, etc.) | New transport integration | Four transports are sufficient for beta. |
| D3 | Admin APIs, webhook servers, HTTP endpoints | Infrastructure beyond adapter layer | Not required for library beta. |
| D4 | Plugin system redesign | Architectural work | Phase 1 plugin API is sufficient. |
| D5 | Bridge policy runtime redesign | Architectural work | Current routing/planning layer is sufficient. |
| D6 | Runtime-level reconnect orchestration | Cross-session coordination | Sessions own their own reconnect. |
| D7 | Background retry scheduler | Phase 1 limitation (see `phase-1-limitations.md`) | Retry is synchronous/receipt-level. |
| D8 | Multi-transport integration test | Nice-to-have, not blocking | No test exercises two transports simultaneously. |
| D9 | Rate limiting or backpressure handling | Infrastructure beyond adapter layer | Adapters send when asked. |
| D10 | Deployment tooling, scaling, operations | Operational concern, not framework concern | Beta is a library, not a service. |
| D11 | Multi-device key verification (Matrix E2EE) | Complex E2EE feature | Basic E2EE is sufficient for beta. |
| D12 | Propagation node operation (LXMF) | Advanced LXMF feature | Direct delivery is sufficient for beta. |
| D13 | Meshtastic inbound DM support | Feature expansion | Classified as deferred. |
| D14 | Receipt deduplication during replay | Phase 1 limitation | See `phase-1-limitations.md`. |
| D15 | CI pipeline running live tests | Infrastructure | Live tests require hardware/secrets. |
| D16 | Cross-signed device trust (Matrix) | Complex E2EE feature | Not required for beta. |


## 4. Per-Transport Beta Blockers

### 4.1 Matrix Beta Blockers

| Blocker | Severity | Resolution |
|---------|----------|------------|
| Live harness not recorded against real homeserver | Must | Run `test_matrix_live.py` and record results. |
| E2EE live harness not recorded | Must | Run `test_matrix_e2ee_live.py` and record results. |
| No confirmed inbound from third party | Must | Add inbound reception test. Send from a second account, verify `publish_inbound()` fires. |
| Access token is plain string in config | Should | Document secure handling recommendations. No code change needed. |
| `mindroom-nio` fork maintenance risk | Should | Pin version, document dependency. |

### 4.2 Meshtastic Beta Blockers

| Blocker | Severity | Resolution |
|---------|----------|------------|
| Live harness not recorded against real radio | Must | Run `test_meshtastic_live.py` and record results. |
| `deliver()` returns `None` (queued, no delivery result to caller) | Should | The queue worker produces a result internally. Document the limitation. Plumb packet ID if SDK provides it on send. |
| No confirmed delivery (ACK not de-duplicated) | Should | Document as inherent to fire-and-forget radio. Not fixable without protocol change. |
| Duplicate-send risk from retry | Should | Document. Consumer handles duplicates. |

### 4.3 MeshCore Beta Blockers

| Blocker | Severity | Resolution |
|---------|----------|------------|
| Live harness not recorded against real radio | Must | Run `test_meshcore_live.py` and record results. |
| No confirmed delivery (ACK not de-duplicated) | Should | Same as Meshtastic. Document as inherent. |
| BLE connection mode untested | Should | Test or document as unsupported for beta. |
| `meshcore` SDK maturity (v2.2.5, small community) | Should | Pin version, document dependency risk. |

### 4.4 LXMF Beta Blockers

| Blocker | Severity | Resolution |
|---------|----------|------------|
| Live harness not recorded against real Reticulum | Must | Run `test_lxmf_live.py` and record results. |
| Delivery state progression not live-validated | Should | Add test verifying state transitions from `"outbound"` through `"delivered"`. |
| Identity file is 64-byte private key with no secure storage | Should | Document file permission requirements. |
| Reticulum network availability dependency | Should | Document requirement for local/network Reticulum instance. |


## 5. Live-Test Requirements

### 5.1 Live Test Execution Protocol

Before beta, each transport's live harness must be run and results recorded in the corresponding runbook. The protocol:

1. Set required environment variables (see section 5.2).
2. Run `pytest tests/test_<transport>_live.py -m live --tb=short`.
3. Record pass/fail counts, any failures with traceback summaries.
4. Update the corresponding runbook with results and date.
5. Commit the updated runbook.

### 5.2 Required Environment Variables

| Transport | Required Env Vars | Optional Env Vars |
|-----------|-------------------|-------------------|
| Matrix | `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, `MATRIX_ACCESS_TOKEN`, `MATRIX_ROOM_ID` | None |
| Matrix E2EE | All Matrix vars + `MATRIX_DEVICE_ID`, `MATRIX_STORE_PATH` | None |
| Meshtastic | `MESHTASTIC_CONNECTION_TYPE`, `MESHTASTIC_HOST` (for TCP) | `MESHTASTIC_CHANNEL_INDEX` |
| MeshCore | `MESHCORE_CONNECTION_TYPE`, `MESHCORE_HOST` (for TCP) | `MESHCORE_CHANNEL_INDEX` |
| LXMF | `LXMF_CONNECTION_TYPE`, `LXMF_IDENTITY_PATH` | `LXMF_DISPLAY_NAME`, `LXMF_DESTINATION_HASH` |

### 5.3 Secret Handling

- **Matrix:** `MATRIX_ACCESS_TOKEN` must be read from environment variable only. Never logged, never committed.
- **LXMF:** `LXMF_IDENTITY_PATH` points to a 64-byte private key file. The file must be protected with restrictive file permissions (`chmod 600`). Tests never log file contents.
- **Meshtastic/MeshCore:** No secrets required. Radio connection parameters only.

### 5.4 What Live Tests Must Prove for Beta

| Capability | Minimum Required Proof |
|-----------|----------------------|
| Adapter lifecycle | Start, health check reports healthy, stop, health check reports stopped. |
| Outbound send | `deliver()` returns without error, `AdapterDeliveryResult` contains non-None `native_message_id` (except Meshtastic which may return `None`). |
| Diagnostics | `diagnostics()` returns non-empty dict, no secrets present. |
| Inbound reception | At least one transport confirms inbound message callback fires from real traffic. |

### 5.5 Live Test Limitations for Beta

Live tests for beta are smoke tests, not reliability tests. They do not prove:
- Sustained throughput under load.
- Reconnect resilience under network failure.
- Multi-hop delivery for radio transports.
- Concurrent delivery to multiple targets.
- Delivery receipt pipeline correctness against real network.


## 6. Docs/Runbook Requirements

### 6.1 Required Runbooks (Existing)

All eight runbooks exist. They must be updated with live test results before beta.

| Runbook | Status | Required Update |
|---------|--------|----------------|
| `matrix-live-smoke.md` | Exists, no recorded results | Add live test results section with date and pass/fail. |
| `matrix-alpha-operation.md` | Exists | Add access token handling recommendations. |
| `meshtastic-live-smoke.md` | Exists, no recorded results | Add live test results section with date and pass/fail. |
| `meshtastic-alpha-operation.md` | Exists | Add fire-and-forget delivery limitation documentation. |
| `meshcore-live-smoke.md` | Exists, no recorded results | Add live test results section with date and pass/fail. |
| `meshcore-alpha-operation.md` | Exists | Add BLE mode status (tested or unsupported for beta). |
| `lxmf-live-smoke.md` | Exists, no recorded results | Add live test results section with date and pass/fail. |
| `lxmf-alpha-operation.md` | Exists | Add identity file protection requirements. |

### 6.2 Required Contract Documents

| Document | Status |
|----------|--------|
| 29-diagnostics-contract.md | Done (this tranche) |
| 30-delivery-result-contract.md | Done (this tranche) |
| 31-session-boundary-contract.md | Done (this tranche) |
| 32-beta-readiness-checklist.md | Done (this tranche) |

### 6.3 Required Doc Updates to Existing Contracts

| Document | Required Update |
|----------|----------------|
| 27-diagnostics-consistency-audit.md | None. Reference from contract 29. |
| 28-alpha-readiness-report.md | None. Superseded by this checklist for beta criteria. |
| `phase-1-limitations.md` | None. Accurately documents current limitations. |


## 7. Packaging/Dependency Requirements

### 7.1 Version Pins

All transport SDK dependencies must be version-pinned before beta:

| Dependency | Transport | Current Pin | Risk |
|------------|-----------|-------------|------|
| `mindroom-nio` | Matrix | Pinned | Fork maintenance risk. Monitor upstream. |
| `mtjk` | Meshtastic | Pinned | Firmware API stability risk. |
| `meshcore` | MeshCore | Pinned (v2.2.5) | Small community, API instability risk. |
| `lxmf` / `rns` | LXMF | Pinned | Low risk, stable APIs. |
| `vodozemac` | Matrix E2EE | Pinned (optional dep) | Required for E2EE, optional dependency. |

### 7.2 Optional Dependencies

| Dependency | Required For | Optional For | Mechanism |
|------------|-------------|-------------|-----------|
| `mindroom-nio[e2e]` | Matrix E2EE | Matrix plaintext | Optional extra. `HAS_E2EE` flag guards import. |
| `vodozemac` | Matrix E2EE crypto | All other modes | Transitive via `mindroom-nio[e2e]`. |

### 7.3 Python Version

Beta should declare a minimum Python version in `pyproject.toml`. The codebase uses `from __future__ import annotations` and union syntax (`str | None`), requiring Python 3.10+.

### 7.4 Packaging Checklist

| # | Item | Status |
|---|------|--------|
| P1 | All SDK dependencies version-pinned | Done |
| P2 | Optional dependencies declared as extras | Done |
| P3 | `pyproject.toml` declares minimum Python version | Needs verification |
| P4 | No SDK objects in public API surface | Verified (contract 27) |
| P5 | All imports guarded by `HAS_*` compat flags | Done |
| P6 | Fake adapters work without any SDK installed | Done |


## 8. Beta Release Criteria Summary

A beta release requires:

1. **All 16 must-have items (M1-M16) satisfied.**
2. **All packaging items (P1-P6) verified.**
3. **All live test results recorded in runbooks.**
4. **All four contract documents (29-32) published.**
5. **No critical regressions in existing unit test suite.**

Should-have items (S1-S11) are strongly recommended. If any remain unsatisfied at beta, they must be documented as known limitations in the release notes.

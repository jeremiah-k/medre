# Beta Readiness Checklist

> Contract version: 4
> Last updated: 2026-05-12
> Track: 8 (README Operator Positioning), Track 9 (Beta Checklist Update), Track 12 (Beta Candidate Closure), M14 Inbound Validation Attempt
> Supersedes: Version 3 (2026-05-12). Consolidates hardware probe findings, fixes stale license claims, aligns with Contract 62 maturity matrix.
> Status: Checklist. Defines what must be true before beta release.
> Head: (current)
>
> **M14 Update (2026-05-12):** Inbound test infrastructure (`test_inbound_message_received`) is complete and validates all M14 requirements: sender attribution, room attribution, canonical event shape (`event_kind`, `source_adapter`, `payload`), `source_native_ref` (Matrix event_id, adapter name), and diagnostics counters (`inbound_published >= 1`). The test waits 30 s for a third-party message with `xfail` on timeout. **Homeserver `matrix.sk.community` confirmed reachable and healthy.** No `MATRIX_*` env vars are set in the current session — all 13 live tests skip cleanly. Previous credential attempt produced `M_UNKNOWN_TOKEN`. Blocker: need fresh `MATRIX_ACCESS_TOKEN` obtained via password-to-token exchange (`curl -X POST https://matrix.sk.community/_matrix/client/v3/login`). A second Matrix account or manual message during the test window is needed to complete third-party inbound validation.

This document is the beta readiness checklist for the MEDRE framework. It defines three tiers: must-have before beta, should-have before beta, and explicitly deferred. It records per-transport beta blockers, live-test requirements, docs/runbook requirements, and packaging/dependency requirements.

This is a checklist document. No new features, transports, or runtime redesign are proposed. Items marked "must-have" are blocking. Items marked "should-have" are strongly recommended but not blocking. Items marked "deferred" are explicitly out of scope for beta.


## 1. Must-Have Before Beta

These items are blocking. Beta cannot ship without them.

### 1.1 Cross-Transport Must-Haves

| # | Item | Current Status | Live Validation Evidence | Gap |
|---|------|---------------|--------------------------|-----|
| M1 | All four adapters pass unit test suite | ✅ Satisfied | Unit run 2026-05-11: `compileall` clean. `pytest -q`: 4596 passed, 25 skipped, 63 deselected. `PYTHONPATH=src pytest -q`: 4596 passed, 25 skipped, 63 deselected. All adapter-specific tests pass. | None. |
| M2 | All four adapters have live test harness files | ✅ Satisfied | Harnesses confirmed: `test_matrix_live.py`, `test_meshtastic_live.py`, `test_meshcore_live.py`, `test_lxmf_live.py`. All use `pytest.mark.live` and `@require_live` skip guards. | Harnesses exist but have not been run against real endpoints. See section 1.3.2 for live run status. |
| M3 | All four adapters have fake mode implementations | ✅ Satisfied | Confirmed: `FakeMatrixAdapter` (`fake_matrix.py`), `FakeMeshtasticAdapter` (`fake_meshtastic.py`), `FakeMeshCoreAdapter` (`fake_meshcore.py`), `FakeLxmfAdapter` (`fake_lxmf.py`). | None. |
| M4 | Diagnostics contract documented (contract 29) | ✅ Satisfied | File exists with substantial content. Verified 2026-05-10. | None. |
| M5 | Delivery result contract documented (contract 30) | ✅ Satisfied | File exists with substantial content. Verified 2026-05-10. | None. |
| M6 | Session boundary contract documented (contract 31) | ✅ Satisfied | File exists with substantial content. Verified 2026-05-10. | None. |
| M7 | No secrets leak through any diagnostic path | ✅ Satisfied | Verified (contract 27, section 3.3). | None. |
| M8 | No SDK objects leak through any adapter boundary | ✅ Satisfied | Verified (contract 27, section 5.3). | None. |
| M9 | Metadata namespacing enforced across all adapters | ✅ Satisfied | Verified (contract 27, section 5.1). | None. |
| M10 | Delivery receipt pipeline unit-tested | ✅ Satisfied | `test_delivery.py`: 65/65 pass (run 2026-05-10). | Not validated against real network. Live validation deferred to should-have S3. |

### 1.2 Per-Transport Must-Haves

| # | Transport | Item | Current Status | Live Validation Evidence | Gap |
|---|-----------|------|---------------|--------------------------|-----|
| M11 | Matrix | Live smoke test run against real homeserver | ✅ Satisfied | `test_matrix_live.py -m live`: 13 passed / 0 failed / 0 skipped against matrix.org homeserver, room `!sRlwdLCwIGBpSzoRsV:matrix.org`. Lifecycle, health, send/receive, diagnostics, session all passed. See `docs/runbooks/operational-evidence.md` §1.1. | None. |
| M12 | Matrix | E2EE live smoke test run | ✅ Satisfied | `test_matrix_e2ee_live.py -m live`: 7 passed / 0 failed / 0 skipped in 3.73s against encrypted room `!rnmyZMhUoraPwZUDPP:matrix.org`. Initial run hit `OlmUnverifiedDeviceError` (2 tests); adapter fix (`ignore_unverified_devices=True`) applied; re-test passed full suite. See `docs/runbooks/operational-evidence.md` §1.3. | None. |
| M13 | Meshtastic | Live smoke test run against real radio | ✅ Satisfied | `test_meshtastic_live.py -m live`: 10 passed / 0 failed / 0 skipped in 34.47s against real device. Serial connection to `/dev/ttyACM0`, LilyGO T-LORA V2.1.1.6 (`!25d6e474`), firmware 2.7.19, channel Test (PRIMARY, LONG_FAST). **Track 2 follow-up (2026-05-12):** Additional CLI-level diagnostics cycle confirmed device stable at 27616s uptime, 2 nodes in mesh, battery "Powered", 4/4 serial connections succeeded. ACK classified UNRELIABLE, delivery classified BEST EFFORT. See `docs/runbooks/operational-evidence.md` §2.0. | None. |
| M14 | Matrix | Inbound reception confirmed from live test | ⛔ Blocked | `test_inbound_message_received` in `test_matrix_live.py` validates all M14 requirements: sender attribution (`source_transport_id` ≠ self), room attribution (`source_channel_id == MATRIX_ROOM_ID`), canonical event shape (`event_kind == "message.created"`, `source_adapter`, `payload["body"]`), `source_native_ref` (Matrix `event_id`, adapter name), and diagnostics (`inbound_published >= 1`). **Homeserver `matrix.sk.community` confirmed reachable (2026-05-12).** All 13 live tests skip cleanly — no `MATRIX_*` env vars set. Previous attempt: `M_UNKNOWN_TOKEN`. Blocker: need fresh `MATRIX_ACCESS_TOKEN` via password-to-token exchange (`curl -X POST https://matrix.sk.community/_matrix/client/v3/login -d '{"type":"m.login.password","user":"forxrelay","password":"<PW>"}'`). Second account or manual message needed during 30 s test window. | Obtain fresh access token. Set env vars. Have second user send message during test window. |


### 1.3 Live Validation Summary (Evidence as of 2026-05-11, head `36d3706`)

> **Review scope note:** Commit `9c93e05` (`chore(tooling): add trunk configuration and linting setup`) is an intentional unrelated/tooling change that adds Trunk linting config (`.trunk/trunk.yaml`, `.trunk/configs/.markdownlint.yaml`). It does not affect runtime behavior, test outcomes, or adapter code. It is excluded from this beta readiness review.

This section records the live validation status of all test harnesses and unit suites.

#### 1.3.1 Unit Test Suite

| Suite | Run Date | Result | Details |
|-------|----------|--------|---------|
| Full unit suite (non-live) | 2026-05-12 | ✅ 4596 passed, 25 skipped, 63 deselected | All tests pass. `python -m compileall -q src tests` clean. `pytest -q`: 4596 passed, 25 skipped, 63 deselected. `PYTHONPATH=src pytest -q`: 4596 passed, 25 skipped, 63 deselected. |
| `test_delivery.py` | 2026-05-10 | ✅ 65/65 pass | Delivery receipt pipeline fully unit-tested. |
| Live tests (56 tests across 5 files) | 2026-05-11 | ✅ Historical: Matrix 13/13 pass (2026-05-10), Meshtastic 10/10 pass (2026-05-10). Current beta-entry tranche: NOT EXECUTED. | Historical live harnesses run against real endpoints on 2026-05-10. Current tranche live execution has not been re-run. MeshCore, LXMF, Matrix inbound remain skipped. |

#### 1.3.2 Live Test Harness Inventory

> **Historical vs. current note:** Live results recorded below for Matrix and Meshtastic are from 2026-05-10 (historical evidence). They have NOT been re-executed for the current beta-entry tranche. Current beta-entry tranche live execution status: **NOT EXECUTED** for all transports.

| Harness | File | Tests | Markers | Skip Guard | Run Status |
|---------|------|-------|---------|------------|------------|
| Matrix live | `test_matrix_live.py` | Lifecycle, health, send, receive, diagnostics, session | `pytest.mark.live`, `@require_live` | `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, `MATRIX_ACCESS_TOKEN`, `MATRIX_ROOM_ID` | Historical (2026-05-10, matrix.org): 13 passed / 0 failed / 0 skipped. Room `!sRlwdLCwIGBpSzoRsV:matrix.org`. Current tranche: NOT EXECUTED. |
| Matrix E2EE | `test_matrix_e2ee_live.py` | E2EE send/receive (olm/megolm) | `pytest.mark.live`, `@require_live` | All Matrix vars + `MATRIX_DEVICE_ID`, `MATRIX_STORE_PATH` | Historical (2026-05-10, matrix.org): 7 passed / 0 failed / 0 skipped (3.73s). Room `!rnmyZMhUoraPwZUDPP:matrix.org`. Pre-fix: 2 tests failed (`OlmUnverifiedDeviceError`). Post-fix: all pass. Current tranche: NOT EXECUTED. |
| Meshtastic live | `test_meshtastic_live.py` | Lifecycle, health, send, diagnostics | `pytest.mark.live`, `@require_live` | `MESHTASTIC_CONNECTION_TYPE`, `MESHTASTIC_HOST` | Historical (2026-05-10, serial `/dev/ttyACM0`, LilyGO T-LORA V2.1.1.6 `!25d6e474`, firmware 2.7.19): 10 passed / 0 failed / 0 skipped (34.47s). Harness bugs fixed: `isConnected` TypeError, `pypubsub` ListenerMismatchError. Track 2 CLI follow-up (2026-05-12): 4/4 serial connections succeeded, device stable at 27616s uptime, 2 nodes in mesh. ACK: UNRELIABLE. Delivery: BEST EFFORT. Current tranche: NOT EXECUTED (mtjk not in project venv). |
| MeshCore live | `test_meshcore_live.py` | Lifecycle, health, send, diagnostics | `pytest.mark.live`, `@require_live` | `MESHCORE_CONNECTION_TYPE`, `MESHCORE_HOST` | Deferred (§1.4 E1). Alpha/experimental — not beta-blocking. |
| LXMF live | `test_lxmf_live.py` | Lifecycle, health, send, receive, diagnostics, delivery state | `pytest.mark.live`, `@require_live` | `LXMF_CONNECTION_TYPE`, `LXMF_IDENTITY_PATH` | Deferred (§1.4 E2). Alpha/experimental — not beta-blocking. |

#### 1.3.3 Must-Have Tally

| Category | Total | ✅ Satisfied | ⛔ Blocked | 🔀 Deferred | ⚠️ Partial |
|----------|-------|-------------|-----------|--------------|------------|
| Cross-transport must-haves (M1–M10) | 10 | 10 | 0 | 0 | 0 |
| Per-transport must-haves (M11–M14) | 4 | 3 | 1 | 0 | 0 |
| Experimental transport (E1–E2) | 2 | 0 | 0 | 2 | 0 |
| Packaging (P1–P6) | 6 | 6 | 0 | 0 | 0 |
| **Total** | **22** | **19** | **1** | **2** | **0** |


### 1.4 Deferred from Beta Scope — Experimental Transport Blockers

MeshCore and LXMF are classified as **alpha/experimental** (Tier 2, per Contract 62 §3.3–§3.4). Their live smoke validation is **deferred from beta scope**. They are NOT beta-blocking. Beta ships with Matrix and Meshtastic as beta-candidate transports.

| # | Transport | Item | Current Status | Live Validation Evidence | Gap |
|---|-----------|------|---------------|--------------------------|-----|
| E1 | MeshCore | Live smoke test run against real radio | ⛔ NOT EXECUTED (deferred) | Harness exists (`test_meshcore_live.py`). Tests lifecycle, health, send, diagnostics. Not run: requires `MESHCORE_CONNECTION_TYPE`, `MESHCORE_HOST`. Hardware probe (2026-05-12): CP2104 `/dev/ttyUSB0` (stable by-id, likely T-Beam) identified but no serial chatter observed. MeshCore firmware source available at `/home/jeremiah/dev`. | Next validation step: run `esptool chip_id` on CP2104 device, flash MeshCore firmware from local source, then run and record live test. Maturity: Alpha (Tier 2) per Contract 62. |
| E2 | LXMF | Live smoke test run against real Reticulum | ⛔ NOT EXECUTED (deferred) | Harness exists (`test_lxmf_live.py`). Tests lifecycle, health, send, receive, diagnostics, delivery state progression. Not run: requires `LXMF_CONNECTION_TYPE`, `LXMF_IDENTITY_PATH`. Local source repos for LXMF and Reticulum available at `/home/jeremiah/dev`. | Next validation step: install Reticulum and LXMF from local source, configure transport, generate identity, run and record live test. Maturity: Alpha (Tier 2) with experimental downgrade risk per Contract 62 §5.4. |

These items will be addressed in follow-up validation work. They are tracked here for visibility, not as beta blockers.


## 2. Should-Have Before Beta

These items are strongly recommended. Beta can ship without them, but their absence should be documented as a known limitation.

### 2.1 Cross-Transport Should-Haves

| # | Item | Current Status | Gap |
|---|------|---------------|-----|
| S1 | Reconnect resilience tested under real network failure | No live test exercises reconnect | Add reconnect resilience test for at least one transport (Matrix is the easiest to test). |
| S2 | Sustained throughput smoke test | No load/stress testing exists | Add sustained send test for at least Matrix. |
| S3 | Delivery receipt pipeline validated against real network | Unit-tested only | Run delivery receipt recording during live test. |
| S4 | "Diagnostics not authoritative state" caveat documented in all adapter diagnostics methods | Implied by "read-only snapshot" language, not explicitly called out everywhere | Add explicit docstring language. |
| S5 | All runbooks updated with live test results | 2/4 runbooks updated (Matrix, Meshtastic). MeshCore and LXMF pending live runs. | Record results after running live harnesses. |
| S6 | Token/identity secure storage recommendations documented | ✅ Satisfied | `docs/runbooks/secure-credentials.md` created. Covers env vars, git-excluded files, logging hygiene, and existing `MatrixConfig.__repr__` redaction. |

### 2.2 Governance Should-Haves

| # | Item | Current Status | Gap |
|---|------|---------------|-----|
| S6a | README license section describes current posture honestly | ✅ SATISFIED | README updated: GPL-3.0-or-later declared, LICENSE file present, governance docs linked. |
| S6b | License governance status reflected in pyproject.toml | ✅ SATISFIED | `license = "GPL-3.0-or-later"`, classifier `License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)`. GOVERNANCE-PENDING comment removed. |
| S6c | License governance risk recorded in risk register | ✅ Satisfied | Contract 39 updated with governance risk entries (G1, G2, G3). See Track 7. |
| S6d | Toolkit/runtime dual-role documented consistently across contracts | ✅ Satisfied | README §Philosophy, contract 38 §8.1, contract 39 §9 all describe the dual-role distinction. |

### 2.3 Per-Transport Should-Haves

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
| D17 | ~~Final license selection~~ | ~~Governance decision pending~~ | ✅ RESOLVED. GPL-3.0-or-later selected (2026-05-12). Contracts 40–45 updated. LICENSE file added. |
| D18 | ~~Top-level LICENSE file~~ | ~~Packaging task, depends on license selection~~ | ✅ RESOLVED. LICENSE file added with standard FSF GPLv3 text. Tracked in contract 45 §F2. |
| D19 | CLA or DCO policy | Not needed until external contributions arrive | Trigger: first external PR. See contract 42 §5.5. |


## 4. Per-Transport Beta Blockers

### 4.1 Matrix Beta Blockers

| Blocker | Severity | Resolution | Status |
|---------|----------|------------|--------|
| Live harness not recorded against real homeserver | Must | Run `test_matrix_live.py` and record results. | ✅ Historical evidence recorded 2026-05-10: 13/13 pass against matrix.org. Current beta-entry tranche: NOT EXECUTED. See `docs/runbooks/operational-evidence.md` §1.1. |
| E2EE live harness not recorded | Must | Run `test_matrix_e2ee_live.py` and record results. | ✅ Historical evidence recorded 2026-05-10: 7/7 pass after `ignore_unverified_devices=True` fix. Current beta-entry tranche: NOT EXECUTED. See `docs/runbooks/operational-evidence.md` §1.3. |
| No confirmed inbound from third party | Must | Add inbound reception test. Send from a second account, verify `publish_inbound()` fires. | ⛔ Not confirmed. |
| Access token is plain string in config | Should | Document secure handling recommendations. No code change needed. | Unresolved. |
| `mindroom-nio` fork maintenance risk | Should | Pin version, document dependency. | Unresolved. |

### 4.2 Meshtastic Beta Blockers

| Blocker | Severity | Resolution | Status |
|---------|----------|------------|--------|
| Live harness not recorded against real radio | Must | Run `test_meshtastic_live.py` and record results. | ✅ Historical evidence recorded 2026-05-10: 10/10 pass in 34.47s, serial `/dev/ttyACM0`, LilyGO T-LORA V2.1.1.6. Track 2 CLI follow-up 2026-05-12: device stable, 4/4 serial connections, ACK UNRELIABLE, delivery BEST EFFORT. Current beta-entry tranche: NOT EXECUTED. See `docs/runbooks/operational-evidence.md` §2.0. |
| `deliver()` returns `None` (queued, no delivery result to caller) | Should | The queue worker produces a result internally. Document the limitation. Plumb packet ID if SDK provides it on send. | Unresolved. |
| No confirmed delivery (ACK not de-duplicated) | Should | **Track 2 classification:** ACK classified UNRELIABLE, delivery classified BEST EFFORT (CLI-level evidence, 2026-05-12). Document as inherent to fire-and-forget radio. Not fixable without protocol change. | Unresolved. Classified. |
| Duplicate-send risk from retry | Should | Document. Consumer handles duplicates. | Unresolved. |

### 4.3 MeshCore Beta Blockers

| Blocker | Severity | Resolution | Status |
|---------|----------|------------|--------|
| Live harness not recorded against real radio | Must (deferred) | Run `test_meshcore_live.py` and record results. | ⛔ Not run. CP2104 `/dev/ttyUSB0` identified (stable by-id, likely T-Beam). No serial chatter. MeshCore firmware source at `/home/jeremiah/dev`. Next: flash firmware, then run live test. Alpha (Tier 2) per Contract 62. Deferred from beta scope — see §1.4 E1. |
| No confirmed delivery (ACK not de-duplicated) | Should | Same as Meshtastic. Document as inherent. | Unresolved. |
| BLE connection mode untested | Should | Test or document as unsupported for beta. | Unresolved. |
| `meshcore` SDK maturity (v2.2.5, small community) | Should | Pin version, document dependency risk. | Unresolved. |

### 4.4 LXMF Beta Blockers

| Blocker | Severity | Resolution | Status |
|---------|----------|------------|--------|
| Live harness not recorded against real Reticulum | Must (deferred) | Run `test_lxmf_live.py` and record results. | ⛔ Not run. Local source repos for LXMF and Reticulum available at `/home/jeremiah/dev`. Next: install from source, configure Reticulum transport, generate identity, run live test. Alpha (Tier 2) with experimental downgrade risk per Contract 62 §5.4. Deferred from beta scope — see §1.4 E2. |
| Delivery state progression not live-validated | Should | Add test verifying state transitions from `"outbound"` through `"delivered"`. | Unresolved. |
| Identity file is 64-byte private key with no secure storage | Should | Document file permission requirements. | Unresolved. |
| Reticulum network availability dependency | Should | Document requirement for local/network Reticulum instance. | Unresolved. |


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
| `matrix-live-smoke.md` | Exists, live results recorded | ✅ Historical: 13/13 pass recorded 2026-05-10. E2EE follow-up 7/7 pass post-fix. Current tranche: NOT EXECUTED. |
| `matrix-alpha-operation.md` | Exists | Add access token handling recommendations. |
| `meshtastic-live-smoke.md` | Exists, live results recorded | ✅ Historical: 10/10 pass recorded 2026-05-10. Serial, LilyGO T-LORA V2.1, firmware 2.7.19. Current tranche: NOT EXECUTED. |
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

All transport SDK dependencies use **minimum-version floor pins** (`>=`).
This is a deliberate strategy, not an oversight. See contract 34, section 7
for the full rationale.

**Summary of the strategy:**

- Transport SDKs: `>=` (minimum validated version, allows newer).
- Core dependency (`msgspec`): `==` (exact pin for deterministic serialization).
- Dev dependencies: `>=` (not shipped).
- No lockfile committed. No upper-bound caps.
- MEDRE is a library, not an application — libraries declare minimums.

| Dependency | Transport | Current Pin | Strategy | Rationale |
|------------|-----------|-------------|----------|-----------|
| `msgspec` | Core | `==0.21.1` | Exact pin | Serialization determinism; msgspec has broken forward compat before. |
| `mindroom-nio` | Matrix | `>=0.25.3` | Floor pin | Fork dependency; project controls versioning. |
| `mtjk` | Meshtastic | `>=2.7.8` | Floor pin | Fork dependency; project controls versioning. |
| `meshcore` | MeshCore | `>=2.3.7` | Floor pin | Third-party SDK; allows security patches. |
| `lxmf` | LXMF | `>=0.9.6` | Floor pin | Third-party SDK; stable API. |
| `vodozemac` | Matrix E2EE | (transitive) | Inherited | Pulled by `mindroom-nio[e2e]`. Not directly pinned. |

### 7.2 Optional Dependencies

| Dependency | Required For | Optional For | Mechanism |
|------------|-------------|-------------|-----------|
| `mindroom-nio[e2e]` | Matrix E2EE | Matrix plaintext | Optional extra. `HAS_E2EE` flag guards import. |
| `vodozemac` | Matrix E2EE crypto | All other modes | Transitive via `mindroom-nio[e2e]`. |

### 7.3 Python Version

Beta should declare a minimum Python version in `pyproject.toml`. The codebase uses `from __future__ import annotations` and union syntax (`str | None`), requiring Python 3.10+.

### 7.4 Packaging Checklist

| # | Item | Status | Live Validation Evidence |
|---|------|--------|--------------------------|
| P1 | All SDK dependencies version-pinned | ✅ Satisfied | `pyproject.toml` uses minimum-version floor pins (`>=`) for transport SDKs and exact pin (`==`) for core `msgspec`. Strategy documented in contract 34, section 7. Not strict pins — intentional library strategy. Verified from local reference repos. |
| P2 | Optional dependencies declared as extras | ✅ Satisfied | `pyproject.toml` declares `dev`, `matrix`, `matrix-e2e`, `meshtastic`, `meshcore`, `lxmf` extras. Verified 2026-05-10. |
| P3 | `pyproject.toml` declares minimum Python version | ✅ Satisfied | `requires-python = ">=3.11"` declared. Verified 2026-05-10. |
| P4 | No SDK objects in public API surface | ✅ Satisfied | Verified (contract 27). |
| P5 | All imports guarded by `HAS_*` compat flags | ✅ Satisfied | Confirmed: `HAS_E2EE` (`matrix/compat.py`), `HAS_MESHTASTIC` (`meshtastic/compat.py`), `HAS_MESHCORE` (`meshcore/compat.py`), `HAS_LXMF` (`lxmf/compat.py`). Verified 2026-05-10. |
| P6 | Fake adapters work without any SDK installed | ✅ Satisfied | All four fake adapters confirmed present. Verified 2026-05-10. |


## 8. Beta Release Criteria Summary

A beta release requires:

1. **All 14 must-have items (M1–M14) satisfied.** → Currently 13/14 satisfied (M1–M10 ✅, M11–M13 ✅, M14 ⛔). MeshCore/LXMF live smoke are deferred from beta scope (§1.4 E1–E2) — not beta-blocking.
2. **All packaging items (P1-P6) verified.** → ✅ All 6/6 satisfied. SDK deps floor-pinned from verified local repos.
3. **All live test results recorded in runbooks.** → Currently 2/4 recorded (Matrix ✅, Meshtastic ✅; MeshCore and LXMF not yet run).
4. **All four contract documents (29-32) published.** → ✅ All published.
5. **No critical regressions in existing unit test suite.** → ✅ 4596 passed, 25 skipped, 63 deselected. Clean suite.
6. **License governance resolved.** → ✅ GPL-3.0-or-later selected. LICENSE file added. pyproject.toml, README, contracts 40–45 updated. Development Status updated to Beta.

Should-have items (S1-S11) are strongly recommended. If any remain unsatisfied at beta, they must be documented as known limitations in the release notes. Governance should-haves (S6a-S6d) are satisfied.


## 8.1 Classification Summary

| Classification | Items | Count |
|---------------|-------|-------|
| **SATISFIED** | M1–M13, P1–P6, S6, S6a–S6d, NB1 | 24 |
| **PARTIAL** | S1–S5, S7–S11, R4 | 10 |
| **BLOCKED** (requires external resource) | M14 (Matrix inbound) | 1 |
| **DEFERRED** (experimental, not beta-blocking) | E1 (MeshCore live smoke), E2 (LXMF live smoke) | 2 |
| **RESOLVED** (was deferred, now done) | D17 (license), D18 (LICENSE file) | 2 |
| **NOT REQUIRED** | D1–D16, D19 | 16 |


## 9. Remaining Beta Blockers (Consolidated)

As of 2026-05-12:

### 9.1 Must-Fix (Blocking Beta)

| # | Blocker | Affects | Resolution |
|---|---------|---------|------------|
| B1 | Live smoke tests not run against real hardware/services | E1–E2 | MeshCore and LXMF live harnesses — deferred from beta scope (§1.4). M11–M13 have historical evidence from 2026-05-10 (Matrix 13/13, Matrix E2EE 7/7, Meshtastic 10/10). Current beta-entry tranche live execution: NOT EXECUTED. |
| B2 | No confirmed inbound reception from third party | M14 | Run Matrix live test with a second account sending to the test room. Verify `publish_inbound()` fires. |
| B3 | ~~SDK dependencies not strictly version-pinned~~ | P1 | ✅ Resolved. Floor pins applied: `mindroom-nio>=0.25.3`, `mtjk>=2.7.8`, `meshcore>=2.3.7`, `lxmf>=0.9.6`. Verified from local reference repos. |

### 9.2 Should-Fix (Not Blocking, But Recommended Before Beta)

| # | Item | Notes |
|---|------|-------|
| R1 | ~~Fix `test_diagnostic_contract.py` regression~~ | ✅ Resolved. `_sanitize_value()` refactor complete; full suite passes. No remaining regression. |
| R2 | ~~Document secure token/identity handling in runbooks~~ | ✅ Resolved. `docs/runbooks/secure-credentials.md` created. |
| R3 | ~~Document fire-and-forget limitations for radio transports~~ | ✅ Resolved. `docs/contracts/36-radio-limitations.md` created. |
| R4 | Test or document BLE mode as unsupported (MeshCore) | BLE constructor exists but untested. |
| R5 | ~~Update public docs for license governance consistency~~ | ✅ RESOLVED (2026-05-12). GPL-3.0-or-later selected. All contracts updated. LICENSE file added. README, pyproject.toml, classifiers all consistent. |


### 9.3 Known Non-Blocking Issues (Tracked for RC, Not Beta)

| # | Issue | Scope | Resolution Target | Notes |
|---|-------|-------|-------------------|-------|
| NB1 | ~~`test_runner.py` coroutine `RuntimeWarning`~~ | RC cleanup | ✅ RESOLVED (2026-05-12). Root cause: `fake_asyncio_run` captured but never closed the coroutine. Fix: added `coro.close()` in mock. Regression test added (`test_main_no_unawaited_coroutine_warning`). |


## 10. Next Recommended Tranche

After this beta validation update, the recommended next tranche should focus on **resolving the remaining must-have (M14) and advancing deferred experimental transports (E1–E2)**:

### Tranche Priority Order

1. ~~**Fix the diagnostic contract regression (R1).**~~ ✅ Done. Resolved in `_sanitize_value()` refactor. Full suite passes.

2. ~~**Run Matrix live smoke tests (B1: M11, M12).**~~ ✅ Historical evidence from 2026-05-10. Matrix plaintext 13/13, Matrix E2EE 7/7 (post-fix `ignore_unverified_devices=True`). Current tranche: NOT EXECUTED.

3. ~~**Run Meshtastic live smoke test (B1: M13).**~~ ✅ Historical evidence from 2026-05-10. 10/10 in 34.47s, serial connection to LilyGO T-LORA V2.1. Current tranche: NOT EXECUTED.

4. ~~**Pin transport SDK dependencies (B3).**~~ ✅ Done. Floor pins from verified local repos: `mindroom-nio>=0.25.3`, `mtjk>=2.7.8`, `meshcore>=2.3.7`, `lxmf>=0.9.6`.

5. **Confirm Matrix inbound reception (B2: M14).** Send a message from a second Matrix account to the test room and verify `publish_inbound()` fires.

6. **Run LXMF live smoke test (deferred E2).** Requires a Reticulum instance. Local source repos available at `/home/jeremiah/dev`. Install Reticulum and LXMF from source, configure transport, run `pytest tests/test_lxmf_live.py -m live --tb=short`, record results in `lxmf-live-smoke.md`. Deferred from beta scope — see §1.4 E2.

7. **Run MeshCore live smoke test (deferred E1).** CP2104 `/dev/ttyUSB0` identified (likely T-Beam). Next: run `esptool chip_id` on CP2104 device, flash MeshCore firmware from local source repo at `/home/jeremiah/dev`, verify serial chatter, then run `pytest tests/test_meshcore_live.py -m live --tb=short`. Record results in `meshcore-live-smoke.md`. Deferred from beta scope — see §1.4 E1.

8. **Update runbooks with results.** After live runs, update remaining `*-live-smoke.md` runbooks with dates and pass/fail.

9. ~~**Document should-fix items (R2–R4).**~~ ✅ R2 and R3 resolved this tranche (secure-credentials runbook, radio limitations contract). R4 (BLE mode) remains open.

**Estimated effort:** Step 5 can be completed in a single session with Matrix credentials available. Steps 6–7 depend on infrastructure availability. Step 8 is documentation-only.


## 11. PC Decision Recommendation

This section provides the project coordinator with an evidence-based
recommendation on beta readiness. It does not prescribe a decision.

### 11.1 Evidence Summary

| Dimension | Status | Evidence |
|-----------|--------|----------|
| Unit test suite | Clean | 4596 passed, 25 skipped, 63 deselected. No regressions. |
| Cross-transport must-haves (M1–M10) | All satisfied | Boundary contracts, diagnostics, delivery results, metadata namespacing, fake adapters all verified. |
| Packaging (P1–P6) | All satisfied | Floor-pinned SDKs, optional extras, import guards, fake adapters work without SDKs. |
| Live evidence — Matrix | Historical (2026-05-10) | Historical: 13/13 plaintext + 7/7 E2EE against matrix.org. Runbooks recorded. Current beta-entry tranche: NOT EXECUTED. |
| Live evidence — Meshtastic | Historical (2026-05-10) | Historical: 10/10 against real LilyGO T-LORA V2.1. Runbook recorded. Current beta-entry tranche: NOT EXECUTED. |
| Live evidence — MeshCore | Not run | Harness exists. CP2104 `/dev/ttyUSB0` identified (likely T-Beam, no serial chatter). Firmware flash pending follow-up. No live evidence. Alpha (Tier 2) per Contract 62. |
| Live evidence — LXMF | Not run | Harness exists. Local source repos available at `/home/jeremiah/dev`. Reticulum live path setup pending follow-up. No live evidence. Alpha (Tier 2) with experimental downgrade risk per Contract 62. |
| Matrix inbound from third party | Not confirmed | Harness includes inbound test but no recorded run from a second account. |
| License governance | Resolved | GPL-3.0-or-later declared. LICENSE file present. Governance docs (contracts 40–45) updated. Reticulum license ambiguity documented (contract 44). |
| Runtime | Exists, early | RuntimeBuilder assembles from TOML, starts adapters in deterministic order, deterministic lifecycle. Not load-tested. |

### 11.2 Remaining Blockers and Deferred Items

One must-have item remains blocked, requiring an external resource:

| Blocker | What it needs | PC action required |
|---------|--------------|-------------------|
| M14: Matrix inbound confirmation | Second Matrix account sending to test room | Run one manual test with two accounts, or decide to document the gap. |

Two experimental transports are deferred from beta scope:

| Item | Transport | Current status | Next step |
|------|-----------|---------------|-----------|
| E1: Live smoke validation | MeshCore | CP2104 `/dev/ttyUSB0` identified (likely T-Beam, no serial chatter). Firmware flash required. | Run follow-up hardware operations: `esptool chip_id`, flash firmware from local source, then run live test. |
| E2: Live smoke validation | LXMF | Reticulum instance not configured. Local source repos available. | Install Reticulum + LXMF from local source, configure transport, run live test. |

### 11.3 Decision Options for the PC

**Option A: Resolve M14 before beta tag; advance E1/E2 as deferred experimental transports.**
- Requires: A second Matrix account.
- Result: 14/14 must-haves satisfied. MeshCore and LXMF shipped with explicit "unit-tested only, no live evidence" labeling. E1/E2 tracked for follow-up validation.
- Risk: MeshCore and LXMF may have fundamental issues undiscoverable without live testing. This is already the honest status quo — the gap is explicitly documented in §1.4.

**Option B: Ship beta with M14 documented; E1/E2 as deferred.**
- Requires: No external resources.
- Result: 13/14 must-haves satisfied. All three gaps explicitly documented (M14 in §1.2, E1/E2 in §1.4).
- Risk: Same as Option A, plus the Matrix inbound gap. The Matrix adapter's send capability is live-validated; only inbound from a third party is unconfirmed. The inbound code path is unit-tested and works against fake adapters.

**Option C: Advance E1/E2 to beta scope (requires hardware/infrastructure).**
- Requires: MeshCore radio and Reticulum instance.
- Result: Transports with live evidence; beta delayed.
- Risk: Hardware/infrastructure may not be available immediately. Beta delivery delayed.

### 11.4 Recommendation

**Option A is recommended.** Rationale:

1. Matrix inbound confirmation (M14) is low-effort and removes the most visible gap for the most mature transport.
2. MeshCore and LXMF live validation depends on hardware/infrastructure that may not be available on the PC's timeline. Their alpha-operational status is already honestly documented in the README, the maturity classification contract (Contract 62), and §1.4 above.
3. The deferred experimental transport model aligns with Contract 38 (RC criteria) §1.2, which explicitly allows transports without live evidence to be "explicitly labeled 'alpha-operational, not live-validated'."

**Accepted direction:** Scoped beta — Matrix and Meshtastic as beta-candidate transports; MeshCore and LXMF deferred as alpha/experimental until real adapter send/receive validation is completed.t bounded.
5. Shipping beta with two transports clearly labeled "alpha-operational" is more honest than delaying beta indefinitely for hardware access.

The PC should make the final call. This recommendation is based on evidence, not urgency.

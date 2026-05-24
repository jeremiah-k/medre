# Transport Maturity Classification

> Contract version: 2
> Last updated: 2026-05-24
> Track: Beta Operational Maturity (Track 2)
> Supersedes: Contract 37 v1 (2026-05-10). Tranche 1 evidence tiering cleanup: live-test evidence now carries H/C/S/R tier labels per Contract 61. Meshtastic live-test evidence reclassified as H-tier (historical, not current-tranche live-validated). Added Meshtastic queue local-acceptance caveats throughout. MeshCore/LXMF live-test axes explicitly marked NOT EXECUTED.
> Status: Classification. Rates each transport along defined maturity axes.
> Evidence schema: `docs/contracts/61-operational-evidence-contract.md` (H/C/S/R tiers, NOT EXECUTED).
> Capability status anchor: `docs/STATUS.md`.

This document classifies MEDRE's four transport adapters (Matrix, Meshtastic,
MeshCore, LXMF) along multiple maturity axes: architectural, unit-test,
live-test, operational, known risks, and production suitability. The
classifications are honest. Transports are not forced into parity. A transport
that is significantly more mature than another will be classified accordingly.

**Evidence tiering (per Contract 61 §2):** All live-test claims in this document
carry an evidence tier label. H-tier means historical (not re-confirmed against
current code). C-tier means current-tranche. R-tier means real-live-runtime.
S-tier means simulated/fake. NOT EXECUTED means no evidence of any tier exists.
Where this document's claims appear to conflict with `docs/STATUS.md`,
STATUS.md is the capability status anchor.

The four classification tiers are:

| Tier  | Label             | Meaning                                                                                                                                                                                                                         |
| ----- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1** | Experimental      | Architectural skeleton exists. Unit tests pass against mocks. No live validation. Not suitable for any real workload.                                                                                                           |
| **2** | Alpha-operational | Core pipeline works (codec, renderer, session, adapter). Unit tests comprehensive. Live smoke test passed against real endpoint. Known limitations documented. Suitable for controlled testing, not production.                 |
| **3** | Beta-candidate    | Alpha-operational plus: live test evidence recorded, failure modes classified, resource containment reviewed, diagnostics contract satisfied, delivery semantics documented. Suitable for beta release with documented caveats. |
| **4** | Constrained-beta  | Beta-candidate plus: operational runbook validated, edge cases exercised, sustained testing done. Suitable for constrained production use within documented boundaries.                                                         |

No transport in MEDRE currently qualifies as production-ready.

## 1. Scope

- Per-transport maturity rating along defined axes.
- Production suitability assessment.
- Known risks and operational caveats.
- Recommended classification tier.

## 2. Non-goals

- Forcing parity across transports.
- Proposing new features or transports.
- Claiming production readiness for any transport.
- Redesigning adapter architecture.

## 3. Maturity Axes

| Axis                       | What it measures                                                                    | Evidence sources                                              |
| -------------------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| **Architectural maturity** | Completeness of codec, renderer, session, adapter, config, errors, compat, metadata | Source file inventory, LOC, interface coverage                |
| **Unit-test maturity**     | Depth and breadth of mock-based test coverage                                       | Test file count, test function count, LOC, edge case coverage |
| **Live-test maturity**     | Evidence of real endpoint validation                                                | Live harness results recorded in `operational-evidence.md`    |
| **Operational maturity**   | Runbooks, failure taxonomy, resource containment, delivery semantics documentation  | Contract inventory                                            |
| **Known risks**            | Documented failure modes, SDK risks, platform caveats                               | Contracts 33, 34, 35, 36                                      |
| **Production suitability** | Honest assessment of readiness for real workloads                                   | All of the above                                              |

## 4. Matrix Transport

### 4.1 Classification: Beta-candidate (Tier 3)

### 4.2 Axis Assessment

| Axis                       | Rating      | Evidence                                                                                                                                                                                                                                     |
| -------------------------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Architectural maturity** | High        | Full adapter suite: codec (217 LOC), renderer (162 LOC), session (694 LOC), adapter (559 LOC), config (144 LOC), errors (41 LOC), compat (44 LOC), metadata (125 LOC), relations (103 LOC). E2EE layer implemented. Most complete transport. |
| **Unit-test maturity**     | High        | 2,903 LOC across 10 test files. `test_matrix_session.py` alone has 102 test functions. Covers codec, renderer, adapter, session, config, metadata, relations, boundaries, lifecycle, pipeline, E2EE live.                                    |
| **Live-test maturity**     | High        | H-tier: 13/13 plaintext pass against matrix.org (2026-05-10, historical). H-tier: 7/7 E2EE pass in encrypted room (2026-05-10, historical). Inbound reception confirmed via self-message suppression. Third-party inbound **not** confirmed (M14 blocked). No current-tranche live re-run (2026-05-12 credential failures). Per STATUS.md, Matrix is `live-validated` on recorded evidence. |
| **Operational maturity**   | High        | Full runbooks (`matrix-live-smoke.md`, `matrix-alpha-operation.md`). Failure modes classified in contract 33. Resource containment reviewed in contract 35. E2EE readiness documented in contract 25. Session boundary in contract 31.       |
| **Known risks**            | Moderate    | Fork dependency (`mindroom-nio`) maintained by project. E2EE install friction (vodozemac/Rust). Access token stored as plain string. No cross-signed device trust. No confirmed third-party inbound.                                         |
| **Production suitability** | Constrained | Suitable for beta with documented caveats. Fork maintenance is an ongoing responsibility. E2EE requires Rust toolchain on some platforms. Token security is operator's responsibility.                                                       |

### 4.3 Operational Caveats

1. **Fork dependency.** `mindroom-nio` is a maintained fork of `matrix-nio`. The project must track upstream for security patches and API changes.
2. **E2EE friction.** `vodozemac` (Rust) is required for E2EE. Binary wheels exist for common platforms but not all. Alpine and ARM may require compilation.
3. **Token security.** Access tokens are plain strings in config. No rotation or refresh mechanism. Operator must secure credentials.
4. **Unverified device workaround.** Encrypted-room sends require `ignore_unverified_devices=True` (contract 25, section 5.2). This is a deliberate trade-off: without cross-signing support, strict verification would block all encrypted sends.
5. **Third-party inbound unconfirmed.** No live test has confirmed inbound reception from a second Matrix account.

## 5. Meshtastic Transport

### 5.1 Classification: Beta-candidate (Tier 3)

### 5.2 Axis Assessment

| Axis                       | Rating        | Evidence                                                                                                                                                                                                                                                 |
| -------------------------- | ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Architectural maturity** | Moderate–High | Full adapter suite: codec (164 LOC), renderer (162 LOC), session (608 LOC), adapter (516 LOC), config (100 LOC), errors (45 LOC), compat (77 LOC), packet_classifier (233 LOC), queue (206 LOC). Queue-based send architecture unique to this transport. |
| **Unit-test maturity**     | High          | 3,429 LOC across 8 test files. `test_meshtastic_adapter.py` has 89 test functions. Covers codec, renderer, adapter, session, config, boundaries, pipeline, packet_classifier, live.                                                                      |
| **Live-test maturity**     | Moderate      | H-tier: 10/10 pass against real radio (2026-05-10, historical, not re-confirmed). Serial `/dev/ttyACM0`, LilyGO T-LORA V2.1, firmware 2.7.19. R-tier CLI-level serial validation (2026-05-12): device discovery, one outbound send, 3 reconnect cycles, ACK classified UNRELIABLE, delivery classified BEST EFFORT. MEDRE adapter live pytest suite NOT EXECUTED at current commit (`mtjk` not in project venv). No sustained throughput or reconnect resilience testing. BLE mode untested. `queued`/`sent` statuses for Meshtastic mean local queue acceptance and local SDK send return, not RF confirmation or remote-node receipt (see Contract 61 §3.8.3). |
| **Operational maturity**   | Moderate      | Full runbooks (`meshtastic-live-smoke.md`, `meshtastic-alpha-operation.md`). Fire-and-forget delivery documented (contract 36). Failure modes classified (contract 33). Resource containment reviewed (contract 35).                                     |
| **Known risks**            | Moderate      | Fork dependency (`mtjk`). Fire-and-forget delivery (no E2E ACK). Duplicate-send risk from retries. Radio-specific behavior varies by firmware. BLE untested. Serial permission requirements.                                                             |
| **Production suitability** | Constrained   | Suitable for beta with documented caveats. Radio transport inherent limitations apply (contract 36). Duplicate handling is consumer's responsibility.                                                                                                    |

### 5.3 Operational Caveats

1. **Fire-and-forget delivery.** `AdapterDeliveryResult.success=True` means "local radio accepted the packet," not "remote node received it." This is inherent to the Meshtastic protocol (contract 36).
2. **Queue local acceptance.** Meshtastic `deliver()` returns `delivery_status="enqueued"` when the adapter-local queue accepts the payload. The subsequent `"sent"` status means the SDK send returned success from the local node. Neither `queued` nor `sent` means RF confirmation, remote-node receipt, or ACK (see Contract 61 §3.8.3, §3.8.6).
3. **Duplicate-send risk.** Session retries transient failures up to 3 times. Consumers must handle duplicates.
4. **Firmware version sensitivity.** The `mtjk` library assumes a specific protobuf schema. Firmware version mismatches may cause deserialization errors.
5. **Serial permissions.** On Linux, user must be in `dialout` group. Docker requires `--device` passthrough.
6. **BLE untested.** BLE connection mode constructor exists but has not been exercised in any live harness.
7. **Fork dependency.** `mtjk` is a maintained fork of upstream Meshtastic Python library.

## 6. MeshCore Transport

### 6.1 Classification: Alpha-operational (Tier 2)

### 6.2 Axis Assessment

| Axis                       | Rating                                          | Evidence                                                                                                                                                                                                                                   |
| -------------------------- | ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Architectural maturity** | Moderate                                        | Full adapter suite: codec (138 LOC), renderer (177 LOC), session (654 LOC), adapter (489 LOC), config (168 LOC), errors (45 LOC), compat (20 LOC), packet_classifier (84 LOC). Async-native (no thread bridging).                          |
| **Unit-test maturity**     | Moderate                                        | 2,321 LOC across 7 test files. `test_meshcore_session.py` has 18 test functions (lowest session test count of all transports). Covers codec, renderer, adapter, session, config, boundaries, pipeline, packet_classifier, live.            |
| **Live-test maturity**     | Low                                             | Harness exists (`test_meshcore_live.py`, 401 LOC) but **NOT EXECUTED** against real hardware. Requires physical radio hardware and environment variables. No live evidence recorded. Hardware probe (2026-05-12): serial path NOT VIABLE (companion heartbeat protocol), BLE preconditions met but connection NOT ATTEMPTED. |
| **Operational maturity**   | Moderate                                        | Runbooks exist (`meshcore-live-smoke.md`, `meshcore-alpha-operation.md`) but without live results. Fire-and-forget delivery documented (contract 36). Failure modes classified (contract 33). Resource containment reviewed (contract 35). |
| **Known risks**            | Moderate–High                                   | Small SDK community (`meshcore_py` v2.2.5–2.3.7). No live validation. BLE untested. Radio hardware required for validation.                                                                                                                |
| **Production suitability** | Not recommended for beta without live evidence. | Unit-tested only. No proof of real hardware operation.                                                                                                                                                                                     |

### 6.3 Operational Caveats

1. **No live evidence.** The live harness has not been run. All validation is mock-based. The transport may work perfectly or may have fundamental issues with real hardware.
2. **SDK maturity.** `meshcore_py` is a small-community project (v2.2.5–2.3.7). API stability is not guaranteed.
3. **Fire-and-forget delivery.** Same inherent limitation as Meshtastic (contract 36).
4. **Hardware dependency.** Validation requires physical MeshCore radio hardware. TCP mode requires a networked node.
5. **BLE untested.** Same as Meshtastic.
6. **Lowest session test count.** 18 test functions for session vs. 102 (Matrix), 41 (LXMF). Session edge cases may be under-tested.

## 7. LXMF Transport

### 7.1 Classification: Alpha-operational (Tier 2)

### 7.2 Axis Assessment

| Axis                       | Rating                                          | Evidence                                                                                                                                                                                                                                                                                                                                       |
| -------------------------- | ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Architectural maturity** | Moderate                                        | Full adapter suite: codec (154 LOC), renderer (190 LOC), session (1,260 LOC — largest session), adapter (428 LOC), config (164 LOC), errors (49 LOC), compat (54 LOC), fields (210 LOC), packet_classifier (167 LOC). Session is the most complex (1,260 LOC), handling LXMRouter lifecycle, identity, announce loop, delivery state tracking. |
| **Unit-test maturity**     | Moderate                                        | 3,381 LOC across 8 test files. `test_lxmf_session.py` has 41 test functions. Covers codec, renderer, adapter, session, config, fields, boundaries, pipeline, packet_classifier, live.                                                                                                                                                          |
| **Live-test maturity**     | Low                                             | Harness exists (`test_lxmf_live.py`, 829 LOC) but **NOT EXECUTED** against real Reticulum instance. Requires Reticulum setup and identity file. No live evidence recorded. Hardware probe (2026-05-12): RNode KISS probe silent at both baud rates. |
| **Operational maturity**   | Moderate                                        | Runbooks exist (`lxmf-live-smoke.md`, `lxmf-alpha-operation.md`) but without live results. Delivery state model implemented but not live-validated. Fire-and-forget documented (contract 36). Failure modes classified (contract 33). Resource containment reviewed (contract 35).                                                             |
| **Known risks**            | Moderate                                        | Reticulum requires ongoing daemon process. Identity file is 64-byte private key (no encryption). Non-standard license (Reticulum License). Delivery state progression not confirmed against real network.                                                                                                                                      |
| **Production suitability** | Not recommended for beta without live evidence. | Unit-tested only. Delivery state model is the most ambitious of all radio transports but unvalidated.                                                                                                                                                                                                                                          |

### 7.3 Operational Caveats

1. **No live evidence.** Same situation as MeshCore. The 829-LOC live harness is the most comprehensive of any transport, but it has not been run.
2. **Delivery state complexity.** The session tracks `OUTBOUND → SENDING → SENT → DELIVERED` state progression, but this has not been confirmed against a real Reticulum network. The model may be correct or may have timing/state assumptions that break in practice.
3. **Identity file security.** 64-byte raw private key. No encryption. No header. Anyone with the file can impersonate the identity.
4. **Reticulum daemon dependency.** Reticulum is designed for long-running daemons. Short-lived processes may not establish stable mesh connectivity.
5. **Non-standard license.** Reticulum License is not OSI-approved. Review for downstream distribution.
6. **Largest session.** At 1,260 LOC, `LxmfSession` is the most complex session in MEDRE. Complexity correlates with risk.

## 8. Classification Summary

| Transport  | Tier | Label             | Key Distinguishing Factor                                                                                           |
| ---------- | ---- | ----------------- | ------------------------------------------------------------------------------------------------------------------- |
| Matrix     | 3    | Beta-candidate    | Live evidence recorded H-tier (20/20 pass). E2EE validated. Most complete adapter. Per STATUS.md: `live-validated`. |
| Meshtastic | 3    | Beta-candidate    | Live evidence recorded H-tier (10/10 pass). CLI-level R-tier hardware evidence (2026-05-12). Per STATUS.md: `opt-in live test exists` for most capabilities; adapter live pytest NOT EXECUTED at current commit. `queued`/`sent` = local acceptance, not RF confirmation. |
| MeshCore   | 2    | Alpha-operational | No live evidence. Unit-tested only. Hardware probe: serial NOT VIABLE, BLE not attempted.                           |
| LXMF       | 2    | Alpha-operational | No live evidence. Most complex session. Delivery state model unvalidated. Hardware probe: RNode serial blocked.     |

**Honest assessment:** Matrix and Meshtastic are ahead of MeshCore and LXMF by one maturity tier. The gap is primarily live-test maturity. All four have comparable architectural and unit-test maturity, but MeshCore and LXMF lack the real-endpoint validation that moves a transport from alpha to beta-candidate. Note: Meshtastic live evidence is H-tier (historical) and CLI-level R-tier; MEDRE adapter live pytest has not been executed at the current commit. Matrix live evidence is H-tier (historical); current-tranche re-run failed on credential issues (not code issues). See `docs/STATUS.md` for the capability status anchor.

## 9. Path to Next Tier

### 9.1 MeshCore → Beta-candidate (Tier 3)

1. Run live harness against real radio hardware. Record results.
2. Increase session test count (currently 18, target 40+).
3. Document BLE mode status (tested or unsupported).

### 9.2 LXMF → Beta-candidate (Tier 3)

1. Run live harness against real Reticulum instance. Record results.
2. Confirm delivery state progression (`OUTBOUND → DELIVERED`) against real network.
3. Document identity file protection requirements.

### 9.3 Matrix → Constrained-beta (Tier 4)

1. Confirm third-party inbound reception (M14).
2. Document access token handling recommendations.
3. Run sustained throughput smoke test.

### 9.4 Meshtastic → Constrained-beta (Tier 4)

1. Test reconnect resilience under real network failure.
2. Run sustained throughput smoke test.
3. Document BLE mode status.

## 10. Cross-Transport Risk Summary

| Risk                             | Matrix                    | Meshtastic       | MeshCore                        | LXMF                     |
| -------------------------------- | ------------------------- | ---------------- | ------------------------------- | ------------------------ |
| Fork dependency                  | Yes (`mindroom-nio`)      | Yes (`mtjk`)     | No                              | No                       |
| Fire-and-forget delivery         | No (Matrix has event IDs) | Yes              | Yes                             | Yes                      |
| Duplicate-send risk              | Low                       | Medium           | Medium                          | Low                      |
| Hardware required for validation | No                        | Yes (serial/TCP) | Yes (serial/TCP)                | Yes (Reticulum)          |
| SDK maturity risk                | Low (mature fork)         | Moderate (fork)  | Moderate–High (small community) | Moderate (single-author) |
| Install friction (base)          | Low                       | Low              | Low                             | Moderate                 |
| Install friction (E2EE)          | High (Rust)               | N/A              | N/A                             | N/A                      |
| Live evidence recorded           | Yes (20/20, H-tier)      | Yes (10/10, H-tier + R-tier CLI) | No                              | No                       |

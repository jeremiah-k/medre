# Contract 62 — Adapter Operational Maturity Matrix

> Contract version: 1
> Last updated: 2026-05-12
> Track: Operational Maturity Consolidation
> Status: Active. Cross-adapter maturity assessment with per-field evidence labels.
> References: Contract 37 (Transport Maturity Classification), Contract 61 (Operational Evidence Contract), Contract 32 (Beta Readiness Checklist)
> Evidence source: `docs/runbooks/operational-evidence.md`, `docs/releases/beta-candidate-notes.md`

This document provides a single cross-adapter view of operational maturity. Every field is either backed by recorded evidence with a tier label (H/C/S/R per Contract 61 §2) or explicitly marked NOT EXECUTED. No field is invented or extrapolated.

## 1. Maturity Tier Definitions

From Contract 37 §3:

| Tier | Label | Meaning |
|------|-------|---------|
| **1** | Experimental | Architectural skeleton exists. Unit tests pass against mocks. No live validation. Not suitable for real workloads. |
| **2** | Alpha-operational | Core pipeline works. Unit tests comprehensive. Live smoke test passed against real endpoint. Known limitations documented. Suitable for controlled testing, not production. |
| **3** | Beta-candidate | Alpha-operational plus: live test evidence recorded, failure modes classified, resource containment reviewed, diagnostics contract satisfied, delivery semantics documented. Suitable for beta release with documented caveats. |
| **4** | Constrained-beta | Beta-candidate plus: operational runbook validated, edge cases exercised, sustained testing done. Suitable for constrained production use. |

No transport qualifies as production-ready.

## 2. Cross-Adapter Maturity Summary

| Field | Matrix | Meshtastic | MeshCore | LXMF |
|-------|--------|------------|----------|------|
| **Maturity label** | Beta-candidate (Tier 3) | Beta-candidate (Tier 3) | Alpha (Tier 2) | Alpha (Tier 2)* |
| **Fake mode** | ✅ S-tier | ✅ S-tier | ✅ S-tier | ✅ S-tier |
| **Unit test suite** | ✅ 2,903 LOC / 10 files (S-tier) | ✅ 3,429 LOC / 8 files (S-tier) | ✅ 2,321 LOC / 7 files (S-tier) | ✅ 3,381 LOC / 8 files (S-tier) |
| **Mocked SDK** | ✅ mindroom-nio (S-tier) | ✅ mtjk (S-tier) | ✅ meshcore_py (S-tier) | ✅ Reticulum/LXMF (S-tier) |
| **Live startup** | ✅ H-tier (2026-05-10) | ✅ H-tier (2026-05-10) | ❌ NOT EXECUTED | ❌ NOT EXECUTED |
| **Live send** | ✅ H-tier (2026-05-10) | ✅ H-tier + R-tier (2026-05-10/12) | ❌ NOT EXECUTED | ❌ NOT EXECUTED |
| **Live receive** | ⚠️ Partial H-tier (self-echo only) | ✅ H-tier (pubsub callback) | ❌ NOT EXECUTED | ❌ NOT EXECUTED |
| **Repeated start/stop** | ✅ H-tier (2026-05-10) | ✅ H-tier (2026-05-10) | ❌ NOT EXECUTED | ❌ NOT EXECUTED |
| **Cleanup/reconnect** | ✅ H-tier (2026-05-10) | ⚠️ R-tier CLI-level only | ❌ NOT EXECUTED | ❌ NOT EXECUTED |
| **Deterministic suite** | ✅ 3237 passed (2026-05-11) | ✅ included in 3237 | ✅ included in 3237 | ✅ included in 3237 |
| **Known blockers** | Third-party inbound (M16) | mtjk not in project venv; BLE untested | No hardware available | No Reticulum instance; delivery state unvalidated |

*\*LXMF is at risk of downgrade to Experimental (Tier 1) if Reticulum live path proves non-viable — see §5.4.*

## 3. Per-Adapter Evidence Detail

### 3.1 Matrix — Beta-candidate (Tier 3)

| Evidence Field | Status | Tier | Source |
|----------------|--------|------|--------|
| **Fake mode** | `FakeMatrixAdapter` confirmed in `fake_matrix.py` | S | Contract 32 M3 |
| **Unit tests** | 2,903 LOC / 10 test files. `test_matrix_session.py`: 102 test functions. | S | Contract 37 §4.2 |
| **Mocked SDK** | mindroom-nio mocked across all test files | S | Source audit |
| **Live startup** | Adapter started, `restore_login` succeeded, sync task running. 13/13 passed (2026-05-10, matrix.org) | H | `operational-evidence.md` §1.1 |
| **Live send (plaintext)** | `room_send` returned event_id starting with `$`. Confirmed in 13/13 run and 12/12 sk.community run (2026-05-12). | H | `operational-evidence.md` §1.1, `beta-candidate-notes.md` |
| **Live send (E2EE)** | Encrypted send succeeds with `ignore_unverified_devices=True`. 7/7 passed (2026-05-10). | H | `operational-evidence.md` §1.3 |
| **Live receive** | Self-echo suppression confirmed live. Third-party inbound: **NOT EXECUTED** (requires second Matrix account, M16). Deterministic unit tests cover full third-party path (8 tests). | H (partial) | `operational-evidence.md` §1.7 |
| **Repeated start/stop** | Stop → start cycle re-establishes sync; second `health_check()` returns `healthy`. | H | `operational-evidence.md` §1.1 |
| **Cleanup/reconnect** | Health stays `degraded` during reconnect, `healthy` after recovery. Budget exhaustion → `failed`. | H | `operational-evidence.md` §1.1 |
| **2026-05-12 live attempt** | sk.community: NOT EXECUTED (access token rejected `M_UNKNOWN_TOKEN`). matrix.org: NOT EXECUTED (password login rejected `M_FORBIDDEN`). | — | `operational-evidence.md` §1.4, §1.4b |
| **Known blockers** | (1) Third-party inbound live validation blocked — no second account. (2) Fork dependency `mindroom-nio`. (3) E2EE requires `ignore_unverified_devices=True` (upstream nio gap, not MEDRE deferral). | — | `operational-evidence.md` §1.7.4 |

**Assessment:** Most complete adapter. Live evidence is H-tier (historical, 2026-05-10). Current-tranche live re-run failed (credential issues, not code issues). All deterministic tests pass. Beta-candidate is justified on H-tier evidence strength plus full deterministic coverage.

### 3.2 Meshtastic — Beta-candidate (Tier 3)

| Evidence Field | Status | Tier | Source |
|----------------|--------|------|--------|
| **Fake mode** | `FakeMeshtasticAdapter` confirmed in `fake_meshtastic.py` | S | Contract 32 M3 |
| **Unit tests** | 3,429 LOC / 8 test files. `test_meshtastic_adapter.py`: 89 test functions. | S | Contract 37 §5.2 |
| **Mocked SDK** | mtjk mocked across all test files | S | Source audit |
| **Live startup (adapter)** | Adapter created client via `_create_client()`, connected and subscribed. `health_check()` returned `healthy`. 10/10 passed (2026-05-10). | H | `operational-evidence.md` §2.1 |
| **Live send (adapter)** | `sendText()` returned `MeshPacket` with populated `id`. `sendData()` returned `MeshPacket`. Unique packet IDs across sends. | H | `operational-evidence.md` §2.1 |
| **Live send (CLI)** | `meshtastic --sendtext` on ch0, exit code 0, no error. Device: LilyGO T-LORA V2.1.1.6, firmware 2.7.19. | R | `operational-evidence.md` §2.0.2 |
| **Live receive** | Pubsub callback fired on packet reception. Received packets have expected shape. Inbound telemetry packet observed. | H | `operational-evidence.md` §2.1 |
| **Second-node inbound** | **NOT EXECUTED** — second node `!ee4a65b1` observed in node DB (confirms radio range overlap only, NOT message delivery). | — | `operational-evidence.md` §2.0.3 |
| **Repeated start/stop** | Adapter start → healthy, stop → clean teardown (2026-05-10). Restart idempotency not explicitly tested for Meshtastic. | H | `operational-evidence.md` §2.1 |
| **Cleanup/reconnect (CLI)** | 4/4 serial connections succeeded across ~7.7 hours device uptime. No serial errors. Device stable (rebootCount unchanged). | R | `operational-evidence.md` §2.0.4 |
| **Cleanup/reconnect (adapter)** | **NOT EXECUTED** — MEDRE adapter session reconnect with exponential backoff not tested against real hardware. | — | `operational-evidence.md` §2.0.7 |
| **ACK reliability** | **UNRELIABLE** — no ACK for broadcast sends observed (2026-05-12). | R | `operational-evidence.md` §2.0.6 |
| **Delivery guarantee** | **BEST EFFORT** — fire-and-forget LoRa broadcast. | R | `operational-evidence.md` §2.0.6 |
| **Known blockers** | (1) `mtjk` not installed in project venv — blocks MEDRE adapter live pytest against real hardware. (2) BLE untested. (3) No second-node inbound. (4) Fire-and-forget delivery inherent. | — | `operational-evidence.md` §2.0.7, §2.3 |

**Assessment:** Adapter-level live evidence is H-tier (2026-05-10, 10/10 passed). CLI-level R-tier evidence (2026-05-12) confirms hardware/firmware/serial connectivity. MEDRE adapter session reconnect and sustained operation remain NOT EXECUTED. Beta-candidate is justified on H-tier adapter evidence + R-tier hardware evidence. Adapter-level evidence is historical — current-tranche re-run requires `mtjk` in project venv.

### 3.3 MeshCore — Alpha (Tier 2)

| Evidence Field | Status | Tier | Source |
|----------------|--------|------|--------|
| **Fake mode** | `FakeMeshCoreAdapter` confirmed in `fake_meshcore.py` | S | Contract 32 M3 |
| **Unit tests** | 2,321 LOC / 7 test files. `test_meshcore_session.py`: 18 test functions (lowest session test count). | S | Contract 37 §6.2 |
| **Mocked SDK** | meshcore_py mocked across all test files | S | Source audit |
| **Live startup** | **NOT EXECUTED** | — | `operational-evidence.md` §3.1 |
| **Live send** | **NOT EXECUTED** | — | `operational-evidence.md` §3.1 |
| **Live receive** | **NOT EXECUTED** | — | `operational-evidence.md` §3.1 |
| **Repeated start/stop** | **NOT EXECUTED** | — | `operational-evidence.md` §3.1 |
| **Cleanup/reconnect** | **NOT EXECUTED** | — | `operational-evidence.md` §3.1 |
| **Known blockers** | (1) No physical MeshCore radio hardware available. (2) SDK is small-community (`meshcore_py` v2.2.5–2.3.7). (3) Lowest session test count (18). (4) TCP mode requires networked node. | — | Contract 37 §6.3 |

**Assessment:** Unit-tested only. No live evidence of any tier. The transport may work perfectly or may have fundamental issues with real hardware. Alpha (Tier 2) reflects complete unit-test coverage with zero live validation. **Cannot promote beyond alpha until hardware-validated live evidence is recorded.** This is a hard gate, not a documentation gap.

### 3.4 LXMF — Alpha (Tier 2, downgrade risk)

| Evidence Field | Status | Tier | Source |
|----------------|--------|------|--------|
| **Fake mode** | `FakeLxmfAdapter` confirmed in `fake_lxmf.py` | S | Contract 32 M3 |
| **Unit tests** | 3,381 LOC / 8 test files. `test_lxmf_session.py`: 41 test functions. Largest session at 1,260 LOC. | S | Contract 37 §7.2 |
| **Mocked SDK** | Reticulum/LXMF mocked across all test files | S | Source audit |
| **Live startup** | **NOT EXECUTED** | — | `operational-evidence.md` §4.1 |
| **Live send** | **NOT EXECUTED** | — | `operational-evidence.md` §4.1 |
| **Live receive** | **NOT EXECUTED** | — | `operational-evidence.md` §4.1 |
| **Repeated start/stop** | **NOT EXECUTED** | — | `operational-evidence.md` §4.1 |
| **Cleanup/reconnect** | **NOT EXECUTED** | — | `operational-evidence.md` §4.1 |
| **Delivery state model** | Implemented (`OUTBOUND → SENDING → SENT → DELIVERED`) but **NOT validated** against real Reticulum network. State progression may have timing/assumption errors. | — | Contract 37 §7.3 |
| **Known blockers** | (1) No Reticulum instance available. (2) Identity file is 64-byte raw private key (no encryption). (3) Non-standard license (Reticulum License, not OSI-approved). (4) Reticulum designed for long-running daemons — short-lived processes may not establish stable connectivity. (5) Largest/most complex session — complexity correlates with risk. | — | Contract 37 §7.3 |

**Assessment:** Unit-tested only. No live evidence. The delivery state model (1,260 LOC session) is the most ambitious of all radio transports but completely unvalidated against real Reticulum infrastructure. Currently classified Alpha (Tier 2) on unit-test strength. **At risk of downgrade to Experimental (Tier 1)** if Reticulum live path validation reveals that the delivery state model has fundamental issues (e.g., timing assumptions, state progression errors, daemon lifecycle mismatches). This is not a vague blocker — it is a specific, documented gap that requires exactly one thing: running the live harness against a real Reticulum instance and observing actual state transitions.

## 4. Evidence Tier Distribution

| Adapter | S-tier fields | H-tier fields | R-tier fields | NOT EXECUTED fields |
|---------|---------------|---------------|---------------|---------------------|
| Matrix | 3 (fake, unit, mock) | 6 (startup, send-plain, send-e2ee, start/stop, reconnect, receive-partial) | 0 | 1 (third-party inbound live) |
| Meshtastic | 3 (fake, unit, mock) | 4 (startup, send-adapter, receive, start/stop) | 2 (send-CLI, reconnect-CLI) | 2 (adapter reconnect, second-node inbound) |
| MeshCore | 3 (fake, unit, mock) | 0 | 0 | 5 (startup, send, receive, start/stop, reconnect) |
| LXMF | 3 (fake, unit, mock) | 0 | 0 | 5 (startup, send, receive, start/stop, reconnect) |

## 5. Live Evidence Consolidation Plan

### 5.1 What exists now (2026-05-12)

- **Matrix:** H-tier evidence from 2026-05-10 (plaintext 13/13, E2EE 7/7, encrypted room 7/7 post-fix). 2026-05-12 live attempt failed (credential issues, not code issues). Deterministic: 3237 passed.
- **Meshtastic:** H-tier adapter evidence from 2026-05-10 (10/10). R-tier CLI evidence from 2026-05-12 (serial validation, 4 reconnects, 1 outbound). Deterministic: 3237 passed.
- **MeshCore:** S-tier only. Deterministic: 3237 passed.
- **LXMF:** S-tier only. Deterministic: 3237 passed.

### 5.2 What Wave 2 must produce

| Adapter | Minimum Wave 2 deliverable | Evidence tier target | Success criteria |
|---------|-----------------------------|---------------------|------------------|
| Matrix | Current-tranche live re-run with valid credentials | C-tier | ≥13/13 plaintext passed. Previously passing tests still pass. |
| Matrix | Third-party inbound live test | C-tier or R-tier | `test_inbound_message_received` passes with second account sending during 30s window. |
| Meshtastic | `mtjk` installed in project venv + adapter live re-run | C-tier | ≥10/10 passed against real hardware. Adapter lifecycle (start/stop/health) confirmed current-tranche. |
| Meshtastic | Adapter-level reconnect against real hardware | C-tier or R-tier | MEDRE session reconnect with exponential backoff observed, not just CLI-level. |
| MeshCore | **Any** live evidence | R-tier (minimum) | ≥1 adapter lifecycle test (start → health → stop) against real hardware. |
| MeshCore | Hardware-validated send/receive | R-tier | Send to real radio, confirm delivery at application level. |
| LXMF | **Any** live evidence | R-tier (minimum) | ≥1 adapter lifecycle test against real Reticulum instance. |
| LXMF | Delivery state model validation | R-tier | Observe actual `OUTBOUND → SENDING → SENT → DELIVERED` transitions on real network. |

### 5.3 What stays as-is (no Wave 2 dependency)

- All S-tier evidence (fake modes, unit tests, mocked SDKs): confirmed and current.
- Matrix H-tier evidence (2026-05-10): valid historical record. Not re-confirmed but not contradicted.
- Meshtastic R-tier CLI evidence (2026-05-12): valid current-tranche hardware evidence.
- Deterministic suite (3237 passed): confirmed 2026-05-11.
- Maturity labels: Matrix and Meshtastic beta-candidate justified on current evidence. MeshCore and LXMF alpha justified on unit-test-only basis.

### 5.4 Maturity promotion gates

| From → To | Required evidence | Current status |
|-----------|-------------------|----------------|
| MeshCore: Alpha → Beta-candidate | ≥1 R-tier live startup + send + receive against real hardware. Session reconnect observed. | ❌ No hardware. Blocked. |
| LXMF: Alpha → Beta-candidate | ≥1 R-tier live startup + send + receive against real Reticulum. Delivery state model confirmed. | ❌ No Reticulum instance. Blocked. |
| LXMF: Alpha → Experimental | If Reticulum live path reveals delivery state model is fundamentally broken (timing assumptions, state errors, daemon lifecycle mismatch). | ⏳ Awaiting live evidence. |
| Meshtastic: H-tier → C-tier | Current-tranche adapter live re-run with `mtjk` in venv. | ⏳ `mtjk` installation needed. |
| Matrix: H-tier → C-tier | Current-tranche live re-run with valid credentials. | ⏳ Credential refresh needed. |

## 6. Not in Scope

- Governance or audit expansion (contract 42, 43, 44, 45).
- License cleanup beyond beta-scope consistency.
- Production readiness claims for any transport.
- New features, transports, or runtime redesign.

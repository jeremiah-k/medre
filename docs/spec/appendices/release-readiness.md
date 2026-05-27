# Release Readiness

Transport maturity and readiness checklist for MEDRE release.

---

## 1. Capability Matrix

| Capability                          | Matrix              | Meshtastic         | MeshCore     | LXMF        |
| ----------------------------------- | --------------------| ------------------ | ------------ | ----------- |
| Config load                         | live-validated       | fake-tested        | fake-tested  | fake-tested |
| Instance-scoped env overrides       | live-validated       | fake-tested        | fake-tested  | fake-tested |
| Env-first adapter creation          | fake-tested          | fake-tested        | fake-tested  | fake-tested |
| Env-driven route creation           | fake-tested          | fake-tested        | fake-tested  | fake-tested |
| Route policy enforcement            | fake-tested          | fake-tested        | fake-tested  | fake-tested |
| Fake lifecycle                      | live-validated       | fake-tested        | fake-tested  | fake-tested |
| Real adapter import safe            | live-validated       | opt-in live exists | designed     | designed    |
| Live start/health                   | live-validated       | opt-in live exists | not started  | not started |
| Outbound delivery                   | live-validated       | opt-in live exists | not started  | not started |
| Inbound decode                      | live-validated       | opt-in live exists | not started  | not started |
| Storage native refs                 | live-validated       | fake-tested        | fake-tested  | fake-tested |
| Evidence bundle                     | live-validated       | fake-tested        | fake-tested  | fake-tested |
| Delivery reliability                | fake-tested          | fake-tested        | designed     | designed    |
| Delivery evidence (unified inspect) | fake-tested          | fake-tested        | not started  | not started |
| Run-session path                    | live-validated       | not started        | not started  | not started |
| Operator runbook                    | live-validated       | opt-in live exists | designed     | designed    |
| Live validation recorded            | live-validated       | not started        | not started  | not started |
| Local delivery outbox               | fake-tested          | fake-tested        | fake-tested  | fake-tested |
| Matrix live adapter (local Synapse) | live-validated (Docker SDK-boundary) | | | |

## 2. Status Definitions

| Status                    | Meaning                                                                                                               |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `not started`             | No implementation exists.                                                                                             |
| `designed`                | Spec/contract exists. No working code.                                                                                |
| `fake-tested`             | Works with fake adapters. Proves pipeline wiring, not SDK integration.                                                |
| `opt-in live test exists` | Test harness exists, gated by env vars. Not yet run against a real transport with recorded results.                   |
| `live-validated`          | Tested against a real transport with results recorded in the repository.                                              |

Docker SDK-boundary evidence validates SDK integration and adapter wiring but
not external network behavior, federation, or real-world rate limits.

## 3. Readiness Checklist

### 3.1 Matrix

- [x] Config load and validation
- [x] Instance-scoped env overrides
- [x] Fake lifecycle
- [x] Real adapter import
- [x] Live start/health (Docker Synapse)
- [x] Outbound delivery (plaintext + E2EE)
- [x] Inbound decode (plaintext + E2EE)
- [x] Storage native refs
- [x] Evidence bundle
- [x] Run-session path
- [x] Operator runbook
- [x] Live validation recorded
- [ ] External live validation (not Docker SDK-boundary)
- [ ] Multi-room concurrent inbound (live)
- [ ] E2EE reactions, edits, media

### 3.2 Meshtastic

- [x] Config load and validation
- [x] Fake lifecycle
- [x] Opt-in live test harness exists
- [x] Operator runbook
- [ ] Live validation against physical radio
- [ ] Inbound processing beyond text messages
- [ ] Delivery reliability with real hardware

### 3.3 MeshCore

- [x] Config load and validation
- [x] Fake lifecycle
- [x] Session lifecycle code source-audited
- [x] Renderer byte-budget (mock-tested)
- [ ] Live validation against physical node
- [ ] BLE hardware validation
- [ ] Delivery reliability with real hardware

### 3.4 LXMF

- [x] Config load and validation
- [x] Fake lifecycle
- [x] Session lifecycle code source-audited
- [ ] Live validation against Reticulum network
- [ ] Multi-hop delivery testing
- [ ] Delivery state progression observation

## 4. Known Blockers

No capabilities are currently `blocked`. The primary gap is hardware access for
live validation of Meshtastic, MeshCore, and LXMF transports.

## 5. Pre-Release Status

MEDRE is pre-first-release. No public API is frozen. Breaking changes to the
specification are permitted when they simplify the model.

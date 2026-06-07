# Release Readiness

Transport maturity and readiness checklist for MEDRE release.

## Pre-Release Status

MEDRE is pre-first-release (package version `0.1.0`, Alpha). No public API is
frozen or committed to. Breaking changes to the specification, CLI surface,
schema shapes, and adapter contracts are permitted when they simplify the model.
Schema version is frozen at `1` during prerelease and carries no compatibility
commitment.

---

## 1. Capability Matrix

| Capability                          | Matrix                               | Meshtastic         | MeshCore    | LXMF        |
| ----------------------------------- | ------------------------------------ | ------------------ | ----------- | ----------- |
| Config load                         | live-validated                       | synthetic-tested        | synthetic-tested | synthetic-tested |
| Instance-scoped env overrides       | live-validated                       | synthetic-tested        | synthetic-tested | synthetic-tested |
| Env-first adapter creation          | synthetic-tested                          | synthetic-tested        | synthetic-tested | synthetic-tested |
| Env-driven route creation           | synthetic-tested                          | synthetic-tested        | synthetic-tested | synthetic-tested |
| Route policy enforcement            | synthetic-tested                          | synthetic-tested        | synthetic-tested | synthetic-tested |
| Fake lifecycle                      | live-validated                       | synthetic-tested        | synthetic-tested | synthetic-tested |
| Real adapter import safe            | live-validated                       | opt-in live exists | designed    | designed    |
| Live start/health                   | live-validated                       | opt-in live exists | not started | not started |
| Outbound delivery                   | live-validated                       | opt-in live exists | not started | not started |
| Inbound decode                      | live-validated                       | opt-in live exists | not started | not started |
| Storage native refs                 | live-validated                       | synthetic-tested        | synthetic-tested | synthetic-tested |
| Evidence bundle                     | live-validated                       | synthetic-tested        | synthetic-tested | synthetic-tested |
| Delivery reliability                | synthetic-tested                          | synthetic-tested        | designed    | designed    |
| Delivery evidence (unified inspect) | synthetic-tested                          | synthetic-tested        | not started | not started |
| Run-session path                    | live-validated                       | not started        | not started | not started |
| Operator runbook                    | live-validated                       | opt-in live exists | designed    | designed    |
| Live validation recorded            | live-validated                       | not started        | not started | not started |
| Local delivery outbox               | synthetic-tested                          | synthetic-tested        | synthetic-tested | synthetic-tested |
| Matrix live adapter (local Synapse) | live-validated (Docker SDK-boundary) |                    |             |             |

## 2. Status Definitions

| Status                    | Meaning                                                                                             |
| ------------------------- | --------------------------------------------------------------------------------------------------- |
| `not started`             | No implementation exists.                                                                           |
| `designed`                | Spec/contract exists. No working code.                                                              |
| `synthetic-tested`             | Works with fake/mock adapters. Unit tests pass. No real network traffic. Proves pipeline wiring, not SDK integration. |
| `conformance-tested`      | Tested against the current codebase with deterministic fixtures. Reproducible at the same commit.                     |
| `docker-validated`        | Tested against a local Docker container with real SDK dependencies. Not external network or hardware.                 |
| `opt-in live test exists` | Test harness exists, gated by env vars. Not yet run against a real transport with recorded results. |
| `live-validated`          | Tested against a real transport (`live_service` or `hardware` tier) with results recorded in the repository.          |

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

## 6. Authority Domains

The following authority domains have established spec pages and developer audit
docs. Authority domain docs define ownership boundaries; they do not imply
release readiness.

| Domain             | Spec page                        | Audit doc                                         |
| ------------------ | -------------------------------- | ------------------------------------------------- |
| Lifecycle          | `delivery-lifecycle.md`          | `docs/dev/lifecycle-authority-audit.md`           |
| Adapter boundary   | `adapter-runtime.md`             | `docs/dev/adapter-reality-audit.md`               |
| Conversation graph | `event-model.md`                 | `docs/dev/conversation-graph-audit.md`            |
| Planning           | `routing-delivery.md`            | `docs/dev/planning-authority-audit.md`            |
| Operator surface   | `diagnostics-evidence.md`        | `docs/dev/operator-surface-audit.md`              |
| Persistence        | `storage.md`                     | `docs/dev/persistence-authority-audit.md`         |
| Runtime execution  | (no spec page; audit is interim) | `docs/dev/runtime-execution-authority-audit.md`   |
| Runtime evidence   | `diagnostics-evidence.md`        | `docs/dev/runtime-evidence-completeness-audit.md` |

## 7. Readiness Gates

Gates that must pass before any release. Each gate records whether it has been
executed at the current commit.

### 7.1 Executed gates (evidence exists)

| Gate                                                             | Evidence class  | Status |
| ---------------------------------------------------------------- | --------------- | ------ |
| Compile / import                                                 | S-tier          | Pass   |
| Fake-adapter pipeline tests                                      | S-tier          | Pass   |
| Schema / example validation                                      | S-tier          | Pass   |
| CLI smoke (`medre smoke --json`)                                 | S-tier          | Pass   |
| Run-session (`medre smoke --run-session`)                        | S-tier          | Pass   |
| Operator read-only workflows (inspect, trace, evidence, recover) | S-tier          | Pass   |
| Adapter boundary tests (parity, lifecycle authority)             | S-tier          | Pass   |
| Doc structure tests (single authority, status vocabulary)        | S-tier          | Pass   |
| Matrix Docker SDK-boundary validation                            | R-tier (docker) | Pass   |

### 7.2 Not-executed gates (no evidence at any tier)

| Gate                                 | Required for           | Status       |
| ------------------------------------ | ---------------------- | ------------ |
| External live Matrix validation      | Non-Docker production  | NOT EXECUTED |
| Multi-room concurrent inbound (live) | Production throughput  | NOT EXECUTED |
| E2EE reactions, edits, media (live)  | Production feature     | NOT EXECUTED |
| Meshtastic live validation (radio)   | Meshtastic release     | NOT EXECUTED |
| MeshCore live validation (node)      | MeshCore release       | NOT EXECUTED |
| LXMF live validation (Reticulum)     | LXMF release           | NOT EXECUTED |
| Hardware byte-budget measurement     | Constrained transports | NOT EXECUTED |

### 7.3 Future release gates (not required for prerelease)

These gates apply to a future stable release and are not blocking the current
prerelease cycle:

- All transports reach live-validated status with recorded evidence
- Schema version bump protocol documented and tested
- Public API compatibility commitment documented
- Migration path for existing storage tested
- Performance benchmarks under sustained load

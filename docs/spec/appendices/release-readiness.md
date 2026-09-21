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

| Capability                          | Matrix             | Meshtastic              | MeshCore                    | LXMF                        |
| ----------------------------------- | ------------------ | ----------------------- | --------------------------- | --------------------------- |
| Config load                         | live-validated     | synthetic-tested        | synthetic-tested            | synthetic-tested            |
| Instance-scoped env overrides       | live-validated     | synthetic-tested        | synthetic-tested            | synthetic-tested            |
| Env-first adapter creation          | synthetic-tested   | synthetic-tested        | synthetic-tested            | synthetic-tested            |
| Env-driven route creation           | synthetic-tested   | synthetic-tested        | synthetic-tested            | synthetic-tested            |
| Route policy enforcement            | synthetic-tested   | synthetic-tested        | synthetic-tested            | synthetic-tested            |
| Fake lifecycle                      | live-validated     | synthetic-tested        | synthetic-tested            | synthetic-tested            |
| Real adapter import safe            | live-validated     | opt-in live exists      | designed                    | designed                    |
| Live start/health                   | live-validated     | opt-in live exists      | not started                 | not started                 |
| Outbound delivery                   | live-validated     | opt-in live exists      | not started                 | not started                 |
| Inbound decode                      | live-validated     | opt-in live exists      | not started                 | not started                 |
| Storage native refs                 | live-validated     | synthetic-tested        | synthetic-tested            | synthetic-tested            |
| Evidence bundle                     | live-validated     | synthetic-tested        | synthetic-tested            | synthetic-tested            |
| Delivery reliability                | synthetic-tested   | synthetic-tested        | designed                    | designed                    |
| Delivery evidence (unified inspect) | synthetic-tested   | synthetic-tested        | not started                 | not started                 |
| Run-session path                    | live-validated     | not started             | not started                 | not started                 |
| Operator runbook                    | live-validated     | opt-in live exists      | designed                    | designed                    |
| Live validation recorded            | live-validated     | not started             | not started                 | not started                 |
| Local delivery outbox               | synthetic-tested   | synthetic-tested        | synthetic-tested            | synthetic-tested            |
| Matrix live adapter (local Synapse) | docker-validated   |                         |                             |                             |
| Installed-SDK contract              | conformance-tested | conformance-tested      | conformance-tested          | conformance-tested          |
| Deterministic local integration     | docker-validated   | docker-validated        | local-integration-validated | local-integration-validated |
| Transport soak harness              | synthetic-tested   | opt-in live test exists | implemented-not-executed    | implemented-not-executed    |

## 2. Status Definitions

| Status                        | Meaning                                                                                                               |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `not started`                 | No implementation exists.                                                                                             |
| `designed`                    | Spec/contract exists. No working code.                                                                                |
| `implemented-not-executed`    | Working harness exists, but no current-tree execution evidence is recorded.                                           |
| `synthetic-tested`            | Works with fake/mock adapters. Unit tests pass. No real network traffic. Proves pipeline wiring, not SDK integration. |
| `conformance-tested`          | Tested against the current codebase with deterministic fixtures. Reproducible for the same Git tree.                  |
| `docker-validated`            | Tested against a local Docker container with real SDK dependencies. Not external network or hardware.                 |
| `local-integration-validated` | Tested against a deterministic local endpoint/process with the pinned real SDK. Not external network or hardware.     |
| `opt-in live test exists`     | Test harness exists, gated by env vars. Not yet run against a real transport with recorded results.                   |
| `live-validated`              | Tested against a real transport (`live_service` or `hardware` tier) with results recorded in the repository.          |

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
- [x] Active stale-sync supervision with fail-closed loop recycle
- [x] Bounded Megolm missing-key recovery admission
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
- [x] Installed-SDK contract matrix
- [x] Docker local-integration boundary (lifecycle/outbound)
- [x] Opt-in hardware lifecycle soak harness

### 3.3 MeshCore

- [x] Config load and validation
- [x] Fake lifecycle
- [x] Session lifecycle code source-audited
- [x] Renderer byte-budget (mock-tested)
- [x] Installed-SDK contract matrix
- [x] Deterministic real-SDK TCP local-integration harness
- [x] Local-integration lifecycle/send soak harness
- [x] Record current-tree execution of local-integration harness
- [ ] Live validation against physical node
- [ ] BLE hardware validation
- [ ] Delivery reliability with real hardware

### 3.4 LXMF

- [x] Config load and validation
- [x] Fake lifecycle
- [x] Session lifecycle code source-audited
- [x] Installed-SDK contract matrix
- [x] Process-isolated real RNS/LXMRouter local-integration harness
- [x] Local-integration repeated lifecycle soak harness
- [x] Record current-tree execution of local-integration harness
- [ ] Live validation against Reticulum network
- [ ] Multi-hop delivery testing
- [ ] Delivery state progression observation

## 5. Authority Domains

Each authority domain has one owning spec page (see
[glossary.md](glossary.md) for the full authority map, including key source
modules and explicit non-responsibilities). Authority domain pages define
ownership boundaries; they do not imply release readiness. Runtime execution
ownership is defined in [architecture.md](../architecture.md) §7.

| Domain             | Owning spec page                             |
| ------------------ | -------------------------------------------- |
| Lifecycle          | `delivery-lifecycle.md`, `state-machines.md` |
| Adapter boundary   | `adapter-runtime.md`                         |
| Conversation graph | `event-model.md`                             |
| Planning           | `routing-delivery.md`                        |
| Operator surface   | `diagnostics-evidence.md`                    |
| Persistence        | `storage.md`                                 |
| Runtime execution  | `architecture.md` §7                         |
| Runtime evidence   | `diagnostics-evidence.md`                    |

## 6. Readiness Gates

Gates that must pass before any release. The rows below are **recorded
historical evidence**: they describe what was executed against an earlier
Git tree, not against the current tree. Every new Git tree requires fresh
CI evidence of its own — historical rows MUST NOT be cited as proof for a
tree they were not executed against. Live and hardware gates that have not
been executed in this cycle remain `NOT EXECUTED` and are not promoted by
any historical pass.

### 6.1 Recorded historical evidence (pre-consolidation tree)

| Gate                                                             | Evidence class | Status (historical) |
| ---------------------------------------------------------------- | -------------- | ------------------- |
| Compile / import                                                 | conformance    | Pass                |
| Fake-adapter pipeline tests                                      | synthetic      | Pass                |
| Schema / example validation                                      | conformance    | Pass                |
| CLI smoke (`medre smoke --json`)                                 | conformance    | Pass                |
| Run-session (`medre smoke --run-session`)                        | synthetic      | Pass                |
| Operator read-only workflows (inspect, trace, evidence, recover) | conformance    | Pass                |
| Adapter boundary tests (parity, lifecycle authority)             | conformance    | Pass                |
| Doc structure tests (single authority, status vocabulary)        | conformance    | Pass                |
| Matrix Docker SDK-boundary validation                            | docker         | Pass                |
| Meshtastic Docker local integration                              | docker         | Pass                |
| MeshCore deterministic real-SDK TCP local integration            | conformance    | Pass                |
| LXMF process-isolated real RNS/LXMRouter local integration       | conformance    | Pass                |

Recorded historical evidence for the pre-consolidation tree:

- Recorded date: 2026-08-21 (historical — **not** evidence for the current tree).
- Tree exercised: `ba2bceffad6810855e1858d202aee6039ac49824`.
- Workflow run: `32529498484` at PR head
  `409762d0cbba1d46aab1fafb60449eca0370ae00`; both
  `transport-local-integration (meshcore)` and
  `transport-local-integration (lxmf)` completed successfully.
- Landed `main` commit `5c8a67e922612f18ab01deefaeeb39c429b4df02` carried
  that identical tree at the time of the run.

> **Note for current tree:** Each row above is a recorded historical pass
> only. A new Git tree must record its own CI execution (conformance,
> synthetic, docker, and any executed local-integration jobs) before the
> same rows can be cited as current-tree evidence. Live and hardware
> validation (Matrix, Meshtastic RF, MeshCore node, LXMF Reticulum) remain
> `NOT EXECUTED` until a fresh run records them.

### 6.2 Not-executed gates (no evidence at any tier)

| Gate                                 | Required for          | Status       |
| ------------------------------------ | --------------------- | ------------ |
| External live Matrix validation      | Non-Docker production | NOT EXECUTED |
| Multi-room concurrent inbound (live) | Production throughput | NOT EXECUTED |
| E2EE reactions, edits, media (live)  | Production feature    | NOT EXECUTED |
| Meshtastic live validation (radio)   | Meshtastic release    | NOT EXECUTED |
| MeshCore live validation (node)      | MeshCore release      | NOT EXECUTED |
| LXMF live validation (Reticulum)     | LXMF release          | NOT EXECUTED |

### 6.3 Future release gates (not required for prerelease)

These gates apply to a future stable release and are not blocking the current
prerelease cycle:

- All transports reach live-validated status with recorded evidence
- Schema version bump protocol documented and tested
- Public API compatibility commitment documented
- Prerelease incompatible-storage rejection/reset path tested
- Performance benchmarks under sustained load

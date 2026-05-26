# MEDRE Contract Taxonomy

> **Generated:** 2026-05-24
>
> **Context:** This index classifies every document under `docs/contracts/` so that readers can distinguish current authority from historical audit, planning criteria, superseded drafts, governance records, and reference material. Files are not moved or renamed; classification is in-place via disposition headers and this table.
>
> **Source-of-truth anchors:** `docs/STATUS.md` (transport capability status) and `docs/contracts/61-operational-evidence-contract.md` (evidence classification). When this index conflicts with those documents, they take precedence.

## Classification Scheme

| Classification       | Meaning                                                                                     |
| -------------------- | ------------------------------------------------------------------------------------------- |
| **Active/Normative** | Current authority. Implementation must conform. These contracts govern runtime behaviour.   |
| **Historical/Audit** | Point-in-time snapshot or audit record. Not current authority. Preserved for traceability.  |
| **Assessment**       | Readiness, gap, risk, or review assessment. Describes state; does not prescribe behaviour.  |
| **Planning**         | Future criteria or roadmap. Not yet in effect. Becomes normative when a gate is cut.        |
| **Superseded**       | Replaced by a newer contract. Preserved for historical reference only.                      |
| **Governance**       | Legal, license, contributor, or distribution governance. Records decisions and constraints. |
| **Design Note**      | Design record for unimplemented work. Not normative.                                        |
| **Reference**        | Supplementary reference material. Supports but does not override normative contracts.       |

## Contract Index

### Active/Normative

| File                                        | Title                                     | Scope                                                          | Current Authority                         |
| ------------------------------------------- | ----------------------------------------- | -------------------------------------------------------------- | ----------------------------------------- |
| `01-canonical-event-contract.md`            | Canonical Event Contract                  | Core event model, schema, immutability, SQL schema             | Primary event model contract              |
| `02-adapter-runtime-contract.md`            | Adapter Runtime Contract                  | Adapter protocol, lifecycle, capabilities, registry            | Primary adapter protocol contract         |
| `03-storage-contract.md`                    | Storage Contract                          | StorageBackend protocol, SQLite schema, receipts               | Primary storage contract                  |
| `04-routing-planning-contract.md`           | Routing and Delivery Planning Contract    | Route model, matching, fanout, delivery plans, receipts, retry | Primary routing/planning contract         |
| `05-plugin-api-contract.md`                 | Plugin API Contract                       | Plugin interface, capabilities, state store (scaffolding only) | Target plugin API; Phase 1 scaffolding    |
| `06-metadata-embedding-contract.md`         | Metadata Embedding Contract               | Metadata namespaces, Matrix/LXMF embedding, privacy modes      | Primary metadata contract                 |
| `07-replay-event-log-contract.md`           | Replay and Event Log Contract             | Event log semantics, replay modes, constraints, archive        | Primary replay contract                   |
| `08-matrix-tranche-1.md`                    | Matrix Adapter Tranche 1                  | Matrix adapter features, boundaries, config, lifecycle         | Current Matrix adapter contract           |
| `09-meshtastic-tranche-1.md`                | Meshtastic Adapter Tranche 1              | Meshtastic adapter features, classifier, queue, config         | Current Meshtastic adapter contract       |
| `14-lxmf-tranche-1.md`                      | LXMF Adapter Tranche 1                    | LXMF adapter features, fields, config                          | Current LXMF adapter contract             |
| `21-adapter-operational-contract.md`        | Adapter Operational Contract              | Operational boundaries, pacing, queueing, health               | Current adapter operational contract      |
| `22-delivery-semantics-matrix.md`           | Delivery Semantics Matrix                 | Cross-transport delivery guarantees, retry, ordering           | Supersedes delivery sections of 65        |
| `23-identity-and-addressing.md`             | Identity and Addressing Across Transports | Cross-transport identity, privacy boundaries                   | Supersedes identity sections of 12 and 65 |
| `25-matrix-e2ee-readiness.md`               | Matrix E2EE Readiness and Design Contract | E2EE text alpha, dependency topology, crypto store             | Current E2EE contract                     |
| `29-diagnostics-contract.md`                | Diagnostics Contract                      | Diagnostics shape, safety guarantees, serialization            | Locked-in diagnostics contract for beta   |
| `30-delivery-result-contract.md`            | Delivery Result Contract                  | AdapterDeliveryResult semantics, per-transport models          | Locked-in delivery result contract        |
| `31-session-boundary-contract.md`           | Session Boundary Contract                 | Session ownership, size risks, extraction boundaries           | Locked-in session boundary contract       |
| `33-failure-taxonomy.md`                    | Failure Taxonomy                          | Delivery failure classification, retry semantics               | Current failure taxonomy                  |
| `36-radio-limitations.md`                   | Radio Transport Limitations Contract      | Fire-and-forget model for Meshtastic/MeshCore/LXMF             | Current radio limitations contract        |
| `46-runtime-storage-and-path-contract.md`   | Runtime Storage and Path Model            | XDG paths, database ownership, MEDRE_HOME                      | Authoritative path model                  |
| `47-runtime-assembly-contract.md`           | Runtime Assembly Contract                 | RuntimeBuilder, multi-adapter, startup ordering                | Authoritative assembly contract           |
| `48-runtime-observability-contract.md`      | Runtime Observability Contract            | Logging, diagnostics, lifecycle events                         | Authoritative observability contract      |
| `49-routing-and-bridge-contract.md`         | Routing and Bridge Contract               | Route definitions, bridge directionality, loop prevention      | Authoritative routing contract            |
| `50-runtime-topology-contract.md`           | Runtime Topology Contract                 | Layer boundaries, subsystem composition                        | Authoritative topology contract           |
| `51-route-attribution-contract.md`          | Route Attribution Contract                | Route attribution fields, receipt attribution, replay          | Authoritative attribution contract        |
| `52-routed-delivery-result-contract.md`     | Routed Delivery Result Contract           | Per-destination results, self-loop, delivery finality          | Authoritative delivery result routing     |
| `53-runtime-resource-control-contract.md`   | Runtime Resource Control Contract         | Capacity controller, delivery concurrency, drain               | Authoritative resource control            |
| `54-runtime-shutdown-contract.md`           | Runtime Shutdown Contract                 | Shutdown phases, drain timeout, persistence                    | Authoritative shutdown contract           |
| `55-runtime-persistence-contract.md`        | Runtime Persistence Contract              | Where state is stored, write timing, persistence mapping       | Authoritative persistence contract        |
| `56-runtime-supervision-contract.md`        | Runtime Supervision Contract              | Health classification, startup outcome, supervision scope      | Authoritative supervision contract        |
| `57-runtime-accounting-contract.md`         | Runtime Accounting Contract               | Delivery accounting, receipt lineage                           | Current accounting contract               |
| `58-packaging-and-install-contract.md`      | Packaging and Install Contract            | Package metadata, optional extras, import boundaries           | Active packaging contract                 |
| `59-runtime-durability-contract.md`         | Runtime Durability Contract               | Durability guarantees, crash recovery, non-guarantees          | Current durability contract               |
| `60-runtime-cancellation-contract.md`       | Runtime Cancellation Contract             | Cancellation semantics, timeout behavior                       | Current cancellation contract             |
| `61-operational-evidence-contract.md`       | Operational Evidence Contract             | Evidence classification (H/C/S/R tiers), validation policy     | Source-of-truth anchor for evidence       |
| `62-adapter-operational-maturity-matrix.md` | Adapter Operational Maturity Matrix       | Per-adapter maturity classification                            | Current maturity matrix                   |
| `63-runtime-snapshot-schema.md`             | Runtime Snapshot Schema                   | Snapshot shape, versioning, field stability                    | Authoritative snapshot schema             |
| `meshtastic-relations.md`                   | Meshtastic Relations and MMRelay Metadata | Relation mapping, renderer config resolution                   | Active Meshtastic relations contract      |
| `phase-1-limitations.md`                    | Phase 1 Limitations                       | Phase 1 scope, taxonomy, constraints                           | Current Phase 1 scope contract            |

### Historical/Audit

| File                                  | Title                                     | Scope                                                                      | Audit Context                                |
| ------------------------------------- | ----------------------------------------- | -------------------------------------------------------------------------- | -------------------------------------------- |
| `10-meshtastic-source-audit.md`       | Meshtastic Source-of-Truth Audit          | mtjk/MMRelay API findings, packet shapes, PortNum enum                     | Pre-production audit of mtjk SDK and MMRelay |
| `13-lxmf-source-audit.md`             | LXMF Source-of-Truth Audit                | LXMF/Reticulum SDK findings, wire format                                   | Pre-production audit of LXMF/RNS SDKs        |
| `26-metadata-normalization-audit.md`  | Metadata Normalization Audit Observations | Cross-transport metadata flow, asymmetries                                 | Audit observations; no code changes proposed |
| `27-diagnostics-consistency-audit.md` | Diagnostics Consistency Audit             | Cross-adapter diagnostics, session patterns, envelopes                     | Audit observations; no code changes proposed |
| `34-dependency-reality-audit.md`      | Dependency Reality Audit                  | Install friction, platform caveats, optional imports                       | Dependency audit across all transports       |
| `41-third-party-license-audit.md`     | Third-Party License Audit                 | License findings for all runtime/optional dependencies                     | License audit record                         |
| `45-spdx-metadata-audit.md`           | SPDX and Metadata Hygiene Audit           | pyproject metadata, SPDX identifiers, LICENSE presence                     | Audit deliverable; metadata changes applied  |
| `64-meshcore-source-audit.md`         | MeshCore Source-of-Truth Audit            | MeshCore SDK API findings, wire format                                     | Pre-production audit of MeshCore SDK         |
| `66-release-hygiene-audit.md`         | Release Hygiene Audit                     | pyproject metadata, README accuracy, stale artifacts (frozen at `7046ecc`) | Historical snapshot; not re-audited          |

### Assessment

| File                                      | Title                                  | Scope                                               | Assessment Context                                              |
| ----------------------------------------- | -------------------------------------- | --------------------------------------------------- | --------------------------------------------------------------- |
| `16-production-connectivity-readiness.md` | Production Connectivity Readiness      | Per-adapter readiness for real network operation    | Readiness assessment; superseded by 28 for cross-transport view |
| `17-event-lineage-debugging.md`           | Event Lineage and Debugging Contract   | Lineage mechanisms, gap audit, operator tooling     | Operational gap audit; no new features introduced               |
| `18-operational-readiness-gaps.md`        | Operational Readiness Gaps             | Per-transport readiness gaps, blockers              | Readiness gap assessment                                        |
| `19-meshcore-connectivity-readiness.md`   | MeshCore Connectivity Readiness        | SDK availability, send semantics, unknown areas     | Readiness assessment from source extraction                     |
| `20-lxmf-connectivity-readiness.md`       | LXMF/Reticulum Connectivity Readiness  | SDK findings, session lifecycle, delivery callbacks | Readiness assessment for LXMF/Reticulum                         |
| `28-alpha-readiness-report.md`            | Cross-Transport Alpha Readiness Report | Per-transport alpha status, beta blockers, risks    | Cross-transport assessment consolidating 16–27                  |
| `35-resource-containment-review.md`       | Resource Containment Review            | Per-session resource ownership, task cleanup, leaks | Resource containment risk review                                |
| `39-operational-risk-register.md`         | Operational Risk Register              | Operational risks by category, severity, mitigation | Risk register for beta stage                                    |

### Planning

| File                                    | Title                           | Scope                                                 | Planning Context                                |
| --------------------------------------- | ------------------------------- | ----------------------------------------------------- | ----------------------------------------------- |
| `24-production-connectivity-roadmap.md` | Production Connectivity Roadmap | Rollout order, per-transport risk, test requirements  | Planning document; no timelines committed       |
| `32-beta-readiness-checklist.md`        | Beta Readiness Checklist        | Must-have/should-have/deferred criteria for beta gate | Planning criteria; project has NOT entered beta |

### Superseded

| File                                     | Title                            | Scope                                                          | Superseded By                                                                                            |
| ---------------------------------------- | -------------------------------- | -------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `12-adapter-platform-identity.md`        | Adapter/Platform Identity Audit  | Identity concepts, adapter_id, platform, overloading           | `23-identity-and-addressing.md` (canonical identity reference)                                           |
| `15-adapter-baseline-consolidation.md`   | Adapter Baseline Consolidation   | Cross-adapter consistency audit across 12 dimensions           | `21-adapter-operational-contract.md`, `22-delivery-semantics-matrix.md`, `23-identity-and-addressing.md` |
| `65-constrained-transport-comparison.md` | Constrained Transport Comparison | Protocol-neutral comparison of Matrix/Meshtastic/MeshCore/LXMF | `22-delivery-semantics-matrix.md` (delivery behavior), `23-identity-and-addressing.md` (identity)        |

### Governance

| File                                   | Title                          | Scope                                                                 | Governance Context                        |
| -------------------------------------- | ------------------------------ | --------------------------------------------------------------------- | ----------------------------------------- |
| `40-license-governance.md`             | License Governance             | License direction, dependency pressure, GPL-3.0-or-later decision     | License governance record                 |
| `42-contributor-governance.md`         | Contributor Governance         | Contributor expectations, licensing posture, relicensing constraints  | Contributor governance contract           |
| `43-distribution-boundary-analysis.md` | Distribution Boundary Analysis | Single-package structure, optional extras, future split paths         | Distribution boundary analysis            |
| `44-reticulum-license-notes.md`        | Reticulum/LXMF License Notes   | Reticulum License text, restriction clauses, operational implications | License observation; no legal conclusions |

### Design Note

| File                                   | Title                                      | Scope                                           | Design Context                      |
| -------------------------------------- | ------------------------------------------ | ----------------------------------------------- | ----------------------------------- |
| `11-meshtastic-connection-boundary.md` | Meshtastic Connection Boundary Design Note | Ownership boundaries for future real connection | Design note only; no implementation |

### Reference

| File                                      | Title                             | Scope                                                  | Reference Context                     |
| ----------------------------------------- | --------------------------------- | ------------------------------------------------------ | ------------------------------------- |
| `37-transport-maturity-classification.md` | Transport Maturity Classification | Per-transport maturity levels and progression criteria | Reference for maturity classification |
| `38-release-candidate-criteria.md`        | Release Candidate Criteria        | Criteria for release candidate readiness               | Reference for release gating          |

## Disposition Header Format

Contracts that carry a disposition header use this blockquote format:

```markdown
> **Status:** Active
> **Classification:** Normative
> **Authority:** [what this contract governs]
> **Last reviewed:** YYYY-MM-DD
```

Files without a disposition header are either owned by other agents (see below) or have equivalent classification metadata already embedded in their opening blockquote.

## Files Owned by Other Agents

The following files are reserved for other Tranche 1 agents. They appear in this index but do not carry disposition headers added by this agent:

- `18-operational-readiness-gaps.md`
- `33-failure-taxonomy.md`
- `37-transport-maturity-classification.md`
- `38-release-candidate-criteria.md`
- `57-runtime-accounting-contract.md`
- `59-runtime-durability-contract.md`
- `60-runtime-cancellation-contract.md`
- `61-operational-evidence-contract.md`
- `62-adapter-operational-maturity-matrix.md`

## Cross-Reference Notes

- **Supersession chain:** `65` → `22` (delivery) + `23` (identity). `12` → `23`. `15` → `21`/`22`/`23`.
- **Consolidation:** `28` consolidates `16`, `18`–`20`, `21`, `22`, `24`–`27`.
- **`docs/STATUS.md`** is the single source of truth for per-transport capability tracking, independent of this index.
- **`docs/contracts/61-operational-evidence-contract.md`** is the source-of-truth anchor for evidence classification (H/C/S/R tiers).
- **Historical evidence:** Live validation results in `32` and `66` are Tier H (Historical) per contract 61. They must not be presented as evidence of current behavior without re-execution.

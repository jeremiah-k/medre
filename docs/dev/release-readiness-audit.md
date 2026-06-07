# Release Readiness Audit

Compact inventory of every artifact that carries release-significance claims.
Each row records current status, source of truth, stale or conflicting language,
required cleanup, and evidence class. This is an honest prerelease snapshot.

## Evidence Class Legend

| Class        | Meaning                                                                                       |
| ------------ | --------------------------------------------------------------------------------------------- |
| S-tier       | Synthetic: fake adapters, mock objects, no real network or hardware                           |
| R-tier       | Runtime-tier: docker, live_service, or hardware — validated against a real endpoint or device |
| live         | Recorded against a real external service with full metadata in the repository                 |
| manual       | Human-verified through code inspection or reference repo comparison                           |
| NOT EXECUTED | No evidence of any tier exists                                                                |

## 1. Package Metadata

| Artifact                 | Current status                    | Source of truth  | Stale / conflicting language | Required cleanup | Evidence class |
| ------------------------ | --------------------------------- | ---------------- | ---------------------------- | ---------------- | -------------- |
| `pyproject.toml` name    | `medre`                           | `pyproject.toml` | None                         | None             | manual         |
| `pyproject.toml` version | `0.1.0`                           | `pyproject.toml` | None                         | None             | manual         |
| `pyproject.toml` status  | `Development Status :: 3 - Alpha` | `pyproject.toml` | None                         | None             | manual         |
| `pyproject.toml` license | `GPL-3.0-or-later`                | `pyproject.toml` | None                         | None             | manual         |

## 2. README Claims

| Artifact                 | Current status                                                              | Source of truth | Stale / conflicting language | Required cleanup | Evidence class |
| ------------------------ | --------------------------------------------------------------------------- | --------------- | ---------------------------- | ---------------- | -------------- |
| Prerelease banner        | "Pre-release. No stable public API. Not production-ready."                  | `README.md`     | None                         | None             | manual         |
| Subject-to-change notice | "Everything is subject to change without notice."                           | `README.md`     | None                         | None             | manual         |
| Transport list           | Matrix, Meshtastic, MeshCore, LXMF                                          | `README.md`     | None                         | None             | manual         |
| Pipeline description     | codec → renderer → session pipeline with optional config-file-first runtime | `README.md`     | None                         | None             | manual         |
| Doc directory table      | Lists spec, ops, dev, schemas, changes                                      | `README.md`     | None                         | None             | manual         |

## 3. CLI Surface

| Artifact                                           | Current status                            | Source of truth                         | Stale / conflicting language | Required cleanup | Evidence class |
| -------------------------------------------------- | ----------------------------------------- | --------------------------------------- | ---------------------------- | ---------------- | -------------- |
| `medre run`                                        | Starts full runtime from config           | `src/medre/cli/run_commands.py`         | None                         | None             | S-tier (smoke) |
| `medre smoke`                                      | Fake adapter pipeline exercise            | `src/medre/cli/smoke_commands.py`       | None                         | None             | S-tier         |
| `medre smoke --run-session`                        | Full session lifecycle with fake adapters | `src/medre/cli/smoke_commands.py`       | None                         | None             | S-tier         |
| `medre inspect` (event/receipts/native-ref/replay) | Read-only storage queries                 | `src/medre/cli/inspect_commands.py`     | None                         | None             | S-tier         |
| `medre trace` (event/replay)                       | Read-only timeline queries                | `src/medre/cli/trace_commands.py`       | None                         | None             | S-tier         |
| `medre evidence`                                   | Read-only evidence bundle collection      | `src/medre/cli/evidence_commands.py`    | None                         | None             | S-tier         |
| `medre recover`                                    | Read-only recovery runbook                | `src/medre/cli/recover_commands.py`     | None                         | None             | S-tier         |
| `medre replay`                                     | Replays events through pipeline           | `src/medre/cli/replay_commands.py`      | None                         | None             | S-tier         |
| `medre diagnostics`                                | Build-time or live-start snapshot         | `src/medre/cli/diagnostics_commands.py` | None                         | None             | S-tier         |
| `medre config check / sample`                      | Config validation                         | `src/medre/cli/config_commands.py`      | None                         | None             | S-tier         |
| `medre routes validate/topology/list`              | Route inspection                          | `src/medre/cli/route_commands.py`       | None                         | None             | S-tier         |
| `medre adapters`                                   | Adapter inventory                         | `src/medre/cli/config_commands.py`      | None                         | None             | S-tier         |
| `medre paths`                                      | Path resolver                             | `src/medre/cli/config_commands.py`      | None                         | None             | S-tier         |

## 4. Spec Authority Pages

| Artifact                       | Current status                                          | Source of truth                     | Stale / conflicting language | Required cleanup | Evidence class |
| ------------------------------ | ------------------------------------------------------- | ----------------------------------- | ---------------------------- | ---------------- | -------------- |
| `spec/principles.md`           | Design philosophy and invariants                        | `docs/spec/principles.md`           | None                         | None             | manual         |
| `spec/architecture.md`         | System overview and pipeline stages                     | `docs/spec/architecture.md`         | None                         | None             | manual         |
| `spec/event-model.md`          | CanonicalEvent, relations, metadata                     | `docs/spec/event-model.md`          | None                         | None             | manual         |
| `spec/adapter-runtime.md`      | Adapter protocol and lifecycle                          | `docs/spec/adapter-runtime.md`      | None                         | None             | manual         |
| `spec/routing-delivery.md`     | Route matching, fanout, receipts, planning authority    | `docs/spec/routing-delivery.md`     | None                         | None             | manual         |
| `spec/storage.md`              | SQLite schema, append-only guarantees, replay semantics | `docs/spec/storage.md`              | None                         | None             | manual         |
| `spec/state-machines.md`       | Receipt and outbox transition graphs                    | `docs/spec/state-machines.md`       | None                         | None             | manual         |
| `spec/diagnostics-evidence.md` | Observability, snapshots, evidence shape                | `docs/spec/diagnostics-evidence.md` | None                         | None             | manual         |
| `spec/delivery-lifecycle.md`   | Receipt/outbox state machines, vocabulary tables        | `docs/spec/delivery-lifecycle.md`   | None                         | None             | manual         |
| `spec/conformance.md`          | Conformance language rules                              | `docs/spec/conformance.md`          | None                         | None             | manual         |
| `spec/configuration.md`        | Configuration model                                     | `docs/spec/configuration.md`        | None                         | None             | manual         |
| `spec/identity-addressing.md`  | Identity and addressing model                           | `docs/spec/identity-addressing.md`  | None                         | None             | manual         |
| `spec/metadata.md`             | Metadata model                                          | `docs/spec/metadata.md`             | None                         | None             | manual         |
| `spec/security-privacy.md`     | Security and privacy model                              | `docs/spec/security-privacy.md`     | None                         | None             | manual         |
| `spec/index.md`                | Reading order and authority rules                       | `docs/spec/README.md`               | None                         | None             | manual         |

## 5. Appendices

| Artifact                              | Current status                               | Source of truth                                 | Stale / conflicting language | Required cleanup | Evidence class |
| ------------------------------------- | -------------------------------------------- | ----------------------------------------------- | ---------------------------- | ---------------- | -------------- |
| `appendices/evidence-levels.md`       | Tier definitions, classification rules       | `docs/spec/appendices/evidence-levels.md`       | None                         | None             | manual         |
| `appendices/failure-taxonomy.md`      | Per-transport failure classification         | `docs/spec/appendices/failure-taxonomy.md`      | None                         | None             | manual         |
| `appendices/transport-limitations.md` | Per-transport constraint reference           | `docs/spec/appendices/transport-limitations.md` | None                         | None             | manual         |
| `appendices/release-readiness.md`     | Transport maturity matrix, prerelease status | `docs/spec/appendices/release-readiness.md`     | None                         | None             | manual         |
| `appendices/glossary.md`              | Key term definitions                         | `docs/spec/appendices/glossary.md`              | None                         | None             | manual         |

## 6. Transport Profiles

| Artifact                           | Current status                     | Source of truth                              | Stale / conflicting language | Required cleanup | Evidence class |
| ---------------------------------- | ---------------------------------- | -------------------------------------------- | ---------------------------- | ---------------- | -------------- |
| `transport-profiles/matrix.md`     | Matrix current-state reference     | `docs/spec/transport-profiles/matrix.md`     | None                         | None             | manual         |
| `transport-profiles/meshtastic.md` | Meshtastic current-state reference | `docs/spec/transport-profiles/meshtastic.md` | None                         | None             | manual         |
| `transport-profiles/meshcore.md`   | MeshCore current-state reference   | `docs/spec/transport-profiles/meshcore.md`   | None                         | None             | manual         |
| `transport-profiles/lxmf.md`       | LXMF current-state reference       | `docs/spec/transport-profiles/lxmf.md`       | None                         | None             | manual         |

## 7. Schema Files

| Artifact                       | Current status          | Source of truth | Stale / conflicting language | Required cleanup | Evidence class              |
| ------------------------------ | ----------------------- | --------------- | ---------------------------- | ---------------- | --------------------------- |
| `adapter-config.schema.json`   | Machine-readable schema | `docs/schemas/` | None                         | None             | S-tier (validated by tests) |
| `canonical-event.schema.json`  | Machine-readable schema | `docs/schemas/` | None                         | None             | S-tier (validated by tests) |
| `delivery-receipt.schema.json` | Machine-readable schema | `docs/schemas/` | None                         | None             | S-tier (validated by tests) |
| `delivery-result.schema.json`  | Machine-readable schema | `docs/schemas/` | None                         | None             | S-tier (validated by tests) |
| `diagnostics.schema.json`      | Machine-readable schema | `docs/schemas/` | None                         | None             | S-tier (validated by tests) |
| `evidence-bundle.schema.json`  | Machine-readable schema | `docs/schemas/` | None                         | None             | S-tier (validated by tests) |
| `routing-config.schema.json`   | Machine-readable schema | `docs/schemas/` | None                         | None             | S-tier (validated by tests) |
| `runtime-snapshot.schema.json` | Machine-readable schema | `docs/schemas/` | None                         | None             | S-tier (validated by tests) |

## 8. Schema Examples

| Artifact                                    | Current status                                                    | Source of truth          | Stale / conflicting language | Required cleanup | Evidence class              |
| ------------------------------------------- | ----------------------------------------------------------------- | ------------------------ | ---------------------------- | ---------------- | --------------------------- |
| 8 example files in `docs/schemas/examples/` | One per schema, validated by `tests/test_docs_schema_examples.py` | `docs/schemas/examples/` | None                         | None             | S-tier (validated by tests) |

## 9. Sample Configs

| Artifact              | Current status                              | Source of truth | Stale / conflicting language | Required cleanup | Evidence class |
| --------------------- | ------------------------------------------- | --------------- | ---------------------------- | ---------------- | -------------- |
| `medre config sample` | Generates TOML sample config from templates | CLI / templates | None                         | None             | S-tier         |

## 10. Ops Runbooks

| Artifact                          | Current status                                | Source of truth             | Stale / conflicting language | Required cleanup | Evidence class |
| --------------------------------- | --------------------------------------------- | --------------------------- | ---------------------------- | ---------------- | -------------- |
| `ops/install.md`                  | Installation instructions                     | `docs/ops/install.md`       | None                         | None             | manual         |
| `ops/configuration.md`            | Configuration guide                           | `docs/ops/configuration.md` | None                         | None             | manual         |
| `ops/running-medre.md`            | Running instructions                          | `docs/ops/running-medre.md` | None                         | None             | manual         |
| `ops/diagnostics-and-evidence.md` | Evidence collection and diagnostics workflows | `docs/ops/`                 | None                         | None             | manual         |
| `ops/recovery-and-replay.md`      | Recovery and replay operator procedures       | `docs/ops/`                 | None                         | None             | manual         |
| `ops/operator-workflows.md`       | Adapter status lifecycle, read-only workflows | `docs/ops/`                 | None                         | None             | manual         |

## 11. Test Evidence Levels

| Artifact category                            | Current status                                                | Source of truth                                                               | Stale / conflicting language | Required cleanup | Evidence class  |
| -------------------------------------------- | ------------------------------------------------------------- | ----------------------------------------------------------------------------- | ---------------------------- | ---------------- | --------------- |
| Pipeline behavior tests (13 behaviors)       | All 13 have synthetic-tier coverage                           | `evidence-levels.md` §8                                                       | None                         | None             | S-tier          |
| Delivery state vocabulary tests              | Vocabulary frozensets, classification, transitions            | `tests/test_delivery_state.py`                                                | None                         | None             | S-tier          |
| Adapter parity tests                         | Cross-adapter contract consistency                            | `tests/test_adapter_parity.py`                                                | None                         | None             | S-tier          |
| Lifecycle authority doc tests                | Docs/code vocabulary alignment, metadata naming               | `tests/test_docs_lifecycle_authority.py`                                      | None                         | None             | S-tier          |
| Schema/example validation tests              | JSON Schema and example file conformance                      | `tests/test_docs_schema_examples.py`                                          | None                         | None             | S-tier          |
| Single authority / status vocabulary tests   | Doc structure and vocabulary tests                            | `tests/test_docs_single_authority.py`, `tests/test_docs_status_vocabulary.py` | None                         | None             | S-tier          |
| Meshtastic self-echo and classifier          | Classifier + adapter integration                              | `tests/test_meshtastic_*.py`                                                  | None                         | None             | S-tier          |
| MeshCore session startup/recovery            | Session lifecycle with mocked SDK                             | `tests/test_meshcore_session*.py`                                             | None                         | None             | S-tier          |
| LXMF session + startup                       | Session lifecycle with mocked Reticulum                       | `tests/test_lxmf_session*.py`                                                 | None                         | None             | S-tier          |
| Matrix boundary tests                        | Error code classification, M_DUPLICATE_ANNOTATION             | `tests/test_matrix_boundaries.py`                                             | None                         | None             | S-tier          |
| Runtime execution / evidence completeness    | Runtime event taxonomy, evidence boundaries                   | Various runtime test files                                                    | None                         | None             | S-tier          |
| Adapter runtime live tests (Matrix/Docker)   | Docker Synapse SDK-boundary validation                        | Opt-in live test harness                                                      | None                         | None             | R-tier (docker) |
| Meshtastic live test harness                 | Exists, gated by env vars, not yet run against physical radio | Opt-in live test harness                                                      | None                         | None             | NOT EXECUTED    |
| MeshCore live validation                     | Not started                                                   | —                                                                             | None                         | None             | NOT EXECUTED    |
| LXMF live validation                         | Not started                                                   | —                                                                             | None                         | None             | NOT EXECUTED    |
| External live validation (non-Docker Matrix) | Not executed                                                  | —                                                                             | None                         | None             | NOT EXECUTED    |
| Multi-room concurrent inbound (live)         | Not executed                                                  | —                                                                             | None                         | None             | NOT EXECUTED    |
| E2EE reactions/edits/media (live)            | Not executed                                                  | —                                                                             | None                         | None             | NOT EXECUTED    |

## 12. Live Validation Status

| Transport  | Config load | Fake lifecycle | SDK integration | Live start/health | Outbound delivery | Inbound decode  | Run-session     | Recorded live evidence |
| ---------- | ----------- | -------------- | --------------- | ----------------- | ----------------- | --------------- | --------------- | ---------------------- |
| Matrix     | S-tier      | S-tier         | R-tier (docker) | R-tier (docker)   | R-tier (docker)   | R-tier (docker) | R-tier (docker) | docker-validated       |
| Meshtastic | S-tier      | S-tier         | NOT EXECUTED    | NOT EXECUTED      | NOT EXECUTED      | NOT EXECUTED    | NOT EXECUTED    | NOT EXECUTED           |
| MeshCore   | S-tier      | S-tier         | NOT EXECUTED    | NOT EXECUTED      | NOT EXECUTED      | NOT EXECUTED    | NOT EXECUTED    | NOT EXECUTED           |
| LXMF       | S-tier      | S-tier         | NOT EXECUTED    | NOT EXECUTED      | NOT EXECUTED      | NOT EXECUTED    | NOT EXECUTED    | NOT EXECUTED           |

## 13. Known Gaps

| Gap                                                          | Impact                                                  | Evidence class                | Status               |
| ------------------------------------------------------------ | ------------------------------------------------------- | ----------------------------- | -------------------- |
| No live_service or hardware tier evidence for any transport  | Cannot claim production-adjacent behavior               | NOT EXECUTED                  | Open                 |
| No external live Matrix validation (federation, rate limits) | Docker evidence does not prove external network         | NOT EXECUTED                  | Open                 |
| Meshtastic self-echo requires real radio for live proof      | Pipeline wiring proven in S-tier only                   | NOT EXECUTED                  | Open                 |
| MeshCore/Meshtastic byte budget not measured dynamically     | Protobuf overhead risk for constrained transports       | S-tier (static budget)        | Open                 |
| Native-ref persistence gap on crash (Meshtastic queue)       | In-memory queue items lost on process exit              | manual (code inspection)      | Open                 |
| Schema version frozen at 1 during prerelease                 | No compatibility commitment implied                     | manual                        | Frozen (intentional) |
| No `docs/spec/runtime.md`                                    | Runtime behavior documented in dev audits only          | manual                        | Open                 |
| Conversation `conversation_id` equals `root_event_id`        | Cannot diverge until future authority rule              | S-tier                        | Open (deferred)      |
| `native_thread_id` always None on all types                  | Thread-aware rendering blocked                          | manual                        | Open (reserved)      |
| Fallback rendering path has no R-tier evidence               | No transport currently uses capability level "fallback" | S-tier (synthetic tests only) | Open                 |

## 14. Authority Audit Doc Coverage

| Audit doc                                         | Status   | Date reference |
| ------------------------------------------------- | -------- | -------------- |
| `docs/dev/lifecycle-authority-audit.md`           | Complete | 2026-06        |
| `docs/dev/adapter-reality-audit.md`               | Complete | 2026-06-05     |
| `docs/dev/native-relations-audit.md`              | Complete | 2026-06        |
| `docs/dev/conversation-graph-audit.md`            | Complete | 2026-06-06     |
| `docs/dev/planning-authority-audit.md`            | Complete | 2026-06        |
| `docs/dev/operator-surface-audit.md`              | Complete | 2026-06        |
| `docs/dev/persistence-authority-audit.md`         | Complete | 2026-06        |
| `docs/dev/runtime-execution-authority-audit.md`   | Complete | 2026-06        |
| `docs/dev/runtime-evidence-completeness-audit.md` | Complete | 2026-06        |

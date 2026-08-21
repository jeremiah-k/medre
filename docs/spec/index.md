# MEDRE Specification Index

Table of contents for the MEDRE normative specification.

---

## Normative Specifications

| Document                                                   | Purpose                                                       |
| ---------------------------------------------------------- | ------------------------------------------------------------- |
| [principles.md](principles.md)                             | Design philosophy and invariants                              |
| [architecture.md](architecture.md)                         | System overview, pipeline stages, module boundaries           |
| [event-model.md](event-model.md)                           | CanonicalEvent, relations, event kinds, schema versioning     |
| [adapter-runtime.md](adapter-runtime.md)                   | Adapter protocol, lifecycle, capabilities, codec              |
| [routing-delivery.md](routing-delivery.md)                 | Route matching, fanout, delivery plans, receipts              |
| [storage.md](storage.md)                                   | SQLite schema, append-only guarantees, replay semantics       |
| [identity-addressing.md](identity-addressing.md)           | Native identities, canonical actors, privacy boundaries       |
| [metadata.md](metadata.md)                                 | Metadata namespaces, embedding modes, never-embed list        |
| [compatibility-boundaries.md](compatibility-boundaries.md) | External compatibility and prerelease rejection policy        |
| [matrix-event-shape.md](matrix-event-shape.md)             | Stable Matrix ingress/native metadata contract                |
| [configuration.md](configuration.md)                       | YAML config, XDG paths, env overrides, config model           |
| [security-privacy.md](security-privacy.md)                 | Security model, credential handling, privacy boundaries       |
| [diagnostics-evidence.md](diagnostics-evidence.md)         | Observability, diagnostics snapshots, evidence classification |
| [conformance.md](conformance.md)                           | Conformance definition, test categories, authority rules      |

## Transport Profiles

| Document                                   | Purpose                             |
| ------------------------------------------ | ----------------------------------- |
| [transport-profiles/](transport-profiles/) | Per-adapter current-state reference |

## Appendices

| Document                                                                   | Purpose                                    |
| -------------------------------------------------------------------------- | ------------------------------------------ |
| [appendices/glossary.md](appendices/glossary.md)                           | Term definitions                           |
| [appendices/failure-taxonomy.md](appendices/failure-taxonomy.md)           | Per-transport failure classification       |
| [appendices/evidence-levels.md](appendices/evidence-levels.md)             | Evidence provenance tiers                  |
| [appendices/transport-limitations.md](appendices/transport-limitations.md) | Cross-transport limitation summary         |
| [appendices/known-limitations.md](appendices/known-limitations.md)         | Limitations surfaced from test annotations |
| [appendices/release-readiness.md](appendices/release-readiness.md)         | Transport maturity and readiness checklist |

## Reading Order

1. **Principles** — design philosophy and invariants
2. **Architecture** — system overview and pipeline stages
3. **Event Model** — CanonicalEvent, relations, metadata
4. **Adapter Runtime** — adapter protocol and lifecycle
5. **Routing & Delivery** — route matching, fanout, receipts
6. **Storage** — SQLite schema, append-only guarantees, replay semantics
7. **Identity & Addressing** — identity model, privacy boundaries
8. **Metadata** — namespaces, embedding modes
9. **Compatibility Boundaries** — external compatibility and prerelease rejection policy
10. **Matrix Event Shape** — Matrix ingress/native metadata contract
11. **Configuration** — YAML system, XDG paths, env overrides
12. **Security & Privacy** — credential handling, no-secret-leakage
13. **Diagnostics & Evidence** — observability, snapshots
14. **Conformance** — what it means to conform, test categories

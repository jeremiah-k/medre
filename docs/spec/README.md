# MEDRE Specification

This directory contains the **authoritative normative specification** for the
MEDRE runtime. Every behavioral claim about how MEDRE works MUST be grounded
in a document under this tree.

## Organization

| Path       | Purpose                                                                |
| ---------- | ---------------------------------------------------------------------- |
| `spec/`    | Normative specifications — runtime semantics, data models, contracts   |
| `schemas/` | Machine-readable JSON Schema definitions derived from source           |
| `ops/`     | Operator documentation — how to install, run, and validate MEDRE       |
| `dev/`     | Developer documentation — how to contribute, test, and author adapters |
| `changes/` | Change fragments tracking spec and ops modifications                   |

## Document Index

### Normative Specifications

| Document                                                   | Purpose                                                         |
| ---------------------------------------------------------- | --------------------------------------------------------------- |
| [principles.md](principles.md)                             | Design philosophy and invariants                                |
| [architecture.md](architecture.md)                         | System overview, pipeline stages, runtime orchestration         |
| [event-model.md](event-model.md)                           | CanonicalEvent, relations, event kinds, schema versioning       |
| [adapter-runtime.md](adapter-runtime.md)                   | Adapter protocol, lifecycle, capabilities, codec                |
| [routing-delivery.md](routing-delivery.md)                 | Route matching, fanout, delivery plans, receipts                |
| [storage.md](storage.md)                                   | SQLite schema, append-only guarantees, replay semantics         |
| [state-machines.md](state-machines.md)                     | Receipt and outbox transition graphs                            |
| [delivery-lifecycle.md](delivery-lifecycle.md)             | Delivery lifecycle authority hierarchy and vocabulary tables    |
| [durable-ingress.md](durable-ingress.md)                   | Durable admission boundary, checkpoint ownership, drain handoff |
| [identity-addressing.md](identity-addressing.md)           | Native identities, canonical actors, privacy boundaries         |
| [metadata.md](metadata.md)                                 | Metadata namespaces, embedding modes, never-embed list          |
| [compatibility-boundaries.md](compatibility-boundaries.md) | External compatibility and prerelease rejection policy          |
| [matrix-event-shape.md](matrix-event-shape.md)             | Stable Matrix ingress/native metadata contract                  |
| [configuration.md](configuration.md)                       | YAML config, XDG paths, env overrides, config model             |
| [security-privacy.md](security-privacy.md)                 | Security model, credential handling, privacy boundaries         |
| [diagnostics-evidence.md](diagnostics-evidence.md)         | Observability, diagnostics snapshots, evidence classification   |
| [conformance.md](conformance.md)                           | Conformance definition, test categories, authority rules        |

### Transport Profiles

| Document                                   | Purpose                             |
| ------------------------------------------ | ----------------------------------- |
| [transport-profiles/](transport-profiles/) | Per-adapter current-state reference |

Transport profiles include machine-readable capability declarations
(`*-capabilities.json`) validated by `tests/test_capability_conformance.py`.

### Appendices

| Document                                                                   | Purpose                                    |
| -------------------------------------------------------------------------- | ------------------------------------------ |
| [appendices/glossary.md](appendices/glossary.md)                           | Term definitions and authority map         |
| [appendices/failure-taxonomy.md](appendices/failure-taxonomy.md)           | Per-transport failure classification       |
| [appendices/evidence-levels.md](appendices/evidence-levels.md)             | Evidence provenance tiers                  |
| [appendices/transport-limitations.md](appendices/transport-limitations.md) | Cross-transport limitation summary         |
| [appendices/transport-realism.md](appendices/transport-realism.md)         | Required evidence ladder and coverage      |
| [appendices/known-limitations.md](appendices/known-limitations.md)         | Limitations surfaced from test annotations |
| [appendices/release-readiness.md](appendices/release-readiness.md)         | Transport maturity and readiness checklist |

## Reading Order

1. **Principles** (`principles.md`) — design philosophy and invariants
2. **Architecture** (`architecture.md`) — system overview, pipeline stages,
   runtime orchestration
3. **Event Model** (`event-model.md`) — CanonicalEvent, relations, metadata
4. **Adapter Runtime** (`adapter-runtime.md`) — adapter protocol and lifecycle
5. **Routing & Delivery** (`routing-delivery.md`) — route matching, fanout,
   receipts
6. **Storage** (`storage.md`) — SQLite schema, append-only guarantees, replay
   semantics
7. **State Machines** (`state-machines.md`) — receipt and outbox transition
   graphs
8. **Delivery Lifecycle** (`delivery-lifecycle.md`) — lifecycle authority
   hierarchy and vocabulary
9. **Diagnostics & Evidence** (`diagnostics-evidence.md`) — observability,
   snapshots
10. **Durable Ingress** (`durable-ingress.md`) — admission boundary and
    checkpoint ownership
11. **Identity & Addressing** (`identity-addressing.md`) — identity model,
    privacy boundaries
12. **Metadata** (`metadata.md`) — namespaces, embedding modes
13. **Configuration** (`configuration.md`) — YAML system, XDG paths, env
    overrides
14. **Security & Privacy** (`security-privacy.md`) — credential handling,
    no-secret-leakage
15. **Compatibility Boundaries** (`compatibility-boundaries.md`) — external
    interoperability and prerelease rejection policy
16. **Matrix Event Shape** (`matrix-event-shape.md`) — Matrix ingress/native
    metadata contract
17. **Conformance** (`conformance.md`) — what it means to conform, test
    categories

Transport profiles and appendices are reference material; consult them as
needed after the core sequence.

## Authority Rules

- If a document under `spec/` conflicts with any other documentation, `spec/`
  takes precedence.
- Operator docs (`ops/`) describe how to use the runtime; they do not define
  semantics.
- Developer docs (`dev/`) describe how to extend the runtime; they do not
  define semantics.
- Historical planning documents are not preserved as authoritative references.

## Conformance Language

Documents under `spec/` use RFC 2119 keywords:

- **MUST** / **MUST NOT** — absolute requirement
- **SHOULD** / **SHOULD NOT** — recommendation unless there is a valid reason
- **MAY** — optional

These keywords MUST NOT appear in `ops/` or `dev/` documentation. Those
directories use plain descriptive language.

## Pre-Release Status

MEDRE is pre-first-release. No public API is frozen. Breaking changes to the
specification are permitted when they simplify the model. When a breaking
change is made, update the relevant schema files and tests in the same commit.

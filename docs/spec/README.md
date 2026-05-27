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

## Reading Order

1. **Principles** (`spec/principles.md`) — design philosophy and invariants
2. **Architecture** (`spec/architecture.md`) — system overview and pipeline stages
3. **Event Model** (`spec/event-model.md`) — CanonicalEvent, relations, metadata
4. **Adapter Runtime** (`spec/adapter-runtime.md`) — adapter protocol and lifecycle
5. **Routing & Delivery** (`spec/routing-delivery.md`) — route matching, fanout, receipts
6. **Storage** (`spec/storage.md`) — SQLite schema, append-only guarantees, replay
7. **State Machines** (`spec/state-machines.md`) — receipt and outbox transition graphs
8. **Diagnostics & Evidence** (`spec/diagnostics-evidence.md`) — observability, snapshots
9. **Transport Profiles** (`spec/transport-profiles/`) — per-adapter current-state reference

Transport profiles include machine-readable capability declarations
(`*-capabilities.json`) validated by `tests/test_capability_conformance.py`.

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

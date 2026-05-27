# Operator Documentation

This directory contains practical documentation for running, validating, and
troubleshooting MEDRE deployments.

## Reading Order

| Document | Purpose |
|----------|---------|
| `install.md` | Installing MEDRE and setting up a development environment |
| `configuration.md` | TOML configuration reference, environment variables, XDG paths |
| `running-medre.md` | Starting, stopping, and monitoring the MEDRE runtime |
| `operator-workflows.md` | Day-to-day operational workflows: smoke tests, evidence, tracing |
| `diagnostics-and-evidence.md` | Collecting evidence bundles, interpreting diagnostic output |
| `recovery-and-replay.md` | Crash recovery, event replay, and failure drill procedures |
| `transport-setup/` | Per-transport setup guides (Matrix, Meshtastic, MeshCore, LXMF) |
| `live-validation/` | Per-transport live smoke test procedures |
| `troubleshooting.md` | Common issues and resolution steps |

## Scope

Operator docs describe **how to use** MEDRE. They do not define runtime
semantics. For normative specifications, see `docs/spec/`.

## Conventions

- Every procedure includes: prerequisites, steps, expected output, and failure
  modes.
- Commands are copy-paste ready.
- No internal planning-cycle vocabulary.
- No RFC 2119 keywords (MUST/SHOULD/MAY) — use plain language.

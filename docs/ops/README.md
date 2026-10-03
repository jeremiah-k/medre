# Operator Documentation

This directory contains practical documentation for running, validating, and
troubleshooting MEDRE deployments.

Begin with [installation](install.md). Operator commands assume an activated
MEDRE environment; checkout setup uses uv, and built-package installs also
support pip. A source checkout can use `uv run --no-sync` after selecting its
extras instead of activating `.venv`.

## Reading Order

| Document                      | Purpose                                                          |
| ----------------------------- | ---------------------------------------------------------------- |
| [install.md](install.md)                  | Installing MEDRE and setting up a development environment        |
| [configuration.md](configuration.md)            | YAML configuration reference, environment variables, XDG paths   |
| [running-medre.md](running-medre.md)            | Starting, stopping, and monitoring the MEDRE runtime             |
| [operator-workflows.md](operator-workflows.md)       | Day-to-day operational workflows: smoke tests, evidence, tracing |
| [diagnostics-and-evidence.md](diagnostics-and-evidence.md) | Collecting evidence bundles, interpreting diagnostic output      |
| [recovery-and-replay.md](recovery-and-replay.md)      | Crash recovery, event replay, and failure drill procedures       |
| [transport-setup/](transport-setup/)            | Per-transport setup guides (Matrix, Meshtastic, MeshCore, LXMF)  |
| [live-validation/short-iterations.md](live-validation/short-iterations.md) | Bounded hardware campaign from native pairs through cross-transport bridges |
| [live-validation/](live-validation/)            | Per-transport live smoke test procedures                         |
| [troubleshooting.md](troubleshooting.md)          | Common issues and resolution steps                               |

## Scope

Operator docs describe **how to use** MEDRE. They do not define runtime
semantics. For normative specifications, see `docs/spec/`.

## Conventions

- Every procedure includes: prerequisites, steps, expected output, and failure
  modes.
- Commands are copy-paste ready.
- No internal planning-cycle vocabulary.
- No RFC 2119 keywords (MUST/SHOULD/MAY) — use plain language.

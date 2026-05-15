# Operator Tooling Boundary

> Last updated: 2026-05-14

Operator tools are CLI-facing commands that exercise the runtime to produce
reports, evidence bundles, and failure drills. They are operator-level
concerns, not runtime infrastructure.


## Current location

Operator tools currently live in `medre/runtime/` because they depend on
`MedreApp`, `RuntimeBuilder`, and the runtime lifecycle to do their work:

| Tool | Module | Depends on |
|------|--------|------------|
| `medre smoke` | `runtime.smoke` | `MedreApp`, `RuntimeBuilder`, fake adapters |
| `medre drill` | `runtime.drill` | `MedreApp`, failure injection, pipeline |
| `medre evidence` | `runtime.evidence` | `MedreApp`, `smoke`, `drill`, storage |
| `medre trace` | `runtime.trace` | Storage queries, receipt enrichment |
| `medre recover` | `runtime.recover` | Storage queries, incident classification |
| `run_session` | `runtime.run_session` | `MedreApp`, standardized report generation |

These modules orchestrate the runtime for operator purposes. They are not
called during normal `medre run` operation.


## Target location

Operator tools should move to `medre/operator/` when the import graph allows:

```
src/medre/
  operator/       smoke, drill, evidence, trace, recover, run_session
  runtime/        builder, app, route engine, capacity, snapshot
  core/           event model, storage, pipeline, routing, rendering
  config/         loader, model, env overrides, paths
  adapters/       base class + per-transport packages
  cli/            argparse, command dispatch, I/O formatting
```


## Import invariants

| From | May import | Must not import |
|------|-----------|-----------------|
| `operator/` | `runtime.*`, `core.*`, `config.*`, `adapters.base` | Specific adapter SDK modules |
| `runtime/` | `core.*`, `config.*`, `adapters.base` | `operator.*` |
| `core/` | Other `core/*` sub-packages | `operator.*`, `runtime.*`, `adapters.*` |

The critical rule: **`runtime/` and `core/` must never import from `operator/`.**
Operator tools are consumers of the runtime, not dependencies of it. The
runtime must remain usable without any operator tooling present.


## Why they are still in runtime/

1. `smoke.py` and `drill.py` construct `MedreApp` via `RuntimeBuilder` and
   call adapter lifecycle methods directly.
2. `evidence.py` aggregates results from `smoke`, `drill`, storage queries,
   and diagnostics — all runtime-level APIs.
3. `trace.py` and `recover.py` query storage and enrich results with
   pipeline-domain knowledge that lives in `runtime/` modules.
4. Extracting these modules today would require `runtime/` to expose a
   stable internal API surface (or operator tools would need to reach into
   runtime internals, violating the boundary from the wrong direction).


## When to Split

Move operator tools to `medre/operator/` when **all** of the following are
true:

1. `MedreApp` exposes a stable public API for start, stop, inject, and
   snapshot that operator tools can call without accessing private attributes
   (currently operator tools access `app._runtime_accounting` and similar).
2. `RuntimeBuilder` returns a typed result object (not a raw `MedreApp`) that
   operator tools can consume without importing `runtime.app` internals.
3. Failure injection has a public interface in `core/` or `runtime/` that
   operator tools call without importing test harness internals.
4. Storage query methods used by `trace` and `recover` are available from
   `core.storage` without operator-specific wrappers in `runtime/`.

Until these conditions are met, keeping operator tools in `runtime/` avoids
circular imports and preserves the buildability of the package. The cost is
that `runtime/` is larger than its core mandate, but the import direction is
correct: operator tools import from runtime, never the reverse.


## Operator report standardization

Operator tools that produce JSON reports follow a shared contract:

- **Status values are lowercase:** `"pass"`, `"fail"`, `"ok"`, `"partial"`,
  `"error"`, `"skipped"`.
- **Scenario category:** every report includes a `scenario_category` field
  (e.g., `"smoke"`, `"drill"`, `"evidence"`, `"trace"`, `"recovery"`).
- **Command provenance:** reports include `command` (the CLI subcommand),
  `simulated` (boolean, true for fake-adapter runs), and `commands_argv`
  / `commands_text` capturing the full invocation.
- **Simulation method:** drill reports include `simulation_method` (e.g.,
  `"fake_adapter"`, `"config_injection"`, `"failure_injection"`) to document
  how the failure scenario was produced.

This contract is enforced by `run_session` and the operator report builders,
not by the runtime itself.

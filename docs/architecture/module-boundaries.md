# Module Boundaries

Package structure and import rules after the runtime refactor.

> **No public API commitment yet.** MEDRE has no stable public API commitment
> yet. All import paths are internal and may change until a deliberate
> `medre.sdk` or stable public facade is introduced later after API design
> settles.

## Package layout

```text
src/medre/
  cli/            argparse, command dispatch, I/O formatting
  runtime/        builder, app, route engine, smoke/drill/trace
  core/           event model, storage, pipeline, routing, rendering
    observability/  logging setup, diagnostic events, metrics, sanitization
  adapters/       base class + per-transport packages (matrix/, meshtastic/, meshcore/, lxmf/)
  config/         loader, model, env overrides, paths, sample generation
```

## Package ownership

### `cli/` — command layer

Owns argument parsing, subcommand dispatch, and terminal output. Command
modules translate user input into calls on `config/` or `runtime/`.

**Contents:** `main.py` (argparse tree), `run_commands.py`,
`config_commands.py`, `smoke_commands.py`, `diagnostics_commands.py`,
`evidence_commands.py`, `inspect_commands.py`, `recover_commands.py`,
`replay_commands.py`, `route_commands.py`, `trace_commands.py`,
`exit_codes.py`, `storage_helpers.py`, `json.py`.

### `runtime/` — orchestration layer

Owns adapter assembly, lifecycle management, and (currently) operational
tooling. Operator tooling will move to `medre/operator/` when the import
graph allows — see [Operator Tooling Boundary](operator-tooling-boundary.md).

**Contents:** `builder.py` (`RuntimeBuilder`), `app.py` (`MedreApp`),
`route_engine.py`, `routes.py`, `observability.py` (diagnostics collector),
`events.py`, `evidence.py`, `trace.py`, `drill.py`, `smoke.py`,
`snapshot.py`, `boot_summary.py`, `errors.py`, `summaries.py`.

> **Note:** `capacity.py` moved to `core/runtime/capacity.py` — see `core/` below.

### `core/` — domain primitives

Transport-agnostic building blocks. No adapter or SDK imports.

**Sub-packages:** `events/` (bus, canonical event, schema, kinds),
`storage/`, `rendering/`, `routing/`, `planning/`, `policies/`,
`engine/` (pipeline runner), `diagnostics/`, `identity/`, `lifecycle/`,
`transforms/`, `observability/` (logging setup, diagnostic events, metrics).

### `adapters/` — transport boundary

Each adapter package owns its SDK, codec, renderer, session, config, and
compat guard entirely. No adapter touches another adapter's transport.

**Per-transport contents:** `adapter.py`, `codec.py`, `renderer.py`,
`session.py`, `config.py`, `errors.py`, `compat.py`.
**Fakes:** `fake_matrix.py`, `fake_meshtastic.py`, `fake_meshcore.py`,
`fake_lxmf.py` at the `adapters/` level.

### `config/` — configuration layer

Owns TOML loading, model classes, environment overrides, and path resolution.

**Contents:** `loader.py`, `model.py`, `env.py`, `paths.py`, `errors.py`,
`sample.py`, `adapters/` (per-transport config models and credential helpers).

**Import rules:**

- `config/model.py` may import adapter config dataclasses from
  `medre.config.adapters.*`.
- `config/` must not import adapter implementations, protocol SDKs,
  `medre.runtime.builder`, route engine behavior, or `medre.core.engine.*`.
- Concrete imports should target specific modules:
  `medre.config.model`, `medre.config.loader`,
  `medre.config.adapters.matrix`, `medre.config.adapters.errors`.

## Import rules

| From                    | May import                                                                                      | Must not import                                           |
| ----------------------- | ----------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| `cli/` commands         | `config.*`, `runtime.*`, `core.observability.*`                                                 | Adapter implementations, unrelated `core.*` internals     |
| `runtime/builder`       | `core.contracts.adapter`, `config.model`, `core.*`                                              | Specific adapter SDK modules                              |
| `runtime/observability` | `core.diagnostics`, `core.routing.stats`                                                        | Adapter code                                              |
| `core/*`                | Other `core/*` sub-packages                                                                     | `adapters.*`, `runtime.*`, `cli.*`                        |
| `adapters/<transport>/` | `core.contracts.adapter`, `core.events`, `core.rendering`                                       | Other adapter packages, `runtime.*`                       |
| `config/`               | `medre.config.*` (own internals), `medre.runtime.routes`, `medre.core.observability.log_levels` | `medre.adapters.*`, adapter SDKs, `medre.runtime.builder` |

Key invariants:

- **CLI commands never import adapter implementations directly.** The `run`
  command calls `RuntimeBuilder` which handles adapter construction.
- **`RuntimeBuilder` is the single assembly point.** It is the only module that
  imports both config model types and adapter base classes to wire the system.
- **`core/` is transport-agnostic.** No module under `core/` imports from
  `adapters/` or `runtime/`.
- **Config package follows the same `no-adapters, no-SDK` rule as core.** It
  imports from `medre.config.*` (its own internals), `medre.core.observability.log_levels`
  (lightweight), and `medre.runtime.routes` (route config models). It must **not**
  import `medre.adapters.*`, protocol SDKs, `medre.runtime.builder`, or `medre.core.engine.*`.

## What was removed or moved

| Before                        | After                                                  | Reason                                                             |
| ----------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------ |
| `runner.py` (top-level)       | Deleted                                                | Logic moved into `runtime/builder.py` and `runtime/app.py`         |
| `cli.py` (monolithic)         | `cli/` package                                         | Split into per-command modules for maintainability                 |
| `_sanitize_error` (scattered) | `medre.core.observability.sanitization.sanitize_error` | Consolidated into core observability                               |
| `medre.observability` package | Removed                                                | Modules moved to canonical homes (see Observability section below) |

## Operator Tooling Boundary

Operator tools (smoke, drill, evidence, trace, recover, run_session) currently
live in `runtime/` because they depend on `MedreApp`, `RuntimeBuilder`, and the
runtime lifecycle. They should move to `medre/operator/` when the import graph
allows it — specifically when `MedreApp` exposes a stable public API for
start/stop/inject/snapshot without private-attribute access.

**Invariant:** `runtime/` and `core/` must never import from operator tooling.
Operator tools are consumers of the runtime, not dependencies of it.

See [Operator Tooling Boundary](operator-tooling-boundary.md) for the full
decision record, import invariants, and split criteria.

## Observability

`medre.core.observability` is the canonical internal observability
implementation. It contains logging setup, diagnostic events, metrics, and
the canonical sanitization implementation
(`medre.core.observability.sanitization`). Used by the pipeline, routing
engine, and internal framework code.

The top-level `medre.observability` package was removed. Its modules moved to
canonical internal homes:

- `classification.py` → `medre.core.observability.classification`
- `summaries.py` → `medre.runtime.summaries`
- `logging.py` → deleted (unused)

The canonical sanitization path is `medre.core.observability.sanitization`.

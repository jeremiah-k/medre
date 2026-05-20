# Module Boundaries

Package structure and import rules after the runtime refactor.

## Package layout

```text
src/medre/
  cli/            argparse, command dispatch, I/O formatting
  runtime/        builder, app, route engine, smoke/drill/trace
  core/           event model, storage, pipeline, routing, rendering
    observability/  logging setup, diagnostic events, metrics
  observability/  user-facing: sanitization, adapter loggers, summaries
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
`snapshot.py`, `boot_summary.py`, `errors.py`.

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
`sample.py`.

## Import rules

| From                    | May import                                                | Must not import                             |
| ----------------------- | --------------------------------------------------------- | ------------------------------------------- |
| `cli/` commands         | `config.*`, `observability`, `runtime.builder`            | Adapter implementations, `core.*` internals |
| `runtime/builder`       | `core.contracts.adapter`, `config.model`, `core.*`        | Specific adapter SDK modules                |
| `runtime/observability` | `core.diagnostics`, `core.routing.stats`                  | Adapter code                                |
| `core/*`                | Other `core/*` sub-packages                               | `adapters.*`, `runtime.*`, `cli.*`          |
| `adapters/<transport>/` | `core.contracts.adapter`, `core.events`, `core.rendering` | Other adapter packages, `runtime.*`         |
| `config/`               | `pathlib`, stdlib only                                    | `core.*`, `adapters.*`, `runtime.*`         |

Key invariants:

- **CLI commands never import adapter implementations directly.** The `run`
  command calls `RuntimeBuilder` which handles adapter construction.
- **`RuntimeBuilder` is the single assembly point.** It is the only module that
  imports both config model types and adapter base classes to wire the system.
- **`core/` is transport-agnostic.** No module under `core/` imports from
  `adapters/` or `runtime/`.
- **Observability has two packages** — see "Observability: Two Packages" below.
- **Config package is dependency-free.** `config/` imports only stdlib — no
  core types, no adapter types.

## What was removed or moved

| Before                        | After                                                  | Reason                                                                      |
| ----------------------------- | ------------------------------------------------------ | --------------------------------------------------------------------------- |
| `runner.py` (top-level)       | Deleted                                                | Logic moved into `runtime/builder.py` and `runtime/app.py`                  |
| `cli.py` (monolithic)         | `cli/` package                                         | Split into per-command modules for maintainability                          |
| `_sanitize_error` (scattered) | `medre.core.observability.sanitization.sanitize_error` | Consolidated into core observability; re-exported via `medre.observability` |

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

## Observability: Two Packages

medre has two observability packages with distinct responsibilities and consumers.

### `medre.observability` — user-facing

Import path: `from medre.observability import ...`

Used by CLI commands, runtime orchestration, and adapter-facing code. This is the
public observability surface.

| Symbol               | Module          | Purpose                                                                                                                                                                                   |
| -------------------- | --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sanitize_error`     | `.sanitization` | Redact tokens/passwords from error strings, truncate to safe length. Implementation lives in `medre.core.observability.sanitization`; re-exported here.                                   |
| `sanitize_for_log`   | `.sanitization` | Canonical dict redaction path — strip secret keys from dicts, coerce values for structured log output. Implementation lives in `medre.core.observability.sanitization`; re-exported here. |
| `adapter_logger`     | `.logging`      | LoggerAdapter factory injecting `adapter_id` and `transport` context                                                                                                                      |
| `startup_summary`    | `.summaries`    | Multi-line startup summary string for the runtime                                                                                                                                         |
| `shutdown_summary`   | `.summaries`    | Multi-line shutdown summary string for the runtime                                                                                                                                        |
| `format_duration_ms` | `.summaries`    | Human-readable duration from monotonic timestamps                                                                                                                                         |

### `medre.core.observability` — framework-internal

Import path: `from medre.core.observability import ...`

Used by the pipeline, routing engine, and internal framework code. Not intended
for direct consumption by CLI or adapter code.

| Symbol                     | Module     | Purpose                                                               |
| -------------------------- | ---------- | --------------------------------------------------------------------- |
| `setup_logging`            | `.logging` | Configure the root `medre` logger (handler, level, JSON format)       |
| `get_logger`               | `.logging` | Obtain a child logger in the `medre.*` namespace                      |
| `diagnostic_event`         | `.logging` | Emit structured diagnostic log entries with category and context      |
| `log_route_matched`        | `.logging` | Log route match event                                                 |
| `log_route_delivered`      | `.logging` | Log successful route delivery                                         |
| `log_route_failed`         | `.logging` | Log failed route delivery (sanitizes error via `medre.observability`) |
| `log_route_loop_prevented` | `.logging` | Log loop-prevention skip                                              |
| `EventMetrics`             | `.metrics` | Per-stage event counters with snapshot support                        |
| `RouteMetrics`             | `.metrics` | Per-route delivery counters with snapshot support                     |
| `Diagnostician`            | `.metrics` | Structured failure and diagnostic event recorder                      |

### Boundary rules

- `medre.observability` must not import from `medre.core.observability`.
  **Exception:** `medre.observability.sanitization` is a documented re-export
  from `medre.core.observability.sanitization`. The `medre.observability`
  package MAY import specific symbols from `medre.core.observability.sanitization`
  for the sole purpose of maintaining the user-facing `medre.observability.sanitization`
  public API. No other cross-boundary imports from `medre.core.observability`
  are permitted.
- `medre.core.observability` may import from `medre.observability` (e.g.
  `log_route_failed` delegates sanitization to `medre.observability.sanitize_error`,
  and `core.observability.logging` delegates dict redaction to
  `medre.observability.sanitize_for_log`).
- CLI and adapter code import from `medre.observability`. Pipeline and routing
  internals import from `medre.core.observability`.
- No duplicate APIs: each symbol lives in exactly one package.
  Implementation lives in `medre.core.observability.sanitization`; re-exported
  via `medre.observability.sanitization` for user-facing import.

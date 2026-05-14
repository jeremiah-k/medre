# Module Boundaries

Package structure and import rules after the runtime refactor.

## Package layout

```
src/medre/
  cli/            argparse, command dispatch, I/O formatting
  runtime/        builder, app, route engine, smoke/drill/trace
  core/           event model, storage, pipeline, routing, rendering
    observability/  logging setup, sanitization, metrics
  adapters/       base class + per-transport packages (matrix/, meshtastic/, meshcore/, lxmf/)
  config/         loader, model, env overrides, paths, sample generation
  logging.py      consolidated structured logging helpers (single source of truth)
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

Owns adapter assembly, lifecycle management, and operational tooling.

**Contents:** `builder.py` (`RuntimeBuilder`), `app.py` (`MedreApp`),
`route_engine.py`, `routes.py`, `observability.py` (diagnostics collector),
`events.py`, `evidence.py`, `trace.py`, `drill.py`, `smoke.py`,
`snapshot.py`, `boot_summary.py`, `capacity.py`, `errors.py`.

### `core/` — domain primitives

Transport-agnostic building blocks. No adapter or SDK imports.

**Sub-packages:** `events/` (bus, canonical event, schema, kinds),
`storage/`, `rendering/`, `routing/`, `planning/`, `policies/`,
`engine/` (pipeline runner), `diagnostics/`, `identity/`, `lifecycle/`,
`transforms/`, `observability/` (logging setup, metrics).

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

| From | May import | Must not import |
|------|-----------|-----------------|
| `cli/` commands | `config.*`, `logging`, `runtime.builder` | Adapter implementations, `core.*` internals |
| `runtime/builder` | `adapters.base`, `config.model`, `core.*` | Specific adapter SDK modules |
| `runtime/observability` | `core.diagnostics`, `core.routing.stats` | Adapter code |
| `core/*` | Other `core/*` sub-packages | `adapters.*`, `runtime.*`, `cli.*` |
| `adapters/<transport>/` | `adapters.base`, `core.events`, `core.rendering` | Other adapter packages, `runtime.*` |
| `config/` | `pathlib`, stdlib only | `core.*`, `adapters.*`, `runtime.*` |

Key invariants:

- **CLI commands never import adapter implementations directly.** The `run`
  command calls `RuntimeBuilder` which handles adapter construction.
- **`RuntimeBuilder` is the single assembly point.** It is the only module that
  imports both config model types and adapter base classes to wire the system.
- **`core/` is transport-agnostic.** No module under `core/` imports from
  `adapters/` or `runtime/`.
- **Logging utilities come from `medre.logging`** (top-level module) or
  `medre.core.observability`. These are the only sanctioned logging helpers.
- **Config package is dependency-free.** `config/` imports only stdlib — no
  core types, no adapter types.

## What was removed or moved

| Before | After | Reason |
|--------|-------|--------|
| `runner.py` (top-level) | Deleted | Logic moved into `runtime/builder.py` and `runtime/app.py` |
| `cli.py` (monolithic) | `cli/` package | Split into per-command modules for maintainability |
| `_sanitize_error` (scattered) | `medre.logging.sanitize_for_log` | Consolidated into single logging module |

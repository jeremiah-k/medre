# Embedding MEDRE as a Library

This runbook describes how to embed MEDRE inside a host application — constructing the runtime programmatically, loading configuration from files, starting and stopping the app, and accessing diagnostics. It covers the "library" usage path, as opposed to running MEDRE as a standalone service via `medre run`.


## Dual Role

MEDRE serves two roles:

1. **Importable toolkit** — you import `RuntimeBuilder`, `RuntimeConfig`, and `MedrePaths` directly, construct a runtime in-process, and drive its lifecycle from your own async code. No TOML file is required.
2. **Optional runtime** — the `medre run` CLI and `medre.runner` module provide a standalone process that reads TOML, applies environment overrides, handles signals, and runs the full lifecycle. This path is for operators.

Both paths produce the same `MedreApp` object via the same `RuntimeBuilder`. The difference is solely in how `RuntimeConfig` and `MedrePaths` are obtained.


## Importing RuntimeBuilder

The core embedding API lives in three modules:

```python
from medre.runtime.builder import RuntimeBuilder
from medre.config.model import RuntimeConfig, RuntimeOptions, StorageConfig, AdapterConfigSet
from medre.config.paths import MedrePaths
```

Additional imports you will typically need:

```python
from medre.config.model import (
    MatrixRuntimeConfig,
    MeshtasticRuntimeConfig,
    MeshCoreRuntimeConfig,
    LxmfRuntimeConfig,
)
from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.meshtastic.config import MeshtasticConfig
from medre.adapters.meshcore.config import MeshCoreConfig
from medre.adapters.lxmf.config import LxmfConfig
```

The adapter config classes (`MatrixConfig`, `MeshtasticConfig`, etc.) are frozen dataclasses that hold transport-specific settings. Each `XxxRuntimeConfig` wraps one adapter config and adds runtime-level fields (`enabled`, `adapter_id`, `adapter_kind`).


## Programmatic Config

You can construct `RuntimeConfig` entirely in code — no TOML file needed. This is the path for test harnesses, integration tests, and applications that configure MEDRE from their own config source.

The key pieces:

- `RuntimeConfig` is a frozen dataclass with fields: `runtime`, `logging`, `storage`, `adapters`, `routes`.
- `AdapterConfigSet` groups adapter configs by transport: `matrix`, `meshtastic`, `meshcore`, `lxmf`.
- Each transport group is a `dict[str, XxxRuntimeConfig]` keyed by instance name.
- `adapter_kind="fake"` selects a fake adapter (no real SDK required).
- `StorageConfig(backend="memory")` gives you an in-memory SQLite database — no filesystem writes.
- `MedrePaths` resolves filesystem layout. For in-memory/test use, any valid directory works.

```python
config = RuntimeConfig(
    runtime=RuntimeOptions(name="test-runtime", shutdown_timeout_seconds=5),
    storage=StorageConfig(backend="memory"),
    adapters=AdapterConfigSet(
        matrix={"bot": MatrixRuntimeConfig(
            adapter_id="bot",
            enabled=True,
            adapter_kind="fake",
            config=None,
        )},
        meshtastic={"radio": MeshtasticRuntimeConfig(
            adapter_id="radio",
            enabled=True,
            adapter_kind="fake",
            config=None,
        )},
    ),
)
```


## Loading TOML Config

For the operator path, load configuration from a TOML file using the config loader:

```python
from medre.config.loader import load_config

config, source, paths = load_config("/path/to/config.toml")
```

`load_config` returns:

- `config` — a fully-validated `RuntimeConfig`.
- `source` — a `ConfigSource` enum indicating where the file was found.
- `paths` — a `MedrePaths` instance resolved from the config location.

You can also use `find_config()` to search the standard config path chain, or pass `None` to let the loader search automatically.

Environment variable overrides can be applied separately:

```python
from medre.config.env import apply_env_overrides

config = apply_env_overrides(config, paths)
```


## Building the Runtime

`RuntimeBuilder` takes a `RuntimeConfig` and `MedrePaths` and produces a fully wired `MedreApp`:

```python
builder = RuntimeBuilder(config, paths)
app = builder.build()
```

The builder constructs all subsystems in order (see `builder.py` docstring):

1. `EventBus` — central async pub/sub
2. `RenderingPipeline` — with a default `TextRenderer`
3. `Router` — empty route table
4. `FallbackResolver` — capability degradation
5. `SQLiteStorage` — using resolved database path
6. `Diagnostician` — metrics and diagnostics
7. `RelationResolver` — cross-adapter event linking
8. `PipelineConfig` / `PipelineRunner` — orchestration
9. Adapters — constructed from enabled adapter configs
10. `asyncio.Event` — shutdown signal

After `build()`, the app is **constructed but not started**. Check `app.build_failures` for any adapters that failed during construction.

Routes are registered automatically from `config.routes` during the build step. No separate route registration call is needed.


## Starting and Stopping

### Starting

`app.start()` is an async method that:

1. Creates required directories.
2. Initialises storage (SQLite).
3. Starts the pipeline runner.
4. Starts adapters in deterministic order (sorted by adapter_id).

```python
await app.start()
```

Individual adapter start failures are collected and logged but do not abort other adapters. The first adapter error is re-raised after all adapters have been attempted. Core subsystem failures (storage, pipeline runner) trigger immediate cleanup.

### Stopping

`app.stop()` is an async method that:

1. Sets the shutdown event.
2. Stops adapters in **reverse start order**.
3. Stops the pipeline runner.
4. Closes storage.

```python
await app.stop()
```

The shutdown timeout is configured via `RuntimeOptions.shutdown_timeout_seconds` (default: 10 seconds). Adapters that exceed the timeout are logged and skipped.

### Waiting for Shutdown

```python
await app.wait_for_shutdown()
```

Blocks until the shutdown event is set (e.g., by signal handling or explicit `app.shutdown_event.set()`). An optional `timeout` parameter is available.


## Accessing Diagnostics

### Route Stats

`app.route_stats` provides per-route delivery counters (`RouteStats`). It tracks delivered, failed, skipped, and loop-prevented counts per route.

```python
if app.route_stats is not None:
    stats = app.route_stats.snapshot()
    # stats is a dict mapping route_id → RouteMetrics dataclass
```

### Diagnostician

`app.diagnostician` records structured failure events from the pipeline:

```python
diag = app.diagnostician.snapshot()
# Returns dict with keys:
#   planner_failures, renderer_failures, storage_failures,
#   adapter_failures, replay_skips, replay_downgrades,
#   correlation_misses
```

### Per-Adapter Health

Each adapter in `app.adapters` exposes `health_check()` and `diagnostics()` methods. These are transport-specific and return plain dicts.

### Build Failures

If any adapters failed during construction (before start), they are recorded:

```python
for failure in app.build_failures:
    print(f"  {failure.transport}.{failure.adapter_id}: {failure.error}")
```


## Examples

### Example 1 — Programmatic Fake Multi-Adapter Runtime

Build a runtime with two fake adapters and in-memory storage — no files, no TOML, no real SDKs:

```python
import asyncio
from medre.runtime.builder import RuntimeBuilder
from medre.config.model import (
    RuntimeConfig, RuntimeOptions, StorageConfig, AdapterConfigSet,
    MatrixRuntimeConfig, MeshtasticRuntimeConfig,
)
from medre.config.paths import MedrePaths

async def main():
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="test", shutdown_timeout_seconds=5),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={"bot": MatrixRuntimeConfig(
                adapter_id="bot", enabled=True,
                adapter_kind="fake", config=None,
            )},
            meshtastic={"radio": MeshtasticRuntimeConfig(
                adapter_id="radio", enabled=True,
                adapter_kind="fake", config=None,
            )},
        ),
    )
    paths = MedrePaths.resolve(medre_home="/tmp/medre-test")
    app = RuntimeBuilder(config, paths).build()
    await app.start()
    try:
        # Use the runtime: send events, inspect diagnostics, etc.
        pass
    finally:
        await app.stop()

asyncio.run(main())
```

### Example 2 — Load Config from TOML and Build App

```python
import asyncio
from medre.config.loader import load_config
from medre.runtime.builder import RuntimeBuilder

async def main():
    config, source, paths = load_config("medre.toml")
    print(f"Config loaded from: {source}")
    app = RuntimeBuilder(config, paths).build()
    if app.build_failures:
        for f in app.build_failures:
            print(f"Build failed: {f.transport}.{f.adapter_id}: {f.error}")
    await app.start()
    await app.stop()

asyncio.run(main())
```

### Example 3 — Embed in Async Host Application

A longer-lived host application that starts MEDRE, runs its own work, and shuts down gracefully:

```python
import asyncio
import signal
from medre.runtime.builder import RuntimeBuilder
from medre.config.model import (
    RuntimeConfig, RuntimeOptions, StorageConfig, AdapterConfigSet,
    MatrixRuntimeConfig,
)
from medre.config.paths import MedrePaths

async def host_application(app):
    """Simulate a host application doing its own work."""
    while not app.shutdown_event.is_set():
        await asyncio.sleep(1)

async def main():
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="embedded", shutdown_timeout_seconds=10),
        storage=StorageConfig(backend="sqlite"),
        adapters=AdapterConfigSet(
            matrix={"bot": MatrixRuntimeConfig(
                adapter_id="bot", enabled=True,
                adapter_kind="fake", config=None,
            )},
        ),
    )
    paths = MedrePaths.resolve(medre_home="/tmp/medre-embedded")
    app = RuntimeBuilder(config, paths).build()
    await app.start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, app.shutdown_event.set)

    await host_application(app)
    await app.stop()

asyncio.run(main())
```


## Stability Expectations

### What Is Stable-ish Now

- `RuntimeBuilder(config, paths).build()` → `MedreApp` — the core build-then-start pattern.
- `MedreApp.start()` / `MedreApp.stop()` / `MedreApp.wait_for_shutdown()` — lifecycle methods.
- `RuntimeConfig` frozen dataclass hierarchy — field names and types.
- `MedrePaths.resolve()` — path resolution API.
- `AdapterConfigSet` with `adapter_kind="fake"` — test-only adapters are always available.
- Startup ordering: adapters sorted by adapter_id, shutdown in reverse.
- `app.diagnostician.snapshot()` and `app.route_stats.snapshot()` — diagnostic access.

### What Is Internal

- `PipelineConfig` and `PipelineRunner` internals — these are wired by the builder, not intended for direct construction.
- `EventBus` subscription API — used internally by the pipeline.
- `AdapterContext` construction — created inside `MedreApp.start()`, not exposed to embedders.
- Per-adapter `start(ctx)` / `stop()` method signatures — these are adapter-internal.

### What May Change Before Release

- `RouteConfigSet` shape and route registration API — route configuration is still evolving.
- `Diagnostician` and `RouteStats` field names — counters may be added or renamed.
- `MedreApp` dataclass fields — new subsystems may be added, some may be reorganized.
- Adapter config dataclass fields per transport — each transport's config is transport-specific and may grow.
- Error types and exception hierarchy — `RuntimeStartupError`, `AdapterStartupError`, etc. may gain fields or subtypes.

This is a pre-release codebase. Do not depend on internal module paths, dataclass field layouts, or private methods remaining unchanged across minor versions. The embedding API (`RuntimeBuilder` → `build()` → `start()` / `stop()`) is the intended stable surface.

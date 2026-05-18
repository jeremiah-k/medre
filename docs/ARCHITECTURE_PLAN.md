# MEDRE Architecture Refactor Plan

> **Status**: Review-ready synthesis of four research tracks.
> **Audience**: Project coordinator deciding the next implementation tranche.
> **Constraint**: medre is unreleased. Prefer clean moves over compatibility facades.

---

## 0. Canonical Architecture (Current)

> **This section supersedes any earlier sections that reference old module
> paths.**  The project is pre-release and old imports were intentionally
> removed in favor of clean canonical ownership.

### 0.1 Core Adapter Contracts

Core adapter contracts live in **`medre.core.contracts.adapter`**:

```python
from medre.core.contracts.adapter import AdapterContract
from medre.core.contracts.adapter import AdapterRole
from medre.core.contracts.adapter import AdapterCodec
from medre.core.contracts.adapter import AdapterContext
from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.contracts.adapter import AdapterInfo
from medre.core.contracts.adapter import AdapterDeliveryResult
from medre.core.contracts.adapter import AdapterSendError
from medre.core.contracts.adapter import AdapterPermanentError
```

- `AdapterContract` replaces the old `BaseAdapter` name.
- `medre.core.contracts` re-exports all of the above from its `__init__.py`.
- The following old modules **do not exist** and must not be imported:
  - `medre.core.ports`
  - `medre.core.adapter_base`
  - `medre.adapters.base`

### 0.2 Adapter Configuration

Adapter config dataclasses live in **`medre.config.adapters.*`**:

```python
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.lxmf import LxmfConfig
```

- The following old modules **do not exist** and must not be imported:
  - `medre.adapters.matrix.config`
  - `medre.adapters.meshtastic.config`
  - `medre.adapters.meshcore.config`
  - `medre.adapters.lxmf.config`

### 0.3 Adapter Config Validation Errors

Config validation errors live in **`medre.config.adapters.errors`**:

```python
from medre.config.adapters.errors import AdapterConfigError
from medre.config.adapters.errors import MatrixConfigError
from medre.config.adapters.errors import MeshtasticConfigError
from medre.config.adapters.errors import MeshCoreConfigError
from medre.config.adapters.errors import LxmfConfigError
```

- Config errors are `ValueError` subclasses, not adapter runtime errors.
- Runtime adapter errors (session, network, protocol) remain in
  `medre.adapters.*.errors`.

### 0.4 Matrix Credential Sidecar

Matrix credential sidecar helpers live in
**`medre.config.adapters.matrix_credentials`**:

```python
from medre.config.adapters.matrix_credentials import get_credentials_path
from medre.config.adapters.matrix_credentials import load_credentials_json
```

### 0.5 Layer Dependencies

| Layer | May Import From | Must Not Import From |
|---|---|---|
| `medre.core` | `medre.core` only (documented exceptions: `observability/sanitization`) | `medre.adapters`, `medre.config` |
| `medre.config` | `medre.config` (including `config.adapters`) | `medre.adapters` |
| `medre.adapters` | `medre.core.contracts.adapter`, `medre.config.adapters`, `medre.core.*` | — |

- Concrete adapters depend inward on core contracts and config models.
- `medre.adapters` packages own concrete implementations only — no config
  dataclasses, no config errors, no core contract re-exports.

---

## 1. Current State Summary

### 1.1 Package Tree (as-is)

```
medre/
├── __init__.py              # empty
├── __main__.py              # delegates to cli:main
├── py.typed
├── adapters/                # concrete adapter implementations only
│   ├── __init__.py          # re-exports all fakes (no BaseAdapter/AdapterContract re-export)
│   ├── fake_lxmf.py
│   ├── fake_matrix.py
│   ├── fake_meshcore.py
│   ├── fake_meshtastic.py
│   ├── fake_presentation.py
│   ├── fake_transport.py
│   ├── matrix/              # 10 modules (adapter, auth, cli, codec, compat,
│   │                        #            errors, metadata, relations, renderer, session)
│   ├── meshtastic/          # 9 modules (no config.py)
│   ├── lxmf/                # 9 modules (no config.py)
│   └── meshcore/            # 8 modules (no config.py)
├── cli/                     # 18 command modules + main + __main__
├── config/
│   ├── __init__.py          # PEP 562 deferred-import dict (_DEFERRED)
│   ├── adapters/            # adapter config models + errors + matrix credentials
│   │   ├── __init__.py
│   │   ├── errors.py        # AdapterConfigError hierarchy (not in adapter error modules)
│   │   ├── matrix.py        # MatrixConfig dataclass
│   │   ├── matrix_credentials.py  # get_credentials_path, load_credentials_json
│   │   ├── meshtastic.py    # MeshtasticConfig dataclass
│   │   ├── meshcore.py      # MeshCoreConfig dataclass
│   │   └── lxmf.py          # LxmfConfig dataclass
│   ├── env.py
│   ├── errors.py
│   ├── loader.py
│   ├── model.py             # imports adapter config dataclasses
│   ├── paths.py
│   └── sample.py
├── core/
│   ├── contracts/           # adapter runtime contracts (canonical)
│   │   ├── __init__.py      # re-exports AdapterContract, AdapterRole, etc.
│   │   └── adapter.py       # AdapterContract (was BaseAdapter), AdapterRole, AdapterCodec,
│   │                        # AdapterContext, AdapterCapabilities, AdapterInfo,
│   │                        AdapterDeliveryResult, AdapterSendError, AdapterPermanentError
│   ├── diagnostics/         # replay_metrics, snapshot
│   ├── engine/              # pipeline.py (orchestration, ~1300 lines)
│   ├── events/              # bus, canonical, kinds, metadata, schema
│   ├── identity/            # actor, resolver
│   ├── lifecycle/           # states, manager
│   ├── observability/       # logging (uses observability/sanitization), metrics (Diagnostician)
│   ├── planning/            # delivery_plan, fallback_resolution, relation_resolution
│   ├── policies/            # empty
│   ├── rendering/           # renderer, text
│   ├── routing/             # models, router, stats
│   ├── runtime/             # accounting, capabilities, diagnostic_contract,
│   │                        # diagnostics, health, supervision
│   ├── storage/             # backend, replay, sqlite
│   └── transforms/          # empty
├── interop/                 # mmrelay wire-format constants
├── observability/           # classification, logging, sanitization, summaries
├── plugins/                 # scaffolding only: Plugin protocol, PluginCapability enum,
│   │                        # validate_plugin_payload (in __init__.py)
│   └── __init__.py
└── runtime/                 # app, builder, capacity, retry, routes, route_engine,
                             # boot_summary, drill, smoke, snapshot, timeline, trace,
                             # errors, events, observability, docker_bridge_artifacts,
                             # evidence/, run_session/
```

### 1.2 Strengths to Preserve

| Capability | medre | mmrelay |
|---|---|---|
| Adapter abstraction | `AdapterContract` protocol + `AdapterCodec` | Inline utility classes |
| Event model | `CanonicalEvent` msgspec struct, frozen | Dict-based payloads |
| Rendering boundary | `RenderingPipeline` protocol; adapters never render | Formatting mixed into relay logic |
| Routing engine | `Router` with `RouteSource`/`RouteTarget` models | Hardcoded room/channel map |
| Event bus | Async pub/sub with middleware chain | Direct callback invocation |
| Planning layer | `DeliveryPlan`, `FallbackResolver`, `RelationResolver` | None |
| Storage | `StorageBackend` protocol + SQLite + replay engine | SQLite for node info only |
| Identity | `CanonicalActor` + `NativeIdentity` + verification levels | None |
| Lifecycle | State machine (`INITIALIZED -> STARTING -> RUNNING -> ...`) | None formalized |
| Retry | Exponential backoff + lineage + dead-letter + `RetryWorker` | None |
| Schema versioning | `SchemaRegistry` + migration registry | None |
| Diagnostics | `Diagnostician`, `RouteStats`, `ReplayMetrics`, evidence bundles | None |
| Capacity control | Semaphore-based `CapacityController` | None |

---

## 2. Problems Identified

### 2.1 Dependency Inversion Violation (Resolved)

**Tranche 3 (current) fix applied**: Core adapter contracts now live in
`medre.core.contracts.adapter`.  `BaseAdapter` has been renamed to
`AdapterContract`.  The old `core/ports.py`, `core/adapter_base.py`, and
`adapters/base.py` have been removed.  All source and test imports updated
to use `medre.core.contracts.adapter`.

**Tranche 1 fix applied**: `core` modules now import from `core/ports.py` and `core/adapter_base.py` instead of `adapters/base.py`.

**Tranche 2 fix applied**: Config decoupling — adapter config dataclasses moved from `medre.adapters.*.config` to `medre.config.adapters.*` so the global config layer no longer imports concrete adapter packages at module level.

### 2.2 Dual Observability

Two `logging.py` files with different responsibilities:
- `core/observability/logging.py` (structured logging, diagnostic events, imports `sanitize_for_log` from `observability/sanitization.py`)
- `observability/logging.py` (adapter-scoped logger factory, 53 lines)

The dependency `core/observability/ -> observability/` goes from core outward, which is backwards. The test `TestCoreLoggingImportsCanonicalSanitizer` (in `test_operational_boundaries.py`) enforces that core logging uses the canonical sanitizer rather than defining its own.

There is a second core->observability dependency: `core/routing/stats.py` imports `sanitize_error` from `observability/sanitization.py` (line 17). Both exceptions target the same module (`observability/sanitization.py`) and both import pure functions with no I/O or SDK coupling. See Section 3.3 for the full documented-exceptions table.

### 2.3 Fragmented Diagnostics

Three locations for observability-adjacent code:
- `core/observability/` (Diagnostician, logging)
- `core/diagnostics/` (ReplayMetrics, snapshot builder)
- `observability/` (classification, logging, sanitization, summaries)

### 2.4 Naming Collision: `core/runtime/` vs `runtime/`

`core/runtime/` contains pure domain types (accounting counters, health enums, supervision classifiers). `runtime/` contains the actual orchestration implementation. The collision confuses contributors.

### 2.5 Empty Packages

`core/policies/` and `core/transforms/` are empty packages that add noise.

### 2.6 Fake Adapters in Production Package

6 `fake_*.py` files (plus `FaultyPresentationAdapter`) live alongside real adapters in `adapters/`. They are test doubles used by `runtime/drill.py` and `runtime/smoke.py`. Having them loose in the adapter package clutters the import surface. `adapters/__init__.py` re-exports all of them (lines 274-289 of `test_packaging_and_install_contract.py` test this directly).

### 2.7 Config to Adapter Config Dependency

`config/model.py` imports from all four `adapters/*/config.py` modules. This is a config-time dependency on value types only (no I/O, no SDK imports). `config/__init__.py` uses PEP 562 deferred imports (`_DEFERRED` dict) to keep lightweight CLI paths SDK-free. Any module path changes must be reflected in this dict.

---

## 3. Target Architecture

### 3.1 Layer Model

```
                     +---------------------------+
                     |         cli/              |  Presentation
                     |   (commands, entry)       |
                     +------------+--------------+
                                  |  (arrows = depends-on direction,
                     +------------v--------------+   upper layers depend on lower)
                     |       runtime/             |  Construction + Orchestration
                     |  (builder, app, retry,     |
                     |   routes, evidence)        |
                     +------------+--------------+
                                  |
               +------------------+------------------+
               |                  |                  |
     +---------v--------+ +------v-------+ +--------v--------+
     |   adapters/       | |   config/    | |  observability/  |
     |  (implementations)| |  (models)    | |  (logging,       |
     |                   | |              | |   summaries)      |
     +---------+---------+ +------+-------+ +--------+---------+
               |                  |                  |
               +------------------+------------------+
                                  |
                     +------------v--------------+
                     |        core/              |  Domain Layer
                     |  (events, routing,        |  Aspirational: zero external deps
                     |   planning, rendering,    |  Documented exceptions: 2 imports
                     |   identity, storage,      |  from observability/sanitization
                     |   ports, adapter_base,    |  (pure functions, no SDK/I/O)
                     |   supervision)            |
                     +---------------------------+
```

### 3.2 Target Package Tree

```
medre/
├── __init__.py
├── __main__.py
├── py.typed
│
├── core/                                # INNERMOST -- aspirational zero external deps
│   │                                    #   (2 documented exceptions to observability/sanitization)
│   ├── __init__.py
│   ├── contracts/                       # adapter runtime contracts (canonical)
│   │   ├── __init__.py                  # re-exports AdapterContract, AdapterRole, etc.
│   │   └── adapter.py                   # AdapterContract, AdapterRole, AdapterCodec,
│   │                                    # AdapterContext, AdapterCapabilities, AdapterInfo,
│   │                                    # AdapterSendError, AdapterPermanentError,
│   │                                    # AdapterDeliveryResult
│   ├── engine/
│   │   └── pipeline.py                  # imports core.contracts.adapter
│   ├── events/
│   │   ├── bus.py
│   │   ├── canonical.py
│   │   ├── kinds.py
│   │   ├── metadata.py
│   │   └── schema.py
│   ├── identity/
│   │   ├── actor.py
│   │   └── resolver.py
│   ├── lifecycle/
│   │   ├── manager.py
│   │   └── states.py
│   ├── observability/                   # domain observability interfaces
│   │   ├── metrics.py                   # Diagnostician (unchanged)
│   │   └── logging.py                   # structured logging (unchanged position)
│   ├── planning/
│   │   ├── delivery_plan.py             # imports core.contracts.adapter
│   │   ├── fallback_resolution.py
│   │   └── relation_resolution.py
│   ├── rendering/
│   │   ├── renderer.py
│   │   └── text.py
│   ├── routing/
│   │   ├── models.py
│   │   ├── router.py
│   │   └── stats.py
│   ├── storage/
│   │   ├── backend.py
│   │   ├── replay.py
│   │   └── sqlite.py
│   ├── runtime/                           # domain types (a.k.a. supervision)
│   │   ├── accounting.py
│   │   ├── capabilities.py              # imports core.contracts.adapter (not adapters.base)
│   │   ├── diagnostic_contract.py
│   │   ├── diagnostics.py
│   │   ├── health.py                    # imports core.contracts.adapter (not adapters.base)
│   │   └── supervision.py
│   └── diagnostics/                     # MERGED into core/observability/diagnostics.py
│       (deleted)                         # in Tranche 2 if PC approves
│
├── adapters/                            # depends on core only
│   ├── __init__.py                      # re-exports fakes only
│   ├── fakes/                           # consolidated test doubles
│   │   ├── __init__.py
│   │   ├── fake_lxmf.py
│   │   ├── fake_matrix.py
│   │   ├── fake_meshcore.py
│   │   ├── fake_meshtastic.py
│   │   ├── fake_presentation.py
│   │   └── fake_transport.py
│   ├── matrix/                          # concrete implementation, no config.py shim
│   ├── meshtastic/                      # concrete implementation, no config.py shim
│   ├── lxmf/                            # concrete implementation, no config.py shim
│   └── meshcore/                        # concrete implementation, no config.py shim
│
├── config/                              # owns adapter config models and errors
│   ├── __init__.py                      # _DEFERRED dict updated for any path changes
│   ├── adapters/                        # adapter config dataclasses + errors + credentials
│   │   ├── __init__.py
│   │   ├── errors.py                    # AdapterConfigError hierarchy
│   │   ├── matrix.py                    # MatrixConfig dataclass
│   │   ├── matrix_credentials.py        # get_credentials_path, load_credentials_json
│   │   ├── meshtastic.py                # MeshtasticConfig dataclass
│   │   ├── meshcore.py                  # MeshCoreConfig dataclass
│   │   └── lxmf.py                      # LxmfConfig dataclass
│   ├── env.py
│   ├── errors.py
│   ├── loader.py
│   ├── model.py
│   ├── paths.py
│   └── sample.py
│
├── observability/                       # infrastructure observability
│   ├── __init__.py
│   ├── classification.py
│   ├── logging.py                       # adapter-scoped logger factory (unchanged)
│   ├── sanitization.py                  # PII stripping (canonical source)
│   └── summaries.py
│
├── interop/                             # external wire-format constants
│   └── mmrelay.py
│
├── plugins/                             # scaffolding only (unchanged)
│   └── __init__.py                      # Plugin protocol, PluginCapability, validate_plugin_payload
│
└── runtime/                             # construction + orchestration
    ├── app.py
    ├── boot_summary.py
    ├── builder.py
    ├── capacity.py
    ├── docker_bridge_artifacts.py
    ├── drill.py
    ├── errors.py
    ├── events.py
    ├── evidence/
    ├── observability.py
    ├── retry.py
    ├── route_engine.py
    ├── routes.py
    ├── run_session/
    ├── smoke.py
    ├── snapshot.py
    ├── timeline.py
    └── trace.py
```

### 3.3 Import Direction Rules

```
cli/           ->  runtime/, config/, core/, observability/
runtime/       ->  core/, adapters/, config/, observability/
adapters/      ->  core/ (contracts.adapter + domain types), config/adapters.*, observability/
config/        ->  config/adapters/ (owned adapter config value types + errors + credentials)
observability/ ->  core/ (types only)
plugins/       ->  core/ (types only)
interop/       ->  NOTHING (pure constants)
core/          ->  core/ ONLY (aspirational; documented exceptions below)
```

**Hard rule**: `core/` MUST NOT import from `adapters/`, `config/`, `cli/`, or top-level `runtime/` at runtime.

**Hard rule**: `config/` MUST NOT import from `adapters/`.

**Documented runtime exceptions** (core -> outside core):

| Source | Import | Reason | Resolution path |
|---|---|---|---|
| `core/observability/logging.py` | `sanitize_for_log` from `observability/sanitization.py` | Pure function, no I/O or SDK coupling | Resolve in T2: extract to core, accept documented tradeoff, or move logging out of core (Decision Point 3) |
| `core/routing/stats.py` | `sanitize_error` from `observability/sanitization.py` | Pure function, no I/O or SDK coupling | Resolve alongside T2: same options as above, or extract a shared core sanitizer |

Both exceptions import the same pure-function module (`observability/sanitization.py`). They are the only runtime core->external dependencies. All other core->external imports must be eliminated by T1. If a future code path needs to import from outside core, it must be documented here and approved before merging.

**Type-only coupling** (acceptable, no runtime dependency):

| Source | Import | Guard |
|---|---|---|
| `core/engine/pipeline.py` | `CapacityController` from `runtime.capacity` | `if TYPE_CHECKING:` block. Used only for type annotations. |
| `core/storage/replay.py` | `CapacityController` from `runtime.capacity` | `if TYPE_CHECKING:` block. Used only for type annotations. |

These `TYPE_CHECKING` imports do not create runtime dependencies. They exist because core modules type-hint against an orchestration-layer class that is injected at runtime. This is acceptable type-only coupling. If it becomes a maintenance burden, the type could be extracted to a `core/ports.py` protocol.

### 3.4 Package Ownership Matrix

| Package | Owns | Depends On |
|---|---|---|
| `core/` | Domain types, protocols, pure logic | **Nothing outside core/** (after T3), with documented exceptions: `core/observability/logging.py` imports `sanitize_for_log` from `observability/sanitization.py`; `core/routing/stats.py` imports `sanitize_error` from `observability/sanitization.py` |
| `core/contracts/adapter.py` | `AdapterContract` ABC, `AdapterRole`, `AdapterCodec`, `AdapterContext`, `AdapterCapabilities`, `AdapterInfo`, `AdapterSendError`, `AdapterPermanentError`, `AdapterDeliveryResult` | `core/` only (zero imports from outside core) |
| `core/engine/` | Pipeline orchestration | `core/contracts/adapter`, `core/events`, `core/routing`, `core/planning`, `core/rendering`, `core/storage`, `core/observability`; TYPE_CHECKING import of `runtime.capacity.CapacityController` (type-only coupling) |
| `adapters/*` | Transport/platform implementations | `core/contracts/adapter`, `config/adapters.*`, `core/events`, `core/rendering`, `observability/` |
| `adapters/fakes/` | Test doubles for smoke/drill | Same as adapters |
| `config/` | Configuration loading and validation | `config/adapters/` (owned adapter config value types + errors + credentials); no imports from `adapters/*` |
| `config/adapters/errors.py` | `AdapterConfigError`, `MatrixConfigError`, `MeshtasticConfigError`, `MeshCoreConfigError`, `LxmfConfigError` | `ValueError` only |
| `config/adapters/matrix_credentials.py` | `get_credentials_path`, `load_credentials_json` | stdlib only |
| `observability/` | Logging, sanitization, classification, summaries | `core/` (types only) |
| `interop/` | External wire-format constants | **Nothing** |
| `plugins/` | Plugin protocol scaffolding | `core/events/` (for event types) |
| `runtime/` | Construction, orchestration, lifecycle | `core/`, `adapters/`, `config/`, `observability/` |
| `cli/` | User-facing commands | `runtime/`, `config/`, `core/` (types), `observability/` |

---

## 4. What We Are Deliberately NOT Copying from mmrelay

mmrelay is a useful reference project, but its architecture is flat and optimized for a single-transport relay with plugins. medre is a multi-transport routing engine with adapters. Copying mmrelay's patterns wholesale would be a step backward.

| mmrelay Pattern | Why We Reject It |
|---|---|
| **Facade re-export globals** (`_config`, `_relay`) | mmrelay uses module-level singletons mutated by `set_config()`. medre uses explicit dependency injection through `RuntimeBuilder`. Global mutable state is a testing hazard. |
| **`set_config()` global mutation** | Configuration is passed through constructors, not set on globals. This makes the system testable without monkeypatching module state. |
| **Cross-facade coupling** (relay imports config, config imports relay) | mmrelay's circular dependencies between config and relay are tolerable in a single-file relay but would be catastrophic in medre's layered architecture. |
| **Mechanical flattening** (all modules at root level) | mmrelay has ~40 files at root. medre has 9 top-level packages with clear layering. Flattening would destroy the dependency boundaries that the boundary tests enforce. |
| **`constants/` package as junk drawer** | mmrelay's `constants/` has 14 files mixing domain, adapter, and UI constants. medre places event kinds in `core/events/kinds.py`, adapter constants in their adapter packages, and CLI constants in `cli/transport_constants.py`. Each constant lives in the layer that owns it. |
| **`tools/` catch-all package** | mmrelay's `tools/` mixes sample configs, validation scripts, and utility functions. medre places operator tooling in `cli/`, sample configs in `config/sample.py` and `examples/`, and docker artifacts in `runtime/docker_bridge_artifacts.py`. These are contextually placed. |
| **`validation/` package** | Validation is context-specific: config validation in `config/errors.py`, route validation in `runtime/routes.py`, event schema validation in `core/events/schema.py`. A shared validation package would add abstraction without value and become a junk drawer. |
| **Dict-based event payloads** | mmrelay passes dicts everywhere. medre has `CanonicalEvent` (frozen msgspec struct) with schema versioning. This is a fundamental design advantage, not a candidate for change. |
| **Inline adapter construction** | mmrelay constructs adapters inline in the relay. medre uses `RuntimeBuilder` with `adapter_kind` dispatch, supporting both real and fake adapters without SDK imports. |

---

## 5. Refactor Tranche Sequencing

Tranches are ordered by risk (lowest first) and impact (highest first within risk tier). Each tranche is independently shippable.

### Tranche 0: Cleanups (Zero Behavioral Change)

**Objective**: Remove dead weight, organize test doubles. No logic changes.

**Estimated effort**: ~30 minutes.

| # | Action | Rationale |
|---|---|---|
| 0.1 | Delete `core/policies/` (empty `__init__.py` only) | Removes noise |
| 0.2 | Delete `core/transforms/` (empty `__init__.py` only) | Removes noise |
| 0.3 | Move `adapters/fake_*.py` (6 files) + `FaultyPresentationAdapter` to `adapters/fakes/` | Organizes test doubles |
| 0.4 | Update `adapters/__init__.py` to re-export from `adapters.fakes.*` | Preserves `from medre.adapters import FakeMatrixAdapter` |
| 0.5 | Update test imports referencing `medre.adapters.fake_*` | Direct path change in test files |

**Tests to update**:
- `test_packaging_and_install_contract.py` (imports FakeMatrixAdapter etc. from `medre.adapters` -- works if adapters/__init__.py re-exports)
- `test_architectural_boundaries.py` (imports `medre.adapters.fake_matrix`, `medre.adapters.fake_meshtastic` -- update import paths)
- `test_packaging_and_install_contract.py::TestFakeAdaptersNoTransitiveSDKImports` (imports `medre.adapters.fake_*` -- update paths)

**Verification**:
```bash
python -m pytest tests/ -x --timeout=60
grep -r "from medre\.adapters\.fake_" tests/ | grep -v fakes
# Should return zero (no imports of old fake paths)
```

**Risks**:
- `adapters/__init__.py` re-export list must be complete. If a fake is missed, downstream tests fail at import time.
- `test_packaging_and_install_contract.py::test_all_fakes_importable_from_adapters_init` specifically validates the re-exports.

**Rollback**: `git revert` the commit. Fake files are self-contained; no other code depends on their internal structure.

### Tranche 1: Port Extraction (High Impact, Medium Risk)

**Objective**: Break the `core -> adapters` dependency inversion. Extract adapter interface types into core, splitting pure value types from the behavioral `BaseAdapter` ABC.

**Estimated effort**: ~2 hours. This is the single most important change.

**Status**: SUPERSEDED by Tranche 3 — `core/ports.py` and `core/adapter_base.py` removed. All imports now target `medre.core.contracts.adapter`.

**Tranche 2 status**: SUPERSEDED by Tranche 3 — adapter config shims (`adapters/*/config.py`) removed. Config imports now target `medre.config.adapters.*` directly.

**Background**: The current dependency is bidirectional. `adapters/base.py` imports `CanonicalEvent` from `core.events.canonical` and `RenderingResult` from `core.rendering.renderer` (line 32-33), while four core files import types from `adapters.base`. The split extraction (Decision 6) breaks both directions of the coupling.

`BaseAdapter` is not a thin protocol. It is an ABC with concrete Template Method behavior:
- `publish_inbound()` wraps `ctx.publish_inbound` with a stale-event guard
- `_is_stale_event()` compares event timestamps against the adapter start time
- `_mark_started()` records the start time from the context clock
- `get_codec()` provides a default implementation returning `None`

These behavioral methods mean `BaseAdapter` cannot be treated as a pure value type. The extraction must account for this by placing it in a separate file from the pure port types.

| # | Action | Rationale |
|---|---|---|
| 1.1 | Create `core/ports.py` with pure value types from `adapters/base.py` | Types: `AdapterRole`, `AdapterCodec`, `AdapterContext`, `AdapterCapabilities`, `AdapterSendError`, `AdapterDeliveryResult`, `AdapterInfo`. No behavioral logic, no imports outside core. |
| 1.2 | Create `core/adapter_base.py` with `BaseAdapter` ABC | Moves Template Method class to core. Imports from `core/ports`, `core/events/canonical`, `core/rendering/renderer`. |
| 1.3 | Make `adapters/base.py` a thin re-export shim: imports from `core.ports` + `core.adapter_base` | Preserves all existing import paths during migration |
| 1.4 | Update `core/engine/pipeline.py` to import `BaseAdapter` from `core.adapter_base`, value types from `core.ports` | Breaks core->adapters dependency for pipeline |
| 1.5 | Update `core/runtime/capabilities.py` to import `AdapterCapabilities` from `core.ports` | Breaks core->adapters dependency for capabilities |
| 1.6 | Update `core/runtime/health.py` to import `AdapterInfo` from `core.ports` | Breaks core->adapters dependency for health |
| 1.7 | Update `core/planning/delivery_plan.py` to import `AdapterSendError` from `core.ports` | Breaks core->adapters dependency for delivery plan |
| 1.8 | Audit all `core/` imports: `grep -r "from medre\.adapters" src/medre/core/` | Must return zero hits |
| 1.9 | Update `adapters/__init__.py` re-exports to reference `core.ports` and `core.adapter_base` (optional) | Cleaner re-export chain |
| 1.10 | Update adapter implementations to import from `core.ports` / `core.adapter_base` (optional) | Preferred for consistency |

**Tests to update**:
- `test_cross_transport_boundaries.py::TestDeliveryContractBoundary` -- verifies `AdapterDeliveryResult` is in `adapters.base`. After T1, the re-export shim satisfies this. If PC wants the canonical location to move, this test needs updating.
- `test_cross_transport_boundaries.py::TestCodecBoundary` -- checks `medre.adapters.base` in codec import lines. Re-export shim keeps this passing.
- `test_beta_scope_boundaries.py::TestNoTransportSdkInRuntimeCore` -- `_CORE_MODULES` list does not include `core.ports` or `core.adapter_base` yet. Add `"medre.core.ports"` and `"medre.core.adapter_base"` to the parametrize list.
- `test_operational_boundaries.py::TestDiagnosticsNoTransportCoupling` -- `_DIAGNOSTICS_SOURCE_MODULES` includes `medre.core.runtime.capabilities` and `medre.core.runtime.health`, which no longer import from adapters. No test change needed (they already pass with the current imports).

**Verification**:
```bash
# Core must not import from adapters
grep -r "from medre\.adapters" src/medre/core/ | grep -v __pycache__
# Must return ZERO hits

# Verify new modules are importable
python -c "import medre.core.ports; import medre.core.adapter_base; print('OK')"

# Run boundary tests
python -m pytest tests/test_architectural_boundaries.py tests/test_cross_transport_boundaries.py \
    tests/test_beta_scope_boundaries.py tests/test_operational_boundaries.py -x --timeout=60
```

**Risks**:
- `adapters/base.py` may contain more than just the listed symbols (helper functions, constants). These must stay in `adapters/base.py`, not be blindly copied to core. Only the listed protocol/ABC/value-type symbols move.
- Circular import risk: `core/adapter_base.py` imports from `core/events/canonical.py` and `core/rendering/renderer.py`. If either of those imports from `core/adapter_base` or `core/ports`, we get a cycle. Audit carefully. `core/ports.py` imports nothing from outside core, so it cannot participate in a cycle.
- `BaseAdapter`'s `publish_inbound()` method references `CanonicalEvent` in its type signature. This coupling is intentional (the Template Method needs the event type) and stays within core.
- The `test_cross_transport_boundaries.py::TestAdapterRuntimeContainment` checks that adapters do not import `medre.core.runtime.diagnostics/health/capabilities`. After T1, these modules import from `core/ports` (not adapters), so the check still passes.

**Rollback**: Delete `core/ports.py` and `core/adapter_base.py`, restore `adapters/base.py` from git, revert the 4 core file import changes.

### Tranche 2: Observability and Diagnostics Consolidation (Medium Risk)

**Objective**: Consolidate fragmented diagnostics. Resolve or document the `core/observability -> observability/` dependency.

**Estimated effort**: ~1.5 hours.

This tranche has a decision point (see Section 7, Decision Point 3). The two sub-options are:

**Option A: Merge diagnostics, keep core/observability/ logging separate**

| # | Action | Rationale |
|---|---|---|
| 2.1 | Merge `core/diagnostics/replay_metrics.py` + `snapshot.py` into `core/observability/diagnostics.py` | Single diagnostics location within core |
| 2.2 | Delete `core/diagnostics/` directory | Follows from 2.1 |
| 2.3 | Keep `core/observability/logging.py` where it is | It depends on `observability/sanitization.py`. Accept the documented exception (Section 3.3). |
| 2.4 | Keep `core/routing/stats.py` importing `sanitize_error` from `observability/sanitization.py` | Same pure-function exception as logging.py. Accept the documented exception (Section 3.3). |
| 2.5 | Keep `observability/logging.py` where it is | Adapter-scoped logger factory; different responsibility. |

**Option B: Merge everything (aggressive)**

| # | Action | Rationale |
|---|---|---|
| 2.1 | Merge `core/observability/logging.py` into `observability/logging.py` | Single logging module |
| 2.2 | Merge `core/diagnostics/` into `core/observability/diagnostics.py` | Single diagnostics location |
| 2.3 | Delete `core/diagnostics/` and empty `core/observability/logging.py` | Cleanup |
| 2.4 | Move `core/runtime/health.py` to `core/observability/health.py` | Health is observability |

**Recommended**: Option A. It avoids creating core->observability dependencies (beyond the two already documented in Section 3.3) and respects the existing boundary test that enforces the canonical sanitizer import pattern. Option B requires either moving `sanitize_for_log` and `sanitize_error` into core (breaking the separation) or accepting core->external dependencies that violate the layer model.

**Tests to update (either option)**:
- `test_snapshot_schema_stability.py` -- imports from `medre.core.diagnostics.replay_metrics`, `medre.core.diagnostics.snapshot`. Update to `medre.core.observability.diagnostics`.
- `test_operational_boundaries.py` -- `_EVIDENCE_SOURCE_MODULES` includes `medre.core.diagnostics`, `medre.core.diagnostics.replay_metrics`, `medre.core.diagnostics.snapshot`. Update paths.
- `test_beta_scope_boundaries.py` -- `_CORE_MODULES` includes `medre.core.diagnostics`, `medre.core.diagnostics.replay_metrics`, `medre.core.diagnostics.snapshot`. Update paths.
- `test_beta_scope_boundaries.py` -- `_CORE_MODULES` includes `medre.core.observability.logging`, `medre.core.observability.metrics`. If Option B merges logging, remove the logging entry.

**Verification**:
```bash
test -d src/medre/core/diagnostics && echo "FAIL: core/diagnostics still exists" || echo "OK"
python -m pytest tests/test_snapshot_schema_stability.py tests/test_operational_boundaries.py \
    tests/test_beta_scope_boundaries.py -x --timeout=60
```

**Risks**:
- `test_snapshot_schema_stability.py` imports `build_diagnostics_snapshot` from `core.diagnostics.snapshot`. The function name and signature must not change, only the import path.
- If Option B is chosen, `test_operational_boundaries.py::TestCoreLoggingImportsCanonicalSanitizer` needs rewriting because the module it tests (`core.observability.logging`) no longer exists.

**Rollback**: Restore `core/diagnostics/` from git. Revert test import path changes.

### Tranche 3: Rename core/runtime/ to core/supervision/ (Low Risk, High Clarity)

**Objective**: Eliminate the naming collision between `core/runtime/` (domain types) and `runtime/` (orchestration).

**Estimated effort**: ~1 hour.

| # | Action | Rationale |
|---|---|---|
| 3.1 | Rename `core/runtime/` directory to `core/supervision/` | Clear semantic separation |
| 3.2 | Update all imports of `medre.core.runtime.*` to `medre.core.supervision.*` | Includes `TYPE_CHECKING` imports (see source files below) |
| 3.3 | Update source files that import from `medre.core.runtime.*` | See exhaustive list below |
| 3.4 | Update test files with `medre.core.runtime.*` string literals | Find-and-replace across test suite |

**Source files with `core.runtime.*` imports** (verified by audit):

| File | Imports |
|---|---|
| `runtime/app.py` | `core.runtime.accounting.RuntimeAccounting`, `core.runtime.health.*`, `core.runtime.supervision.*` |
| `runtime/builder.py` | `core.runtime.accounting.RuntimeAccounting` |
| `runtime/events.py` | `core.runtime.diagnostic_contract.sanitize_diagnostic_mapping` |
| `core/storage/replay.py` | `medre.runtime.capacity.CapacityController` (TYPE_CHECKING only, no change needed for T3) |
| `core/engine/pipeline.py` | `medre.runtime.capacity.CapacityController` (TYPE_CHECKING only, no change needed for T3) |
| `adapters/meshcore/adapter.py` | `core.runtime.diagnostic_contract.sanitize_diagnostic_mapping` |

Note: `core/engine/pipeline.py` and `core/storage/replay.py` import `CapacityController` from the top-level `runtime/` package (not `core/runtime/`), so those references are unaffected by this rename. The import is under `TYPE_CHECKING` and represents type-only coupling to the orchestration layer (see Section 3.3, type-only coupling note).

**Tests to update**:
- `test_beta_scope_boundaries.py` -- `_CORE_MODULES` has 7 entries for `medre.core.runtime.*`: `medre.core.runtime`, `.accounting`, `.capabilities`, `.diagnostic_contract`, `.diagnostics`, `.health`, `.supervision`. All change to `medre.core.supervision.*`.
- `test_operational_boundaries.py` -- `_EVIDENCE_SOURCE_MODULES` includes `medre.core.runtime.diagnostics`, `.diagnostic_contract`, `.health`. Also `_DIAGNOSTICS_SOURCE_MODULES` includes `.diagnostics`, `.health`, `.capabilities`, `.supervision`, `.accounting`, `.diagnostic_contract`.
- `test_cross_transport_boundaries.py` -- `_CORE_MODULES` includes `medre.core.runtime`, `.diagnostics`, `.health`, `.capabilities`. Also `_RUNTIME_MODULES` includes `medre.core.runtime.diagnostics`, `.health`, `.capabilities`. Also `_DIAGNOSTIC_CONTRACT_MODULES` includes 3 `medre.core.runtime.*` entries. Also `_SESSION_RUNTIME_FORBIDDEN_PREFIXES` references `medre.core.runtime.diagnostics` and `medre.core.runtime.health`. Also `TestAdapterRuntimeContainment` checks for `medre.core.runtime.diagnostics`, `.health`, `.capabilities`.
- `test_snapshot_schema_stability.py` -- imports from `medre.core.runtime.accounting`, `medre.core.runtime.diagnostic_contract`.
- `test_architectural_boundaries.py` -- no direct `core.runtime` references (it checks SDK/adapter imports, not module paths).
- `test_evidence_package_boundary.py` -- references `medre.runtime.evidence` (top-level runtime, not core.runtime). No change needed.

**Verification**:
```bash
# Source audit: find all remaining core.runtime references
grep -r "core\.runtime\." src/medre/ | grep -v __pycache__ | grep -v "medre\.runtime\."
# Must return ZERO hits (medre.runtime.* top-level is legitimate)

# Full source import audit for core.runtime.* (pre-T3 baseline):
grep -rn "from medre\.core\.runtime" src/medre/ | grep -v __pycache__
# All hits must be updated to core.supervision

# Full source import audit for core.runtime.* in string literals:
grep -rn "medre\.core\.runtime\." src/medre/ tests/ | grep -v __pycache__ | grep -v "medre\.runtime\."
# Must return ZERO hits after rename

python -m pytest tests/ -x --timeout=60
```

**Risks**:
- Many test files use string literals for module paths (e.g., `_CORE_MODULES = ["medre.core.runtime.diagnostics", ...]`). A simple find-and-replace works but must be exhaustive.
- `test_cross_transport_boundaries.py` has both forbidden-prefix tuples and parametrize lists referencing `medre.core.runtime.*`. Missing any entry causes false-positive test failures.

**Rollback**: Rename `core/supervision/` back to `core/runtime/`. Revert import changes.

### Tranche 4: Plugin Foundation Audit (Low Risk, Additive)

**Objective**: Verify and document the existing plugin scaffolding. No new code needed.

**Estimated effort**: ~15 minutes.

The plugins package already contains scaffolding (verified by `test_beta_scope_boundaries.py::TestNoPluginRuntime`):
- `Plugin` protocol
- `PluginCapability` enum
- `validate_plugin_payload` function

These live in `plugins/__init__.py`. The previous plan proposed creating `plugins/base.py`, but the scaffolding already exists and the beta scope boundary tests enforce that no loader/manager/registry is added.

| # | Action | Rationale |
|---|---|---|
| 4.1 | Document the plugin scaffolding in this plan | Already exists, no code change |
| 4.2 | Mark T4 as informational only | Nothing to implement |

**Verification**: Existing tests already cover this (`TestNoPluginRuntime`).

---

## 6. Test Impact Matrix

Each test file with hardcoded module paths that must be updated during refactoring:

| Test File | Affected Tranches | Module Lists to Update |
|---|---|---|
| `test_beta_scope_boundaries.py` | T0, T1, T2, T3 | `_RUNTIME_MODULES` (10 entries), `_CORE_MODULES` (44 entries including `core.diagnostics.*`, `core.runtime.*`, `core.policies`, `core.transforms`) |
| `test_architectural_boundaries.py` | T0 | Fake adapter imports |
| `test_operational_boundaries.py` | T2, T3 | `_EVIDENCE_SOURCE_MODULES` (8 entries), `_DIAGNOSTICS_SOURCE_MODULES` (8 entries) |
| `test_cross_transport_boundaries.py` | T1, T3 | `_CORE_MODULES` (23 entries), `_RUNTIME_MODULES` (3 entries), `_DIAGNOSTIC_CONTRACT_MODULES` (3 entries), `_SESSION_RUNTIME_FORBIDDEN_PREFIXES`, forbidden import checks in `TestAdapterRuntimeContainment` |
| `test_packaging_and_install_contract.py` | T0 | Fake adapter imports (7 entries in `TestFakeAdaptersWithoutSDKs`, `TestFakeAdaptersNoTransitiveSDKImports`) |
| `test_evidence_package_boundary.py` | None | References `medre.runtime.evidence` (top-level runtime, unaffected) |
| `test_snapshot_schema_stability.py` | T2, T3 | Imports from `core.diagnostics.*`, `core.runtime.*`, `runtime.snapshot` |
| `test_runtime_durability_boundaries.py` | T3 | Verified: references `medre.core.runtime.diagnostics`, `medre.core.runtime.health` in module lists (3 occurrences) |
| `test_supervision_boundaries.py` | T3 | Verified: references `medre.core.runtime.supervision`, `.diagnostics`, `.health`, `.diagnostic_contract`, `.capabilities` (5+ occurrences) |
| `test_resource_boundaries.py` | T3 | Verified: no `core.runtime.*` references. Audit before implementation to confirm. |
| `test_route_runtime_boundaries.py` | T3 | Verified: no `core.runtime.*` references. Audit before implementation to confirm. |
| `test_queue_boundaries.py` | T3 | Verified: no `core.runtime.*` references. Audit before implementation to confirm. |
| `test_deployment_boundaries.py` | T3 | Verified: no `core.runtime.*` references. Audit before implementation to confirm. |
| `test_runtime_deployment_boundaries.py` | T3 | Verified: references `medre.core.runtime.diagnostics`, `.health`, `.accounting`, `.capabilities` (5 occurrences) |

**Additional files with module path references**:
- `medre/config/__init__.py` -- `_DEFERRED` dict maps symbol names to `(module_path, attr_name)` tuples. Currently references `medre.config.model`, `medre.runtime.routes`, `medre.config.loader`, `medre.config.env`. No `core.*` paths, so T1-T3 do not affect it. But if `runtime/routes.py` is ever moved, this dict needs updating.
- `scripts/ci/` -- CI scripts reference test file names, not module paths. Verify before each tranche but expect no changes.
- `docs/` -- Architecture docs, runbooks, and contracts may reference module paths. Update after implementation.

---

## 7. Decision Points for PC

These choices require coordinator approval before implementation begins.

### Decision 1: Should adapters/base.py remain a re-export shim or be deleted?

The plan proposes making `adapters/base.py` a thin re-export shim that re-exports from both `core.ports` and `core.adapter_base`. This preserves backward compatibility but creates an indirection layer.

Since medre is unreleased, the cleaner option is to update all imports directly to `core.ports` / `core.adapter_base` and delete `adapters/base.py` entirely. However, `test_cross_transport_boundaries.py::TestDeliveryContractBoundary` explicitly verifies that `AdapterDeliveryResult` exists in `medre.adapters.base`. Deleting `base.py` requires updating that test.

**Recommendation**: Keep the re-export shim for now. It costs nothing, keeps boundary tests passing, and can be removed in a later cleanup pass once all consumers have been migrated to import directly from `core.ports` and `core.adapter_base`. The shim is temporary scaffolding, not permanent architecture. Schedule its removal as a follow-up item after T1 lands and all adapter implementations have been updated.

### Decision 2: Should core/diagnostics/ be merged into core/observability/ or kept separate?

`core/diagnostics/` (ReplayMetrics, snapshot builder) serves a different purpose than `core/observability/` (Diagnostician, structured logging). ReplayMetrics is a storage-layer artifact; the Diagnostician is a runtime diagnostic aggregator. Merging them into a single `core/observability/diagnostics.py` is coherent (both are about inspecting system state) but mixes storage-layer metrics with runtime-layer diagnostics.

**Recommendation**: Merge. Both are domain-level observability types with no external dependencies. The separation adds navigation cost without semantic benefit.

### Decision 3: What to do about core/ importing from observability/sanitization.py?

Two core modules import from `observability/sanitization.py`:
- `core/observability/logging.py` imports `sanitize_for_log`
- `core/routing/stats.py` imports `sanitize_error`

This is the most sensitive dependency. Three options:

1. **Accept the deviation and document it**: Both modules import pure functions with no I/O or SDK coupling. The existing boundary test (`TestCoreLoggingImportsCanonicalSanitizer`) enforces that core logging uses the canonical sanitizer rather than defining its own. Document both as deliberate tradeoffs (see Section 3.3 exceptions table).

2. **Move `sanitize_for_log` and `sanitize_error` into core**: Make sanitization a domain utility. This is cleaner architecturally but moves PII-awareness logic into the domain layer, which may not be appropriate. Also note that `core/runtime/diagnostic_contract.py` has its own `_SECRET_KEY_PATTERNS` regex list that overlaps with `observability/sanitization.py`'s `_SECRET_KEY_PATTERNS`. Moving sanitization into core would need to address this duplication.

3. **Move `core/observability/logging.py` out of core entirely**: The structured logging setup is more of an infrastructure concern than a domain concern. Move it to `observability/logging.py` and merge with the existing adapter logger factory. This does not address the `core/routing/stats.py` import.

**Recommendation**: Option 1 (accept and document). It preserves the existing test contract, avoids moving PII logic into the domain layer, and keeps the change surface small. Both dependencies are on pure functions, not services or SDKs. The duplicated `_SECRET_KEY_PATTERNS` between `core/runtime/diagnostic_contract.py` and `observability/sanitization.py` should be documented as a follow-up cleanup item (see Section 9.2, deferred decisions), but not resolved in this refactor tranche.

### Decision 4: Naming for the renamed core/runtime/ package

Options: `core/supervision/`, `core/oversight/`, `core/contracts/`, `core/domain_runtime/`.

**Recommendation**: `core/supervision/`. It accurately describes the contents (accounting, capabilities, health checks, diagnostic contracts, supervision classifier) and is distinct from `runtime/` (orchestration).

### Decision 5: Should the fake adapter move (T0) happen before the port extraction (T1)?

T0 is zero-risk and independent of T1. T1 is the highest-impact change. Doing T0 first gives a clean workspace for T1, but T0 touches `adapters/__init__.py`, which T1 also touches. Order matters.

**Historical note**: T1 was implemented first on branch `maint-517-2`. The port extraction (T1) is the critical dependency-inversion fix and can safely precede the fake-adapter move (T0). T0 remains valuable as a follow-up cleanliness improvement but does not block any architectural work.

### Decision 6: Split port extraction into two files or keep as one?

The original plan proposed a single `core/ports.py` for everything extracted from `adapters/base.py`. However, `BaseAdapter` is not a thin protocol. It is an ABC with concrete Template Method behavior: `publish_inbound()` wraps the context publisher with a stale-event guard, `_is_stale_event()` implements timestamp comparison logic, and `_mark_started()` records the adapter start time from the context clock. It also imports `CanonicalEvent` from `core.events.canonical` and `RenderingResult` from `core.rendering.renderer`. The remaining symbols (`AdapterRole`, `AdapterSendError`, `AdapterDeliveryResult`, `AdapterCapabilities`, `AdapterInfo`, `AdapterContext`, `AdapterCodec`) are pure value types, enums, and protocol definitions with no behavioral logic and no imports outside core.

**Option A: Split into two files** (recommended)

- `core/ports.py` contains pure value types and protocols: `AdapterRole`, `AdapterContext`, `AdapterCapabilities`, `AdapterInfo`, `AdapterSendError`, `AdapterDeliveryResult`, `AdapterCodec`. Zero behavioral logic. Zero imports outside core. The name "ports" is honest.
- `core/adapter_base.py` contains `BaseAdapter` (ABC with Template Methods). Imports from `core/ports`, `core/events/canonical`, and `core/rendering/renderer`. The name "adapter_base" signals that this is infrastructure, not a pure interface.

The four core files update as follows:
- `core/runtime/capabilities.py` → imports `AdapterCapabilities` from `core.ports` (value type only)
- `core/runtime/health.py` → imports `AdapterInfo` from `core.ports` (value type only)
- `core/planning/delivery_plan.py` → imports `AdapterSendError` from `core.ports` (value type only)
- `core/engine/pipeline.py` → imports `BaseAdapter` from `core.adapter_base`, remaining types from `core.ports`

**Option B: Single file with honest name**

- `core/adapter_contracts.py` contains everything. The name "contracts" covers both port interfaces and behavioral ABCs. Simpler (one file) but bundles pure types with behavioral code.

**Tradeoff**: Option A adds a second file but keeps naming honest. Option B is simpler but the name is vaguer. Since the whole point of this refactor is clarity, Option A is recommended.

**Recommendation**: Option A (split). The naming accuracy is worth the extra file.

---

## 8. Recommended Next Tranche

**Start with Tranche 0** (cleanups).

Rationale:
- Zero behavioral change means zero risk of regressions.
- Removes dead packages that would otherwise need to be accounted for in T1's `_CORE_MODULES` audit.
- Organizes fake adapters, which cleans up the `adapters/` directory before the port extraction touches `adapters/base.py`.
- Fast (~30 minutes) and gives confidence in the test-update workflow before tackling the higher-risk T1.
- After T0, the `adapters/` directory is clean: `base.py` (types), `fakes/` (test doubles), and four transport packages.

After T0 lands, **T1 (port extraction)** is the next priority. It is the only change that fixes a real architectural defect (the core->adapters dependency inversion). All other tranches are naming/organization improvements.

---

## 9. Explicit Tradeoffs

### 9.1 Decisions Made

| Decision | Rationale |
|---|---|
| **No `constants/` package** | Event kinds are domain types in `core/events/kinds.py`. Adapter constants live in their adapter packages. A root `constants/` becomes a junk drawer. |
| **No `validation/` package** | Validation is context-specific: config in `config/errors.py`, routes in `runtime/routes.py`, schemas in `core/events/schema.py`. Shared validation adds abstraction without value. |
| **No `tools/` package** | Operator tooling lives in `cli/`. Sample configs in `config/sample.py` and `examples/`. Docker artifacts in `runtime/docker_bridge_artifacts.py`. Contextually placed. |
| **`config/model.py` adapter config imports kept** | Each adapter declares its own frozen-dataclass config. The runtime config model wraps these. Config-time dependency on value types only. Acceptable. |
| **Port extraction consolidated into core/contracts/adapter.py** | Tranche 3 merged `core/ports.py` and `core/adapter_base.py` into a single canonical module `core/contracts/adapter.py`. `BaseAdapter` renamed to `AdapterContract`. Old files removed. All imports updated because the project is pre-release and clean architecture is preferred over compatibility. |
| **Config errors centralized in config/adapters/errors.py** | Config validation errors (`MatrixConfigError`, etc.) are `ValueError` subclasses, not adapter runtime errors. They live in the config layer. Runtime adapter errors remain in `medre.adapters.*.errors`. |
| **Config does not import adapters** | `config/adapters/matrix.py` previously imported `load_credentials_json` from `adapters/matrix/auth.py`. That function (and `get_credentials_path`) moved to `config/adapters/matrix_credentials.py`, eliminating the boundary violation. |
| **adapters/base.py removed** | No longer a re-export shim. All consumers import directly from `core.contracts.adapter`. |
| **adapters/*/config.py shims removed** | All consumers import config dataclasses directly from `config.adapters.*`. |
| **`interop/` kept separate from adapters/** | Wire-format constants define a cross-adapter contract. Placing them in any single adapter creates adapter-to-adapter coupling. |
| **Fake adapters kept in production package** | `runtime/drill.py` and `runtime/smoke.py` use fakes for operational testing. They ship as part of the runtime. Consolidating into `adapters/fakes/` is the right balance. |
| **NOT flattening to match mmrelay** | medre's layered architecture is objectively superior for a multi-transport routing engine. Flattening would destroy the boundary tests that enforce layer separation. |

### 9.2 Decisions Deferred

| Decision | Why Deferred |
|---|---|
| Plugin system implementation | Scaffolding exists. Full implementation is a separate workstream. |
| Adapter hot-reload | Architecture supports it (ports are protocols) but no implementation planned. |
| Core as standalone package | Port extraction enables this, but extraction is not planned pre-release. |
| Event schema migrations | `SchemaRegistry` + `MIGRATION_REGISTRY` exist. No migrations needed pre-release. |
| ~~`adapters/base.py` shim removal~~ | **Done** in Tranche 3. The shim was removed; all consumers now import from `core.contracts.adapter`. |
| Duplicated secret-key patterns | `core/runtime/diagnostic_contract.py` and `observability/sanitization.py` both define `_SECRET_KEY_PATTERNS` with overlapping regex lists. After T2/T3 dependency cleanup, evaluate whether one module can import from the other or whether a shared `core/sanitization.py` is warranted. Do not deduplicate before the dependency graph stabilizes. |

---

## 10. File-Level Change Impact

| File | Change | Tranche |
|---|---|---|
| `core/contracts/__init__.py` | **NEW** — re-exports from `core/contracts/adapter.py` | T3 |
| `core/contracts/adapter.py` | **NEW** — merged `core/ports.py` + `core/adapter_base.py`, `BaseAdapter` renamed to `AdapterContract` | T3 |
| `core/ports.py` | **DELETED** | T3 |
| `core/adapter_base.py` | **DELETED** | T3 |
| `adapters/base.py` | **DELETED** | T3 |
| `adapters/matrix/config.py` | **DELETED** | T3 |
| `adapters/meshtastic/config.py` | **DELETED** | T3 |
| `adapters/meshcore/config.py` | **DELETED** | T3 |
| `adapters/lxmf/config.py` | **DELETED** | T3 |
| `config/adapters/errors.py` | **NEW** — centralized config error hierarchy | T3 |
| `config/adapters/matrix_credentials.py` | **NEW** — credential helpers moved from `adapters/matrix/auth.py` | T3 |
| `config/adapters/matrix.py` | **MODIFY** — import `MatrixConfigError` from `errors`, import `load_credentials_json` from `matrix_credentials` | T3 |
| `config/adapters/meshtastic.py` | **MODIFY** — import `MeshtasticConfigError` from `errors` | T3 |
| `config/adapters/meshcore.py` | **MODIFY** — import `MeshCoreConfigError` from `errors` | T3 |
| `config/adapters/lxmf.py` | **MODIFY** — import `LxmfConfigError` from `errors` | T3 |
| `adapters/matrix/auth.py` | **MODIFY** — import credential helpers from `config.adapters.matrix_credentials`, remove local definitions | T3 |
| `adapters/matrix/__init__.py` | **MODIFY** — remove Config/ConfigError exports | T3 |
| `adapters/meshtastic/__init__.py` | **MODIFY** — remove Config/ConfigError exports | T3 |
| `adapters/meshcore/__init__.py` | **MODIFY** — remove Config/ConfigError exports | T3 |
| `adapters/lxmf/__init__.py` | **MODIFY** — remove Config/ConfigError exports | T3 |
| `adapters/__init__.py` | **MODIFY** — remove BaseAdapter and port type re-exports | T3 |
| All adapter implementations | **MODIFY** — `BaseAdapter` → `AdapterContract`, import from `core.contracts.adapter` | T3 |
| All source files importing config | **MODIFY** — import from `config.adapters.*` instead of `adapters.*.config` | T3 |
| ~70+ test files | **MODIFY** — updated import paths | T3 |

---

*End of architecture plan.*

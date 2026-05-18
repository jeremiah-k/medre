# MEDRE Target Architecture Plan

> **Status**: Proposed — not yet implemented.
> **Audience**: Coordinator synthesising a final plan from multiple analysis tracks.

---

## 1. Current State Summary

### 1.1 Package Tree (as-is)

```
medre/
├── __init__.py              # empty
├── __main__.py              # delegates to cli:main
├── py.typed
├── adapters/                # adapter framework + 4 transport implementations
│   ├── base.py              # AdapterRole, BaseAdapter, AdapterCodec, AdapterContext …
│   ├── fake_*.py            # 7 fake adapters (loose files)
│   ├── matrix/              # 12 modules (adapter, auth, codec, config, renderer, session …)
│   ├── meshtastic/          # 10 modules
│   ├── lxmf/                # 10 modules
│   └── meshcore/            # 9 modules
├── cli/                     # 18 command modules + main
├── config/                  # 7 modules (loader, model, env, paths, errors, sample)
├── core/
│   ├── diagnostics/         # replay_metrics, snapshot
│   ├── engine/              # pipeline.py (orchestration)
│   ├── events/              # bus, canonical, kinds, metadata, schema
│   ├── identity/            # actor, resolver
│   ├── lifecycle/           # states
│   ├── observability/       # logging, metrics (Diagnostician)
│   ├── planning/            # delivery_plan, fallback_resolution, relation_resolution
│   ├── policies/            # EMPTY
│   ├── rendering/           # renderer, text
│   ├── routing/             # models, router, stats
│   ├── runtime/             # accounting, capabilities, diagnostic_contract, diagnostics, health, supervision
│   ├── storage/             # backend, replay, sqlite
│   └── transforms/          # EMPTY
├── interop/                 # mmrelay wire-format constants
├── observability/           # classification, logging, sanitization, summaries
├── plugins/                 # EMPTY
└── runtime/                 # app, builder, capacity, retry, routes, route_engine, evidence/ …
```

### 1.2 What medre Already Does Better Than mmrelay

These strengths are **preserved** — not flattened:

| Capability | medre | mmrelay |
|---|---|---|
| Adapter abstraction | `BaseAdapter` protocol + `AdapterCodec` | Inline utility classes |
| Event model | `CanonicalEvent` msgspec struct, immutable | Dict-based payloads |
| Rendering boundary | `RenderingPipeline` protocol, adapters never render | Formatting mixed into relay logic |
| Routing engine | `Router` with `RouteSource`/`RouteTarget` models | Hardcoded room ↔ channel map |
| Event bus | Async pub/sub with middleware chain | Direct callback invocation |
| Planning layer | `DeliveryPlan`, `FallbackResolver`, `RelationResolver` | None |
| Storage | `StorageBackend` protocol + SQLite impl + replay engine | SQLite for node info only |
| Identity | `CanonicalActor` + `NativeIdentity` + verification levels | None |
| Lifecycle | State machine (`INITIALIZED → STARTING → RUNNING → …`) | None formalized |
| Retry | Exponential backoff + lineage + dead-letter + `RetryWorker` | None |
| Schema versioning | `SchemaRegistry` + migration registry | None |
| Diagnostics | `Diagnostician`, `RouteStats`, `ReplayMetrics`, evidence bundles | None |
| Capacity control | Semaphore-based `CapacityController` | None |

---

## 2. Problems Identified

### 2.1 Critical: Dependency Inversion Violation

**`core/engine/pipeline.py` imports from `adapters/base.py`.**

This creates a `core → adapters` dependency that inverts the layering. The core should be the innermost layer with no knowledge of infrastructure.

Current cycle:
```
core/engine/pipeline.py → adapters/base.py → core/events/canonical.py
```

This works at runtime (Python resolves it) but is architecturally impure and blocks future extraction of core as a standalone package.

### 2.2 Dual Observability

Two `logging.py` files with different responsibilities:
- `core/observability/logging.py` — structured logging setup, route-aware logging, diagnostic events (imports from top-level `observability/sanitization.py`)
- `observability/logging.py` — adapter-scoped logger factory (53 lines)

The `core/observability/` → `observability/` dependency is backwards. The core logging module depends on the top-level sanitization module.

### 2.3 Fragmented Diagnostics

Three locations for observability-adjacent code:
- `core/observability/` — Diagnostician, logging
- `core/diagnostics/` — ReplayMetrics, diagnostics snapshot
- `observability/` — classification, logging, sanitization, summaries

### 2.4 Naming Collision: `core/runtime/` vs `runtime/`

`core/runtime/` contains pure domain types (accounting counters, health enums, supervision classifiers).
`runtime/` contains the actual orchestration implementation.
The collision is confusing for contributors.

### 2.5 Empty Packages

`core/policies/`, `core/transforms/`, `plugins/` — empty packages that add noise and suggest incomplete design.

### 2.6 Fake Adapters in Production Package

7 `fake_*.py` files live alongside real adapters in `adapters/`. They are test doubles used by `runtime/drill.py` and `runtime/smoke.py`. Having them loose in the adapter package clutters the import surface.

### 2.7 Config → Adapter Config Dependency

`config/model.py` imports from all four `adapters/*/config.py` modules. This is a **config-time** dependency on value types only — acceptable but worth documenting as a deliberate tradeoff.

---

## 3. Target Architecture

### 3.1 Layer Model

```
                    ┌─────────────────────────┐
                    │         cli/             │  Presentation
                    │   (commands, entry)      │
                    └─────────┬───────────────┘
                              │
                    ┌─────────▼───────────────┐
                    │       runtime/           │  Construction + Orchestration
                    │  (builder, app, retry,   │
                    │   routes, evidence)      │
                    └─────────┬───────────────┘
                              │
              ┌───────────────┼───────────────────┐
              │               │                   │
    ┌─────────▼──────┐ ┌─────▼──────┐  ┌─────────▼──────┐
    │   adapters/     │ │  config/   │  │  observability/ │
    │  (implementations)│ │ (models)   │  │  (logging,     │
    │                 │ │            │  │   summaries)   │
    └─────────┬──────┘ └─────┬──────┘  └─────────┬──────┘
              │               │                   │
              └───────────────┼───────────────────┘
                              │
                    ┌─────────▼───────────────┐
                    │        core/             │  Domain Layer
                    │  (events, routing,       │  ZERO external deps
                    │   planning, rendering,   │
                    │   identity, storage,     │
                    │   ports, supervision)    │
                    └─────────────────────────┘
```

### 3.2 Target Package Tree

```
medre/
├── __init__.py
├── __main__.py
├── py.typed
│
├── core/                                # INNERMOST — zero external deps
│   ├── __init__.py
│   ├── ports.py                         # ★ NEW: adapter interface types
│   │                                    #   (AdapterRole, BaseAdapter, AdapterCodec,
│   │                                    #    AdapterContext, AdapterCapabilities,
│   │                                    #    AdapterSendError, AdapterDeliveryResult, AdapterInfo)
│   ├── engine/
│   │   └── pipeline.py                  # imports core.ports (not adapters.base)
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
│   │   └── states.py
│   ├── observability/                   # ★ CONSOLIDATED interfaces
│   │   ├── metrics.py                   # Diagnostician (kept)
│   │   ├── diagnostics.py               # ★ MERGED from core/diagnostics/
│   │   └── health.py                    # ★ MOVED from core/runtime/health.py
│   ├── planning/
│   │   ├── delivery_plan.py
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
│   └── supervision/                     # ★ RENAMED from core/runtime/
│       ├── accounting.py
│       ├── capabilities.py
│       ├── diagnostic_contract.py
│       └── supervision.py
│
├── adapters/                            # depends on core only
│   ├── __init__.py                      # re-exports from core.ports
│   ├── base.py                          # thin shim: re-exports core.ports
│   ├── fakes/                           # ★ NEW DIR: consolidated test doubles
│   │   ├── __init__.py
│   │   ├── fake_lxmf.py
│   │   ├── fake_matrix.py
│   │   ├── fake_meshcore.py
│   │   ├── fake_meshtastic.py
│   │   ├── fake_presentation.py
│   │   └── fake_transport.py
│   ├── matrix/
│   │   ├── adapter.py
│   │   ├── auth.py
│   │   ├── cli.py
│   │   ├── codec.py
│   │   ├── compat.py
│   │   ├── config.py
│   │   ├── errors.py
│   │   ├── metadata.py
│   │   ├── relations.py
│   │   ├── renderer.py
│   │   └── session.py
│   ├── meshtastic/
│   │   ├── adapter.py
│   │   ├── codec.py
│   │   ├── compat.py
│   │   ├── config.py
│   │   ├── errors.py
│   │   ├── packet_classifier.py
│   │   ├── queue.py
│   │   ├── renderer.py
│   │   └── session.py
│   ├── lxmf/
│   │   ├── adapter.py
│   │   ├── codec.py
│   │   ├── compat.py
│   │   ├── config.py
│   │   ├── errors.py
│   │   ├── fields.py
│   │   ├── packet_classifier.py
│   │   ├── renderer.py
│   │   └── session.py
│   └── meshcore/
│       ├── adapter.py
│       ├── codec.py
│       ├── compat.py
│       ├── config.py
│       ├── errors.py
│       ├── packet_classifier.py
│       ├── renderer.py
│       └── session.py
│
├── config/                              # depends on adapter config value types
│   ├── __init__.py
│   ├── env.py
│   ├── errors.py
│   ├── loader.py
│   ├── model.py
│   ├── paths.py
│   └── sample.py
│
├── observability/                       # ★ CONSOLIDATED — unified observability
│   ├── __init__.py
│   ├── classification.py               # failure-kind inference
│   ├── logging.py                       # ★ MERGED: structured logging + adapter logger factory
│   ├── sanitization.py                  # PII stripping
│   └── summaries.py                     # human-readable summaries
│
├── interop/                             # external wire-format constants
│   ├── __init__.py
│   └── mmrelay.py
│
├── plugins/                             # ★ STUB with base protocol
│   ├── __init__.py
│   └── base.py                          # PluginProtocol ABC
│
├── runtime/                             # construction + orchestration
│   ├── __init__.py
│   ├── app.py
│   ├── boot_summary.py
│   ├── builder.py
│   ├── capacity.py
│   ├── docker_bridge_artifacts.py
│   ├── drill.py
│   ├── errors.py
│   ├── events.py
│   ├── evidence/
│   │   ├── __init__.py
│   │   ├── _bundle.py
│   │   ├── _config_sections.py
│   │   ├── _diagnostics_sections.py
│   │   ├── _helpers.py
│   │   └── _storage_sections.py
│   ├── observability.py
│   ├── retry.py
│   ├── route_engine.py
│   ├── routes.py
│   ├── run_session/
│   │   ├── __init__.py
│   │   ├── evidence.py
│   │   ├── orchestration.py
│   │   ├── report.py
│   │   └── scenario.py
│   ├── smoke.py
│   ├── snapshot.py
│   ├── timeline.py
│   └── trace.py
│
└── cli/                                 # presentation layer
    ├── __init__.py
    ├── __main__.py
    ├── main.py
    ├── config_commands.py
    ├── contrib.py
    ├── diagnostics_commands.py
    ├── evidence_commands.py
    ├── exit_codes.py
    ├── inspect_commands.py
    ├── json.py
    ├── recover_commands.py
    ├── replay_commands.py
    ├── route_commands.py
    ├── run_commands.py
    ├── smoke_commands.py
    ├── storage_helpers.py
    ├── trace_commands.py
    ├── transport_constants.py
    └── transports.py
```

### 3.3 Changes Summary

| Change | From | To | Type |
|--------|------|----|------|
| Extract port types | `adapters/base.py` | `core/ports.py` | **Structural** |
| Shim base.py | full module | re-export from `core.ports` | **Compatibility** |
| Consolidate observability | `core/observability/logging.py` + `observability/logging.py` | `observability/logging.py` | **Merge** |
| Merge diagnostics | `core/diagnostics/` (2 files) | `core/observability/diagnostics.py` | **Merge** |
| Relocate health | `core/runtime/health.py` | `core/observability/health.py` | **Move** |
| Rename core/runtime/ | `core/runtime/` | `core/supervision/` | **Rename** |
| Consolidate fakes | `adapters/fake_*.py` (7 loose files) | `adapters/fakes/` | **Reorganize** |
| Delete empty packages | `core/policies/`, `core/transforms/` | gone | **Delete** |
| Stub plugins | empty `plugins/` | `plugins/base.py` | **Add** |

---

## 4. Ownership Rules

### 4.1 Package Ownership Matrix

| Package | Owns | Depends On |
|---------|------|------------|
| `core/` | Domain types, protocols, pure logic | **Nothing outside core/** |
| `core/ports.py` | Adapter interface definitions (protocols, ABCs, value types) | `core/events/`, `core/rendering/` |
| `core/engine/` | Pipeline orchestration | `core/ports`, `core/events`, `core/routing`, `core/planning`, `core/rendering`, `core/storage`, `core/observability` |
| `adapters/*` | Transport/platform implementations | `core/ports`, `core/events`, `core/rendering`, `observability/` |
| `adapters/fakes/` | Test doubles for smoke/drill | Same as adapters |
| `config/` | Configuration loading and validation | `adapters/*/config` (value types only) |
| `observability/` | Logging, sanitization, classification, summaries | `core/` (types only) |
| `interop/` | External wire-format constants | **Nothing** |
| `plugins/` | Plugin protocol and loader | `core/events/` (for event types) |
| `runtime/` | Construction, orchestration, lifecycle | `core/`, `adapters/`, `config/`, `observability/` |
| `cli/` | User-facing commands | `runtime/`, `config/`, `core/` (types), `observability/` |

### 4.2 Import Direction Rules

```
cli/           →  runtime/, config/, core/, observability/
runtime/       →  core/, adapters/, config/, observability/
adapters/      →  core/, observability/
config/        →  adapters/*/config (value types only)
observability/ →  core/ (types only)
plugins/       →  core/ (types only)
interop/       →  NOTHING (pure constants)
core/          →  core/ ONLY (zero external imports)
```

**Hard rule**: `core/` MUST NOT import from `adapters/`, `config/`, `runtime/`, `cli/`, or top-level `observability/`.

### 4.3 Module Size Guidelines

| Package | Max modules per subpackage | Max lines per module |
|---------|---------------------------|---------------------|
| `core/` | 8 | 500 (pipeline.py is an exception at ~1300) |
| `adapters/*/` | 12 | 400 |
| `cli/` | No limit | 300 |
| `runtime/` | No limit | 500 |

---

## 5. Refactor Tranche Sequencing

Tranches are ordered by **risk** (lowest first) and **impact** (highest first within risk tier).

### Tranche 0 — Cleanups (Zero Risk, Zero Behavioral Change)

Estimated effort: ~30 minutes.

| # | Action | Rationale |
|---|--------|-----------|
| 0.1 | Delete `core/policies/` (empty) | Removes noise |
| 0.2 | Delete `core/transforms/` (empty) | Removes noise |
| 0.3 | Move `adapters/fake_*.py` → `adapters/fakes/` | Organizes test doubles |
| 0.4 | Update `adapters/__init__.py` fake imports | Follows from 0.3 |
| 0.5 | Update all test imports of fake adapters | Follows from 0.3 |

**Verification**: All tests pass. No behavioral change.

### Tranche 1 — Port Extraction (High Impact, Medium Risk)

Estimated effort: ~2 hours. This is the single most important change.

| # | Action | Rationale |
|---|--------|-----------|
| 1.1 | Create `core/ports.py` with all interface types from `adapters/base.py` | Fixes dependency inversion |
| 1.2 | Make `adapters/base.py` a thin re-export shim importing from `core/ports` | Preserves compatibility during migration |
| 1.3 | Update `core/engine/pipeline.py` to import from `core.ports` | Core no longer depends on adapters |
| 1.4 | Audit all `core/` imports — remove any remaining `adapters.` references | Enforces layer rule |
| 1.5 | Update `adapters/__init__.py` re-exports to reference `core.ports` | Clean re-export chain |
| 1.6 | Update adapter implementations to import from `core.ports` | Optional but preferred |

**Verification**: `grep -r "from medre\.adapters" src/medre/core/` returns zero hits. All tests pass.

### Tranche 2 — Observability Consolidation (Medium Risk)

Estimated effort: ~1.5 hours.

| # | Action | Rationale |
|---|--------|-----------|
| 2.1 | Merge `core/observability/logging.py` into `observability/logging.py` | Single logging module |
| 2.2 | Merge `core/diagnostics/replay_metrics.py` + `snapshot.py` → `core/observability/diagnostics.py` | Single diagnostics location |
| 2.3 | Move `core/runtime/health.py` → `core/observability/health.py` | Health is observability |
| 2.4 | Delete `core/diagnostics/` directory | Follows from 2.2 |
| 2.5 | Update all imports referencing moved modules | Follows from 2.1–2.4 |
| 2.6 | Remove `core/observability/logging.py` (now empty after merge) | Cleanup |

**Verification**: No `core/observability/logging.py` exists. All diagnostics types in `core/observability/diagnostics.py`. All tests pass.

### Tranche 3 — Rename core/runtime/ → core/supervision/ (Low Risk, High Clarity)

Estimated effort: ~1 hour.

| # | Action | Rationale |
|---|--------|-----------|
| 3.1 | Rename `core/runtime/` → `core/supervision/` | Eliminates naming collision |
| 3.2 | Update all imports (many are `TYPE_CHECKING` only) | Follows from 3.1 |
| 3.3 | Update runtime/app.py references to supervision types | Follows from 3.1 |

**Verification**: `grep -r "core\.runtime\." src/medre/` returns zero hits (except legitimate `medre.runtime.` top-level references). All tests pass.

### Tranche 4 — Plugin Foundation (Low Risk, Additive)

Estimated effort: ~30 minutes.

| # | Action | Rationale |
|---|--------|-----------|
| 4.1 | Create `plugins/base.py` with `PluginProtocol` ABC | Foundation for future plugin system |
| 4.2 | Update `plugins/__init__.py` to export `PluginProtocol` | Public API |

**Verification**: All tests pass. New module is importable.

---

## 6. Explicit Tradeoffs

### 6.1 Decisions Made

| Decision | Rationale |
|----------|-----------|
| **No `constants/` package** | Event kinds are domain types that belong in `core/events/kinds.py`. Adapter-specific constants belong in their adapter packages. A root `constants/` becomes a junk drawer. mmrelay's `constants/` works for their flat architecture but would be a step backward for medre's layered design. |
| **No `validation/` package** | Validation is context-specific: config validation in `config/errors.py`, route validation in `runtime/routes.py`, event schema validation in `core/events/schema.py`. Extracting a shared package would add abstraction without value. |
| **No `tools/` package** | Operator tooling lives in `cli/`. Sample configs live in `config/sample.py` and `examples/`. Docker artifacts live in `runtime/docker_bridge_artifacts.py`. These are contextually placed; a `tools/` package would be a catch-all. |
| **`config/model.py` → adapter config imports kept** | Each adapter declares its own frozen-dataclass config. The runtime config model wraps these. This is a config-time dependency on value types only — no I/O, no SDK imports. Acceptable. |
| **Port extraction over full hexagonal architecture** | `core/ports.py` is a single module rather than a full `ports/` package. For a single-process Python application, a single ports module is sufficient. A full ports/ package would add directory depth without proportional value. |
| **`interop/` kept separate from adapters/** | Wire-format compatibility constants define a cross-adapter contract (e.g., mmrelay's Matrix message schema is consumed by both matrix and meshtastic adapters). Placing them in any single adapter would create adapter↔adapter coupling. |
| **Fake adapters kept in production package** | `runtime/drill.py` and `runtime/smoke.py` use fake adapters for operational testing. They ship as part of the runtime. Moving to `tests/` would break the smoke/drill pipeline. Consolidating into `adapters/fakes/` is the right balance. |
| **NOT flattening to match mmrelay** | medre's layered architecture (core → adapters → runtime → cli) is objectively superior for maintainability. mmrelay's flat structure works for a single-transport relay but does not scale to a multi-transport routing engine. |

### 6.2 Decisions Deferred

| Decision | Why Deferred |
|----------|-------------|
| Plugin system implementation | `plugins/` gets a protocol stub only. Full implementation is a separate workstream. |
| Adapter hot-reload | Architecture supports it (ports are protocols) but no implementation planned. |
| Core as standalone package | Port extraction enables this but extraction is not planned pre-release. |
| Event schema migrations | `SchemaRegistry` + `MIGRATION_REGISTRY` exist. No migrations needed pre-release. |

---

## 7. Comparison: mmrelay vs medre Target

```
mmrelay/                            medre/ (target)
├── config.py (flat)                ├── config/ (7 modules, typed models)
├── constants/ (14 files)           ├── core/events/kinds.py (domain-typed)
├── matrix/ (12 files)              ├── adapters/matrix/ (12 modules, protocol-based)
├── meshtastic/ (14 files)          ├── adapters/meshtastic/ (10 modules)
├── plugins/ (12 plugins)           ├── plugins/ (stub → future)
├── tools/ (samples)                ├── cli/ (18 commands, structured)
├── cli_utils.py (flat)             ├── core/ports.py (interface types)
├── db_runtime.py (flat)            ├── core/storage/ (protocol + SQLite + replay)
├── log_utils.py (flat)             ├── observability/ (4 modules, consolidated)
└── ...                             └── core/ (domain layer, zero ext deps)
```

**Key difference**: mmrelay is a relay with plugins. medre is a routing engine with adapters. The architecture reflects this — medre has a formal core domain layer, port/adapter separation, and a pipeline engine. These are not features to copy from mmrelay; they are features that make medre a fundamentally different (and more maintainable) system.

---

## 8. Dependency Graph (Target)

```
                    cli
                     │
            ┌────────┼──────────┐
            │        │          │
         runtime  config   observability
            │        │          │
     ┌──────┼────┐   │          │
     │      │    │   │          │
 adapters  ...  ... │          │
     │              │          │
     └──────┬───────┘          │
            │                  │
          core ◄───────────────┘
         (ports, events,
          routing, planning,
          rendering, storage,
          supervision)
```

**`core/` is the innermost layer. Nothing inside core imports from outside core.**

---

## 9. File-Level Change Impact

| File | Change | Tranche |
|------|--------|---------|
| `core/ports.py` | **NEW** — extract from `adapters/base.py` | T1 |
| `adapters/base.py` | **SHIM** — re-export from `core.ports` | T1 |
| `core/engine/pipeline.py` | **MODIFY** — imports from `core.ports` | T1 |
| `adapters/__init__.py` | **MODIFY** — update re-exports | T1 |
| `core/observability/diagnostics.py` | **NEW** — merge from `core/diagnostics/` | T2 |
| `core/observability/health.py` | **NEW** — move from `core/runtime/health.py` | T2 |
| `core/observability/metrics.py` | Keep (Diagnostician) | — |
| `core/observability/logging.py` | **DELETE** — merge into `observability/logging.py` | T2 |
| `observability/logging.py` | **MERGE** — absorb `core/observability/logging.py` | T2 |
| `core/diagnostics/` | **DELETE** directory | T2 |
| `core/runtime/` → `core/supervision/` | **RENAME** | T3 |
| `adapters/fake_*.py` → `adapters/fakes/` | **MOVE** | T0 |
| `core/policies/` | **DELETE** | T0 |
| `core/transforms/` | **DELETE** | T0 |
| `plugins/base.py` | **NEW** | T4 |
| ~50 test files | **UPDATE** imports | T0–T3 |

---

*End of architecture plan.*

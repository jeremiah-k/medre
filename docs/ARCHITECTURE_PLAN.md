# MEDRE Architecture

> **Status**: Current architecture record.

## 0. Canonical Layout

### 0.1 Core Adapter Contracts — `medre.core.contracts.adapter`

Exports:

- `AdapterContract` (protocol for all adapter implementations)
- `AdapterRole`
- `AdapterCodec`
- `AdapterContext`
- `AdapterCapabilities`
- `AdapterInfo`
- `AdapterDeliveryResult`
- `AdapterSendError`
- `AdapterPermanentError`

The following module names are noncanonical and must not be imported:

- `medre.core.ports` — does not exist
- `medre.core.adapter_base` — does not exist
- `medre.adapters.base` — does not exist

Use `medre.core.contracts.adapter` instead.

### 0.2 Adapter Configuration — `medre.config.adapters.*`

Config dataclasses live in:

- `medre.config.adapters.matrix.MatrixConfig`
- `medre.config.adapters.meshtastic.MeshtasticConfig`
- `medre.config.adapters.meshcore.MeshCoreConfig`
- `medre.config.adapters.lxmf.LxmfConfig`

Config validation errors live in `medre.config.adapters.errors`:

- `AdapterConfigError(ValueError)`
- `MatrixConfigError`
- `MeshtasticConfigError`
- `MeshCoreConfigError`
- `LxmfConfigError`

Config errors are `ValueError` subclasses — they are NOT runtime adapter errors.

### 0.3 Matrix Credential Sidecar — `medre.config.adapters.matrix_credentials`

Canonical home for credential sidecar file operations:

- `get_credentials_path()` -> Path
- `load_credentials_json(path=None)` -> dict | None
- `write_credentials_json(data, path=None)` -> Path

`medre.adapters.matrix.auth` may delegate credential persistence to this module.

### 0.4 Adapter Implementations — `medre.adapters.*`

Concrete adapter implementations live in `medre.adapters.*`:

- `medre.adapters.matrix.*`
- `medre.adapters.meshtastic.*`
- `medre.adapters.meshcore.*`
- `medre.adapters.lxmf.*`

Runtime/session/network/protocol errors live in `medre.adapters.*.errors`:

- `medre.adapters.matrix.errors.MatrixError` (etc.)
- `medre.adapters.meshtastic.errors.MeshtasticError` (etc.)

These are adapter runtime errors — NOT config validation errors.

## 1. Layer Ownership Rules

| Layer            | May Import From                                                              | Must Not Import From             |
| ---------------- | ---------------------------------------------------------------------------- | -------------------------------- |
| `medre.core`     | `medre.core` only (documented exceptions: `core/observability/sanitization`) | `medre.adapters`, `medre.config` |
| `medre.config`   | `medre.config` (including `config.adapters`)                                 | `medre.adapters`                 |
| `medre.adapters` | `medre.core.contracts.adapter`, `medre.config.adapters.*`, `medre.core.*`    | —                                |

- Concrete adapters depend inward on core contracts and config models.
- `medre.config.adapters.matrix_credentials` is the canonical owner of credential file operations.

**Hard rule**: `core/` MUST NOT import from `adapters/`, `config/`, `cli/`, or top-level `runtime/` at runtime.

**Hard rule**: `config/` MUST NOT import from `adapters/`.

**Documented runtime exceptions** (core -> outside core):

| Source                          | Import                                                       | Reason                                |
| ------------------------------- | ------------------------------------------------------------ | ------------------------------------- |
| `core/observability/logging.py` | `sanitize_for_log` from `core/observability/sanitization.py` | Pure function, no I/O or SDK coupling |
| `core/routing/stats.py`         | `sanitize_error` from `core/observability/sanitization.py`   | Pure function, no I/O or SDK coupling |

Both exceptions import the same pure-function module (`core/observability/sanitization.py`). They are the only runtime core->external dependencies.

**Type-only coupling** (acceptable, no runtime dependency):

| Source                    | Import                                            | Guard                     |
| ------------------------- | ------------------------------------------------- | ------------------------- |
| `core/engine/pipeline.py` | `CapacityController` from `core.runtime.capacity` | `if TYPE_CHECKING:` block |
| `core/storage/replay.py`  | `CapacityController` from `core.runtime.capacity` | `if TYPE_CHECKING:` block |

## 2. Package Tree

```text
medre/
├── __init__.py              # empty
├── __main__.py              # delegates to cli:main
├── py.typed
├── adapters/                # concrete adapter implementations only
│   ├── __init__.py          # re-exports all fakes (no AdapterContract re-export)
│   ├── fake_lxmf.py
│   ├── fake_matrix.py
│   ├── fake_meshcore.py
│   ├── fake_meshtastic.py
│   ├── fake_presentation.py
│   ├── fake_transport.py
│   ├── matrix/              # adapter, auth, cli, codec, errors, session, etc.
│   ├── meshtastic/
│   ├── lxmf/
│   └── meshcore/
├── cli/                     # 18 command modules + main + __main__
├── config/
│   ├── __init__.py          # PEP 562 deferred-import dict (_DEFERRED)
│   ├── adapters/
│   │   ├── __init__.py      # re-exports *Config and *ConfigError classes
│   │   ├── errors.py        # AdapterConfigError hierarchy (ValueError subclasses)
│   │   ├── matrix.py        # MatrixConfig dataclass with validate()
│   │   ├── matrix_credentials.py  # credential sidecar helpers
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
│   ├── contracts/
│   │   ├── __init__.py      # re-exports AdapterContract, AdapterRole, etc.
│   │   └── adapter.py       # AdapterContract and contract types
│   ├── diagnostics/         # replay_metrics, snapshot
│   ├── engine/              # pipeline.py (orchestration)
│   ├── events/              # bus, canonical, kinds, metadata, schema
│   ├── identity/            # actor, resolver
│   ├── lifecycle/           # states, manager
│   ├── observability/       # logging, metrics (Diagnostician)
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
├── plugins/                 # scaffolding only: Plugin protocol, PluginCapability enum
└── runtime/                 # app, builder, capacity, retry, routes, route_engine,
                             # boot_summary, drill, smoke, snapshot, timeline, trace,
                             # errors, events, observability, docker_bridge_artifacts,
                             # evidence/, run_session/
```

## 3. Architectural Decisions

- Config validation errors are `ValueError` subclasses, not adapter runtime error subclasses.
- Matrix credential sidecar helpers are owned by the config layer for testability.
- `medre.core.runtime/` is distinct from top-level `medre.runtime/`.

The following modules do not exist and must not be imported:

- `medre.adapters.base` does not exist.
- `medre.core.ports` does not exist.
- `medre.core.adapter_base` does not exist.
- `medre.adapters.*.config` modules do not exist.

## 4. Remaining Follow-Up Work

- Rename `core/runtime/` → `core/supervision/` to eliminate naming collision with top-level `runtime/`
- Move fake adapters to `medre.adapters.fakes/` subdirectory
- Decide disposition of remaining contract/doc documents (audit records vs current specifications)
- Evaluate merging `core/diagnostics/` into `core/observability/`
- Deduplicate `_SECRET_KEY_PATTERNS` between `core/runtime/diagnostic_contract.py` and `core/observability/sanitization.py`
- Delete empty packages `core/policies/` and `core/transforms/`

# MEDRE Architecture

> **Status**: Current architecture record.
>
> **No stable public API.** All import paths are internal and may change.

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

### 0.2a Route Configuration — `medre.config.routes`

Route config dataclasses live in `medre.config.routes`:

- `RouteDirectionality` — direction of flow between source/dest
- `BridgePolicy` — static allowlist policy for a route
- `RouteRetryConfig` — per-route retry policy for transient failures
- `RouteConfig` — a single named route definition
- `RouteConfigSet` — ordered, validated collection of routes

`medre.config.routes` is the canonical home for route configuration models.
Runtime route expansion and topology live in `medre.runtime.route_engine`.
`medre.config` must not import from `medre.runtime`.

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

| Layer            | May Import From                                                           | Must Not Import From                        |
| ---------------- | ------------------------------------------------------------------------- | ------------------------------------------- |
| `medre.core`     | `medre.core` only (with narrowly scoped internal dependency notes)        | `medre.adapters`, `medre.config`            |
| `medre.config`   | `medre.config` (including `config.adapters` and `config.routes`)          | `medre.adapters`, `medre.runtime`           |
| `medre.adapters` | `medre.core.contracts.adapter`, `medre.config.adapters.*`, `medre.core.*` | —                                           |
| `medre.runtime`  | `medre.core.*`, `medre.config.*`, `medre.adapters.*`                      | —                                           |

- Concrete adapters depend inward on core contracts and config models.
- `medre.config.adapters.matrix_credentials` is the canonical owner of credential file operations.
- `medre.config.routes` owns route configuration dataclasses; `medre.runtime.route_engine` owns runtime route expansion, topology, and registration.

**Hard rule**: `core/` MUST NOT import from `adapters/`, `config/`, `cli/`, or top-level `runtime/` at runtime.

**Hard rule**: `config/` MUST NOT import from `adapters/` or `runtime/`.

**Documented intra-core coupling** (acceptable, no runtime cross-boundary dependency):

| Source                          | Import                                                       | Reason                                |
| ------------------------------- | ------------------------------------------------------------ | ------------------------------------- |
| `core/observability/logging.py` | `sanitize_for_log` from `core/observability/sanitization.py` | Pure function, no I/O or SDK coupling |
| `core/routing/stats.py`         | `sanitize_error` from `core/observability/sanitization.py`   | Pure function, no I/O or SDK coupling |

Both imports target the same pure-function module (`core/observability/sanitization.py`) within `medre.core`. They are the only documented internal exception within core.

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
│   ├── __init__.py          # lightweight package marker / docstring only
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
│   ├── __init__.py          # lightweight package marker / docstring only
│   ├── adapters/
│   │   ├── __init__.py      # lightweight package marker / docstring only
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
│   ├── routes.py            # RouteConfig, RouteConfigSet, RouteDirectionality, etc.
│   └── sample.py
├── core/
│   ├── contracts/
│   │   ├── __init__.py      # package-level imports of AdapterContract, AdapterRole, etc.
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
│   ├── runtime/             # accounting, capabilities, capacity,
│   │                        # diagnostic_contract, diagnostics, health, supervision
│   ├── storage/             # backend, replay, sqlite
│   └── transforms/          # empty
├── interop/                 # mmrelay wire-format constants
├── plugins/                 # scaffolding only: Plugin protocol, PluginCapability enum
└── runtime/                 # app, builder, retry, route_engine,
                              # boot_summary, drill, smoke, snapshot, timeline, trace,
                              # errors, events, observability, docker_bridge_artifacts,
                              # evidence/, run_session/
```

## 3. Architectural Decisions

- Config validation errors are `ValueError` subclasses, not adapter runtime error subclasses.
- Matrix credential sidecar helpers are owned by the config layer for testability.
- `medre.core.runtime/` is distinct from top-level `medre.runtime/`.
- Route configuration dataclasses are owned by `medre.config.routes`, not `medre.runtime`. Runtime route expansion and topology remain in `medre.runtime.route_engine`.

Current canonical module homes:

- Adapter base contracts live in `medre.core.contracts.adapter`.
- Core runtime helper types live under `medre.core.runtime/`.
- Adapter config dataclasses live in `medre.config.adapters.*`.
- Route config models live in `medre.config.routes`; runtime expansion and topology live in `medre.runtime.route_engine`.

### 3.1 MMRelay Reference Relationship

MMRelay (`meshtastic-matrix-relay`) is an operational reference implementation for Matrix↔Meshtastic relay behavior. MEDRE learns conceptually from MMRelay in areas such as relay behavior, message truncation, outbound queueing, packet classification, Matrix send reliability, and sidecar credentials.

MMRelay is NOT a dependency, import target, vendor source, or copy target for MEDRE. MEDRE does not import, vendor, merge, cherry-pick, or copy files from MMRelay. The relationship is conceptual reference only. `medre.interop` contains wire-format constants derived from open specifications; it does not import MMRelay code. Future MEDRE-facing details live in `docs/dev/mmrelay-reference-notes.md`.

## 4. Remaining Follow-Up Work

- Rename `core/runtime/` → `core/supervision/` to eliminate naming collision with top-level `runtime/`
- Move fake adapters to `medre.adapters.fakes/` subdirectory
- Decide disposition of remaining contract/doc documents (audit records vs current specifications)
- Evaluate merging `core/diagnostics/` into `core/observability/`
- Deduplicate `_SECRET_KEY_PATTERNS` between `core/runtime/diagnostic_contract.py` and `core/observability/sanitization.py`
- Delete empty packages `core/policies/` and `core/transforms/`

## 5. Deferred Tranches

The following tranches are documented for planning; some are now implemented:

- **docs/mmrelay-reference-map** — Structured documentation mapping MMRelay concepts (truncation, queueing, packet routing, sidecar credentials) to their MEDRE equivalents or gaps. Implemented as `docs/dev/mmrelay-reference-notes.md`.
- **feat/meshtastic-byte-budget-rendering** — Implemented on branch `feat/meshtastic-byte-budget-rendering`. Transport-aware rendering with a `max_text_bytes` setting (default 227, matching Meshtastic's payload budget) that UTF-8-safe byte-truncates the final rendered text to fit within the transport limit. **Known limitation:** `MeshtasticRenderer` uses the first configured Meshtastic adapter's config. In multi-Meshtastic setups, adapters could have different `max_text_bytes`, `radio_relay_prefix`, `meshnet_name`, or `mmrelay_compatibility` values, but rendering uses the first adapter's config until target-aware rendering is implemented in a future tranche.
- **feat/meshtastic-queue-evidence** — Evidence tracking for Meshtastic's queued `send_one` outbound, correlating queue drain events with delivery receipts.
- **feat/meshtastic-packet-classifier-parity** — Inbound packet type classification matching MMRelay's coverage of telemetry, position, nodeinfo, and text portnum types.
- **feat/matrix-send-idempotency** — Matrix outbound send deduplication using transaction IDs or event ID caching to prevent duplicate sends on retry.

# Packaging and Install Contract

> Contract version: 2
> Last updated: 2026-05-16
> Track: 2 (Packaging & Metadata), Track 10 (Import Boundary Validation)
> Supersedes: Nothing. New.
> Status: Active. Governs package metadata, install boundaries, and import isolation.

This document defines the packaging contract for the `medre` distribution.
It specifies what a base install provides, how optional extras are structured,
and what import boundaries must hold.

## 1. Scope

- Package metadata in `pyproject.toml`.
- Base install behaviour (no optional SDKs).
- Optional dependency extras and their contents.
- Import boundary: core imports must never transitively pull in optional SDKs.
- Fake adapter availability without optional SDKs.
- Runtime builder construction with fake adapters.

## 2. Non-goals

- Specifying SDK version policies (see contract 34).
- Defining Docker images or deployment tooling.
- Changing dependency versions or pinning strategy.
- CLI behaviour contracts (owned by CLI agent).
- Runtime smoke tests (owned by runtime agent).

## 3. Package Metadata

| Property | Value | Notes |
|----------|-------|-------|
| **Distribution name** | `medre` | Single-word, lowercase |
| **Version** | `0.1.0` | Semantic dotted notation |
| **License** | `GPL-3.0-or-later` | See contract 42 for governance context |
| **Status classifier** | `Development Status :: 4 - Beta` | Pre-beta; not production-ready |
| **Requires Python** | `>=3.11` | |
| **Build system** | `setuptools>=68` | |
| **Source layout** | `src/` (via `[tool.setuptools.packages.find]`) | |

### 3.1 Console Script Entry Point

```
medre = "medre.cli:main"
```

The `medre` command-line tool is the sole console script. It invokes
`medre.cli:main`, which delegates to subcommands (`run`, `config`, `version`,
`adapters`, `diagnostics`, `routes`).

## 4. Base Dependencies

The base install (`pip install medre`) has a **single** required dependency:

| Dependency | Version | Purpose |
|------------|---------|---------|
| `msgspec` | `==0.21.1` | High-performance structured serialization for canonical events |

No transport SDK is required by the base install.

## 5. Optional Extras

Optional extras follow the convention `pip install medre[extra]`.

| Extra | SDK / Distribution | Import guard | Purpose |
|-------|--------------------|--------------|---------|
| `matrix` | `mindroom-nio>=0.25.3` | `medre.adapters.matrix.compat.HAS_NIO` | Matrix presentation adapter |
| `matrix-e2e` | `mindroom-nio[e2e]>=0.25.3` | `medre.adapters.matrix.compat.HAS_E2EE` | Matrix with E2EE support |
| `meshtastic` | `mtjk>=2.7.8`, `PyPubSub>=4.0` | `medre.adapters.meshtastic.compat.HAS_MESHTASTIC` | Meshtastic radio transport |
| `meshcore` | `meshcore>=2.3.7` | `medre.adapters.meshcore.compat.HAS_MESHCORE` | MeshCore radio transport |
| `lxmf` | `lxmf>=0.9.6` | `medre.adapters.lxmf.compat.HAS_LXMF` | LXMF/Reticulum transport |
| `dev` | `pytest>=8.0`, `pytest-asyncio>=0.24` | N/A | Development / test tooling |

### 5.1 Extra Properties

- Each extra is **additive** — it never modifies or conflicts with base deps.
- No extra's dependencies overlap with base `msgspec`.
- `matrix-e2e` is a superset of `matrix` (adds vodozemac/crypto deps via nio extras).
- `meshtastic` includes `PyPubSub` explicitly because the `mtjk` distribution does
  not declare it as a dependency.

## 6. Import Boundary Rules

### 6.1 Core Imports (base install, no optional SDKs)

The following imports **must** succeed without any optional extras installed:

```python
import medre
import medre.config
import medre.runtime
import medre.adapters
import medre.adapters.base
import medre.cli
```

### 6.2 Adapter Config Imports

Adapter configuration modules (e.g., `medre.adapters.matrix.config`) are pure
dataclass definitions and **must not** transitively import their corresponding SDK.
This ensures `medre.config.model` can reference all adapter config types at
module load time without requiring any SDK.

### 6.3 Compat Module Isolation

Each transport adapter sub-package contains a `compat.py` module that is the
**sole** import site for the optional SDK. All other modules in the sub-package
must access SDK functionality through the compat module's flags and re-exported
module references, never via direct `import nio` / `import meshtastic` / etc.

Compat flags:

| Flag | Type | `True` when |
|------|------|-------------|
| `HAS_NIO` | `bool` | `nio` package importable |
| `HAS_E2EE` | `bool` | `nio.crypto.ENCRYPTION_ENABLED` is truthy |
| `HAS_MESHTASTIC` | `bool` | `meshtastic` (from `mtjk`) importable |
| `HAS_MESHCORE` | `bool` | `meshcore` importable |
| `HAS_LXMF` | `bool` | Both `RNS` and `LXMF` importable |

### 6.4 Forbidden Transitive Imports

The following modules **must not** transitively import any optional SDK at
module load time:

- `medre.__init__`
- `medre.config.*`
- `medre.runtime.builder`
- `medre.adapters.__init__`
- `medre.adapters.fake_*`
- `medre.cli`

## 7. Fake Adapters

All fake adapters are importable and instantiable without optional SDKs:

| Fake adapter | Module | Constructor args |
|--------------|--------|------------------|
| `FakeTransportAdapter` | `medre.adapters.fake_transport` | `adapter_id: str` |
| `FakeMatrixAdapter` | `medre.adapters.fake_matrix` | `adapter_id: str` |
| `FakeMeshtasticAdapter` | `medre.adapters.fake_meshtastic` | `config: MeshtasticConfig` |
| `FakeMeshCoreAdapter` | `medre.adapters.fake_meshcore` | `config: MeshCoreConfig` |
| `FakeLxmfAdapter` | `medre.adapters.fake_lxmf` | `config: LxmfConfig` |
| `FakePresentationAdapter` | `medre.adapters.fake_presentation` | `adapter_id: str` |

All are re-exported from `medre.adapters.__init__`.

### 7.1 Fake Adapter Dependencies

Some fake adapters (meshtastic, meshcore, lxmf) import from their adapter
sub-package's config, codec, and errors modules. These sub-package modules
must **not** import the SDK — they are pure Python data/typing modules.
The compat module is the only SDK import site.

## 8. Runtime Builder — Fake Multi-Adapter Path

The `RuntimeBuilder` supports `adapter_kind="fake"` on each adapter runtime
config. When set, the builder calls `_build_fake_adapter()` which directly
imports the corresponding `Fake*Adapter` class — bypassing the real adapter
factory and its compat guard / SDK import entirely.

This allows constructing a complete runtime with all four transports
(matrix, meshtastic, meshcore, lxmf) using fake adapters, with **zero**
optional SDKs installed.

## 9. Example Config Distribution

Example configs live in `examples/configs/` in the source repository. They are
**not** shipped as package data. The `medre config sample` command generates a
complete, fake-buildable TOML config from code and is the installed-package
config access path.

Decision rationale (alpha):
- Moving examples into the package source tree would break doc references,
  test paths, and the `medre smoke` default config path.
- `medre config sample` already provides a working config from any install.
- Examples serve as reference documentation for source-checkout operators.

This decision may be revisited for beta if operator feedback indicates a need
for installed-package example access beyond `medre config sample`.

## 10. Known Gaps

- **LICENSE file**: Top-level LICENSE file exists (GPL-3.0-or-later).
- **License governance**: GPL-3.0-or-later; see contract 42 for contributor governance.
- **No `pip install -e .` CI gate**: No CI job currently verifies that a
  clean editable install + test run succeeds without optional extras.
- **No wheel size audit**: Wheel/size not yet measured.

## 11. Test Coverage

Tests live in `tests/test_packaging_and_install_contract.py` and verify:

1. `pyproject.toml` metadata (name, version, scripts, extras, classifiers).
2. Base imports without optional SDKs.
3. All fake adapters instantiate without optional SDKs.
4. Compat guard flags are bool-typed.
5. `RuntimeBuilder` builds a 4-transport fake runtime without SDKs.
6. Contract doc exists and mentions all required extras.
7. `py.typed` marker shipped for PEP 561 / `Typing :: Typed` classifier.
8. `python -m medre` delegates to canonical CLI (`medre.cli:main`).
9. CLI help, config check, and smoke fake paths work without optional SDK imports.
10. Missing optional SDK error messages mention the `medre[extra]` install hint.

## 12. Change Protocol

Any change to the following requires updating this contract:

- Adding or removing an optional extra.
- Adding a new base dependency.
- Changing the console script entry point.
- Changing the import boundary structure.
- Adding a new transport adapter with its own SDK.
- Removing or relocating the `py.typed` marker.
- Changing the `__main__.py` delegation target.

Amendments increment the contract version.

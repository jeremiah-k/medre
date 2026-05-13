# Distribution Boundary Analysis

> Contract version: 2
> Last updated: 2026-05-12
> Track: 4 (Distribution Boundary)
> Supersedes: Nothing. First distribution boundary analysis.
> Status: Analysis. Records current packaging posture, boundary seams, and deferred future options. No packaging changes proposed.

This document analyzes the distribution boundaries of medre: how the single-package structure works today, where the seams are, what optional extras protect, how fake adapters and compat guards isolate transport SDKs, and what future split paths could look like. It is an analysis document. No package separation, adapter redesign, runtime redesign, subprocess isolation, or service boundary work is proposed or recommended for the beta timeframe.

The current unified architecture is intentional. It keeps beta simple. The boundaries documented here already preserve future optionality.

This is an analysis document. No packaging redesign, package splitting, adapter redesign, runtime redesign, subprocess isolation, or service boundary work is proposed.


## 1. Scope

- Current single-package (`medre`) structure and what ships where.
- `pyproject.toml` optional dependency groups and their purpose.
- Fake adapter strategy and what it protects against.
- `compat.py` guard pattern per transport.
- Runtime (deferred) SDK imports in sessions.
- Adapter/session boundary and how it preserves extraction optionality.
- Future split paths analyzed as deferred mitigations.

## 2. Non-goals

- Proposing package separation or extraction.
- Designing subprocess or service boundaries.
- Adding new transports, adapters, or features.
- Changing the adapter/session contract.
- Redesigning the runtime to support distribution.
- Recommending any of the deferred paths for current work.


## 3. Current Packaging Structure

### 3.1 Single distribution: `medre`

medre ships as one Python package. Everything lives under `src/medre/`:

```
src/medre/
    __init__.py          (empty)
    runner.py            (Matrix alpha runner, optional entry point)
    adapters/
        __init__.py      (re-exports base types + all fake adapters)
        base.py          (BaseAdapter, AdapterContext, AdapterInfo, etc.)
        fake_*.py        (6 fake adapters for testing)
        matrix/          (10 files: adapter, codec, compat, config, errors,
                         metadata, relations, renderer, session)
        meshtastic/      (11 files: adapter, codec, compat, config, errors,
                         packet_classifier, queue, renderer, session)
        meshcore/        (10 files: adapter, codec, compat, config, errors,
                         packet_classifier, renderer, session)
        lxmf/           (11 files: adapter, codec, compat, config, errors,
                         fields, packet_classifier, renderer, session)
    core/
        engine/          (pipeline runner)
        events/          (canonical event model, bus, kinds, metadata, schema)
        identity/
        lifecycle/
        observability/   (metrics, diagnostics)
        planning/        (fallback resolution, relation resolution)
        policies/
        rendering/       (renderer pipeline)
        routing/
        runtime/         (capabilities, diagnostic contract, health)
        storage/         (SQLite storage)
        transforms/
    plugins/
        __init__.py      (empty, reserved)
```

### 3.2 Why unified is correct for beta

A single package gives beta users one thing to install, one version to track, and one set of contracts to understand. The four transports are all pre-beta. None has proven itself in live operation yet (contracts 37, 39). Splitting before any transport has live validation evidence would be premature packaging driven by theoretical concerns rather than demonstrated need.

The core event model (`CanonicalEvent`, `EventKind`, `EventRelation`, `NativeRef`) is shared by all adapters and the pipeline. The pipeline runner wires adapters, codecs, renderers, routing, and storage together. Extracting any of these into separate packages would require defining stable inter-package APIs before the intra-package APIs have been exercised in production. That is the wrong order.

### 3.3 What ships at install time

`pip install medre` gives the user the core event model, the adapter framework, all four transport adapter modules, all six fake adapters, the pipeline runner, routing, rendering, storage, and observability. None of the optional transport SDKs (nio, meshtastic, meshcore, lxmf) are installed. The user gets the full codebase but no transport dependencies.

The only required dependency is `msgspec==0.21.1` (used by the canonical event model and schema validation). Everything else is optional.


## 4. Optional Extras

### 4.1 Current extras in `pyproject.toml`

| Extra | What it pulls | Import guarded by |
|-------|--------------|-------------------|
| `dev` | pytest, pytest-asyncio | N/A (test-only) |
| `matrix` | `mindroom-nio>=0.25.3` | `medre.adapters.matrix.compat.HAS_NIO` |
| `matrix-e2e` | `mindroom-nio[e2e]>=0.25.3` (adds vodozemac) | `medre.adapters.matrix.compat.HAS_E2EE` |
| `meshtastic` | `mtjk>=2.7.8`, `PyPubSub>=4.0` | `medre.adapters.meshtastic.compat.HAS_MESHTASTIC` |
| `meshcore` | `meshcore>=2.3.7` | `medre.adapters.meshcore.compat.HAS_MESHCORE` |
| `lxmf` | `lxmf>=0.9.6` (implies Reticulum) | `medre.adapters.lxmf.compat.HAS_LXMF` |

A user who only needs Matrix installs `pip install medre[matrix]`. They get the nio client library but not meshtastic, meshcore, or lxmf. Each extra is independent. No extra pulls another extra.

### 4.2 What extras protect against

The extras protect against two things:

1. **Install friction.** Some transport SDKs have heavy transitive dependencies (Rust toolchain for vodozemac, BLE stacks for meshcore, protobuf for meshtastic). Users who don't need those transports shouldn't have to install them.

2. **Platform incompatibility.** A user running on a headless server without BLE support shouldn't fail on `pip install medre` because meshcore's bleak dependency can't find a Bluetooth stack.

### 4.3 What extras don't do

Extras don't prevent code from being importable. `from medre.adapters.lxmf import LxmfAdapter` works whether or not `lxmf` is installed. The adapter code itself imports from `medre.adapters.base` and `medre.core.events`, which are always available. Only the SDK-specific imports inside `session.py` are gated by the compat guard.

This is a feature, not a bug. It means tests can import adapter modules without installing SDKs, as long as they use fake mode.


## 5. Fake Adapters

### 5.1 Current fake adapters

medre ships six fake adapters in the top-level `adapters/` directory:

| Fake Adapter | Real Adapter It Mirrors | Primary Use |
|-------------|------------------------|-------------|
| `FakeTransportAdapter` | Generic transport base | Unit tests for transport-agnostic code |
| `FakePresentationAdapter` | Generic presentation base | Unit tests for presentation-agnostic code |
| `FaultyPresentationAdapter` | Error injection variant | Failure testing |
| `FakeMatrixAdapter` | `MatrixAdapter` | Matrix-specific tests without nio |
| `FakeMeshtasticAdapter` | `MeshtasticAdapter` | Meshtastic-specific tests without mtjk |
| `FakeMeshCoreAdapter` | `MeshCoreAdapter` | MeshCore-specific tests without meshcore |
| `FakeLxmfAdapter` | `LxmfAdapter` | LXMF-specific tests without lxmf/RNS |

### 5.2 What fake adapters prove

The existence of working fake adapters proves that the adapter/session boundary is real. Each fake adapter implements the full `BaseAdapter` contract (start, stop, deliver, diagnostics, health) without importing any transport SDK. This means:

- The `BaseAdapter` contract is SDK-independent by design.
- The adapter's public API (what the pipeline and runtime see) is transport-agnostic.
- SDK-specific types never cross the adapter boundary (verified by contract 27, section 5.3).

If the adapter/session boundary were not clean, fake adapters would not work. The fact that 2127 unit tests pass against fakes is evidence the boundary holds.

### 5.3 Fake adapters and distribution

From a distribution perspective, fake adapters serve a specific role: they let downstream consumers write integration tests against the medre adapter contract without installing any transport SDKs. A downstream project that uses medre as a library can test its own pipeline wiring with `FakeTransportAdapter` and `FakePresentationAdapter` and never install nio, meshtastic, meshcore, or lxmf.

This matters for distribution because it means medre's test surface is usable with just `pip install medre[dev]`. No transport SDKs required.


## 6. Compat Guards

### 6.1 The pattern

Each transport adapter has a `compat.py` module that is the sole import site for its SDK. The pattern is:

```python
# compat.py
HAS_TRANSPORT: bool

try:
    import transport_sdk  # noqa: F401
    HAS_TRANSPORT = True
except ImportError:
    HAS_TRANSPORT = False
```

The adapter checks the flag in `start()`:

```python
async def start(self, context):
    if self._config.connection_type != "fake" and not HAS_TRANSPORT:
        raise TransportConnectionError("SDK not installed; use connection_type='fake'")
```

### 6.2 Per-transport compat modules

| Transport | Compat Module | Flag | Sole import site for |
|-----------|--------------|------|---------------------|
| Matrix | `medre.adapters.matrix.compat` | `HAS_NIO`, `HAS_E2EE` | `nio`, `nio.crypto` |
| Meshtastic | `medre.adapters.meshtastic.compat` | `HAS_MESHTASTIC` | `meshtastic`, `meshtastic.protobuf.portnums_pb2` |
| MeshCore | `medre.adapters.meshcore.compat` | `HAS_MESHCORE` | `meshcore` |
| LXMF | `medre.adapters.lxmf.compat` | `HAS_LXMF` | `lxmf`, `RNS` |

### 6.3 What compat guards protect

Compat guards protect the import graph. When `HAS_NIO` is `False`, no nio code is loaded into the Python process. The Matrix adapter module can be imported, inspected, and tested, but no nio types exist. This means:

- `import medre.adapters.matrix` works without nio installed.
- `MatrixAdapter` can be instantiated (for fake mode or testing).
- `MatrixSession` can be imported but not started against a real server.
- `MatrixCodec`, `MatrixRenderer`, `MatrixConfig` all work without nio.

### 6.4 Runtime imports inside sessions

Sessions import the actual SDK at method call time, not at module import time. For example, `MatrixSession._start_sync()` contains `import nio` inside the method body. This means the `import nio` statement only executes when someone calls `start()` with a non-fake connection type.

This two-layer defense (compat guard at module level, deferred import at runtime) means:

1. Importing the adapter package never triggers SDK imports.
2. Instantiating the adapter never triggers SDK imports.
3. Only calling `start()` with a real connection type triggers SDK imports.
4. If the SDK is absent, the error is a clean `ImportError` or `TransportConnectionError`, not an unresolved module error at import time.


## 7. Adapter/Session Boundary

### 7.1 What the boundary is

The adapter/session boundary (formalized in contract 31) is the most important seam for distribution flexibility. The adapter owns semantic conversion (codec, routing, event publishing). The session owns raw transport management (SDK client lifecycle, connection, callbacks, reconnect).

The adapter never touches the SDK client directly. The session never touches the canonical event model directly. They communicate through a `message_callback` function provided by the adapter.

### 7.2 Why this matters for distribution

If medre ever splits into separate packages, the adapter/session boundary is the natural extraction seam. A hypothetical `medre-matrix` package would contain `medre.adapters.matrix.session`, `medre.adapters.matrix.compat`, and `medre.adapters.matrix.config`. The adapter itself would stay in core medre (or move to a thin adapter wrapper) because it depends on `BaseAdapter`, `CanonicalEvent`, and `RenderingResult`, which are core types.

The session has no dependency on core medre types. It takes a `MatrixConfig` (its own), a `message_callback` (a plain callable), and returns raw transport data to that callback. This is by design, not by accident.

### 7.3 Current optionality preserved

The session boundary today preserves future extraction optionality without requiring any action now. The boundary is clean:

- Sessions don't import from `medre.core`.
- Sessions don't reference `CanonicalEvent` or any core type.
- Sessions communicate through plain callables and dicts.
- Sessions can be replaced, mocked, or extracted independently.

Contract 31 documents the extraction boundary explicitly. If a future split happens, the session moves and the adapter gets a thin wrapper. No core changes required.


## 8. Coupling Surface Analysis

### 8.1 What adapters depend on from core

Every adapter imports from:

| Core module | What adapters use it for |
|------------|-------------------------|
| `medre.adapters.base` | `BaseAdapter`, `AdapterContext`, `AdapterInfo`, `AdapterCapabilities`, `AdapterDeliveryResult` |
| `medre.core.events.canonical` | `CanonicalEvent`, `EventRelation`, `NativeRef` |
| `medre.core.events.kinds` | `EventKind` |
| `medre.core.rendering.renderer` | `RenderingResult` |

These are the shared types. They are frozen dataclasses with stable shapes. They are not expected to change in breaking ways during beta.

### 8.2 What core depends on from adapters

Nothing. Core has no imports from any adapter subpackage. The pipeline runner (`medre.core.engine.pipeline`) takes adapter instances through dependency injection. It does not import `MatrixAdapter` or `LxmfAdapter`. The runner (`medre/runner.py`) does import concrete adapters, but it is an optional entry point, not core.

### 8.3 What the coupling means

The coupling is unidirectional: adapters depend on core, core does not depend on adapters. This means core could ship as a standalone package if needed, with adapters as separate packages that depend on core. The adapter framework (`BaseAdapter` and friends) lives in `medre.adapters.base`, which is part of the adapters package, not core. If adapters were extracted, the base classes would need to move to core or to a shared `medre-base` package.

This is a deferred concern. The current structure works. The unidirectional dependency means extraction is possible, not that it should happen now.


## 9. Future Split Paths (Deferred)

These paths are analyzed for completeness. None is recommended or planned for beta. They are documented so future maintainers understand what seams exist and what a split would cost.

### 9.1 Per-transport packages

Split each transport adapter into its own `medre-matrix`, `medre-meshtastic`, `medre-meshcore`, `medre-lxmf` package. Core (`medre`) would ship the event model, base adapter classes, pipeline, routing, rendering, and storage. Each transport package would ship the adapter, session, codec, renderer, config, and compat modules for that transport.

**What makes this possible now:**
- Clean adapter/session boundary (contract 31).
- Compat guards already isolate SDK imports.
- Fake adapters prove the base contract is SDK-independent.
- Unidirectional dependency (adapters depend on core, not vice versa).
- Optional extras already define the dependency grouping.

**What makes this costly later:**
- `BaseAdapter` lives in `medre.adapters.base`. Would need to move to `medre` core or a shared package.
- Test suite is integrated. Would need per-package test splitting.
- Runner imports concrete adapters. Would need a registry or entry point mechanism.
- Version coordination. Four transport packages plus core means five version numbers to manage.
- Release coordination. A core breaking change requires releasing all five packages.

**When this would make sense:**
- Transports have different release cadences.
- Some transports are stable while others are experimental.
- Users complain about unnecessary dependencies.
- A transport SDK has licensing incompatibility with the core package.

**Current assessment:** Not justified. No transport has live validation yet. Premature extraction would create coordination overhead for zero user benefit.

### 9.2 Core/engine vs. adapters split

Split into two packages: `medre-core` (event model, pipeline, routing, rendering, storage, observability) and `medre-adapters` (base adapter, all four transport adapters, fake adapters). `medre` would become a metapackage that depends on both.

**What makes this possible:**
- Core has no adapter imports.
- Adapter framework (`BaseAdapter`) is small and self-contained.
- Pipeline takes adapters through injection.

**What makes this costly:**
- `BaseAdapter` depends on core types (`CanonicalEvent`, `RenderingResult`). If it stays in adapters, adapters depends on core. If it moves to core, the adapter protocol leaks into core.
- Fake adapters depend on both core types and adapter base classes. They'd need a home.
- The current test suite tests adapters against the full pipeline. Splitting packages means splitting test infrastructure.

**Current assessment:** Not justified. The two-package split doesn't solve a real problem. Users who don't want transport SDKs already get none by default (optional extras). Users who want the full framework get one package.

### 9.3 Subprocess/service boundary

Run each transport adapter in its own process, communicating over IPC (stdio, socket, or similar). The main process runs the pipeline and routing. Transport processes run sessions and SDK clients.

**What makes this possible:**
- Adapter/session boundary is already a clean seam.
- Sessions communicate through a `message_callback` (a plain callable). Replacing a local function call with an IPC message is a local change.
- Adapter health and diagnostics are already serializable dataclasses.

**What makes this costly:**
- IPC serialization/deserialization for every inbound and outbound message.
- Process lifecycle management (spawn, health check, restart, kill).
- Error propagation across process boundaries.
- Debugging becomes harder (multi-process, async IPC).
- Maintaining the current in-process model for users who prefer it.

**Current assessment:** Not justified for beta. The subprocess boundary solves problems (crash isolation, SDK conflict, memory isolation) that do not exist yet. medre has not run in production. There is no evidence that SDK crashes, version conflicts, or memory leaks are real problems. Solving them preemptively adds complexity without evidence of need.

### 9.4 Plugin/entry-point registry

Replace the runner's concrete adapter imports with a registry based on Python entry points (`importlib.metadata`). Each transport package registers its adapter class. The runner discovers available adapters at startup.

**What makes this possible:**
- Runner already takes adapters through injection.
- `BaseAdapter` contract is stable.
- Optional extras already define the grouping.

**What makes this costly:**
- Entry point discovery adds startup complexity.
- Debugging "why isn't my adapter found" is harder than "I forgot to import it".
- The runner is optional. Most users wire adapters themselves.

**Current assessment:** Not justified. The runner is an optional convenience, not the primary integration path. Users who import and wire adapters directly don't benefit from a registry. A registry would make the runner more "magical" without solving a real problem.

### 9.5 Why none of these should happen now

All four paths share the same reason to defer: no evidence of need.

- **No user complaints about install size.** Optional extras already protect against unnecessary SDK installs.
- **No SDK conflicts in practice.** Each transport uses a different SDK. No known version collision.
- **No crash isolation need.** No production deployment has shown SDK crashes propagating to the pipeline.
- **No licensing incompatibility.** All SDKs are permissively licensed. medre is GPL-3.0-or-later.
- **No performance bottleneck from the unified package.** Import time, memory, and startup cost are all negligible for a single package.

The seams exist. The boundaries are clean. The optionality is preserved. Acting on that optionality before there is a concrete reason to do so would be overengineering.


## 10. Licensing and Distribution

### 10.1 Current license

medre is GPL-3.0-or-later licensed (`pyproject.toml`, contract 42). All code in the repository is under GPL-3.0-or-later.

### 10.2 Transport SDK licenses

| SDK | License | Compatibility with GPL-3.0-or-later |
|-----|---------|----------------------|
| mindroom-nio (nio fork) | ISC | Compatible. ISC is permissive. |
| mtjk (Meshtastic fork) | MIT or Apache 2.0 | Compatible. Both are permissive. |
| meshcore | MIT | Compatible. |
| lxmf | MIT | Compatible. |
| RNS (Reticulum) | MIT | Compatible. |

All transport SDKs are permissively licensed. None creates a licensing incompatibility with medre's GPL-3.0-or-later license. None requires special handling in distribution.

### 10.3 Licensing does not justify restructuring

Contract 42 documents the relicensing posture: the project maintainer can relicense their own code, but external contributions add constraints. This is a governance concern, not an architecture concern. It does not require package splitting, adapter extraction, or runtime redesign.

If the project license ever changes (to Apache 2.0, GPLv3, or a dual-license arrangement), the change affects the entire repository uniformly. Transport SDK licenses are all compatible with common open source licenses. No SDK would need to be isolated into a separate package for licensing reasons.


## 11. Summary

| Dimension | Current state | Future optionality |
|-----------|--------------|-------------------|
| Package structure | Single `medre` package | Clean seams for per-transport extraction |
| Optional extras | 6 groups, independent | Already define the natural split boundaries |
| Fake adapters | 6 adapters, no SDK deps | Prove adapter/session boundary is real |
| Compat guards | 4 modules, sole import sites | SDK imports isolated, extraction-ready |
| Runtime imports | Deferred to `start()` call | Two-layer defense, clean errors |
| Adapter/session boundary | Formalized (contract 31) | Natural extraction seam, no core deps in sessions |
| Core coupling | Unidirectional (adapters depend on core) | Core could ship independently |
| Licensing | All permissive, no conflicts | No licensing-driven restructuring needed |

**The current architecture intentionally remains unified for beta simplicity.** The boundaries documented here already preserve optionality for future splits, extractions, or service boundaries. Acting on that optionality before there is evidence of need would add complexity without benefit.

The right time to consider package separation is after at least one transport has been validated in live operation and after there is user feedback about install friction, version conflicts, or licensing constraints. Until then, the single package with optional extras is the correct structure.

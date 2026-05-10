# Dependency Reality Audit

> Contract version: 1
> Last updated: 2026-05-10
> Track: 9 (Transport Capability Contracts)
> Supersedes: Nothing. Consolidates dependency observations from contracts 08, 09, 10, 11, 13, 18, 19, 20.
> Status: Audit. Records install friction, platform caveats, optional import behavior, and Docker suitability per dependency.

This document audits the reality of MEDRE's optional transport dependencies:
what it actually takes to install them, where they break, what platform
quirks exist, and how they behave when absent. It is an operational reference,
not a deployment guide.

No deployment tooling, CI configuration changes, or packaging changes are
proposed.


## 1. Scope

- Per-dependency install experience and friction points.
- Platform-specific caveats.
- Optional import behavior and graceful degradation.
- Docker suitability observations.
- Version compatibility notes.

## 2. Non-goals

- Proposing Docker images or Dockerfiles.
- Creating deployment automation.
- Changing dependency versions or pinning strategy.
- Comparing dependencies on unrelated dimensions (licensing, community size).


## 3. Core Dependencies

### 3.1 msgspec (required)

| Property | Value |
|----------|-------|
| **Distribution name** | `msgspec` |
| **Pinned version** | `0.21.1` |
| **Required** | Yes (core dependency) |
| **Binary wheels** | Available for Linux (x86_64, aarch64), macOS, Windows |
| **Platform issues** | None observed. Binary wheels cover standard platforms. |
| **Install friction** | None. `pip install msgspec==0.21.1` works cleanly. |
| **Optional import** | No. Core MEDRE requires it. |
| **Docker suitability** | Excellent. Pre-built wheels; no compilation needed in standard images. |


## 4. Optional Transport Dependencies

### 4.1 mindroom-nio (Matrix — plaintext)

| Property | Value |
|----------|-------|
| **Distribution name** | `mindroom-nio` |
| **Install command** | `pip install mindroom-nio` or `pip install -e ".[matrix]"` |
| **Import name** | `nio` |
| **MEDRE compat guard** | `medre.adapters.matrix.compat.HAS_NIO` |
| **Audited version** | ≥ 0.25 |
| **Relationship** | Fork of `matrix-nio` (upstream Matrix client library) |
| **Source** | Fork maintained by project; upstream is `matrix-nio` |

**Install friction:**

- `mindroom-nio` installs as `nio`. The distribution name and import name
  differ, which can confuse debugging.
- The fork's version numbering may diverge from upstream. Pin to ≥ 0.25
  but be aware the fork may not track upstream releases.
- No known binary wheel issues on standard platforms.

**Optional import behavior:**

- When absent, `HAS_NIO = False`. The Matrix adapter's `compat.py` module
  sets the flag without raising.
- `MatrixSession.start()` raises `MatrixConnectionError` if `HAS_NIO` is
  `False` and `connection_type` is not `"fake"`.
- All unit tests pass without `mindroom-nio` installed. Tests use mocks.
- Live tests (`test_matrix_live.py`) use `pytest.importorskip("nio")`.

**Platform caveats:**

- Requires `aiohttp` (pulled as a dependency). On some platforms, `aiohttp`
  may need compilation if binary wheels are unavailable.
- No native TLS dependency — relies on Python's `ssl` module and `aiohttp`'s
  TLS handling.

**Docker suitability:**

- Suitable. Standard `pip install` works in Alpine and Debian-based images.
- If binary wheels are unavailable for the target architecture, compilation
  may require `gcc` and `python3-dev` packages.
- No hardware or device dependencies.


### 4.2 mindroom-nio[e2e] (Matrix — E2EE)

| Property | Value |
|----------|-------|
| **Distribution name** | `mindroom-nio[e2e]` |
| **Install command** | `pip install -e ".[matrix-e2e]"` |
| **Extra dependencies** | `vodozemac` (Rust-based Olm/Megolm crypto implementation) |
| **MEDRE compat guard** | `medre.adapters.matrix.compat.HAS_E2EE` |

**Install friction:**

- **High friction on some platforms.** `vodozemac` is a Rust crate with
  Python bindings. It requires either:
  - A pre-built binary wheel (available for common platforms), or
  - Rust toolchain for compilation (`cargo`, `rustc`).
- On Alpine Linux, Rust compilation may require additional system packages
  (`musl-dev`, `gcc`, `cargo`).
- On ARM platforms (Raspberry Pi, etc.), binary wheels may not be available,
  requiring compilation from source.

**Optional import behavior:**

- When absent, `HAS_E2EE = False`. The adapter falls back to plaintext mode.
- `encryption_mode="e2ee_required"` raises `MatrixConnectionError` on start
  if `HAS_E2EE` is `False`.
- `encryption_mode="e2ee_optional"` degrades gracefully to plaintext.
- All unit tests pass without E2EE dependencies.

**Platform caveats:**

- **Crypto store is a SQLite database** (managed by nio). Requires a writable
  filesystem path. Not compatible with read-only containers without volume
  mounts.
- **Device ID must be stable** across restarts for crypto continuity.
  Changing the device ID creates a new crypto identity.
- **First-run key upload** may take several seconds as the client uploads
  identity keys and one-time pre-keys to the homeserver.

**Docker suitability:**

- Moderate. Requires either pre-built wheels or Rust toolchain in the image.
  Crypto store requires a persistent volume mount for production use.
  For testing, `/tmp` is acceptable but loses state across container restarts.


### 4.3 mtjk (Meshtastic)

| Property | Value |
|----------|-------|
| **Distribution name** | `mtjk` |
| **Install command** | `pip install mtjk` |
| **Import name** | `meshtastic` |
| **MEDRE compat guard** | `medre.adapters.meshtastic.compat.HAS_MESHTASTIC` |
| **Audited version** | 2.7.8.post2+ |
| **Relationship** | Fork of upstream Meshtastic Python library |
| **Source** | `github.com/jeremiah-k/mtjk` |

**Install friction:**

- **Distribution name ≠ import name.** `pip install mtjk` installs a package
  imported as `meshtastic`. This is intentional (fork maintains import
  compatibility with upstream).
- **PyPubSub is declared in the `[meshtastic]` extra.** `pip install -e
  ".[meshtastic]"` pulls `PyPubSub>=4.0` (distribution: `PyPubSub`, import:
  `pubsub`) automatically. This was not the case previously; the prior
  packaging agent omitted it, requiring manual `pip install pubsub`.
- Pulls in `protobuf` as a dependency for message serialization.
- No compilation required; pure Python with protobuf-generated stubs.

**Optional import behavior:**

- When absent, `HAS_MESHTASTIC = False`. All Meshtastic adapter unit tests
  pass without it.
- `MeshtasticSession.start()` raises `MeshtasticConnectionError` if
  `HAS_MESHTASTIC` is `False` and `connection_type` is not `"fake"`.
- Live tests use `pytest.importorskip("meshtastic")`.

**Platform caveats:**

- **Serial access requires permissions.** On Linux, user must be in the
  `dialout` group to access `/dev/ttyUSB*` devices. Docker containers need
  `--device /dev/ttyUSB0` or equivalent.
- **TCP connections are synchronous.** The library's `TCPInterface` is
  blocking. MEDRE wraps calls in `asyncio.to_thread()`.
- **BLE requires BlueZ** on Linux and `bleak` package. BLE support is
  documented but not exercised in any live harness.
- **Radio-specific behavior** varies by firmware version. The library
  assumes a specific protobuf schema; firmware version mismatches may cause
  deserialization errors.

**Docker suitability:**

- **TCP mode: Good.** No hardware access needed; connect to a networked node.
- **Serial mode: Moderate.** Requires device passthrough (`--device` flag)
  and proper permissions inside the container.
- **BLE mode: Poor.** Requires Bluetooth hardware access, BlueZ stack,
  and container Bluetooth passthrough — complex and platform-dependent.
- **Pubsub threading:** The library uses background threads for callbacks.
  In Docker, ensure the event loop is properly bridged.


### 4.4 lxmf / RNS (LXMF over Reticulum)

| Property | Value |
|----------|-------|
| **Distribution names** | `lxmf`, `rns` (or `rnspure`) |
| **Install command** | `pip install lxmf` (pulls in `rns` automatically) |
| **Import names** | `LXMF`, `RNS` |
| **MEDRE compat guards** | `medre.adapters.lxmf.compat.HAS_LXMF`, `rns_module`, `lxmf_module` |
| **Audited versions** | lxmf 0.9.6, RNS 1.2.4 |
| **Author** | Mark Qvist |
| **License** | Reticulum License (non-standard; review for your use case) |

**Install friction:**

- **`rns` requires `pyca/cryptography` and `pyserial`.** The `cryptography`
  package may require compilation on platforms without pre-built wheels.
- **Alternative:** `pip install rnspure` for a pure-Python Reticulum (no
  `cryptography` dependency). Slower, less audited, but easier to install.
- `lxmf` pulls in `rns` as a dependency automatically.
- No compilation required for `lxmf` itself (pure Python).

**Optional import behavior:**

- When absent, `HAS_LXMF = False`. Both `RNS` and `lxmf` must be importable
  for `HAS_LXMF` to be `True`.
- `LxmfSession.start()` raises `LxmfConnectionError` if `HAS_LXMF` is
  `False` and `connection_type` is `"reticulum"`.
- All unit tests pass without `lxmf` or `rns`. Tests use fake mode.

**Platform caveats:**

- **Identity file is a raw 64-byte private key.** No encryption, no header.
  Anyone with the file can impersonate the identity. Secure storage is the
  operator's responsibility.
- **`RNS.Identity.from_file()` returns `None` on failure**, not an exception.
  Callers must check for `None`.
- **Reticulum config is platform-specific.** Default config at
  `~/.reticulum/config` may need adjustment for transport interfaces
  (TCP, serial, LoRa, AX.25, etc.).
- **License is non-standard.** The Reticulum License is not OSI-approved.
  Review for your use case before distribution.

**Docker suitability:**

- **Moderate for TCP/serial transports.** Requires writable config directory
  and identity file persistence.
- **Poor for LoRa/AX.25 hardware transports.** Requires hardware passthrough.
- **Network routing is RNS-managed.** Reticulum handles its own routing;
  no Docker networking configuration needed beyond basic connectivity.
- **Long-running processes.** Reticulum is designed for long-running daemons.
  Short-lived Docker containers may not establish stable mesh connectivity.


### 4.5 meshcore

| Property | Value |
|----------|-------|
| **Distribution name** | `meshcore` |
| **Install command** | `pip install meshcore` |
| **Import name** | `meshcore` |
| **MEDRE compat guard** | `medre.adapters.meshcore.compat.HAS_MESHCORE` |
| **Audited version** | 2.2.5 |
| **Source** | `github.com/fdlamotte/meshcore_py` |
| **License** | MIT |

**Install friction:**

- **Low friction.** Pure Python with pre-built wheels for standard platforms.
- Pulls in: `bleak` (BLE support), `pyserial-asyncio-fast` (serial support),
  `pycayennelpp` (CayenneLPP payload parsing).
- **Fully async.** All SDK methods are coroutines. No synchronous wrappers.
  This is a clean fit for MEDRE's async architecture.

**Optional import behavior:**

- When absent, `HAS_MESHCORE = False`. All MeshCore adapter unit tests pass
  without it.
- `MeshCoreSession.start()` raises `MeshCoreConnectionError` if `HAS_MESHCORE`
  is `False` and `connection_type` is not `"fake"`.
- Live tests use `pytest.importorskip("meshcore")`.

**Platform caveats:**

- **Serial access requires permissions.** Same as Meshtastic: `dialout` group
  on Linux, `--device` passthrough in Docker.
- **BLE requires `bleak`** and BlueZ on Linux. BLE is documented but not
  exercised in live harnesses.
- **SDK uses `create_tcp`/`create_serial`/`create_ble` class methods** that
  handle connection setup, including `appstart()` which triggers a
  `SELF_INFO` event.
- **Default TCP port is 4000** (from SDK examples). May differ from firmware
  default — verify with actual hardware.

**Docker suitability:**

- **TCP mode: Good.** No hardware access needed.
- **Serial mode: Moderate.** Requires device passthrough.
- **BLE mode: Poor.** Same challenges as Meshtastic BLE.
- **Async-native.** No thread/event-loop bridging needed, unlike Meshtastic.


## 5. Development Dependencies

### 5.1 pytest / pytest-asyncio

| Property | Value |
|----------|-------|
| **Distribution names** | `pytest`, `pytest-asyncio` |
| **Install command** | `pip install -e ".[dev]"` |
| **Required for** | Test suite only |

**Install friction:** None. Standard test tooling.

**Key configuration:**

- `asyncio_mode = "auto"` — all async test functions are automatically
  wrapped in an event loop.
- `markers = ["live: tests that connect to a real service or hardware"]`
- `addopts = "-m 'not live'"` — live tests excluded by default.

### 5.2 setuptools (build system)

| Property | Value |
|----------|-------|
| **Required** | `setuptools >= 68` |
| **Build backend** | `setuptools.build_meta` |

No friction. Standard build tooling.


## 6. Cross-Dependency Observations

1. **All transport dependencies are optional.** Core MEDRE (`pip install -e .`)
   installs only `msgspec`. Each transport adds its own dependency.
   This is by design: the framework works without any transport SDK.

2. **Import name ≠ distribution name** for two dependencies:
   - `mtjk` → `meshtastic`
   - `mindroom-nio` → `nio`
   This can cause confusion during debugging. The compat modules document
   the mapping.

3. **Two of five dependencies are forks** (`mindroom-nio`, `mtjk`). Fork
   maintenance is the project's responsibility. Track upstream for security
   patches and API changes.

4. **E2EE has the highest install friction** due to the `vodozemac` Rust
   dependency. Plan accordingly for environments without pre-built wheels.

5. **Reticulum's license is non-standard.** Not an install issue, but a
   distribution concern for downstream consumers.

 6. **No dependency requires Docker.** All can be installed via pip. Docker
   suitability varies by connection type (TCP = good, serial = moderate,
    BLE = poor).


## 7. Dependency Versioning Strategy

This section documents MEDRE's intended dependency versioning strategy
explicitly, so that it is not silently assumed.

### 7.1 Strategy: Minimum Validated Versions (`>=`)

MEDRE uses **minimum-version floor pins** (`>=`) for all transport
dependencies. This means:

- `pyproject.toml` declares the minimum version that has been tested and
  validated (`mindroom-nio>=0.25.3`, `mtjk>=2.7.8`, `meshcore>=2.3.7`,
  `lxmf>=0.9.6`).
- Newer compatible versions are allowed at install time.
- Compatibility with newer versions is assumed until proven otherwise
  (semantic versioning by upstream projects).

### 7.2 Exceptions

| Dependency | Pin type | Reason |
|------------|----------|--------|
| `msgspec` | Exact pin (`==0.21.1`) | Core dependency. Exact pin ensures deterministic serialization behavior across all environments. msgspec has broken forward compatibility in minor releases before. |
| `setuptools` | Minimum (`>=68`) | Build system only. Wide compatibility tolerance is acceptable. |
| `pytest` / `pytest-asyncio` | Minimum (`>=`) | Dev-only dependency. Not shipped. |

### 7.3 What MEDRE Does NOT Do

- **No strict pins (`==`) for transport SDKs.** Transport dependencies are
  optional and maintained by third parties (or forks). Strict pins would
  prevent users from getting security patches and bug fixes.
- **No compatible-range pins (`~=`) for transport SDKs.** Fork versioning
  (`mindroom-nio`, `mtjk`) may not follow semantic versioning strictly enough
  for `~=` to be meaningful.
- **No lockfile in the repository.** There is no `requirements.lock`,
  `poetry.lock`, or `pdm.lock` committed. A lockfile may be appropriate for
  CI or reproducible deployment pipelines in the future, but MEDRE does not
  currently ship one.
- **No upper-bound caps (`<`).** No dependency is capped at a maximum version.
  Upper bounds would require proactive maintenance for every upstream release.

### 7.4 Rationale

The `>=` strategy is a deliberate choice:

1. **MEDRE is a library, not an application.** Libraries should declare
   minimum compatibility and allow downstream consumers to resolve versions.
   Applications pin strictly.
2. **Two of five transport SDKs are forks.** Fork versioning is controlled by
   the project. Strict pins would be redundant (we control the version) or
   harmful (we'd miss our own updates).
3. **Core dep (`msgspec`) is exact-pinned.** The one dependency that every
   MEDRE installation requires is strictly controlled. This limits
   nondeterminism to the edges (optional transport SDKs).
4. **Testing is against specific versions.** Contract 37 (transport maturity)
   records exact tested versions. The `>=` floor pin says "this or newer,"
   not "any version." The floor is the version that was actually validated.

### 7.5 If This Strategy Changes

If MEDRE later moves to strict pins, compatible ranges, or lockfiles, this
section must be updated to reflect the new strategy. The change must be
documented as an intentional decision, not a silent drift.

Potential future strategies and when they might become appropriate:

| Strategy | When appropriate |
|----------|-----------------|
| Lockfile in repo | When MEDRE has CI or reproducible deployment requirements |
| Compatible ranges (`~=`) | When upstream SDKs demonstrate reliable semver compliance |
| Strict pins (`==`) for all | When MEDRE becomes an application rather than a library |

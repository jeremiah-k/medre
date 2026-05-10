# Developer Environment Setup Guide

> Last updated: 2026-05-10
> Status: Reproducibility guide. Goal: another developer can validate MEDRE from this document alone.

This document describes how to set up a development environment for MEDRE,
run the unit test suite, and optionally validate against live transport
endpoints. It records exact tested versions, platform assumptions, and
transport-specific setup requirements.


## 1. Core Requirements

### 1.1 Python Version

| Requirement | Value |
|-------------|-------|
| **Minimum Python** | 3.11 |
| **Tested Python** | 3.12 |
| **Why 3.11+** | Codebase uses `str | None` union syntax (PEP 604) and `from __future__ import annotations`. Python 3.10 supports the syntax with the future import; 3.11+ supports it natively. `pyproject.toml` declares `requires-python = ">=3.11"`. |

### 1.2 Operating System

| OS | Status | Notes |
|----|--------|-------|
| Linux (x86_64) | **Primary development platform** | Tested on Ubuntu/Debian derivatives. Serial access requires `dialout` group membership. |
| macOS | **Expected to work** | No known issues. Binary wheels available for all core deps. Serial access via `/dev/tty.usb*` devices. |
| Windows | **Not tested** | No known blocking issues for core functionality. Serial port names differ (`COM*`). Path separators may affect config file loading. |

### 1.3 Core Dependencies

The only required dependency is `msgspec`. Everything else is optional.

| Package | Version | Install command |
|---------|---------|-----------------|
| `msgspec` | `==0.21.1` | Automatic with `pip install -e .` |

### 1.4 Development Dependencies

| Package | Version constraint | Purpose |
|---------|-------------------|---------|
| `pytest` | `>=8.0` | Test runner |
| `pytest-asyncio` | `>=0.24` | Async test support |

Install with: `pip install -e ".[dev]"`


## 2. Quick Start

```bash
# Clone and enter the repository
git clone <repo-url> && cd meshnet-framework

# Create a virtual environment (recommended)
python3.12 -m venv .venv
source .venv/bin/activate

# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Verify: run the full unit test suite
PYTHONPATH=src pytest -q
# Expected: ~2127 passed, ~61 deselected (live tests skipped)

# Verify: compile check
python -m compileall -q src tests
# Expected: no output (clean compilation)
```

If both commands pass, your environment is correctly configured.


## 3. Transport-Specific Setup

Each transport has optional dependencies. Install only what you need.

### 3.1 Matrix (Plaintext)

```bash
pip install -e ".[matrix]"
```

| Dependency | Version | Notes |
|------------|---------|-------|
| `mindroom-nio` | `>=0.25.3` | Fork of `matrix-nio`. Installs as `nio`. |
| `aiohttp` | (transitive) | HTTP transport for Matrix protocol. |

**Platform notes:**
- Binary wheels available for Linux (x86_64, aarch64), macOS, Windows.
- No compilation required on standard platforms.
- `aiohttp` may need compilation on unusual architectures.

### 3.2 Matrix (E2EE)

```bash
pip install -e ".[matrix-e2e]"
```

| Dependency | Version | Notes |
|------------|---------|-------|
| `mindroom-nio[e2e]` | `>=0.25.3` | Adds E2EE dependencies |
| `vodozemac` | (transitive, `~=0.9`) | Rust-based Olm/Megolm implementation |

**E2EE-specific caveats:**

1. **`vodozemac` requires Rust toolchain** if binary wheels are unavailable for your platform.
   - Pre-built wheels exist for: Linux x86_64, macOS x86_64/ARM, Windows x86_64.
   - Alpine Linux: requires `apk add musl-dev gcc cargo`.
   - ARM (Raspberry Pi): may require compilation from source.
2. **Crypto store is SQLite.** Requires a writable filesystem path. The path is configured via `MATRIX_STORE_PATH` env var.
3. **Device ID must be stable** across restarts. Changing the device ID creates a new crypto identity and invalidates previous sessions.
4. **First-run key upload** takes several seconds as the client uploads identity keys and one-time pre-keys.

### 3.3 Meshtastic

```bash
pip install -e ".[meshtastic]"
```

| Dependency | Version | Notes |
|------------|---------|-------|
| `mtjk` | `>=2.7.8` | Fork of Meshtastic Python SDK. Installs as `meshtastic`. |
| `PyPubSub` | `>=4.0` | Callback mechanism (pulled by `[meshtastic]` extra). Installs as `pubsub`. |
| `protobuf` | (transitive) | Message serialization |

**Meshtastic-specific caveats:**

1. **Distribution name ≠ import name.** `pip install mtjk` installs as `import meshtastic`. This is intentional (fork maintains import compatibility).
2. **Serial access requires permissions.** On Linux:
   ```bash
   sudo usermod -aG dialout $USER
   # Log out and back in for group change to take effect
   ```
3. **Radio firmware matters.** The library assumes a specific protobuf schema. Firmware version mismatches may cause deserialization errors. Tested with firmware 2.7.19.
4. **TCP mode is synchronous** in the SDK. MEDRE wraps calls in `asyncio.to_thread()`. TCP mode requires a networked Meshtastic node.

### 3.4 MeshCore

```bash
pip install -e ".[meshcore]"
```

| Dependency | Version | Notes |
|------------|---------|-------|
| `meshcore` | `>=2.3.7` | Async-native MeshCore SDK |
| `bleak` | (transitive) | BLE support |
| `pyserial-asyncio-fast` | (transitive) | Serial support |
| `pycayennelpp` | (transitive) | CayenneLPP payload parsing |
| `pycryptodome` | (transitive) | Crypto primitives |

**MeshCore-specific caveats:**

1. **Async-native.** All SDK methods are coroutines. No thread/event-loop bridging needed. Clean fit for MEDRE's async architecture.
2. **Serial permissions.** Same as Meshtastic: `dialout` group on Linux.
3. **Default TCP port is 4000.** May differ from firmware default — verify with actual hardware.
4. **No live evidence recorded.** This transport has not been validated against real hardware. Unit tests pass but real-world behavior is unconfirmed.

### 3.5 LXMF / Reticulum

```bash
pip install -e ".[lxmf]"
```

| Dependency | Version | Notes |
|------------|---------|-------|
| `lxmf` | `>=0.9.6` | LXMF message layer |
| `rns` | (transitive) | Reticulum networking layer. Alternatively: `rnspure` for pure-Python. |
| `pyca/cryptography` | (transitive via `rns`) | May require compilation on some platforms |
| `pyserial` | (transitive via `rns`) | Serial transport support |

**LXMF/Reticulum-specific caveats:**

1. **Reticulum config is platform-specific.** Default config at `~/.reticulum/config`. May need adjustment for transport interfaces (TCP, serial, LoRa, AX.25).
2. **Identity file is a 64-byte private key.** No encryption, no header. Protect with:
   ```bash
   chmod 600 path/to/identity.key
   ```
3. **`RNS.Identity.from_file()` returns `None` on failure**, not an exception. Callers must check for `None`.
4. **Pure-Python alternative.** If `pyca/cryptography` compilation is problematic, use `rnspure`:
   ```bash
   pip install rnspure lxmf
   ```
   Slower and less audited, but easier to install.
5. **Reticulum is designed for long-running daemons.** Short-lived processes may not establish stable mesh connectivity.
6. **Non-standard license.** Reticulum License is not OSI-approved. Review before distribution.
7. **No live evidence recorded.** Same as MeshCore — unit tests only.


## 4. Running Tests

### 4.1 Unit Tests (No Hardware/Services Required)

```bash
# Full suite (excludes live tests by default)
PYTHONPATH=src pytest -q

# Specific transport unit tests
PYTHONPATH=src pytest tests/test_matrix_session.py tests/test_matrix_adapter.py -q
PYTHONPATH=src pytest tests/test_meshtastic_adapter.py tests/test_meshtastic_session.py -q
PYTHONPATH=src pytest tests/test_meshcore_adapter.py tests/test_meshcore_session.py -q
PYTHONPATH=src pytest tests/test_lxmf_adapter.py tests/test_lxmf_session.py -q
```

All unit tests use mocks. No transport SDK or hardware is required.

### 4.2 Live Tests (Require Real Endpoints)

Live tests are excluded by default via `addopts = "-m 'not live'"` in `pyproject.toml`.

```bash
# Run all live tests (requires ALL env vars for ALL transports)
PYTHONPATH=src pytest -m live -v

# Run a specific transport's live tests
PYTHONPATH=src pytest tests/test_matrix_live.py -m live -v
PYTHONPATH=src pytest tests/test_meshtastic_live.py -m live -v
```

**Required environment variables per transport:**

| Transport | Required Env Vars |
|-----------|-------------------|
| Matrix | `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, `MATRIX_ACCESS_TOKEN`, `MATRIX_ROOM_ID` |
| Matrix E2EE | All Matrix vars + `MATRIX_DEVICE_ID`, `MATRIX_STORE_PATH` |
| Meshtastic | `MESHTASTIC_CONNECTION_TYPE`, `MESHTASTIC_HOST` (for TCP) |
| MeshCore | `MESHCORE_CONNECTION_TYPE`, `MESHCORE_HOST` (for TCP) |
| LXMF | `LXMF_CONNECTION_TYPE`, `LXMF_IDENTITY_PATH` |

**Live test safety:**
- All live test files use `pytestmark = pytest.mark.live` (module-level marker).
- All live test functions use `@require_live` decorator (skips if env vars missing).
- Running `pytest` without `-m live` will never execute live tests.


## 5. Tested Environment Reference

This is the exact environment used for validation as of 2026-05-10:

| Component | Version |
|-----------|---------|
| Python | 3.12 |
| OS | Linux (Debian/Ubuntu derivative) |
| `msgspec` | 0.21.1 |
| `mindroom-nio` | 0.25.3 (fork) |
| `mtjk` | 2.7.8.post2+ (fork) |
| `meshcore` | 2.3.7 |
| `lxmf` | 0.9.6 |
| `rns` (Reticulum) | 1.2.4 |
| `pytest` | 8.x |
| `pytest-asyncio` | 0.24+ |
| Meshtastic radio firmware | 2.7.19 (LilyGO T-LORA V2.1) |
| Matrix homeserver | matrix.org (public) |

**Unit test results:** 2127 passed, 61 deselected (live tests), 0 failed.

**Live test results:** Matrix 13/13 pass (plaintext), Matrix 7/7 pass (E2EE), Meshtastic 10/10 pass. MeshCore and LXMF live tests not run.


## 6. Install All Transports

For a full development environment with all transports:

```bash
pip install -e ".[dev,matrix,matrix-e2e,meshtastic,meshcore,lxmf]"
```

For minimal environment (core only):

```bash
pip install -e ".[dev]"
```


## 7. Common Issues

| Issue | Cause | Resolution |
|-------|-------|------------|
| `ModuleNotFoundError: No module named 'nio'` | Matrix SDK not installed | `pip install -e ".[matrix]"` |
| `ModuleNotFoundError: No module named 'meshtastic'` | Meshtastic SDK not installed | `pip install -e ".[meshtastic]"` |
| `ListenerMismatchError` | Missing `pubsub` package | `pip install -e ".[meshtastic]"` (includes PyPubSub) |
| `Permission denied: /dev/ttyACM0` | Serial permissions | `sudo usermod -aG dialout $USER`, then re-login |
| `OlmUnverifiedDeviceError` | Matrix E2EE strict device check | MEDRE handles this via `ignore_unverified_devices=True`. If you see it, the adapter fix was not applied. |
| `vodozemac` build failure | No Rust toolchain | Install Rust: `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \| sh` |
| `RNS.Identity.from_file()` returns `None` | Identity file not found or corrupted | Check path, verify file is 64 bytes, check file permissions |
| `ImportError: cannot import name 'HAS_E2EE'` | Old install without E2EE extra | Reinstall: `pip install -e ".[matrix-e2e]"` |

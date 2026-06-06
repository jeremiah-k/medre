# Installation and First-Run Validation

## Prerequisites

| Requirement | Details                                                                                  |
| ----------- | ---------------------------------------------------------------------------------------- |
| Python      | 3.11 or later (3.12 tested)                                                              |
| pip         | `>= 21.3` for extras support                                                             |
| git         | For source checkout only                                                                 |
| Docker      | Optional — for integration test configs referencing containerized Synapse or meshtasticd |

## Project Metadata

- **License:** GPL-3.0-or-later
- **Status:** Pre-release — Development Status :: 3 - Alpha
- **Entry point:** `medre.cli:main` (console script: `medre`)

## Install Paths

### Source Checkout (Developers)

```bash
git clone <repo-url> && cd medre

python3 -m venv .venv
source .venv/bin/activate

pip install -e .
pip install -e ".[dev]"
```

`pip install -e .` gives you the `medre` command with fake adapters only (no test tooling). `pip install -e ".[dev]"` adds `pytest` and dev dependencies. The only core dependency is `msgspec`. Example configs are in `examples/configs/`.

### Installed Package (Operators)

From a wheel or sdist:

```bash
pip install medre-0.1.0-py3-none-any.whl
# or
pip install medre-0.1.0.tar.gz
```

This gives you the `medre` console script. You can also invoke via `python -m medre` or `python -m medre.cli` — all three are equivalent.

Installed packages do not include the `examples/` directory. Use `medre config sample` to generate an equivalent config.

## Transport Extras

Optional transport SDKs add real connectivity. Without them, only fake adapters are available.

| Extra        | Install command                  | Notes                                                                |
| ------------ | -------------------------------- | -------------------------------------------------------------------- |
| `matrix`     | `pip install -e ".[matrix]"`     | Matrix plaintext via `mindroom-nio`                                  |
| `matrix-e2e` | `pip install -e ".[matrix-e2e]"` | Matrix E2EE (adds `vodozemac`; needs Rust toolchain on Alpine/ARM)   |
| `meshtastic` | `pip install -e ".[meshtastic]"` | Meshtastic LoRa via `mtjk`; serial requires `dialout` group on Linux |
| `meshcore`   | `pip install -e ".[meshcore]"`   | MeshCore radio; serial requires `dialout` group                      |
| `lxmf`       | `pip install -e ".[lxmf]"`       | LXMF / Reticulum; Reticulum uses a non-OSI license                   |

Combine as needed: `pip install -e ".[matrix,meshtastic,dev]"`.

### Transport-Specific Setup

#### Matrix (Plaintext)

```bash
pip install -e ".[matrix]"
```

Binary wheels available for Linux (x86_64, aarch64), macOS, Windows. No compilation required on standard platforms.

#### Matrix (E2EE)

```bash
pip install -e ".[matrix-e2e]"
```

- `vodozemac` requires a Rust toolchain if binary wheels are unavailable.
  - Pre-built wheels: Linux x86_64, macOS x86_64/ARM, Windows x86_64.
  - Alpine: `apk add musl-dev gcc cargo`.
  - ARM (Raspberry Pi): may require source compilation.
- Crypto store is SQLite, derived automatically — no operator configuration.
- Device ID discovered automatically via `whoami()` on startup.
- First-run key upload takes several seconds.

#### Meshtastic

```bash
pip install -e ".[meshtastic]"
```

- Distribution name is `mtjk`, import name is `meshtastic` (fork maintains import compatibility).
- Serial access requires Linux `dialout` group:
  ```bash
  sudo usermod -aG dialout $USER
  # Log out and back in
  ```
- Radio firmware matters — tested with firmware 2.7.19.
- TCP mode is synchronous in the SDK; MEDRE wraps in `asyncio.to_thread()`.

#### MeshCore

```bash
pip install -e ".[meshcore]"
```

- Async-native SDK — clean fit for MEDRE's async architecture.
- Serial permissions same as Meshtastic (`dialout` group).
- Default TCP port is 4000.
- No live evidence recorded — unit tests only.

#### LXMF / Reticulum

```bash
pip install -e ".[lxmf]"
```

- Reticulum config at `~/.reticulum/config` may need adjustment for transport interfaces.
- Identity file is a 64-byte private key — protect with `chmod 600`.
- `RNS.Identity.from_file()` returns `None` on failure (not an exception) — callers check for `None`.
- Pure-Python alternative if `pyca/cryptography` compilation is problematic:
  ```bash
  pip install rnspure lxmf
  ```
- Reticulum is designed for long-running daemons; short-lived processes may not establish stable connectivity.
- Non-standard license (not OSI-approved).
- No live evidence recorded — unit tests only.

## Verify the Install

Run these in order. Every one should succeed without errors.

```bash
# Version check (three equivalent invocations)
medre version
# Expected: medre 0.1.0, Python version, platform info

# Show resolved paths
medre paths
# Expected: config, state, data, cache, log directory paths

# List adapter types and SDK availability
medre adapters
# Expected: all transports show "not installed" (core-only).
# Fake adapters work regardless of SDK status.
```

## Generate and Validate a Config

`medre config sample` generates a complete TOML config using fake adapters. It works out of the box without any optional SDKs or network access.

```bash
# Print the sample config
medre config sample

# Save and validate
medre config sample > /tmp/medre-alpha.toml
medre config check --config /tmp/medre-alpha.toml
# Expected: "Config valid", 2 enabled fake adapters,
# route inventory showing matrix_radio_bridge as active.
```

The sample config uses `adapter_kind = "fake"` for all adapters. To switch to real adapters, change `adapter_kind` to `"real"` and fill in transport-specific credentials.

For live Matrix setups, use the auth CLI:

```bash
medre adapter matrix auth login \
  --homeserver https://matrix.example.com \
  --user @bot:example.com
```

This performs an interactive login and saves credentials to the Matrix sidecar JSON file. Accepted flags: `--homeserver`, `--user`, `--password`, `--password-stdin`. See [configuration.md](configuration.md) for credential handling details.

Config file locations:

- Default: `~/.config/medre/config.toml` (XDG)
- Override: set `MEDRE_HOME` environment variable
- Explicit: pass `--config /path/to/config.toml` to any command

## Run a Smoke Test

`medre smoke` builds a runtime from a config, injects a test message through the full pipeline, and reports results.

**Source checkout (has `examples/` directory):**

```bash
medre smoke --config examples/configs/fake-bridge-smoke.toml --json
# Expected: JSON with "status": "passed", event_id, delivery receipts.
# For persistent evidence, edit the config to set storage.backend = "sqlite"
# with a storage.path, then re-run.
```

**Installed package (use generated config):**

```bash
medre config sample > /tmp/medre-alpha.toml
medre smoke --config /tmp/medre-alpha.toml --json
# Expected: same as above.
```

Storage backend is determined by the config file. The default shipped config uses `storage.backend = "memory"` (ephemeral). For persistent evidence, set `storage.backend = "sqlite"` with a `path` in the config.

Without `--config`, `medre smoke` looks for `examples/configs/fake-bridge-smoke.toml` relative to the source tree — works in development checkouts only.

## Inspect Results

After a smoke test, use inspect commands to examine what happened. These are read-only and need only `--storage-path`.

```bash
medre inspect event <event_id> --storage-path /tmp/medre-alpha.db
medre inspect receipts --event <event_id> --storage-path /tmp/medre-alpha.db
```

See [operator-workflows.md](operator-workflows.md) for the full investigation workflow.

## Validate Example Configs (Source Checkout Only)

The `examples/configs/` directory exists only in the source repository. If you installed from a wheel, skip this — use `medre config sample` instead.

```bash
medre config check --config examples/configs/fake-multi-adapter.toml
medre config check --config examples/configs/fake-bridge-smoke.toml
medre config check --config examples/configs/fake-retry-smoke.toml

# Real adapter configs (will show credential errors without env vars)
medre config check --config examples/configs/matrix.toml
medre config check --config examples/configs/meshtastic-serial.toml
```

Available example configs:

| Config                          | Purpose                            | Requires SDKs?   |
| ------------------------------- | ---------------------------------- | ---------------- |
| `fake-multi-adapter.toml`       | All four fake adapters with routes | No               |
| `fake-bridge-smoke.toml`        | Cross-adapter bridge patterns      | No               |
| `fake-retry-smoke.toml`         | Retry worker with fake adapters    | No               |
| `matrix.toml`                   | Real Matrix adapter                | Yes (matrix)     |
| `meshtastic-serial.toml`        | Real Meshtastic serial adapter     | Yes (meshtastic) |
| `mixed-matrix-meshtastic.toml`  | Mixed real Matrix + Meshtastic     | Yes (both)       |
| `docker-matrix-bridge.toml`     | Docker Synapse + Meshtastic        | Yes + Docker     |
| `docker-meshtastic-bridge.toml` | Docker meshtasticd + Matrix        | Yes + Docker     |
| `docker-bridge-smoke.toml`      | Docker integration smoke test      | Yes + Docker     |

## Docker Validation (Optional)

Some example configs reference Docker services (Synapse, meshtasticd). These require Docker Compose.

```bash
docker compose -f docker-compose.integration.yaml up -d
source examples/env/docker.env.example  # edit with real values first
medre config check --config examples/configs/docker-bridge-smoke.toml
docker compose -f docker-compose.integration.yaml down
```

Docker is not required for the basic install or fake-only smoke path.

## Run the Test Suite (Source Checkout Only)

```bash
PYTHONPATH=src pytest -q
# Expected: all non-live tests pass; live tests deselected by default.

python -m compileall -q src tests
# Expected: no output (clean compilation)
```

Live tests are excluded by default via `pyproject.toml`:

```toml
[tool.pytest.ini_options]
addopts = "-m 'not live'"
```

To run live tests (requires real credentials and hardware):

```bash
PYTHONPATH=src pytest -m live -v
```

## Common Issues

| Issue                                               | Cause                                | Resolution                                                                      |
| --------------------------------------------------- | ------------------------------------ | ------------------------------------------------------------------------------- |
| `ModuleNotFoundError: No module named 'nio'`        | Matrix SDK not installed             | `pip install -e ".[matrix]"`                                                    |
| `ModuleNotFoundError: No module named 'meshtastic'` | Meshtastic SDK not installed         | `pip install -e ".[meshtastic]"`                                                |
| `ListenerMismatchError`                             | Missing `pubsub` package             | `pip install -e ".[meshtastic]"` (includes PyPubSub)                            |
| `Permission denied: /dev/ttyACM0`                   | Serial permissions                   | `sudo usermod -aG dialout $USER`, re-login                                      |
| `OlmUnverifiedDeviceError`                          | Matrix E2EE strict device check      | Adapter handles via `ignore_unverified_devices=True`                            |
| `vodozemac` build failure                           | No Rust toolchain                    | Install Rust: `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \| sh` |
| `RNS.Identity.from_file()` returns `None`           | Identity file not found or corrupted | Check path, verify file is 64 bytes, check permissions                          |
| `ImportError: cannot import name 'HAS_E2EE'`        | Old install without E2EE extra       | `pip install -e ".[matrix-e2e]"`                                                |

## Quick Reference

```bash
# Install
pip install -e ".[dev]"                    # source checkout
pip install medre-0.1.0-py3-none-any.whl   # wheel

# Verify
medre version && medre paths && medre adapters

# Generate config
medre config sample > /tmp/medre-alpha.toml
medre config check --config /tmp/medre-alpha.toml

# Smoke test (config controls storage backend)
medre smoke --config /tmp/medre-alpha.toml --json

# Inspect
medre inspect event <event_id> --storage-path /tmp/medre-alpha.db
medre inspect receipts --event <event_id> --storage-path /tmp/medre-alpha.db

# Test suite (source checkout)
PYTHONPATH=src pytest -q
python -m compileall -q src tests
```

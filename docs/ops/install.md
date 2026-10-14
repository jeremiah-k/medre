# Installation and First-Run Validation

## Prerequisites

| Requirement | Details                                                                                                                    |
| ----------- | -------------------------------------------------------------------------------------------------------------------------- |
| Python      | 3.11 or later. CI runs the suite on CPython 3.11–3.14 on Ubuntu Linux (x86_64); other hosts unverified                     |
| uv          | Recommended for source checkouts; see the [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/) |
| pip         | Supported for wheel/sdist installs and existing virtual environments                                                       |
| git         | For source checkout only                                                                                                   |
| Docker      | Optional — for integration test configs referencing containerized Synapse or meshtasticd                                   |

## Project Metadata

- **License:** GPL-3.0-or-later
- **Status:** Pre-release — Development Status :: 3 - Alpha
- **Entry point:** `medre.cli:main` (console script: `medre`)

## Install Paths

### Source Checkout (Developers)

```bash
git clone <repo-url> && cd medre

uv sync --locked --extra dev
source .venv/bin/activate
```

`uv sync --locked` gives you an editable MEDRE install and the core dependencies
(`msgspec` and `PyYAML`). Adding `--extra dev` installs pytest and build tools;
transport SDKs are declared by the separate adapter distributions. Commands in this guide assume an
activated environment. Without activation, use `uv run --no-sync medre ...`
after syncing. Example configs are in `examples/configs/`.

Sync core first, then install the required adapter distributions. A later
core sync removes those separately installed packages. The `dev` extra is not a uv dependency group.
See [the development environment guide](../dev/environment.md) for dependency
updates, environment reuse, and CI behavior.

### Existing pip Source Workflow

pip installation remains supported without uv. In a separate or existing
virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .           # core runtime only
# For developer tools:
python -m pip install -e ".[dev]"
# Install sibling adapter distributions and their SDK requirements as needed:
python -m pip install -e ../medre-adapter-matrix -e ../medre-adapter-meshtastic
```

pip respects the declared requirements but does not read `uv.lock`; transitive
versions can differ from the locked checkout and CI environment.

### Installed Package (Operators)

From a wheel or sdist:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install medre-0.1.0-py3-none-any.whl
# or
pip install medre-0.1.0.tar.gz
```

This gives you the `medre` console script. You can also invoke via `python -m medre` or `python -m medre.cli` — all three are equivalent.

Installed packages do not include the `examples/` directory. Use `medre config sample` to generate an equivalent config.

### Verify a Built Artifact (source checkout)

From a source checkout, one command builds a wheel (via the pinned
`build` tooling), installs it into a clean proof environment together with
its core dependencies, and runs the installed-package proof. It fails
loudly on any mismatch — no skips, no fallbacks:

```bash
uv run --no-sync python scripts/check_installed_package.py

# To re-verify a wheel you already built instead of building a new one:
uv run --no-sync python scripts/check_installed_package.py --wheel dist/medre-0.1.0-py3-none-any.whl
```

The script lives in the source repository — it is not part of the installed
wheel — and is wired into the existing CI test job, so every push checks the
installed-package path, not just the source tree. It deliberately installs the
wheel with pip to verify consumer compatibility independently of uv. The proof
environment installs the core dependencies (msgspec, PyYAML) from PyPI, so it needs
network access.

## External transport adapters

MEDRE core contains no built-in transport implementations or SDK dependencies.
Install the transport _distributions_ required by a deployment. Adapters use
Python `medre.adapters` entry points regardless of whether they came from PyPI,
Git (commit-pinned), or a local checkout. See
[adapter installation](adapter-installation.md) for a reproducible uv deployment
manifest that pins all sources and dependencies in one lockfile.

| Adapter distribution        | SDK provided by its metadata                 |
| --------------------------- | -------------------------------------------- |
| `medre-adapter-matrix`      | `mindroom-nio`                               |
| `medre-adapter-matrix[e2e]` | `mindroom-nio[e2e]` and crypto support       |
| `medre-adapter-meshtastic`  | `mtjk` and `PyPubSub`                        |
| `medre-adapter-meshcore`    | `meshcore`                                   |
| `medre-adapter-lxmf`        | `lxmf` and `rns` (review upstream licensing) |

For ad-hoc development after synchronizing MEDRE core, use
`uv pip install -e ../medre-adapter-matrix` or
`uv pip install 'medre-adapter-matrix[e2e]'`, depending on source.
These install the adapter and its SDK together; they are **not** MEDRE extras.
Core-only installation works with no adapters installed. Transport-specific
fakes are part of their adapter distributions, not part of MEDRE core.

Use `medre adapters check --config bridge.yaml` to diagnose missing configured
providers; normal `medre run` never clones or installs packages.

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

`medre config sample` generates a complete YAML config using fake adapters. Install the referenced adapter distributions first; their fakes need no native
SDKs or network access.

```bash
# Print the sample config
medre config sample

# Save and validate
medre config sample > /tmp/medre-alpha.yaml
medre config check --config /tmp/medre-alpha.yaml
# Expected: "Config valid", installed sample providers enabled,
# route inventory showing sample_bridge as active.
```

The sample config includes fake contributions from installed providers only; a single provider gets two instances, and no contributions produce empty adapter/route sections with installation guidance. To switch to real adapters, change `adapter_kind` to `real` and fill in transport-specific credentials.

For live Matrix setups, use the auth CLI:

```bash
medre adapter matrix auth login \
  --homeserver https://matrix.example.com \
  --user @bot:example.com
```

This performs an interactive login and saves credentials to the Matrix sidecar JSON file. Accepted flags: `--homeserver`, `--user`, `--password`, `--password-stdin`. For non-interactive automation, prefer `--password-stdin` (supplying the password via a file or pipe) over `--password`, which is visible in shell history and process listings. See [configuration.md](configuration.md) for credential handling details.

Config file locations:

- Default: `~/.config/medre/config.yaml` (XDG)
- Override: set `MEDRE_HOME` environment variable
- Explicit: pass `--config /path/to/config.yaml` to any command

## Run a Smoke Test

`medre smoke` builds a runtime from a config, injects a test message through the full pipeline, and reports results.

**Source checkout (has `examples/` directory):**

Install the Matrix, Meshtastic, and MeshCore adapter distributions before using
this three-provider example. Fake implementations need no native SDKs.

```bash
medre smoke --config examples/configs/fake-bridge-smoke.yaml --json
# Expected: JSON with "status": "passed", event_id, delivery receipts.
# For persistent evidence, edit the config to set storage.backend: sqlite
# with a storage.path, then re-run.
```

**Installed package (use generated config):**

```bash
medre config sample > /tmp/medre-alpha.yaml
medre smoke --config /tmp/medre-alpha.yaml --json
# Expected: same as above.
```

Storage backend is determined by the config file. The default shipped config uses `storage.backend: memory` (ephemeral). For persistent evidence, set `storage.backend: sqlite` with a `path` in the config.

Without `--config`, `medre smoke` looks for `examples/configs/fake-bridge-smoke.yaml` relative to the source tree — works in development checkouts only.

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
medre config check --config examples/configs/fake-multi-adapter.yaml
medre config check --config examples/configs/fake-bridge-smoke.yaml
medre config check --config examples/configs/fake-retry-smoke.yaml

# Real adapter configs (will show credential errors without env vars)
medre config check --config examples/configs/matrix.yaml
medre config check --config examples/configs/meshtastic-serial.yaml
```

Available example configs:

| Config                          | Purpose                            | Requires SDKs?   |
| ------------------------------- | ---------------------------------- | ---------------- |
| `fake-multi-adapter.yaml`       | All four fake adapters with routes | No               |
| `fake-bridge-smoke.yaml`        | Cross-adapter bridge patterns      | No               |
| `fake-retry-smoke.yaml`         | Retry worker with fake adapters    | No               |
| `matrix.yaml`                   | Real Matrix adapter                | Yes (matrix)     |
| `meshtastic-serial.yaml`        | Real Meshtastic serial adapter     | Yes (meshtastic) |
| `docker-matrix-bridge.yaml`     | Docker Synapse + Meshtastic        | Yes + Docker     |
| `docker-meshtastic-bridge.yaml` | Docker meshtasticd + Matrix        | Yes + Docker     |
| `docker-bridge-smoke.yaml`      | Docker integration smoke test      | Yes + Docker     |

## Docker Validation (Optional)

Some example configs reference Docker services (Synapse, meshtasticd). These require Docker Compose.

```bash
docker compose -f docker-compose.integration.yaml up -d
source examples/env/docker.env.example  # edit with real values first
medre config check --config examples/configs/docker-bridge-smoke.yaml
docker compose -f docker-compose.integration.yaml down
```

Docker is not required for the basic install or fake-only smoke path.

## Run the Test Suite (Source Checkout Only)

```bash
uv sync --locked --extra dev
uv pip install --no-deps -e ../medre-adapter-matrix -e ../medre-adapter-meshtastic \
  -e ../medre-adapter-meshcore -e ../medre-adapter-lxmf
uv run --no-sync pytest -q
# Expected: default-selected tests pass; gated tiers are deselected.

uv run --no-sync python -m compileall -q src tests
# Expected: no output (clean compilation)
```

Default selection excludes live, Docker, hardware, optional SDK, local
integration, and soak tiers. The marker policy is defined in `pyproject.toml`.
Select a gated tier explicitly after installing its owning adapter and SDKs and preparing its
service or hardware prerequisites; see [testing](../dev/testing.md).

To run live tests (requires real credentials and hardware):

```bash
uv run --no-sync pytest -m live -v
```

## Common Issues

| Issue                                               | Cause                                     | Resolution                                                                                                             |
| --------------------------------------------------- | ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `ModuleNotFoundError: No module named 'nio'`        | Matrix SDK not installed                  | `uv pip install medre-adapter-matrix`                                                                                  |
| `ModuleNotFoundError: No module named 'meshtastic'` | Meshtastic SDK not installed              | `uv pip install medre-adapter-meshtastic`                                                                              |
| `ListenerMismatchError`                             | Missing `pubsub` package                  | `uv pip install medre-adapter-meshtastic` (includes PyPubSub)                                                          |
| `Permission denied: /dev/ttyACM0`                   | Serial permissions                        | `sudo usermod -aG dialout $USER`, re-login                                                                             |
| `OlmUnverifiedDeviceError`                          | Matrix peer-device trust check            | E2EE sends intentionally use `ignore_unverified_devices=True`; update to current MEDRE if this surfaces                |
| Cross-signing diagnostics require reset             | Local/server own-device identity mismatch | Back up Matrix state; restore matching state or run authenticated `auth login --adapter-id <id> --reset-cross-signing` |
| `vodozemac` build failure                           | No Rust toolchain                         | Install Rust: `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \| sh`                                        |
| `RNS.Identity.from_file()` returns `None`           | Identity file not found or corrupted      | Check path, verify file is 64 bytes, check permissions                                                                 |
| `ImportError: cannot import name 'HAS_E2EE'`        | Old install without E2EE extra            | `uv pip install 'medre-adapter-matrix[e2e]'`                                                                           |
| `PreReleaseSchemaMismatchError` at startup          | Database created by older prerelease      | Back up and remove the database: `medre storage reset`, then rerun                                                     |

## Quick Reference

```bash
# Prepare a source checkout (wheel installation is covered above)
uv sync --locked --extra dev
source .venv/bin/activate

# Verify
medre version && medre paths && medre adapters

# Install the Matrix and Meshtastic adapter distributions before this sample.
# Generate config
medre config sample > /tmp/medre-alpha.yaml
medre config check --config /tmp/medre-alpha.yaml

# Smoke test (config controls storage backend)
medre smoke --config /tmp/medre-alpha.yaml --json

# The smoke report includes event_id and storage_path (or use the
# config's storage path setting).  Use --storage-path to persist:
#   medre smoke --config /tmp/medre-alpha.yaml --storage-path /tmp/medre-alpha.db --json

# Inspect (use the storage path from the smoke report or config)
medre inspect event <event_id> --storage-path /tmp/medre-alpha.db
medre inspect receipts --event <event_id> --storage-path /tmp/medre-alpha.db

# Test suite (source checkout)
uv run --no-sync pytest -q
uv run --no-sync python -m compileall -q src tests
```

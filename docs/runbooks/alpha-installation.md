# Alpha Installation and First-Run Runbook

> Document version: 1
> Last updated: 2026-05-16
> Status: Alpha operator guide. Covers fake-only install and optional extras.

This runbook covers installing medre and running your first commands. It works
for two scenarios:

1. **Fake-only install** (no hardware, no network, no SDKs). This is the
   recommended starting point.
2. **With optional transport extras** (Matrix, Meshtastic, MeshCore, or LXMF
   SDKs). Add these when you have the corresponding hardware or server access.


## Prerequisites

- Python >= 3.11
- pip
- (Optional) Docker, for integration test configs that reference containerized
  Synapse or meshtasticd instances. Docker is not required for the basic
  fake-only path.


## 1. Install

There are two installation paths. Choose one:

### 1a. Source checkout (developers)

```bash
# Clone the repository
git clone <repo-url> && cd medre

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install core package with dev dependencies (includes pytest)
pip install -e ".[dev]"
```

This gives you the `medre` command with fake adapters only, plus access to
example configs in `examples/configs/`. No transport SDKs are installed.
The only core dependency is `msgspec`.

### 1b. Installed package (operators)

If you received a wheel or sdist (not a git clone), install directly:

```bash
# From a wheel
pip install medre-0.1.0-py3-none-any.whl

# Or from a source distribution
pip install medre-0.1.0.tar.gz
```

This gives you the `medre` console script. You can also invoke via
`python -m medre` or `python -m medre.cli` — all three are equivalent.

**Important:** Installed packages do **not** include the `examples/` directory
from the repository. Use `medre config sample` to generate an equivalent
config instead of referencing `examples/configs/*.toml`.

To add optional transport SDKs later:

```bash
pip install -e ".[matrix]"          # Matrix plaintext (mindroom-nio)
pip install -e ".[matrix-e2e]"      # Matrix with E2EE (adds vodozemac)
pip install -e ".[meshtastic]"      # Meshtastic LoRa (mtjk)
pip install -e ".[meshcore]"        # MeshCore radio (meshcore)
pip install -e ".[lxmf]"            # LXMF / Reticulum (lxmf + rns)
```

Combine as needed: `pip install -e ".[matrix,meshtastic,dev]"`.

Platform notes for optional extras:

| Extra | Notes |
|-------|-------|
| `matrix-e2e` | `vodozemac` needs a Rust toolchain on Alpine and ARM platforms |
| `meshtastic` | Serial access requires `dialout` group on Linux |
| `meshcore` | Serial access requires `dialout` group on Linux |
| `lxmf` | Reticulum uses a non-OSI license; review before distribution |

See the [Developer Environment Guide](developer-environment.md) for detailed
platform-specific setup per transport.


## 2. Verify the install

Run these commands in order. Every one should succeed without errors.

```bash
# Check version (three equivalent invocations)
medre version
# Or: python -m medre version
# Or: python -m medre.cli version
# Expected: medre 0.1.0, Python version, platform info

# Show resolved paths (XDG or MEDRE_HOME based)
medre paths
# Expected: config, state, data, cache, log directory paths

# List adapter types and SDK availability
medre adapters
# Expected: all transports show "not installed" (core-only install).
# Fake adapters work regardless of SDK status.
```


## 3. Generate and validate a config

`medre config sample` generates a complete TOML config that uses fake adapters.
It works out of the box without any optional SDKs or network access. This is
the recommended way to get a config for any installation — source checkout or
installed package.

```bash
# Print the sample config
medre config sample

# Save it and validate
medre config sample > /tmp/medre-alpha.toml
medre config check --config /tmp/medre-alpha.toml
# Expected: "Config valid", adapter inventory showing 2 enabled fake adapters,
# route inventory showing matrix_radio_bridge as active.
```

The sample config uses `adapter_kind = "fake"` for all adapters. To switch to
real adapters, change `adapter_kind` to `"real"` and fill in transport-specific
credentials (homeserver URL, access token, serial port, etc.).

For live Matrix setups, use the auth CLI to obtain and store an access token
without manual editing:

```bash
medre adapter matrix auth login \
  --config /tmp/medre-alpha.toml \
  --adapter matrix \
  --homeserver https://matrix.example.com \
  --user @bot:example.com
```

This performs an interactive login and writes the `homeserver`, `user_id`, and
`access_token` directly into the config file. It does not start the runtime,
never prints the token to the terminal, and prompts for the password securely
unless `--password-stdin` is given. See `docs/runbooks/secure-credentials.md`
for full credential handling guidance.

Config file locations:

- Default: `~/.config/medre/config.toml` (XDG)
- Override: set `MEDRE_HOME` environment variable
- Explicit: pass `--config /path/to/config.toml` to any command

See the [Configuration Runbook](configuration.md) for the full TOML schema.


## 4. Run a smoke test

`medre smoke` builds a runtime from a config, injects a test message through
the full pipeline, and reports results. It is the fastest way to prove the
architecture works.

**Source checkout (has `examples/` directory):**

```bash
medre smoke --config examples/configs/fake-bridge-smoke.toml \
  --storage-path /tmp/medre-alpha.db --json
# Expected: JSON with "status": "passed", an event_id, and delivery receipts.
```

**Installed package (no `examples/` directory — use generated config):**

```bash
# Generate a config first
medre config sample > /tmp/medre-alpha.toml

# Smoke test with the generated config
medre smoke --config /tmp/medre-alpha.toml \
  --storage-path /tmp/medre-alpha.db --json
# Expected: JSON with "status": "passed", an event_id, and delivery receipts.
```

> **Note:** The sample config from `medre config sample` is designed for config
> validation and basic smoke testing. For advanced smoke scenarios (specific
> adapter IDs, unidirectional routes, retry workers), use the dedicated configs
> in `examples/configs/` from a source checkout. Those configs are not shipped
> in the wheel — they are source-repo documentation.

The SQLite database at the storage path now contains the canonical event,
delivery receipts, and native references for inspection.

Without `--storage-path`, the smoke test uses an in-memory database that is
discarded after the run.

Without `--config`, `medre smoke` looks for `examples/configs/fake-bridge-smoke.toml`
relative to the source tree. This works in development checkouts but not from
an installed package. Use `--config` explicitly in that case.


## 5. Inspect results

After a smoke test, use inspect commands to examine what happened. These are
read-only and need only `--storage-path`.

```bash
# Get the event_id from smoke output, then:
medre inspect event <event_id> --storage-path /tmp/medre-alpha.db

# Check delivery receipts
medre inspect receipts --event <event_id> --storage-path /tmp/medre-alpha.db
```

See the [Alpha Walkthrough](alpha-walkthrough.md) for the full inspect and
investigation workflow.


## 6. Optional: Validate example configs (source checkout only)

The `examples/configs/` directory exists **only in the source repository**. It
is not shipped in the wheel or sdist. These configs are reference documentation
for operators working from a checkout.

If you installed from a wheel, skip this section — use `medre config sample`
instead (see section 3).

```bash
# Fake-only configs that work without any SDKs
medre config check --config examples/configs/fake-multi-adapter.toml
medre config check --config examples/configs/fake-bridge-smoke.toml
medre config check --config examples/configs/fake-retry-smoke.toml

# Configs with real adapters (will show credential errors without env vars)
medre config check --config examples/configs/matrix.toml
medre config check --config examples/configs/meshtastic-serial.toml
```

Available example configs:

| Config | Purpose | Requires SDKs? |
|--------|---------|----------------|
| `fake-multi-adapter.toml` | All four fake adapters with routes | No |
| `fake-bridge-smoke.toml` | Cross-adapter bridge patterns | No |
| `fake-retry-smoke.toml` | Retry worker with fake adapters | No |
| `matrix.toml` | Real Matrix adapter (credential placeholder) | Yes (matrix) |
| `meshtastic-serial.toml` | Real Meshtastic serial adapter | Yes (meshtastic) |
| `mixed-matrix-meshtastic.toml` | Mixed real Matrix + Meshtastic | Yes (both) |
| `docker-matrix-bridge.toml` | Docker Synapse + Meshtastic integration | Yes + Docker |
| `docker-meshtastic-bridge.toml` | Docker meshtasticd + Matrix integration | Yes + Docker |
| `docker-bridge-smoke.toml` | Docker integration smoke test | Yes + Docker |


## 7. Docker validation (optional)

Some example configs reference Docker services (Synapse homeserver,
meshtasticd). These are for integration testing and require Docker Compose.

```bash
# Start Docker services
docker compose -f docker-compose.integration.yaml up -d

# Set required environment variables (see examples/env/docker.env.example)
source examples/env/docker.env.example  # edit with real values first

# Run Docker integration configs
medre config check --config examples/configs/docker-bridge-smoke.toml

# Tear down
docker compose -f docker-compose.integration.yaml down
```

Docker is not required for the basic install or the fake-only smoke path.


## Quick reference

```bash
# Install — source checkout
pip install -e ".[dev]"

# Install — wheel/sdist (no examples/ directory available)
pip install medre-0.1.0-py3-none-any.whl

# Verify (all equivalent)
medre version
python -m medre version
python -m medre.cli version
medre adapters
medre paths

# Generate config (works for both source and installed)
medre config sample > /tmp/medre-alpha.toml
medre config check --config /tmp/medre-alpha.toml

# Smoke test — source checkout (has examples/)
medre smoke --config examples/configs/fake-bridge-smoke.toml --storage-path /tmp/medre-alpha.db --json

# Smoke test — installed package (use generated config)
medre smoke --config /tmp/medre-alpha.toml --storage-path /tmp/medre-alpha.db --json

# Inspect (use event_id from smoke output)
medre inspect event <event_id> --storage-path /tmp/medre-alpha.db
medre inspect receipts --event <event_id> --storage-path /tmp/medre-alpha.db

# Run the test suite (source checkout only)
PYTHONPATH=src pytest -q

# Compile check (source checkout only)
python -m compileall -q src tests
```

For the full operator workflow including replay and investigation, see the
[Alpha Walkthrough](alpha-walkthrough.md). For detailed transport setup, see
the [Developer Environment Guide](developer-environment.md).

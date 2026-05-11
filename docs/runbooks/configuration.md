# MEDRE Configuration

## Overview

MEDRE uses **TOML configuration files** as the primary configuration source.
Environment variables provide overrides for every config field and are
especially useful for secrets and container deployments. Path defaults follow
the XDG Base Directory Specification, with a single-directory `MEDRE_HOME` mode
for Docker and Kubernetes.

The configuration system is only used by the MEDRE runtime (`medre run`).
Library consumers construct adapter configs directly in Python — no config file
is needed (see [Library Usage vs Runtime Usage](#library-usage-vs-runtime-usage)).


## Configuration Search Order

When the runtime starts, it locates its config file by searching in this order:

1. **`--config` CLI flag** — explicit path passed on the command line.
   Must exist, or the runtime exits with an error.

2. **`MEDRE_CONFIG` environment variable** — full path to a TOML file.

3. **`$MEDRE_HOME/config.toml`** — if `MEDRE_HOME` is set, MEDRE looks for
   `config.toml` inside that directory.

4. **XDG config path** — `~/.config/medre/config.toml` (or
   `$XDG_CONFIG_HOME/medre/config.toml` when set).

5. **`./medre.toml`** — fallback in the current working directory.

The first file found wins. If no file is found, the runtime exits with a
`ConfigNotFoundError`.

Use `medre config check` to verify which file is being loaded and whether it
parses correctly.


## TOML Schema Reference

### `[runtime]`

Top-level runtime behaviour.

```toml
[runtime]
name = "medre"                  # string — instance name (informational)
shutdown_timeout_seconds = 10   # int — graceful shutdown deadline in seconds
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | `"medre"` | Instance name used in logs and diagnostics. |
| `shutdown_timeout_seconds` | int | `10` | Maximum seconds to wait for adapters to stop before forcing exit. |

### `[logging]`

Logging configuration.

```toml
[logging]
level = "INFO"    # INFO, DEBUG, WARNING, ERROR
format = "text"   # text or json
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `level` | string | `"INFO"` | Log level. One of `INFO`, `DEBUG`, `WARNING`, `ERROR`. |
| `format` | string | `"text"` | Output format. `text` for human-readable, `json` for structured logging. |

### `[storage]`

Persistence and database configuration.

```toml
[storage]
backend = "sqlite"
path = "{state}/medre.sqlite"   # supports path placeholders
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `backend` | string | `"sqlite"` | Storage backend. Currently only `sqlite` is supported. |
| `path` | string | `None` | Database file path. Supports [path placeholders](#path-placeholders). When `None`, defaults to `{state}/medre.sqlite`. |

> **Storage model:** MEDRE uses a single configured storage backend (one SQLite database at `{state}/medre.sqlite`). This database holds canonical events, delivery receipts, native references, replay state, and cross-adapter relationships. There is no per-adapter database. Transport-owned local files (e.g. Matrix crypto stores, LXMF identities) live under adapter state roots (`{state}/adapters/<adapter_id>/`).

### `[adapters.matrix.INSTANCE_NAME]`

Each Matrix adapter instance is a separate TOML table. `INSTANCE_NAME` becomes
the `adapter_id` unless overridden by the `adapter_id` field.

```toml
[adapters.matrix.main]
enabled = true
adapter_kind = "real"                     # real (default) or fake
adapter_id = "main"                           # optional, defaults to instance name
homeserver = "https://matrix.example.com"
user_id = "@bot:example.com"
access_token = "syt_secret_token_here"
room_allowlist = ["!room:example.com"]
sync_timeout_ms = 30000
encryption_mode = "plaintext"          # plaintext, e2ee_required, e2ee_optional
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Whether this adapter instance is active. |
| `adapter_kind` | string | `"real"` | `"real"` builds the live adapter; `"fake"` builds a simulated adapter without optional SDK imports. |
| `adapter_id` | string | instance name | Unique identifier. Defaults to the TOML table key. |
| `homeserver` | string | *(required)* | Matrix homeserver URL. Must start with `http://` or `https://`. |
| `user_id` | string | *(required)* | Fully-qualified Matrix user ID (e.g. `@user:matrix.org`). |
| `access_token` | string | `""` | Access token for authentication. **Treat as a secret.** |
| `room_allowlist` | list of string | `None` | Room IDs to accept. `None` means all rooms. |
| `metadata_embedding_mode` | string | `"safe"` | How metadata is embedded in messages. |
| `sync_timeout_ms` | int | `30000` | Long-polling sync timeout in milliseconds. |
| `encryption_mode` | string | `"plaintext"` | Encryption policy: `plaintext`, `e2ee_required`, or `e2ee_optional`. E2EE modes handle device verification and crypto store internally. |
| `require_encrypted_rooms` | bool | `false` | When `true`, only operate in rooms with encryption enabled. Invalid with `encryption_mode="plaintext"`. |

**Note:** `device_id` and `store_path` are not operator-facing configuration.
MEDRE derives the device ID from `whoami()` on session start and uses an
internal store path under the resolved state directory (`{state}/adapters/{adapter_id}/matrix/store`).
These fields exist on `MatrixConfig` for internal use but should not appear in
operator TOML files.

You can define multiple Matrix instances:

```toml
[adapters.matrix.bot1]
enabled = true
homeserver = "https://matrix.example.com"
user_id = "@bot1:example.com"
access_token = "syt_..."

[adapters.matrix.bot2]
enabled = false
homeserver = "https://matrix.example.com"
user_id = "@bot2:example.com"
access_token = "syt_..."
```

### `[adapters.meshtastic.INSTANCE_NAME]`

```toml
[adapters.meshtastic.radio]
enabled = false
adapter_kind = "real"                     # real (default) or fake
adapter_id = "radio"
connection_type = "serial"          # fake, tcp, serial, ble
serial_port = "/dev/ttyACM0"
host = "meshtastic.local"           # tcp only
port = 4403                         # tcp only
ble_address = ""                    # ble only
meshnet_name = "MyMesh"
default_channel = 0
channel_mapping = {0 = "general", 1 = "admin"}
message_delay_seconds = 0.5
startup_backlog_suppress_seconds = 5.0
sync_timeout_ms = 30000
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Whether this adapter instance is active. |
| `adapter_kind` | string | `"real"` | `"real"` builds the live adapter; `"fake"` builds a simulated adapter without optional SDK imports. |
| `adapter_id` | string | instance name | Unique identifier. |
| `connection_type` | string | `"fake"` | Connection mode: `fake`, `tcp`, `serial`, or `ble`. |
| `host` | string | `None` | Hostname or IP for TCP connections. Required when `connection_type="tcp"`. |
| `port` | int | `None` | Port number for TCP connections. |
| `serial_port` | string | `None` | Serial device path. Required when `connection_type="serial"`. |
| `ble_address` | string | `None` | BLE MAC address. Required when `connection_type="ble"`. |
| `meshnet_name` | string | `""` | Human-readable meshnet name (informational). |
| `default_channel` | int | `0` | Default radio channel index for outbound messages. |
| `channel_mapping` | dict of int→string | `{}` | Maps channel indices to human-readable names. |
| `message_delay_seconds` | float | `0.5` | Minimum delay between outbound messages (pacing). |
| `startup_backlog_suppress_seconds` | float | `5.0` | Seconds after start to suppress stale backlog packets. |
| `sync_timeout_ms` | int | `30000` | Timeout for sync operations in milliseconds. |

### `[adapters.meshcore.INSTANCE_NAME]`

```toml
[adapters.meshcore.radio]
enabled = false
adapter_kind = "real"                     # real (default) or fake
adapter_id = "radio"
connection_type = "serial"          # fake, tcp, serial, ble
serial_port = "/dev/ttyUSB0"
host = "meshcore.local"             # tcp only
port = 4403                         # tcp only
ble_address = ""                    # ble only
meshnet_name = ""
default_channel = 0
channel_mapping = {}
message_delay_seconds = 0.5
startup_backlog_suppress_seconds = 5.0
sync_timeout_ms = 30000
identity = "my-node"
pubkey = "abcdef0123456789"
node_config = {}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Whether this adapter instance is active. |
| `adapter_kind` | string | `"real"` | `"real"` builds the live adapter; `"fake"` builds a simulated adapter without optional SDK imports. |
| `adapter_id` | string | instance name | Unique identifier. |
| `connection_type` | string | `"fake"` | Connection mode: `fake`, `tcp`, `serial`, or `ble`. |
| `host` | string | `None` | Hostname or IP for TCP connections. Required when `connection_type="tcp"`. |
| `port` | int | `None` | Port number for TCP connections. |
| `serial_port` | string | `None` | Serial device path. Required when `connection_type="serial"`. |
| `ble_address` | string | `None` | BLE MAC address. |
| `meshnet_name` | string | `""` | Human-readable meshnet name (informational). |
| `default_channel` | int | `0` | Default radio channel index for outbound messages. |
| `channel_mapping` | dict of int→string | `{}` | Maps channel indices to human-readable names. |
| `message_delay_seconds` | float | `0.5` | Minimum delay between outbound messages (pacing). |
| `startup_backlog_suppress_seconds` | float | `5.0` | Seconds after start to suppress stale backlog packets. |
| `sync_timeout_ms` | int | `30000` | Timeout for sync operations in milliseconds. |
| `identity` | string | `None` | MeshCore node identity string (e.g. node name). |
| `pubkey` | string | `None` | Public key as a hex string. |
| `node_config` | dict | `{}` | Opaque dict for node-specific settings. Must not contain secret keys. |

### `[adapters.lxmf.INSTANCE_NAME]`

```toml
[adapters.lxmf.local]
enabled = false
adapter_kind = "real"                     # real (default) or fake
adapter_id = "local"
connection_type = "reticulum"       # fake, reticulum
display_name = "MEDRE"
stamp_cost = 8
default_delivery_method = "direct"  # direct, opportunistic, propagated, paper
meshnet_name = ""
default_channel = 0
message_delay_seconds = 0.5
metadata_embedding = true
identity_path = "{state}/lxmf/identity"
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Whether this adapter instance is active. |
| `adapter_kind` | string | `"real"` | `"real"` builds the live adapter; `"fake"` builds a simulated adapter without optional SDK imports. |
| `adapter_id` | string | instance name | Unique identifier. |
| `connection_type` | string | `"fake"` | Connection mode: `fake` or `reticulum`. |
| `display_name` | string | `""` | Display name for LXMF announces. |
| `stamp_cost` | int | `8` | Stamp cost. `0` means no stamp required. |
| `default_delivery_method` | string | `"direct"` | Delivery method: `direct`, `opportunistic`, `propagated`, or `paper`. |
| `meshnet_name` | string | `""` | Human-readable meshnet name (informational). |
| `default_channel` | int | `0` | Default channel index for outbound messages. |
| `message_delay_seconds` | float | `0.5` | Minimum delay between outbound messages (pacing). |
| `metadata_embedding` | bool | `true` | Whether to embed MEDRE metadata envelopes in LXMF fields. |
| `identity_path` | string | `None` | Path to Reticulum identity file. Supports [path placeholders](#path-placeholders). |


## XDG Default Paths

When `MEDRE_HOME` is **not** set, MEDRE follows the XDG Base Directory
Specification. Each path category is resolved independently:

```
Config:    $XDG_CONFIG_HOME/medre/    or  ~/.config/medre/
State:     $XDG_STATE_HOME/medre/     or  ~/.local/state/medre/
Data:      $XDG_DATA_HOME/medre/      or  ~/.local/share/medre/
Cache:     $XDG_CACHE_HOME/medre/     or  ~/.cache/medre/
Logs:      {state}/logs/
Database:  {state}/medre.sqlite
Adapters:  {state}/adapters/<adapter_id>/
Matrix:    {state}/adapters/<adapter_id>/matrix/store/
LXMF:      {state}/adapters/<adapter_id>/lxmf/
Meshtastic:{state}/adapters/<adapter_id>/meshtastic/
MeshCore:  {state}/adapters/<adapter_id>/meshcore/
```

The primary config file is at `$XDG_CONFIG_HOME/medre/config.toml`
(`~/.config/medre/config.toml` by default).

Use `medre paths` to print the resolved paths for your environment.


## MEDRE_HOME (Single-Directory Mode)

Setting the `MEDRE_HOME` environment variable switches MEDRE into
single-directory mode. All paths are resolved under one root:

```
MEDRE_HOME=/opt/medre
Config:    /opt/medre/config.toml
State:     /opt/medre/state/
Data:      /opt/medre/data/
Cache:     /opt/medre/cache/
Logs:      /opt/medre/logs/
Database:  /opt/medre/state/medre.sqlite
Adapters:  /opt/medre/state/adapters/<adapter_id>/
Matrix:    /opt/medre/state/adapters/<adapter_id>/matrix/store/
LXMF:      /opt/medre/state/adapters/<adapter_id>/lxmf/
Meshtastic:/opt/medre/state/adapters/<adapter_id>/meshtastic/
MeshCore:  /opt/medre/state/adapters/<adapter_id>/meshcore/
```

Use this mode when:

- **Docker / Podman** — mount a single volume at `MEDRE_HOME`, e.g. `MEDRE_HOME=/opt/medre`.
- **Kubernetes** — use a PersistentVolumeClaim mounted at one path.
- **Portable deployments** — run from a USB drive or single directory.
- **Development** — keep all MEDRE data in one place for easy cleanup.

Example:

```bash
export MEDRE_HOME=/opt/medre
medre paths    # verify paths
medre run      # reads /opt/medre/config.toml
```


## Path Placeholders

TOML config values that represent filesystem paths support placeholder expansion.
Placeholders are enclosed in curly braces and expand to resolved directory paths:

| Placeholder | Expands to |
|-------------|-----------|
| `{config}` | Config directory (`config_dir`, or `config_file.parent` in MEDRE_HOME mode) |
| `{state}` | State directory |
| `{data}` | Data directory |
| `{cache}` | Cache directory |
| `{logs}` | Log directory |

Example usage:

```toml
[storage]
path = "{state}/medre.sqlite"

[adapters.matrix.main]
encryption_mode = "e2ee_required"

[adapters.lxmf.local]
identity_path = "{state}/lxmf/identity"
```

Unrecognised placeholders cause a `MedrePathsError` at startup.


## Environment Variable Overrides

Environment variables override TOML values. They always win.

Adapter env vars target a specific adapter instance via target resolution:

1. **`MEDRE_<TRANSPORT>_ADAPTER_ID` is set** — targets the named adapter.
   Overrides its fields if it exists in the config; creates it if not.
2. **Exactly one adapter of that transport configured** — targets it
   automatically (no `ADAPTER_ID` needed).
3. **No adapters of that transport configured** — creates a new default
   adapter sourced entirely from env vars.
4. **Multiple adapters and no `ADAPTER_ID` specified** — raises
   `ConfigValidationError`. You must specify which adapter to target.

Boolean env vars accept: `1`, `true`, `yes` (truthy) and `0`, `false`, `no`
(falsy). List env vars are comma-separated.

### Core

| Variable | Type | Maps to | Default |
|----------|------|---------|---------|
| `MEDRE_HOME` | string | Single-directory root path | *(not set)* |
| `MEDRE_CONFIG` | string | Config file path | *(not set)* |
| `MEDRE_DB_PATH` | string | `storage.path` | `{state}/medre.sqlite` |
| `MEDRE_LOG_LEVEL` | string | `logging.level` | `INFO` |

### Matrix

| Variable | Type | Target field | Default |
|----------|------|---------|---------|
| `MEDRE_MATRIX_ENABLED` | bool | `enabled` | `true` |
| `MEDRE_MATRIX_ADAPTER_ID` | string | Target adapter selection | *(auto)* |
| `MEDRE_MATRIX_HOMESERVER` | string | `homeserver` | *(required)* |
| `MEDRE_MATRIX_USER_ID` | string | `user_id` | *(required)* |
| `MEDRE_MATRIX_ACCESS_TOKEN` | string | `access_token` | `""` |
| `MEDRE_MATRIX_ROOM_ALLOWLIST` | comma-separated list | `room_allowlist` | `None` (all rooms) |
| `MEDRE_MATRIX_ENCRYPTION_MODE` | string | `encryption_mode` | `"plaintext"` |

#### Internal/test-only overrides

The following Matrix environment variables exist for test harnesses and
internal use.  Normal runtime operation derives these automatically and
operators should not set them.

| Variable | Type | Target field | Default |
|----------|------|---------|---------|
| `MEDRE_MATRIX_DEVICE_ID` | string | `device_id` | `None` (derived via `whoami()`) |
| `MEDRE_MATRIX_STORE_PATH` | string | `store_path` | `None` (derived under state dir) |

### Meshtastic

| Variable | Type | Target field | Default |
|----------|------|---------|---------|
| `MEDRE_MESHTASTIC_ENABLED` | bool | `enabled` | `true` |
| `MEDRE_MESHTASTIC_ADAPTER_ID` | string | Target adapter selection | *(auto)* |
| `MEDRE_MESHTASTIC_CONNECTION_TYPE` | string | `connection_type` | `"fake"` |
| `MEDRE_MESHTASTIC_SERIAL_PORT` | string | `serial_port` | `None` |
| `MEDRE_MESHTASTIC_HOST` | string | `host` | `None` |
| `MEDRE_MESHTASTIC_PORT` | int | `port` | `None` |

### MeshCore

| Variable | Type | Target field | Default |
|----------|------|---------|---------|
| `MEDRE_MESHCORE_ENABLED` | bool | `enabled` | `true` |
| `MEDRE_MESHCORE_ADAPTER_ID` | string | Target adapter selection | *(auto)* |
| `MEDRE_MESHCORE_CONNECTION_TYPE` | string | `connection_type` | `"fake"` |
| `MEDRE_MESHCORE_SERIAL_PORT` | string | `serial_port` | `None` |
| `MEDRE_MESHCORE_HOST` | string | `host` | `None` |
| `MEDRE_MESHCORE_PORT` | int | `port` | `None` |
| `MEDRE_MESHCORE_BLE_ADDRESS` | string | `ble_address` | `None` |

### LXMF

| Variable | Type | Target field | Default |
|----------|------|---------|---------|
| `MEDRE_LXMF_ENABLED` | bool | `enabled` | `true` |
| `MEDRE_LXMF_ADAPTER_ID` | string | Target adapter selection | *(auto)* |
| `MEDRE_LXMF_CONNECTION_TYPE` | string | `connection_type` | `"fake"` |
| `MEDRE_LXMF_IDENTITY_PATH` | string | `identity_path` | `None` |
| `MEDRE_LXMF_DISPLAY_NAME` | string | `display_name` | `""` |
| `MEDRE_LXMF_DESTINATION_HASH` | string | *(reserved)* | `None` |


## Environment Variable `.env` Files

`.env` files are a deployment convenience for container runtimes. They are not
part of MEDRE's configuration model — MEDRE reads environment variables from the
process environment, not from `.env` files directly.

With Docker Compose or Podman:

```bash
# .env file
MEDRE_HOME=/opt/medre
MEDRE_LOG_LEVEL=DEBUG
MEDRE_MATRIX_ENABLED=true
MEDRE_MATRIX_HOMESERVER=https://matrix.example.com
MEDRE_MATRIX_USER_ID=@bot:example.com
MEDRE_MATRIX_ACCESS_TOKEN=syt_...
```

```yaml
# docker-compose.yaml
services:
  medre:
    image: medre:latest
    env_file: .env
    volumes:
      - medre-data:/opt/medre
    devices:
      - /dev/ttyACM0:/dev/ttyACM0
```

The container runtime loads the `.env` file into the process environment before
starting MEDRE. MEDRE sees the variables as regular environment variables.


## Secrets Management

- **Access tokens are secrets.** `MEDRE_MATRIX_ACCESS_TOKEN` is redacted in log
  output and diagnostics (`***REDACTED***`).
- **Prefer environment variables for tokens** over embedding them in TOML files.
  Environment variables are harder to accidentally commit to version control.
- **Prefer Docker secrets or mounted secret files** in production deployments.
- **Never commit secrets to version control.** Add `config.toml` and `.env`
  files to `.gitignore`.
- **LXMF identity files** are 64-byte raw private keys with no encryption.
  File permission management (`chmod 600`) is the operator's responsibility.
- **MeshCore `node_config`** rejects keys named `private_key`, `secret`, or
  `password` at validation time.

See [docs/runbooks/secure-credentials.md](secure-credentials.md) for additional
guidance.


## Docker / K8s Usage

### Recommended Docker pattern

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install ".[matrix,matrix-e2e,meshtastic]"
ENV MEDRE_HOME=/opt/medre
ENTRYPOINT ["medre"]
CMD ["run"]
```

```bash
# Build
docker build -t medre .

# Run with config file in volume
docker run -d \
  --name medre \
  -v /srv/medre:/opt/medre \
  medre run

# Run with env vars instead of config file
docker run -d \
  --name medre \
  -e MEDRE_HOME=/opt/medre \
  -e MEDRE_MATRIX_ENABLED=true \
  -e MEDRE_MATRIX_HOMESERVER=https://matrix.example.com \
  -e MEDRE_MATRIX_USER_ID=@bot:example.com \
  -e MEDRE_MATRIX_ACCESS_TOKEN=syt_... \
  -v medre-state:/opt/medre \
  medre run
```

### Serial passthrough

For Meshtastic and MeshCore adapters using serial connections:

```bash
docker run -d \
  --device /dev/ttyACM0:/dev/ttyACM0 \
  -v /srv/medre:/opt/medre \
  medre run
```

### Matrix E2EE store persistence

The Matrix crypto store is derived automatically under the resolved state
directory (`{state}/adapters/{adapter_id}/matrix/store`). It must persist across
restarts for E2EE session keys to survive. Mount the state volume:

With `MEDRE_HOME=/opt/medre`, the store resolves to `/opt/medre/state/adapters/main/matrix/store`
for an adapter with `adapter_id="main"`. Ensure the volume is persistent.


## Library Usage vs Runtime Usage

### Library usage (no config file needed)

MEDRE's adapter configs are plain Python dataclasses. Import and construct them
directly:

```python
from medre.adapters.matrix import MatrixAdapter, MatrixConfig

config = MatrixConfig(
    adapter_id="my-bot",
    homeserver="https://matrix.example.com",
    user_id="@bot:example.com",
    access_token="syt_...",
    room_allowlist={"!room:example.com"},
)
adapter = MatrixAdapter(config)
```

No TOML file, no CLI, no runtime. The config system is entirely optional for
library consumers.

### Runtime usage (config file driven)

```bash
# Generate a sample config
medre config sample > ~/.config/medre/config.toml

# Edit the config, then run
medre run

# Or specify a config path explicitly
medre run --config /path/to/config.toml
```


## CLI Commands

```
medre run [--config PATH]
    Start the MEDRE runtime. Loads config, resolves paths, starts adapters.

medre config check [--config PATH]
    Load and validate the config file. Prints config source, paths, and
    adapter status. Exits with code 1 on errors.

medre config sample
    Print a complete sample TOML configuration to stdout. Redirect to a
    file to use as a starting point.

medre paths
    Print all resolved MEDRE paths (config, state, data, cache, logs,
    database, matrix store). Useful for debugging path resolution.

medre version
    Print the MEDRE version.
```

All commands that accept `--config` follow the
[Configuration Search Order](#configuration-search-order) when the flag is
omitted.

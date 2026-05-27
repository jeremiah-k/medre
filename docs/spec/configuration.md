# Configuration

TOML configuration system, XDG paths, environment overrides, and the config
model.

See also: [architecture.md](architecture.md), [adapter-runtime.md](adapter-runtime.md),
[routing-delivery.md](routing-delivery.md).

---

## 1. Config Package Structure

The configuration system lives under `medre.config`:

| Module      | Purpose                                                                                                            |
| ----------- | ------------------------------------------------------------------------------------------------------------------ |
| `paths.py`  | XDG-compatible path resolution with `MEDRE_HOME` single-directory override                                         |
| `model.py`  | Typed frozen-dataclass configuration models                                                                        |
| `errors.py` | Configuration error hierarchy (`ConfigError` -> `ConfigNotFoundError`, `ConfigValidationError`, `ConfigFileError`) |
| `loader.py` | TOML file loader with priority search order                                                                        |
| `env.py`    | `MEDRE_*` environment variable override layer                                                                      |
| `sample.py` | Sample config generator (`medre config sample`)                                                                    |
| `routes.py` | Route configuration models (`RouteConfig`, `RouteConfigSet`, `RouteDirectionality`, `BridgePolicy`)                |

Per-transport config dataclasses live in `medre.config.adapters.*`:
`MatrixConfig`, `MeshtasticConfig`, `MeshCoreConfig`, `LxmfConfig`.

Config validation errors are `ValueError` subclasses (`AdapterConfigError`),
not runtime adapter errors.

## 2. Root Configuration Model

```python
@dataclass(frozen=True)
class RuntimeConfig:
    runtime: RuntimeOptions       # name, shutdown_timeout_seconds
    logging: LoggingConfig        # level, format, overrides
    storage: StorageConfig        # backend, path
    limits: RuntimeLimits         # inflight delivery/replay limits, drain timeout
    retry: RetryConfig            # retry worker: enabled, interval, batch_size, max_attempts
    adapters: AdapterConfigSet    # grouped by transport type
    routes: RouteConfigSet        # ordered, validated route definitions
```

### 2.1 RuntimeOptions

| Field                      | Default   | Description               |
| -------------------------- | --------- | ------------------------- |
| `name`                     | `"medre"` | Runtime name              |
| `shutdown_timeout_seconds` | `10`      | Graceful shutdown timeout |

### 2.2 LoggingConfig

| Field       | Default  | Description                                               |
| ----------- | -------- | --------------------------------------------------------- |
| `level`     | `"INFO"` | MEDRE namespace logger level                              |
| `format`    | `"text"` | Log format preset (`"text"` or `"json"`)                  |
| `overrides` | `{}`     | Per-logger namespace level overrides (e.g., suppress nio) |

### 2.3 StorageConfig

| Field     | Default    | Description                                                |
| --------- | ---------- | ---------------------------------------------------------- |
| `backend` | `"sqlite"` | Storage backend (currently only `"sqlite"`)                |
| `path`    | `None`     | Database path. `None` uses default: `{state}/medre.sqlite` |

### 2.4 RuntimeLimits

| Field                              | Default | Description                          |
| ---------------------------------- | ------- | ------------------------------------ |
| `max_inflight_deliveries`          | `100`   | Max concurrent in-flight deliveries  |
| `max_inflight_replay_events`       | `100`   | Max concurrent replay events         |
| `shutdown_drain_timeout_seconds`   | `10`    | Max wait for in-flight work to drain |
| `delivery_acquire_timeout_seconds` | `1.0`   | Timeout acquiring a delivery slot    |

### 2.5 RetryConfig

| Field              | Default | Description                                    |
| ------------------ | ------- | ---------------------------------------------- |
| `enabled`          | `False` | Whether the retry worker is active             |
| `interval_seconds` | `10.0`  | Polling interval for due retry receipts        |
| `batch_size`       | `20`    | Max retry receipts processed per cycle         |
| `max_attempts`     | `3`     | Max total delivery attempts before dead-letter |

## 3. TOML Schema

```toml
[runtime]
name = "medre"
shutdown_timeout_seconds = 10

[logging]
level = "INFO"          # DEBUG | INFO | WARNING | ERROR
format = "text"         # text | json
# [logging.overrides]
# nio = "WARNING"

[storage]
backend = "sqlite"
path = "{state}/medre.sqlite"

[limits]
max_inflight_deliveries = 100
max_inflight_replay_events = 100
shutdown_drain_timeout_seconds = 10
delivery_acquire_timeout_seconds = 1.0

[retry]
enabled = false
interval_seconds = 10.0
batch_size = 20
max_attempts = 3

# --- Adapter instances (multi-instance per type) ---

[adapters.matrix.<name>]
enabled = true
adapter_kind = "real"    # real | fake
homeserver = "https://matrix.example.com"
user_id = "@bot:example.com"
access_token = "<matrix-access-token>"
room_allowlist = ["!room:example.com"]
device_id = "MEDREBOT"
encryption_mode = "plaintext"  # plaintext | e2ee_required | e2ee_optional

[adapters.meshtastic.<name>]
enabled = false
connection_type = "serial"    # serial | tcp
serial_port = "/dev/ttyACM0"
host = "localhost"
port = 4403

[adapters.meshcore.<name>]
enabled = false
connection_type = "serial"    # serial | tcp | ble
serial_port = "/dev/ttyUSB0"
host = "localhost"
port = 4403

[adapters.lxmf.<name>]
enabled = false
connection_type = "reticulum"
identity_path = "{state}/lxmf/identity"
display_name = "MEDRE"

# --- Routes ---

[[routes]]
# Note: the TOML key `from_adapter` maps to `source_adapters` as a
# single-element array in the internal RouteConfig model.
id = "mesh-to-matrix"
from_adapter = "meshcore-radio-1"
to_adapter = "matrix-home"
from_channel = "general"
to_channel = "general"
enabled = true
event_kinds = ["message.text"]
direction = "bidirectional"
```

## 4. Configuration Search Order

The loader searches for configuration files in this priority order:

1. `--config` CLI flag (explicit path, must exist)
2. `MEDRE_CONFIG` environment variable
3. `$MEDRE_HOME/config.toml` (when `MEDRE_HOME` is set)
4. `$XDG_CONFIG_HOME/medre/config.toml` (defaults to `~/.config/medre/config.toml`)
5. `./medre.toml` (local project fallback)

The loader returns a `(RuntimeConfig, ConfigSource, MedrePaths)` triple.

## 5. Environment Overrides

`MEDRE_*` environment variables are applied as overrides on top of the loaded
TOML config. The original config is never mutated — overrides produce a new
frozen instance via `dataclasses.replace()`.

### 5.1 Core Overrides

| Variable          | Target                 |
| ----------------- | ---------------------- |
| `MEDRE_DB_PATH`   | `config.storage.path`  |
| `MEDRE_LOG_LEVEL` | `config.logging.level` |

### 5.2 Adapter Overrides

Adapter overrides target configured adapter instances by normalized adapter
token using the pattern `MEDRE_ADAPTER__<TOKEN>__<FIELD>`. The token is derived
from the TOML `adapter_id` by uppercasing and replacing non-alphanumeric
characters with underscores.

Env overrides do not create virtual adapter instances; the target adapter MUST
already exist in TOML.

Examples:

- `MEDRE_ADAPTER__MATRIX_PRIMARY__ACCESS_TOKEN`
- `MEDRE_ADAPTER__RADIO_A__SERIAL_PORT`
- `MEDRE_ADAPTER__MESHCORE_TBEAM__BLE_ADDRESS`

### 5.3 Route Overrides

Route overrides follow the pattern `MEDRE_ROUTE__<TOKEN>__<FIELD>`.

### 5.4 Unsupported Patterns

Legacy transport-prefixed variables (`MEDRE_MATRIX_*`, `MEDRE_MESHTASTIC_*`,
`MEDRE_MESHCORE_*`, `MEDRE_LXMF_*`) are unsupported and rejected with
migration guidance.

## 6. XDG Path Model

| Category     | XDG Default                                         | MEDRE_HOME Mode                                 |
| ------------ | --------------------------------------------------- | ----------------------------------------------- |
| Config       | `$XDG_CONFIG_HOME/medre/` or `~/.config/medre/`     | `$MEDRE_HOME/config.toml`                       |
| State        | `$XDG_STATE_HOME/medre/` or `~/.local/state/medre/` | `$MEDRE_HOME/state/`                            |
| Data         | `$XDG_DATA_HOME/medre/` or `~/.local/share/medre/`  | `$MEDRE_HOME/data/`                             |
| Cache        | `$XDG_CACHE_HOME/medre/` or `~/.cache/medre/`       | `$MEDRE_HOME/cache/`                            |
| Logs         | `state_dir/logs`                                    | `$MEDRE_HOME/logs/`                             |
| Database     | `state_dir/medre.sqlite`                            | `$MEDRE_HOME/state/medre.sqlite`                |
| Matrix store | `state_dir/adapters/<id>/matrix/store/`             | `$MEDRE_HOME/state/adapters/<id>/matrix/store/` |

Path placeholders `{config}`, `{state}`, `{data}`, `{cache}`, `{logs}` are
expanded via `MedrePaths.expand_placeholder()`. Directories are never created
by pure path resolution — only during runtime startup.

## 7. Adapter Config Wrapping

Each adapter type has a runtime wrapper (`MatrixRuntimeConfig`,
`MeshtasticRuntimeConfig`, `MeshCoreRuntimeConfig`, `LxmfRuntimeConfig`) that:

1. Parses the TOML table via `from_toml_dict(instance_name, data)`
2. Separates runtime fields (`enabled`, `adapter_id`) from adapter-specific fields
3. Coerces TOML types (list to set, string-keyed to int-keyed dicts)
4. Constructs and validates the adapter's own config dataclass
5. Stores the validated adapter config in a `.config` attribute

`AdapterConfigSet` groups all adapters by transport type and provides
`all_enabled()` iteration. Duplicate adapter IDs across transports are rejected
at validation time.

## 8. CLI

```bash
medre run [--config PATH]           # Start the MEDRE runtime
medre config check [--config PATH]  # Validate config file
medre config sample                 # Print a sample TOML config
medre paths                         # Print resolved MEDRE paths
medre version                       # Print MEDRE version
```

## 9. Configuration Error Hierarchy

```text
ConfigError                          # Base
  ConfigNotFoundError                # File not found
  ConfigValidationError              # Validation failure
  ConfigFileError                    # File read/parse error
```

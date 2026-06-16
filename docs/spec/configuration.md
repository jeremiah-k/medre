# Configuration

YAML configuration system, XDG paths, environment overrides, and the config
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
| `loader.py` | YAML file loader with priority search order                                                                        |
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

## 3. YAML Schema

The typed config model in `src/medre/config/model.py` and
`src/medre/config/routes.py` is authoritative. The operator-facing
[Configuration Reference](../ops/configuration.md) renders that model as
per-section YAML examples and field tables; this section normatively asserts
the structural requirements that the spec relies on.

### 3.1 File Format and Boring YAML Subset

- The file suffix MUST be `.yaml` or `.yml`. The loader rejects `.toml` (and
  any other unsupported suffix) with the migration message _"TOML config
  files are no longer supported; use YAML (.yaml or .yml)."_; see §4 for
  discovery and rejection semantics.
- `pyproject.toml` is unrelated project metadata (build, pytest config,
  tooling). The MEDRE runtime never reads it as a runtime config.
- The YAML parser accepts only a deliberately boring subset of YAML:
  - Explicit mappings and sequences of plain scalars (`str`, `int`,
    `float`, `bool`, `null`).
  - **No** anchors (`&`), aliases (`*`), or merge keys (`<<`).
  - **No** custom or exotic tags (`!!binary`, `!!set`, `!!omap`, etc.).
  - **No** duplicate mapping keys — the last-wins behaviour of plain YAML
    loaders is treated as a misconfiguration and rejected at parse time.
  - **No** multi-document streams. The root node MUST be a mapping.
- Values YAML could misread MUST be quoted: Matrix room IDs
  (`"!room:server"`), MXIDs (`"@user:server"`), string-valued channel keys
  (`"0"`), and path placeholders like `"{state}/medre.sqlite"`.

### 3.2 Top-Level Sections

The root mapping MAY contain the following keys. The typed leaf tables in
§2.1–§2.5 are the field-level normative reference for each non-adapter
section.

| Key        | Typed model        | Field table | Notes                                         |
| ---------- | ------------------ | ----------- | --------------------------------------------- |
| `runtime`  | `RuntimeOptions`   | §2.1        | Carries the nested `limits` table — see §2.4. |
| `logging`  | `LoggingConfig`    | §2.2        |                                               |
| `storage`  | `StorageConfig`    | §2.3        |                                               |
| `retry`    | `RetryConfig`      | §2.5        |                                               |
| `adapters` | `AdapterConfigSet` | §3.3        | Per-transport grouping.                       |
| `routes`   | `RouteConfigSet`   | §3.4        | Per-route targeting and channel mapping.      |

`runtime.limits` is the YAML path for the `RuntimeLimits` table (§2.4). A
top-level `limits:` key is rejected by the loader as an unknown root key —
the typed model only reads `runtime.limits`. `medre config check` surfaces
this as a `ConfigValidationError` with `section_path="<root>"` naming the
unknown key and the accepted root keys.

### 3.3 Adapter Instances

Each transport has its own sub-table under
`adapters.<transport>.<instance_name>`:

- `adapters.matrix.<name>` → `MatrixConfig`
- `adapters.meshtastic.<name>` → `MeshtasticConfig`
- `adapters.meshcore.<name>` → `MeshCoreConfig`
- `adapters.lxmf.<name>` → `LxmfConfig`

Each instance goes through a runtime wrapper (`MatrixRuntimeConfig`,
`MeshtasticRuntimeConfig`, `MeshCoreRuntimeConfig`, `LxmfRuntimeConfig`)
that consumes three wrapper-level fields before constructing the adapter
dataclass:

| Wrapper field  | Type   | Default       | Description                                                                                                                                                                       |
| -------------- | ------ | ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `enabled`      | bool   | `true`        | Whether this adapter instance is active at startup.                                                                                                                               |
| `adapter_id`   | string | instance name | Unique identifier across all transports. Duplicate IDs are rejected at validation time.                                                                                           |
| `adapter_kind` | string | `"real"`      | Selects the live (`"real"`) or simulated (`"fake"`) adapter implementation. Any other value raises `ConfigValidationError` with `section_path="adapters.<transport>.<instance>"`. |

All remaining keys in the instance table are forwarded to the transport's
adapter dataclass after list→set, list→tuple, and string-key→int-key
coercion. The per-transport field tables and canonical YAML examples live
in the operator-facing
[Configuration Reference](../ops/configuration.md); the typed dataclasses
in `src/medre/config/adapters/*.py` are authoritative.

Notes on fields whose YAML surface is non-obvious:

- `MatrixConfig.device_id` and `MatrixConfig.store_path` are
  **internal/test-only**. The runtime derives the device ID from `whoami()`
  on login and the crypto store path from the resolved state directory
  (`{state}/adapters/{adapter_id}/matrix/store`). Operators SHOULD NOT set
  these fields in production YAML.
- `MeshtasticConfig` carries four packet-routing fields that control
  inbound packet classification: `encrypted_action` (literal `"drop"` or
  `"deferred"`, default `"drop"`), `chat_portnums` and `disabled_portnums`
  (frozensets of portnum names, default empty), and `detection_sensor_relay`
  (bool, default `false`). These were added by the _Meshtastic Configurable
  Packet Routing_ change fragment.
- `MeshCoreConfig.ble_pin` is a sensitive BLE pairing PIN. It MUST NOT
  appear in diagnostics, logs, or JSON output. `MeshCoreConfig.node_config`
  rejects keys named `private_key`, `secret`, or `password`.
- `LxmfConfig.storage_path` is **required** when
  `connection_type="reticulum"` (the validated LXMF LXMRouter behavior
  raises if it is absent). `storage_path` is ignored in `fake` mode.
- All four adapter configs declare `origin_label: str = ""`. It is
  human-readable attribution only — see §3.4 and
  [routing-delivery.md §17.5.2](routing-delivery.md#1752-origin_label) for
  the precedence chain and the full list of properties.

### 3.4 Routes and Channel Mapping

Routes are defined under `routes.<route_id>`. `RouteConfig` carries:

- The source and dest adapter ID tuples (`source_adapters`, `dest_adapters`)
  and the `directionality` (`source_to_dest` | `dest_to_source` |
  `bidirectional`).
- An `enabled` flag (validated even when `false`; disabled routes are not
  registered).
- Optional targeting fields `source_channel` / `dest_channel` and their
  `*_room` aliases. `source_room` is an alias for `source_channel`,
  `dest_room` for `dest_channel`; setting both to different values is
  rejected.
- An optional static `policy` block (see
  [routing-delivery.md §2.8 Bridge Policy](routing-delivery.md#28-bridge-policy))
  and an optional per-route `retry` block.
- Route-level `source_origin_label` / `dest_origin_label` (default `None` /
  unset) and the optional `channel_room_map` described below.

The full normative semantics for route matching, policy evaluation, and
delivery fanout live in [routing-delivery.md](routing-delivery.md). The
operator-facing YAML examples, including policy and retry tables, live in
[Configuration Reference §routes](../ops/configuration.md#routesroute_id).

#### 3.4.1 channel_room_map

For Matrix↔Meshtastic bridges, `channel_room_map` expands a single route
into one leg per channel→room pair. Each entry is polymorphic:

- **Bare-string shape** — the value is a canonical Matrix room ID string
  (`"!room:server"`). No per-entry labels.
- **Structured shape** — the value is a table with three keys:

  | Key                   | Type           | Default | Notes                                                               |
  | --------------------- | -------------- | ------- | ------------------------------------------------------------------- |
  | `room`                | string         | —       | Canonical Matrix room ID starting with `!`. **Required.**           |
  | `source_origin_label` | string or null | `null`  | Per-entry forward-leg label. `null` inherits the route-level label. |
  | `dest_origin_label`   | string or null | `null`  | Per-entry reverse-leg label. `null` inherits the route-level label. |

  Unknown keys are rejected. Boolean label values are rejected before the
  generic string check, matching route-level label validation.

The two shapes MAY be mixed within a single `channel_room_map`. The
bare-string shape is the legacy form and remains fully supported.

`channel_room_map` is mutually exclusive with `source_channel`,
`dest_channel`, `source_room`, and `dest_room`. When present, the route
MUST have exactly one source and one dest adapter. Channel keys are
integers `0`–`7` (Meshtastic supports up to 8 channels). Duplicate channel
keys are rejected. Room values MUST be canonical Matrix room IDs (starting
with `!`); aliases (`#…`) are not supported.

#### 3.4.2 Origin Label Precedence

`origin_label` is human-readable attribution rendered into relay prefixes.
The formatter variable is `{origin_label}`. Per expanded leg, the resolved
value comes from the most-specific level that is not `null`/`None`:

1. **Per-entry** `source_origin_label` (forward leg) or `dest_origin_label`
   (reverse leg) on the matched `channel_room_map` entry, when set.
2. **Route-level** `source_origin_label` / `dest_origin_label` on
   `RouteConfig`, when set.
3. **Adapter** `origin_label` from the source adapter config (default `""`).
4. **Empty string** — `{origin_label}` renders empty.

An explicit empty string (`""`) at the per-entry or route level suppresses
fallback below that level for that leg: the `{origin_label}` template
variable resolves to empty. An absent, `null`, or `None` label falls
through to the next level. See
[routing-delivery.md §17.5.2](routing-delivery.md#1752-origin_label) and
[§17.5.8](routing-delivery.md#1758-projection-architecture-and-core-boundary)
for the full normative semantics.

`origin_label` is **observational attribution only**. It is not a routing
key, not a transport identity, not a sender identity, and not delivery
evidence. It never affects which route matches an event. The authoritative
machine-readable provenance source is the MEDRE metadata namespace
(`medre.envelope` on Matrix, `fields[0xFD]` on LXMF,
`RenderingResult.metadata` on all transports).

#### 3.4.3 Same-Room Fan-In and Duplicate Matrix Rooms

A `channel_room_map` MAY map two or more channel indices to the same
Matrix room for Meshtastic→Matrix fan-in (e.g. multiple radio channels
relaying into one shared room, each with its own `source_origin_label`).
Whether duplicate rooms are accepted depends on whether the route's
expansion creates a Matrix→Meshtastic leg:

- **Allowed** — no Matrix→Meshtastic leg is created (one-way
  Meshtastic→Matrix routing). The inbound radio channel disambiguates the
  source.
- **Rejected** — a Matrix→Meshtastic leg is created. A Matrix event
  arriving from the shared room would be ambiguous across channels.

The check runs at runtime route-expansion time (in
`medre.runtime.route_engine._validate_duplicate_rooms_for_direction`),
where adapter platform assignments are known. See
[routing-delivery.md §17.6](routing-delivery.md#176-duplicate-room-fan-in-for-channel_room_map)
for the full directionality decision matrix.

#### 3.4.4 Removed Template Placeholders

The attribution surface was canonicalized to a single set of template
variables (see the _Clean Attribution Surface — Canonical Variables Only_
changelog fragment). The following legacy placeholders are no longer
resolved and pass through as literal text in prefix templates:

| Removed placeholder | Current behavior                |
| ------------------- | ------------------------------- |
| `{meshnet_name}`    | Unknown — left as literal text. |
| `{longname}`        | Unknown — left as literal text. |
| `{shortname}`       | Unknown — left as literal text. |
| `{shortname5}`      | Unknown — left as literal text. |
| `{from_id}`         | Unknown — left as literal text. |

The canonical variables are `{origin_label}`, `{sender}`, `{sender_short}`,
`{sender_id}`, `{sender_handle}`, `{platform}`, `{route_id}`, and
`{channel}`. Operators with prefix templates still referencing the removed
names MUST migrate. See
[routing-delivery.md §17.5.5](routing-delivery.md#1755-shared-formatter-and-variable-schema)
for the formatter rules.

### 3.5 Editor Integration

The JSON schemas in `docs/schemas/` carry stable `$id` URLs and can be
wired into YAML language servers for real-time validation while editing.
Add a `# yaml-language-server: $schema=` comment at the top of a config
file pointing at the relevant schema:

```yaml
# yaml-language-server: $schema=../../docs/schemas/adapter-config.schema.json
```

Or register the mappings once in `.vscode/settings.json`:

```json
{
  "yaml.schemas": {
    "docs/schemas/adapter-config.schema.json": ["examples/configs/*.yaml"],
    "docs/schemas/routing-config.schema.json": ["examples/configs/*.yaml"]
  }
}
```

Editor validation is advisory and catches typos early, but the schemas are
a derived view of the typed dataclasses, not the source of truth.
`medre config check` remains the canonical pre-flight gate that blocks a
misconfigured runtime from starting. Publishing to SchemaStore.io is
deferred until the public schema surface stabilizes post-first-release.

## 4. Configuration Search Order

The loader searches for configuration files in this priority order:

1. `--config` CLI flag (explicit path, must exist)
2. `MEDRE_CONFIG` environment variable
3. `$MEDRE_HOME/config.yaml` (when `MEDRE_HOME` is set)
4. `$XDG_CONFIG_HOME/medre/config.yaml` (defaults to `~/.config/medre/config.yaml`)
5. `./medre.yaml` (local project fallback)

The loader accepts `.yaml` and `.yml` extensions and rejects `.toml` with a
clear error. Only the boring YAML subset is supported: explicit mappings and
lists, no anchors/aliases/merge keys, no custom tags.

The loader returns a `(RuntimeConfig, ConfigSource, MedrePaths)` triple.

## 5. Environment Overrides

`MEDRE_*` environment variables are applied as overrides on top of the loaded
YAML config. The original config is never mutated — overrides produce a new
frozen instance via `dataclasses.replace()`.

### 5.1 Core Overrides

| Variable          | Target                 |
| ----------------- | ---------------------- |
| `MEDRE_DB_PATH`   | `config.storage.path`  |
| `MEDRE_LOG_LEVEL` | `config.logging.level` |

### 5.2 Adapter Overrides

Adapter overrides target configured adapter instances by normalized adapter
token using the pattern `MEDRE_ADAPTER__<TOKEN>__<FIELD>`. The token is derived
from the `adapter_id` by uppercasing and replacing non-alphanumeric
characters with underscores.

Env overrides do not create virtual adapter instances; the target adapter MUST
already exist in the YAML config.

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
| Config       | `$XDG_CONFIG_HOME/medre/` or `~/.config/medre/`     | `$MEDRE_HOME/config.yaml`                       |
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

1. Parses the YAML mapping for the instance
2. Separates runtime fields (`enabled`, `adapter_id`) from adapter-specific fields
3. Coerces parsed types (list to set, string-keyed to int-keyed dicts)
4. Constructs and validates the adapter's own config dataclass
5. Stores the validated adapter config in a `.config` attribute

`AdapterConfigSet` groups all adapters by transport type and provides
`all_enabled()` iteration. Duplicate adapter IDs across transports are rejected
at validation time.

## 8. CLI

```bash
medre run [--config PATH]           # Start the MEDRE runtime
medre config check [--config PATH]  # Validate config file
medre config sample                 # Print a sample YAML config
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

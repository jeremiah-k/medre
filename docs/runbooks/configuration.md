# MEDRE Configuration

> Last updated: 2026-05-21

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

| Field                      | Type   | Default   | Description                                                       |
| -------------------------- | ------ | --------- | ----------------------------------------------------------------- |
| `name`                     | string | `"medre"` | Instance name used in logs and diagnostics.                       |
| `shutdown_timeout_seconds` | int    | `10`      | Maximum seconds to wait for adapters to stop before forcing exit. |

### `[logging]`

Logging configuration.

```toml
[logging]
level = "INFO"    # INFO, DEBUG, WARNING, ERROR
format = "text"   # text or json
```

| Field    | Type   | Default  | Description                                                              |
| -------- | ------ | -------- | ------------------------------------------------------------------------ |
| `level`  | string | `"INFO"` | Log level. One of `INFO`, `DEBUG`, `WARNING`, `ERROR`.                   |
| `format` | string | `"text"` | Output format. `text` for human-readable, `json` for structured logging. |

> **`level` controls MEDRE logs only.** It sets the log level for the
> `medre.*` logger namespace. Dependency libraries (nio, meshtastic, aiohttp,
> peewee, etc.) are not affected by this setting — their loggers inherit the
> root logger level (`WARNING`) unless explicitly configured via
> `[logging.overrides]`. Setting `level = "DEBUG"` enables debug output for
> `medre.*` but does **not** enable DEBUG for unknown or unlisted dependency
> loggers.

#### `[logging.overrides]` — troubleshooting escape hatch

> **This section is a troubleshooting tool, not a default.** Dependency
> loggers are intentionally quiet by default. Add overrides only when
> actively debugging an integration issue, and remove them afterward.

Per-logger level overrides for dependency libraries. Each key is a Python
logger name and the value is a log level string (`"DEBUG"`, `"INFO"`,
`"WARNING"`, `"ERROR"`).

| Key             | Value  | Description                         |
| --------------- | ------ | ----------------------------------- |
| _(logger name)_ | string | Log level to force for this logger. |

**Default dependency log levels** (applied automatically — no config needed):

| Logger           | Default Level | Reason                                                       |
| ---------------- | ------------- | ------------------------------------------------------------ |
| `nio`            | `WARNING`     | Crypto key and sync noise at INFO                            |
| `nio.crypto.log` | `ERROR`       | Olm/Megolm session warnings; extremely noisy at lower levels |
| `meshtastic`     | `WARNING`     | SDK prints every radio packet at INFO                        |
| `aiohttp`        | `WARNING`     | HTTP access logs at INFO                                     |
| `peewee`         | `WARNING`     | Query logging at DEBUG, noisy at INFO                        |
| `urllib3`        | `WARNING`     | Noisy HTTP/retry logs                                        |
| `serial`         | `WARNING`     | Verbose device I/O                                           |
| `serial_asyncio` | `WARNING`     | Verbose async serial/device I/O                              |
| `asyncio`        | `WARNING`     | Event-loop/debug chatter                                     |

> Any dependency logger **not** listed above (or in `[logging.overrides]`)
> inherits the root logger's `WARNING` level. Add an entry to
> `[logging.overrides]` to change it.

**Troubleshooting example** — add this block temporarily when diagnosing
integration issues, then remove it after debugging:

```toml
[logging.overrides]
aiohttp = "INFO"
meshtastic = "DEBUG"
nio = "DEBUG"
"nio.crypto.log" = "WARNING"   # WARNING first — DEBUG is extremely noisy
```

**Important notes:**

- `nio.crypto.log` is **extremely noisy** at `DEBUG`. For Matrix E2EE
  troubleshooting, try `WARNING` first. Only drop to `DEBUG` or `INFO` as a
  last resort and expect high log volume.
- Overrides are **temporary scaffolding**. Commit them only in dedicated
  troubleshooting config files, not in main example configs or production
  configs.
- Setting `level = "DEBUG"` in `[logging]` does **not** enable DEBUG for
  dependencies — it only affects the `medre.*` namespace.

**Future direction:** `medre run --debug` would set MEDRE DEBUG only; a
future `--debug-integrations` flag could enable selected dependency
namespaces. These CLI flags are not yet implemented — use `[logging.overrides]`
for now.

> **Matrix room history before startup is suppressed.** The Matrix adapter
> processes only events received _after_ the sync connection is established.
> Messages that arrived in bridged rooms while MEDRE was stopped are not
> replayed or relayed. This is by design — it prevents message duplication
> and stale relays on restart. If you need to bridge historical messages,
> use `medre replay` after startup.

### `[storage]`

Persistence and database configuration.

```toml
[storage]
backend = "sqlite"
path = "{state}/medre.sqlite"   # supports path placeholders
```

| Field     | Type   | Default    | Description                                                                                                            |
| --------- | ------ | ---------- | ---------------------------------------------------------------------------------------------------------------------- |
| `backend` | string | `"sqlite"` | Storage backend. Currently only `sqlite` is supported.                                                                 |
| `path`    | string | `None`     | Database file path. Supports [path placeholders](#path-placeholders). When `None`, defaults to `{state}/medre.sqlite`. |

> **Storage model:** MEDRE uses a single configured storage backend (one SQLite database at `{state}/medre.sqlite`). This database holds canonical events, delivery receipts, native references, replay state, and cross-adapter relationships. There is no per-adapter database. Transport-owned local files (e.g. Matrix crypto stores, LXMF identities) live under adapter state roots (`{state}/adapters/<adapter_id>/`).
>
> **Persistence scope:** The SQLite database is the authoritative persisted state. Events and delivery receipts survive process crashes and restarts. Runtime counters (delivery timeouts, capacity gauges, route statistics), in-flight deliveries, and active replay runs are process-local and are lost on process termination. Operators are responsible for database backup — MEDRE does not replicate or remotely store its database. See Contract 55 (Runtime Persistence) for the complete persistence contract.

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

| Field                     | Type           | Default       | Description                                                                                                                             |
| ------------------------- | -------------- | ------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `enabled`                 | bool           | `true`        | Whether this adapter instance is active.                                                                                                |
| `adapter_kind`            | string         | `"real"`      | `"real"` builds the live adapter; `"fake"` builds a simulated adapter without optional SDK imports.                                     |
| `adapter_id`              | string         | instance name | Unique identifier. Defaults to the TOML table key.                                                                                      |
| `homeserver`              | string         | _(required)_  | Matrix homeserver URL. Must start with `http://` or `https://`.                                                                         |
| `user_id`                 | string         | _(required)_  | Fully-qualified Matrix user ID (e.g. `@user:matrix.org`).                                                                               |
| `access_token`            | string         | `""`          | Access token for authentication. **Treat as a secret.**                                                                                 |
| `room_allowlist`          | list of string | `None`        | Room IDs to accept. `None` means all rooms.                                                                                             |
| `metadata_embedding_mode` | string         | `"safe"`      | How metadata is embedded in messages.                                                                                                   |
| `sync_timeout_ms`         | int            | `30000`       | Long-polling sync timeout in milliseconds.                                                                                              |
| `encryption_mode`         | string         | `"plaintext"` | Encryption policy: `plaintext`, `e2ee_required`, or `e2ee_optional`. E2EE modes handle device verification and crypto store internally. |
| `require_encrypted_rooms` | bool           | `false`       | When `true`, only operate in rooms with encryption enabled. Invalid with `encryption_mode="plaintext"`.                                 |

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
# max_text_bytes = 227              # UTF-8 byte budget for final radio text
# outbound_mode = "enabled"         # enabled or listen_only
```

| Field                              | Type               | Default       | Description                                                                                                                                                                                               |
| ---------------------------------- | ------------------ | ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `enabled`                          | bool               | `true`        | Whether this adapter instance is active.                                                                                                                                                                  |
| `adapter_kind`                     | string             | `"real"`      | `"real"` builds the live adapter; `"fake"` builds a simulated adapter without optional SDK imports.                                                                                                       |
| `adapter_id`                       | string             | instance name | Unique identifier.                                                                                                                                                                                        |
| `connection_type`                  | string             | `"fake"`      | Connection mode: `fake`, `tcp`, `serial`, or `ble`.                                                                                                                                                       |
| `host`                             | string             | `None`        | Hostname or IP for TCP connections. Required when `connection_type="tcp"`.                                                                                                                                |
| `port`                             | int                | `None`        | Port number for TCP connections.                                                                                                                                                                          |
| `serial_port`                      | string             | `None`        | Serial device path. Required when `connection_type="serial"`.                                                                                                                                             |
| `ble_address`                      | string             | `None`        | BLE MAC address. Required when `connection_type="ble"`.                                                                                                                                                   |
| `meshnet_name`                     | string             | `""`          | Human-readable meshnet name (informational).                                                                                                                                                              |
| `default_channel`                  | int                | `0`           | Default radio channel index for outbound messages.                                                                                                                                                        |
| `channel_mapping`                  | dict of int→string | `{}`          | Maps channel indices to human-readable names.                                                                                                                                                             |
| `message_delay_seconds`            | float              | `0.5`         | Minimum delay between outbound messages (pacing).                                                                                                                                                         |
| `startup_backlog_suppress_seconds` | float              | `5.0`         | Seconds after start to suppress stale backlog packets.                                                                                                                                                    |
| `sync_timeout_ms`                  | int                | `30000`       | Timeout for sync operations in milliseconds.                                                                                                                                                              |
| `max_text_bytes`                   | int                | `227`         | Maximum UTF-8 byte budget for final radio text. Applied after all rendering.                                                                                                                              |
| `outbound_mode`                    | string             | `"enabled"`   | Outbound gate: `"enabled"` allows RF transmission; `"listen_only"` suppresses all outbound delivery. Inbound reception is unaffected. See [Outbound Gate Semantics](#outbound-gate-semantics-meshtastic). |

#### Outbound Gate Semantics (Meshtastic)

The `outbound_mode` field controls whether the Meshtastic adapter transmits outbound messages:

| Value           | Inbound | Outbound delivery                                                              | Delivery receipt / evidence                                                                  |
| --------------- | ------- | ------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------- |
| `"enabled"`     | Normal  | Normal                                                                         | Normal                                                                                       |
| `"listen_only"` | Normal  | **Suppressed** — `deliver()` rejects outbound payloads without RF transmission | Non-retryable adapter failure or suppressed detail (`outbound suppressed: listen_only mode`) |

When `outbound_mode = "listen_only"`:

- The adapter connects normally and receives inbound radio packets.
- Outbound messages routed to this adapter are suppressed before RF transmission. The adapter's `deliver()` method rejects the payload as a non-retryable failure.
- Delivery receipts reflect the suppression. The evidence/detail string is `outbound suppressed: listen_only mode` (or equivalent adapter-level detail).
- This is an intentional operator gate, not a bug. It allows monitoring a mesh without transmitting.
- Operators can enable this via TOML or environment variable:

```bash
export MEDRE_ADAPTER__RADIO__OUTBOUND_MODE=listen_only
```

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

| Field                              | Type               | Default       | Description                                                                                         |
| ---------------------------------- | ------------------ | ------------- | --------------------------------------------------------------------------------------------------- |
| `enabled`                          | bool               | `true`        | Whether this adapter instance is active.                                                            |
| `adapter_kind`                     | string             | `"real"`      | `"real"` builds the live adapter; `"fake"` builds a simulated adapter without optional SDK imports. |
| `adapter_id`                       | string             | instance name | Unique identifier.                                                                                  |
| `connection_type`                  | string             | `"fake"`      | Connection mode: `fake`, `tcp`, `serial`, or `ble`.                                                 |
| `host`                             | string             | `None`        | Hostname or IP for TCP connections. Required when `connection_type="tcp"`.                          |
| `port`                             | int                | `None`        | Port number for TCP connections.                                                                    |
| `serial_port`                      | string             | `None`        | Serial device path. Required when `connection_type="serial"`.                                       |
| `ble_address`                      | string             | `None`        | BLE MAC address.                                                                                    |
| `meshnet_name`                     | string             | `""`          | Human-readable meshnet name (informational).                                                        |
| `default_channel`                  | int                | `0`           | Default radio channel index for outbound messages.                                                  |
| `channel_mapping`                  | dict of int→string | `{}`          | Maps channel indices to human-readable names.                                                       |
| `message_delay_seconds`            | float              | `0.5`         | Minimum delay between outbound messages (pacing).                                                   |
| `startup_backlog_suppress_seconds` | float              | `5.0`         | Seconds after start to suppress stale backlog packets.                                              |
| `sync_timeout_ms`                  | int                | `30000`       | Timeout for sync operations in milliseconds.                                                        |
| `identity`                         | string             | `None`        | MeshCore node identity string (e.g. node name).                                                     |
| `pubkey`                           | string             | `None`        | Public key as a hex string.                                                                         |
| `node_config`                      | dict               | `{}`          | Opaque dict for node-specific settings. Must not contain secret keys.                               |

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

| Field                     | Type   | Default       | Description                                                                                         |
| ------------------------- | ------ | ------------- | --------------------------------------------------------------------------------------------------- |
| `enabled`                 | bool   | `true`        | Whether this adapter instance is active.                                                            |
| `adapter_kind`            | string | `"real"`      | `"real"` builds the live adapter; `"fake"` builds a simulated adapter without optional SDK imports. |
| `adapter_id`              | string | instance name | Unique identifier.                                                                                  |
| `connection_type`         | string | `"fake"`      | Connection mode: `fake` or `reticulum`.                                                             |
| `display_name`            | string | `""`          | Display name for LXMF announces.                                                                    |
| `stamp_cost`              | int    | `8`           | Stamp cost. `0` means no stamp required.                                                            |
| `default_delivery_method` | string | `"direct"`    | Delivery method: `direct`, `opportunistic`, `propagated`, or `paper`.                               |
| `meshnet_name`            | string | `""`          | Human-readable meshnet name (informational).                                                        |
| `default_channel`         | int    | `0`           | Default channel index for outbound messages.                                                        |
| `message_delay_seconds`   | float  | `0.5`         | Minimum delay between outbound messages (pacing).                                                   |
| `metadata_embedding`      | bool   | `true`        | Whether to embed MEDRE metadata envelopes in LXMF fields.                                           |
| `identity_path`           | string | `None`        | Path to Reticulum identity file. Supports [path placeholders](#path-placeholders).                  |

### `[routes.ROUTE_ID]`

Routes define named bridges between adapters. Each route declares source
and destination adapter IDs and controls which events flow between them.
Routes are evaluated in the order they appear in the TOML file.

`ROUTE_ID` must contain only alphanumeric characters, underscores, or
hyphens. Route IDs must be unique across the entire configuration.

Routes reference **adapter IDs** (the resolved `adapter_id` value), not the
TOML section key. When `adapter_id` is not explicitly set, it defaults to
the section key.

```toml
[routes.matrix_radio_bridge]
source_adapters = ["main"]
dest_adapters = ["radio"]
directionality = "bidirectional"
enabled = true
source_room = "!room:example.com"
dest_channel = "1"

[routes.matrix_radio_bridge.policy]
allowed_event_types = ["message"]
```

| Field             | Type           | Default            | Description                                                                                |
| ----------------- | -------------- | ------------------ | ------------------------------------------------------------------------------------------ |
| `source_adapters` | list of string | _(required)_       | Adapter IDs that originate events. Must not overlap with `dest_adapters`.                  |
| `dest_adapters`   | list of string | _(required)_       | Adapter IDs that receive events. Must not overlap with `source_adapters`.                  |
| `directionality`  | string         | `"source_to_dest"` | Direction of flow. One of `source_to_dest`, `dest_to_source`, or `bidirectional`.          |
| `enabled`         | bool           | `true`             | Whether this route is active at startup. Disabled routes are validated but not registered. |
| `source_room`     | string         | `None`             | Source Matrix room ID. Alias for `source_channel`.                                         |
| `dest_room`       | string         | `None`             | Destination Matrix room ID. Alias for `dest_channel`.                                      |
| `source_channel`  | string         | `None`             | Source channel/conversation ID.                                                            |
| `dest_channel`    | string         | `None`             | Destination channel/conversation ID.                                                       |

#### Route Policy (`[routes.ROUTE_ID.policy]`)

Optional static allowlist policy attached to a route.

```toml
[routes.matrix_radio_bridge.policy]
allowed_event_types = ["message"]
```

| Field                 | Type           | Default | Description                                                                |
| --------------------- | -------------- | ------- | -------------------------------------------------------------------------- |
| `allowed_event_types`      | list of string | `[]`    | Event kinds this route permits (e.g. `"message"`). Empty means all events. Enforced as structural route-source matching during route expansion. |
| `allowed_source_adapters`  | list of string | `[]`    | Source adapter names to permit. Empty = any. |
| `allowed_dest_adapters`    | list of string | `[]`    | Destination adapter names to permit. Empty = any. |
| `sender_allowlist`         | list of string | `[]`    | Permitted sender identities (`source_transport_id`). Empty = any sender. |
| `room_allowlist`           | list of string | `[]`    | Permitted room identifiers. Checked against `source_channel_id` when present. Empty = any room. |
| `channel_allowlist`        | list of string | `[]`    | Permitted channel identifiers. Checked against `target.channel`, falling back to `source_channel_id`. Empty = any channel. |

> **Note:** `allowed_event_types` is enforced during route expansion (structural
> event-kind matching). The other five fields are route-policy checks evaluated
> after route matching and before delivery side effects. A policy denial produces
> a `status="suppressed"` receipt with `failure_kind="policy_suppressed"` and is
> not retryable. All policy fields are config-file-only (not settable via
> environment variables).
>
> **Validation:** Unknown keys in `[routes.<id>.policy]` are rejected at config
> load time. Allowlist values must be arrays of strings (e.g. `["message"]`);
> bare strings (e.g. `"message"`) are rejected.

#### Route Retry (`[routes.ROUTE_ID.retry]`)

Optional per-route retry policy for transient delivery failures. When enabled,
transient adapter errors on this route produce retry receipts with `next_retry_at`
populated. The global `[retry]` section controls whether the RetryWorker
processes them — route retry governs **scheduling**, global retry governs
**execution**.

```toml
[routes.matrix_radio_bridge.retry]
enabled = true
max_attempts = 3
backoff_base = 2.0
max_delay_seconds = 60.0
jitter = false
```

| Field               | Type  | Default | Description                                                           |
| ------------------- | ----- | ------- | --------------------------------------------------------------------- |
| `enabled`           | bool  | `true`  | Whether retry scheduling is active for this route.                    |
| `max_attempts`      | int   | `3`     | Maximum total delivery attempts (including the initial). Must be > 0. |
| `backoff_base`      | float | `2.0`   | Base delay in seconds for exponential backoff. Must be >= 0.          |
| `max_delay_seconds` | float | `60.0`  | Upper bound for the computed backoff delay. Must be >= 0.             |
| `jitter`            | bool  | `false` | Whether to add jitter to the backoff delay.                           |

Both levels must be active for automatic retry:

- Each route's `[routes.<id>.retry]` controls whether retry receipts are
  scheduled for transient failures on that route.
- The `[retry]` section controls whether the RetryWorker processes them.
- If `[retry] enabled = false` (default), retry receipts accumulate in storage
  with `next_retry_at` set but are never processed. They can be inspected
  manually or processed later when the worker is enabled.

#### channel_room_map shorthand

For Matrix↔Meshtastic bridges, `channel_room_map` expands a single route config
into N channel→room pairs. Instead of writing separate routes for each channel,
you declare a mapping table and the runtime generates one route per entry.

```toml
[routes.multi_channel_bridge]
source_adapters = ["main"]
dest_adapters = ["radio"]
directionality = "bidirectional"
enabled = true
channel_room_map = {0 = "!general:example.com", 1 = "!admin:example.com", 2 = "!alerts:example.com"}
```

This is equivalent to writing three separate bidirectional routes, each with
`source_room` and `dest_channel` set to the corresponding pair. The runtime
generates forward and reverse legs for every entry.

**Limitations:**

- Room IDs must be canonical (`!` prefix). Aliases (`#room:server`) are not
  supported yet.
- `channel_room_map` is mutually exclusive with `source_room`, `dest_room`,
  `source_channel`, and `dest_channel`. A route uses either explicit targeting
  fields or a channel map, never both.
- The route must have exactly one source adapter and one destination adapter.
  Multi-source or multi-dest routes cannot use channel maps.
- Channel keys must be integers in the range 0 through 7 (Meshtastic supports
  up to 8 channels).
- Each room ID must be unique across the map. Mapping two channels to the same
  room is rejected at validation.

**When to use each approach:**

- Use `channel_room_map` when bridging multiple Meshtastic channels to
  dedicated Matrix rooms and the policy is the same for every channel.
- Use explicit `source_room` / `dest_channel` targeting fields when you need
  per-route policies, asymmetric directions, or different adapter combinations.
  Existing explicit routes continue to work alongside channel map routes.

### `[retry]`

Controls the background RetryWorker that polls for due retry receipts and
re-attempts delivery. This is separate from per-route retry policy (which
determines `max_attempts` and backoff for individual delivery failures).

```toml
[retry]
enabled = true
interval_seconds = 10.0
batch_size = 20
max_attempts = 3
```

| Field              | Type  | Default | Description                                                                       |
| ------------------ | ----- | ------- | --------------------------------------------------------------------------------- |
| `enabled`          | bool  | `false` | Whether the RetryWorker runs at all.                                              |
| `interval_seconds` | float | `10.0`  | How often the worker polls for due receipts. Must be > 0.                         |
| `batch_size`       | int   | `20`    | Max receipts processed per polling cycle. Must be >= 1.                           |
| `max_attempts`     | int   | `3`     | Global max attempts before dead-lettering. Each route may override. Must be >= 1. |

#### Adapter ID Semantics in Routes

- Routes reference the **resolved** `adapter_id`, not the TOML section key.
  If you override `adapter_id` in an adapter section, use that override
  value in routes.
- Routes referencing **unknown** adapter IDs (not declared in any
  `[adapters.*]` section) raise a validation error at runtime startup.
- Routes referencing **disabled** adapters (`enabled = false`) also raise a
  validation error — disabled adapters are excluded from the runtime.
- If an adapter is enabled but **fails to build** (e.g. missing optional
  SDK), routes referencing it are degraded rather than failing: routes
  with a failed source adapter are skipped; routes with failed target
  adapters have those targets removed.

## XDG Default Paths

When `MEDRE_HOME` is **not** set, MEDRE follows the XDG Base Directory
Specification. Each path category is resolved independently:

```text
Config:    $XDG_CONFIG_HOME/medre/    or  ~/.config/medre/
State:     $XDG_STATE_HOME/medre/     or  ~/.local/state/medre/
Data:      $XDG_DATA_HOME/medre/      or  ~/.local/share/medre/
Cache:     $XDG_CACHE_HOME/medre/     or  ~/.cache/medre/
```

Runtime paths derived from the resolved state directory (`{state}`):

| Path                                          | Description                                                                                                                                   |
| --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `{state}/medre.sqlite`                        | Single global storage backend (canonical events, delivery receipts, native refs, replay state, cross-adapter relationships, runtime metadata) |
| `{state}/logs/medre.log`                      | Global log file                                                                                                                               |
| `{state}/adapters/{adapter_id}/`              | Per-adapter state root                                                                                                                        |
| `{state}/adapters/{adapter_id}/matrix/store/` | Matrix E2EE crypto store (nio Olm/Megolm keys; created for non-plaintext encryption modes only)                                               |
| `{state}/adapters/{adapter_id}/meshtastic/`   | Meshtastic transport state (future)                                                                                                           |
| `{state}/adapters/{adapter_id}/meshcore/`     | MeshCore transport state (future)                                                                                                             |
| `{state}/adapters/{adapter_id}/lxmf/`         | LXMF transport state (future)                                                                                                                 |

There are **no per-adapter databases**. Adapter-local filesystem state is
transport-owned (e.g., Matrix crypto store, LXMF identity files), not
MEDRE-owned. All canonical data flows through the single global database.

The primary config file is at `$XDG_CONFIG_HOME/medre/config.toml`
(`~/.config/medre/config.toml` by default).

Use `medre paths` to print the resolved paths for your environment.

## MEDRE_HOME (Single-Directory Mode)

Setting the `MEDRE_HOME` environment variable switches MEDRE into
single-directory mode. All paths are resolved under one root:

```text
MEDRE_HOME=/opt/medre
Config:    /opt/medre/config.toml
State:     /opt/medre/state/
Data:      /opt/medre/data/
Cache:     /opt/medre/cache/
```

Runtime paths derived from the state directory:

| Path                                                   | Description                         |
| ------------------------------------------------------ | ----------------------------------- |
| `/opt/medre/state/medre.sqlite`                        | Single global storage backend       |
| `/opt/medre/state/logs/medre.log`                      | Global log file                     |
| `/opt/medre/state/adapters/{adapter_id}/`              | Per-adapter state root              |
| `/opt/medre/state/adapters/{adapter_id}/matrix/store/` | Matrix E2EE crypto store            |
| `/opt/medre/state/adapters/{adapter_id}/meshtastic/`   | Meshtastic transport state (future) |
| `/opt/medre/state/adapters/{adapter_id}/meshcore/`     | MeshCore transport state (future)   |
| `/opt/medre/state/adapters/{adapter_id}/lxmf/`         | LXMF transport state (future)       |

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

| Placeholder | Expands to                                                                  |
| ----------- | --------------------------------------------------------------------------- |
| `{config}`  | Config directory (`config_dir`, or `config_file.parent` in MEDRE_HOME mode) |
| `{state}`   | State directory                                                             |
| `{data}`    | Data directory                                                              |
| `{cache}`   | Cache directory                                                             |
| `{logs}`    | Log directory                                                               |

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

Environment variables override TOML values. They always win. The original TOML config is never mutated; a new frozen config is returned with overrides applied.

MEDRE uses three categories of environment variables:

1. **Core env vars** control global runtime behaviour (paths, logging, limits).
2. **Instance-scoped adapter overrides** target a specific adapter instance by its normalised ID.
3. **Instance-scoped route overrides** create or override routes by an arbitrary token.

`MEDRE_ADAPTER__<TOKEN>__<FIELD>` is the **only** adapter override surface. `MEDRE_ROUTE__<TOKEN>__<FIELD>` is the **only** route override surface. There are no transport-prefixed shortcuts. Env vars with legacy prefixes like `MEDRE_MATRIX_*`, `MEDRE_MESHTASTIC_*`, `MEDRE_MESHCORE_*`, or `MEDRE_LXMF_*` are rejected at startup with migration guidance. See [Unsupported Legacy Prefixes](#unsupported-legacy-prefixes) below.

### Core Environment Variables

| Variable                                         | Type   | Maps to                                   | Default                |
| ------------------------------------------------ | ------ | ----------------------------------------- | ---------------------- |
| `MEDRE_HOME`                                     | string | Single-directory root path                | _(not set)_            |
| `MEDRE_CONFIG`                                   | string | Config file path                          | _(not set)_            |
| `MEDRE_DB_PATH`                                  | string | `storage.path`                            | `{state}/medre.sqlite` |
| `MEDRE_LOG_LEVEL`                                | string | `logging.level`                           | `INFO`                 |
| `MEDRE_RUNTIME_MAX_INFLIGHT_DELIVERIES`          | int    | `limits.max_inflight_deliveries`          | _(TOML default)_       |
| `MEDRE_RUNTIME_MAX_INFLIGHT_REPLAY_EVENTS`       | int    | `limits.max_inflight_replay_events`       | _(TOML default)_       |
| `MEDRE_RUNTIME_SHUTDOWN_DRAIN_TIMEOUT_SECONDS`   | int    | `limits.shutdown_drain_timeout_seconds`   | _(TOML default)_       |
| `MEDRE_RUNTIME_DELIVERY_ACQUIRE_TIMEOUT_SECONDS` | float  | `limits.delivery_acquire_timeout_seconds` | _(TOML default)_       |

> **Route env vars** (`MEDRE_ROUTE__<TOKEN>__<FIELD>`) are documented separately in [Env-Driven Route Creation](#env-driven-route-creation).

### Retry Config

Retry behavior can be configured through environment variables using:

```text
MEDRE_RETRY__ENABLED=true|false
MEDRE_RETRY__MAX_ATTEMPTS=5
MEDRE_RETRY__INTERVAL_SECONDS=10.0
MEDRE_RETRY__BATCH_SIZE=20
```

Field names are case-insensitive. Values are coerced to the expected type
(bool, int, or float). These override any `[retry]` TOML section values.

Retry is opt-in (default: disabled). See the retry worker documentation in
`src/medre/runtime/retry.py` for details.

### Instance-Scoped Adapter Overrides

Adapter overrides use the format:

```text
MEDRE_ADAPTER__<TOKEN>__<FIELD>=<value>
```

- **`<TOKEN>`** is the uppercased, normalised form of the adapter's `adapter_id`. Non-alphanumeric characters are replaced with `_`, consecutive underscores are collapsed, and leading/trailing underscores are stripped.
- **`<FIELD>`** is a field name from the transport's config dataclass, or `enabled` to toggle the adapter on or off.
- The env var targets the adapter whose `adapter_id` normalises to `<TOKEN>`. If no adapter matches, MEDRE raises `ConfigValidationError` at startup.

#### Token Normalisation

The normalisation function (`normalize_adapter_id`) transforms an adapter ID into an env token:

| `adapter_id`     | Normalised token |
| ---------------- | ---------------- |
| `main`           | `MAIN`           |
| `matrix-primary` | `MATRIX_PRIMARY` |
| `matrix_primary` | `MATRIX_PRIMARY` |
| `radio.a`        | `RADIO_A`        |
| `meshcore/tbeam` | `MESHCORE_TBEAM` |
| `my.adapter-1`   | `MY_ADAPTER_1`   |

Token collisions are detected at startup. If two adapter IDs normalise to the same token (e.g. `radio-a` and `radio_a` both become `RADIO_A`), MEDRE raises `ConfigValidationError`.

#### Case-Insensitive Field Names

Field names in env vars are matched case-insensitively against the config dataclass fields. All of these are equivalent:

```bash
MEDRE_ADAPTER__MAIN__ACCESS_TOKEN=syt_...
MEDRE_ADAPTER__MAIN__access_token=syt_...
MEDRE_ADAPTER__MAIN__Access_Token=syt_...
```

The normalised (lowercase) form is conventional and used throughout this documentation, but uppercase or mixed-case will also work. This applies to field names only; the `MEDRE_ADAPTER__` prefix and the `<TOKEN>` segment must be uppercase.

#### Boolean and Collection Values

- **Boolean** fields accept: `1`, `true`, `yes` (truthy) and `0`, `false`, `no` (falsy). Case-insensitive.
- **Integer** and **float** fields are parsed from the raw string value.
- **Set** fields (e.g. `room_allowlist`) accept comma-separated values.
- All other fields are treated as strings.

#### Per-Transport Field Reference

Each transport exposes its config dataclass fields as override targets. The `enabled` field is available on all transports.

**Matrix** (fields from `MatrixConfig`):

| Field                     | Type                          | Env var example                                                    |
| ------------------------- | ----------------------------- | ------------------------------------------------------------------ |
| `enabled`                 | bool                          | `MEDRE_ADAPTER__MAIN__ENABLED=true`                                |
| `homeserver`              | string                        | `MEDRE_ADAPTER__MAIN__HOMESERVER=https://matrix.example.com`       |
| `user_id`                 | string                        | `MEDRE_ADAPTER__MAIN__USER_ID=@bot:example.com`                    |
| `access_token`            | string                        | `MEDRE_ADAPTER__MAIN__ACCESS_TOKEN=syt_...`                        |
| `room_allowlist`          | set of strings                | `MEDRE_ADAPTER__MAIN__ROOM_ALLOWLIST=!room:example.com,!other:...` |
| `metadata_embedding_mode` | string                        | `MEDRE_ADAPTER__MAIN__METADATA_EMBEDDING_MODE=safe`                |
| `sync_timeout_ms`         | int                           | `MEDRE_ADAPTER__MAIN__SYNC_TIMEOUT_MS=30000`                       |
| `encryption_mode`         | string                        | `MEDRE_ADAPTER__MAIN__ENCRYPTION_MODE=plaintext`                   |
| `require_encrypted_rooms` | bool                          | `MEDRE_ADAPTER__MAIN__REQUIRE_ENCRYPTED_ROOMS=false`               |
| `auto_join_rooms`         | tuple of strings \| TOML-only |                                                                    |
| `device_id`               | string                        | `MEDRE_ADAPTER__MAIN__DEVICE_ID=...`                               |
| `store_path`              | string                        | `MEDRE_ADAPTER__MAIN__STORE_PATH=/path/to/store`                   |

> `device_id` and `store_path` are internal/test-only fields. The runtime derives them automatically. Only set them for test harnesses or when you need explicit control.

**Meshtastic** (fields from `MeshtasticConfig`):

| Field                              | Type   | Env var example                                              |
| ---------------------------------- | ------ | ------------------------------------------------------------ |
| `enabled`                          | bool   | `MEDRE_ADAPTER__RADIO__ENABLED=true`                         |
| `connection_type`                  | string | `MEDRE_ADAPTER__RADIO__CONNECTION_TYPE=tcp`                  |
| `host`                             | string | `MEDRE_ADAPTER__RADIO__HOST=meshtastic.local`                |
| `port`                             | int    | `MEDRE_ADAPTER__RADIO__PORT=4403`                            |
| `serial_port`                      | string | `MEDRE_ADAPTER__RADIO__SERIAL_PORT=/dev/ttyACM0`             |
| `ble_address`                      | string | `MEDRE_ADAPTER__RADIO__BLE_ADDRESS=...`                      |
| `meshnet_name`                     | string | `MEDRE_ADAPTER__RADIO__MESHNET_NAME=MyMesh`                  |
| `default_channel`                  | int    | `MEDRE_ADAPTER__RADIO__DEFAULT_CHANNEL=0`                    |
| `channel_mapping`                  | dict   | _(not settable via env)_                                     |
| `message_delay_seconds`            | float  | `MEDRE_ADAPTER__RADIO__MESSAGE_DELAY_SECONDS=0.5`            |
| `startup_backlog_suppress_seconds` | float  | `MEDRE_ADAPTER__RADIO__STARTUP_BACKLOG_SUPPRESS_SECONDS=5.0` |
| `sync_timeout_ms`                  | int    | `MEDRE_ADAPTER__RADIO__SYNC_TIMEOUT_MS=30000`                |
| `matrix_relay_prefix`              | string | `MEDRE_ADAPTER__RADIO__MATRIX_RELAY_PREFIX=[{longname}]:`    |
| `radio_relay_prefix`               | string | `MEDRE_ADAPTER__RADIO__RADIO_RELAY_PREFIX={shortname5}[M]:`  |
| `mmrelay_compatibility`            | bool   | `MEDRE_ADAPTER__RADIO__MMRELAY_COMPATIBILITY=false`          |
| `max_text_bytes`                   | int    | `MEDRE_ADAPTER__RADIO__MAX_TEXT_BYTES=227`                   |
| `outbound_mode`                    | string | `MEDRE_ADAPTER__RADIO__OUTBOUND_MODE=listen_only`            |

**MeshCore** (fields from `MeshCoreConfig`):

| Field                              | Type   | Env var example                                              |
| ---------------------------------- | ------ | ------------------------------------------------------------ |
| `enabled`                          | bool   | `MEDRE_ADAPTER__RADIO__ENABLED=true`                         |
| `connection_type`                  | string | `MEDRE_ADAPTER__RADIO__CONNECTION_TYPE=tcp`                  |
| `host`                             | string | `MEDRE_ADAPTER__RADIO__HOST=meshcore.local`                  |
| `port`                             | int    | `MEDRE_ADAPTER__RADIO__PORT=4403`                            |
| `serial_port`                      | string | `MEDRE_ADAPTER__RADIO__SERIAL_PORT=/dev/ttyUSB0`             |
| `serial_baudrate`                  | int    | `MEDRE_ADAPTER__RADIO__SERIAL_BAUDRATE=115200`               |
| `ble_address`                      | string | `MEDRE_ADAPTER__RADIO__BLE_ADDRESS=...`                      |
| `meshnet_name`                     | string | `MEDRE_ADAPTER__RADIO__MESHNET_NAME=MyMesh`                  |
| `default_channel`                  | int    | `MEDRE_ADAPTER__RADIO__DEFAULT_CHANNEL=0`                    |
| `channel_mapping`                  | dict   | _(not settable via env)_                                     |
| `message_delay_seconds`            | float  | `MEDRE_ADAPTER__RADIO__MESSAGE_DELAY_SECONDS=0.5`            |
| `startup_backlog_suppress_seconds` | float  | `MEDRE_ADAPTER__RADIO__STARTUP_BACKLOG_SUPPRESS_SECONDS=5.0` |
| `sync_timeout_ms`                  | int    | `MEDRE_ADAPTER__RADIO__SYNC_TIMEOUT_MS=30000`                |
| `identity`                         | string | `MEDRE_ADAPTER__RADIO__IDENTITY=my-node`                     |
| `pubkey`                           | string | `MEDRE_ADAPTER__RADIO__PUBKEY=abcdef0123456789`              |
| `node_config`                      | dict   | _(not settable via env)_                                     |

**LXMF** (fields from `LxmfConfig`):

| Field                     | Type   | Env var example                                             |
| ------------------------- | ------ | ----------------------------------------------------------- |
| `enabled`                 | bool   | `MEDRE_ADAPTER__LOCAL__ENABLED=true`                        |
| `connection_type`         | string | `MEDRE_ADAPTER__LOCAL__CONNECTION_TYPE=reticulum`           |
| `display_name`            | string | `MEDRE_ADAPTER__LOCAL__DISPLAY_NAME=MEDRE`                  |
| `stamp_cost`              | int    | `MEDRE_ADAPTER__LOCAL__STAMP_COST=8`                        |
| `default_delivery_method` | string | `MEDRE_ADAPTER__LOCAL__DEFAULT_DELIVERY_METHOD=direct`      |
| `meshnet_name`            | string | `MEDRE_ADAPTER__LOCAL__MESHNET_NAME=MyMesh`                 |
| `default_channel`         | int    | `MEDRE_ADAPTER__LOCAL__DEFAULT_CHANNEL=0`                   |
| `message_delay_seconds`   | float  | `MEDRE_ADAPTER__LOCAL__MESSAGE_DELAY_SECONDS=0.5`           |
| `metadata_embedding`      | bool   | `MEDRE_ADAPTER__LOCAL__METADATA_EMBEDDING=true`             |
| `identity_path`           | string | `MEDRE_ADAPTER__LOCAL__IDENTITY_PATH={state}/lxmf/identity` |
| `storage_path`            | string | `MEDRE_ADAPTER__LOCAL__STORAGE_PATH={state}/lxmf/storage`   |

> **TOML-only fields.** For adapters defined in TOML, `adapter_id` defaults to the TOML instance name and cannot be overridden through env. For adapters created entirely from env (see Env-First Adapter Creation), the `ADAPTER_ID` field can be set to override the default derivation. Dict fields (`channel_mapping`, `node_config`) and tuple fields (`auto_join_rooms`) cannot be set via environment variables for any adapter type — they require structured data that the flat string format of env vars cannot represent. Set them in TOML instead. The env override system will reject these fields with a clear error message.

#### Secret Handling

Fields whose names match the pattern `TOKEN`, `SECRET`, `PASSWORD`, `KEY`, `AUTH`, or `CREDENTIAL` (case-insensitive) are treated as secrets. Their values are redacted to `***REDACTED***` in provenance logs and diagnostic output. The raw values are still applied to the config.

For example, `MEDRE_ADAPTER__MAIN__ACCESS_TOKEN` is redacted in provenance because `ACCESS_TOKEN` matches the `TOKEN` pattern.

#### Unsupported Fields

Setting a field name that does not exist on the transport's config dataclass raises `ConfigValidationError` at startup with a message listing the valid fields for that transport.

#### Unsupported Legacy Prefixes

Environment variables using transport-prefixed patterns are **intentionally unsupported** and will be rejected at startup. MEDRE logs a clear error and prints migration guidance showing the correct `MEDRE_ADAPTER__<TOKEN>__<FIELD>` form.

The following prefix patterns are rejected:

| Rejected pattern     | Why                                                                       |
| -------------------- | ------------------------------------------------------------------------- |
| `MEDRE_MATRIX_*`     | Use `MEDRE_ADAPTER__<TOKEN>__<FIELD>` with the adapter's normalised token |
| `MEDRE_MESHTASTIC_*` | Use `MEDRE_ADAPTER__<TOKEN>__<FIELD>` with the adapter's normalised token |
| `MEDRE_MESHCORE_*`   | Use `MEDRE_ADAPTER__<TOKEN>__<FIELD>` with the adapter's normalised token |
| `MEDRE_LXMF_*`       | Use `MEDRE_ADAPTER__<TOKEN>__<FIELD>` with the adapter's normalised token |

This rejection is by design. The instance-scoped `MEDRE_ADAPTER__` pattern is the single override surface for all transport types. Transport-prefixed vars would create ambiguity when multiple adapters of the same transport type exist, and would not compose with the token normalisation rules.

#### Migration Examples

> **All legacy prefixed vars below are rejected at startup.** They are shown here
> **for migration context only** — do not use them. Replace with the
> `MEDRE_ADAPTER__<TOKEN>__<FIELD>` form shown under each "New:" heading.

**Matrix:**

```bash
# Unsupported — shown for migration context only:
export MEDRE_MATRIX_ACCESS_TOKEN=syt_...
export MEDRE_MATRIX_HOMESERVER=https://matrix.example.com
export MEDRE_MATRIX_USER_ID=@bot:example.com

# New:
export MEDRE_ADAPTER__MAIN__ACCESS_TOKEN=syt_...
export MEDRE_ADAPTER__MAIN__HOMESERVER=https://matrix.example.com
export MEDRE_ADAPTER__MAIN__USER_ID=@bot:example.com
```

Replace `MAIN` with the normalised token of your adapter's `adapter_id`. For an adapter with `adapter_id = "matrix-primary"`, the token is `MATRIX_PRIMARY`.

**Meshtastic:**

```bash
# Unsupported — shown for migration context only:
export MEDRE_MESHTASTIC_CONNECTION_TYPE=tcp
export MEDRE_MESHTASTIC_HOST=meshtastic.local

# New:
export MEDRE_ADAPTER__RADIO__CONNECTION_TYPE=tcp
export MEDRE_ADAPTER__RADIO__HOST=meshtastic.local
```

**MeshCore:**

```bash
# Unsupported — shown for migration context only:
export MEDRE_MESHCORE_HOST=meshcore.local

# New:
export MEDRE_ADAPTER__MESHCORE_RADIO__HOST=meshcore.local
```

**LXMF:**

```bash
# Unsupported — shown for migration context only:
export MEDRE_LXMF_CONNECTION_TYPE=reticulum

# New:
export MEDRE_ADAPTER__LOCAL__CONNECTION_TYPE=reticulum
```

> **Note:** The pytest live-test harness uses convenience variables like `MATRIX_ACCESS_TOKEN` or `MESHTASTIC_CONNECTION_TYPE` (without the `MEDRE_` prefix) for test runner configuration. Those are test-only variables and are not processed by MEDRE's runtime config system. See `docs/dev/live-test-harness.md` for details.

#### Examples

Single Matrix adapter with `adapter_id = "main"`:

```bash
export MEDRE_ADAPTER__MAIN__HOMESERVER=https://matrix.example.com
export MEDRE_ADAPTER__MAIN__USER_ID=@bot:example.com
export MEDRE_ADAPTER__MAIN__ACCESS_TOKEN=syt_secret_token_here
export MEDRE_ADAPTER__MAIN__ENCRYPTION_MODE=plaintext
```

Multiple adapters of the same transport:

```bash
# adapter_id = "matrix-primary"
export MEDRE_ADAPTER__MATRIX_PRIMARY__HOMESERVER=https://matrix.example.com
export MEDRE_ADAPTER__MATRIX_PRIMARY__USER_ID=@bot1:example.com
export MEDRE_ADAPTER__MATRIX_PRIMARY__ACCESS_TOKEN=syt_...

# adapter_id = "matrix-secondary"
export MEDRE_ADAPTER__MATRIX_SECONDARY__HOMESERVER=https://matrix.other.com
export MEDRE_ADAPTER__MATRIX_SECONDARY__USER_ID=@bot2:other.com
export MEDRE_ADAPTER__MATRIX_SECONDARY__ACCESS_TOKEN=syt_...
```

Meshtastic TCP adapter:

```bash
# adapter_id = "radio"
export MEDRE_ADAPTER__RADIO__CONNECTION_TYPE=tcp
export MEDRE_ADAPTER__RADIO__HOST=meshtastic.local
export MEDRE_ADAPTER__RADIO__PORT=4403
export MEDRE_ADAPTER__RADIO__ENABLED=true
```

## Env-First Adapter Creation

In addition to overriding fields on adapters declared in TOML, you can create
entirely new adapters from environment variables alone. Any env var with a
`TRANSPORT` field under a token that does not match an existing TOML adapter
triggers adapter creation.

### A. Override an existing TOML adapter

When the token matches an adapter already defined in TOML, env vars override
its fields as usual. No adapter is created; the existing one is patched.

```bash
MEDRE_ADAPTER__RADIO_A__SERIAL_PORT=/dev/ttyUSB0
```

This changes `serial_port` on whatever adapter has `adapter_id` normalising to
`RADIO_A`. The adapter must already exist in the TOML config.

### B. Create a new adapter from env

When the token does not match any TOML adapter, setting `TRANSPORT` tells
MEDRE which transport config dataclass to build. All other fields under that
token populate the new adapter's config.

```bash
MEDRE_ADAPTER__RADIO_A__TRANSPORT=meshtastic
MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE=serial
MEDRE_ADAPTER__RADIO_A__SERIAL_PORT=/dev/ttyUSB0
```

The `TRANSPORT` field is required for env-created adapters. Accepted values:
`matrix`, `meshtastic`, `meshcore`, `lxmf`. Values are case-insensitive
(`Matrix`, `MATRIX`, and `matrix` are all equivalent). Any field available on
that transport's config dataclass can be set via env, just like overrides on
existing adapters.

Env-created adapters default to `enabled = true` and `adapter_kind = "real"`.
Set them explicitly if you need different behaviour. See
[Adapter kind (env-created adapters)](#f-adapter-kind-env-created-adapters)
below for `ADAPTER_KIND` details.

### C. Multi-adapter env-only deployment

You can define multiple adapters entirely from env, with no TOML adapter
sections at all. Wire them together using env-driven route creation
(see [Env-Driven Route Creation](#env-driven-route-creation)) or with
TOML `[routes.*]` sections.

```bash
# Matrix adapter — token MATRIX_PRIMARY
MEDRE_ADAPTER__MATRIX_PRIMARY__TRANSPORT=matrix
MEDRE_ADAPTER__MATRIX_PRIMARY__HOMESERVER=https://matrix.example.com
MEDRE_ADAPTER__MATRIX_PRIMARY__USER_ID=@bot:example.com
MEDRE_ADAPTER__MATRIX_PRIMARY__ACCESS_TOKEN=syt_...

# Meshtastic adapter — token RADIO_A
MEDRE_ADAPTER__RADIO_A__TRANSPORT=meshtastic
MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE=serial
MEDRE_ADAPTER__RADIO_A__SERIAL_PORT=/dev/ttyACM0
```

Both adapters are created from env. Wire them together using env-driven
route creation (see [Env-Driven Route Creation](#env-driven-route-creation))
or by declaring `[routes.*]` sections in TOML.

### D. Default adapter_id behaviour

For env-created adapters, the token is converted to an adapter_id by
lowercasing and replacing underscores with hyphens.

| Token            | Default `adapter_id` |
| ---------------- | -------------------- |
| `MATRIX_PRIMARY` | `matrix-primary`     |
| `RADIO_A`        | `radio-a`            |
| `MESHCORE_TBEAM` | `meshcore-tbeam`     |

To override this default, set the `ADAPTER_ID` field explicitly:

```bash
MEDRE_ADAPTER__MATRIX_PRIMARY__TRANSPORT=matrix
MEDRE_ADAPTER__MATRIX_PRIMARY__ADAPTER_ID=prod-matrix
MEDRE_ADAPTER__MATRIX_PRIMARY__HOMESERVER=https://matrix.example.com
# ...
```

The `ADAPTER_ID` override only applies to env-created adapters. For adapters
defined in TOML, the `adapter_id` field in the TOML section is authoritative
and cannot be changed via env.

### E. Limitations

- **`ADAPTER_ID` override is env-only.** The `ADAPTER_ID` field only works for
  adapters being created from env. It does not override `adapter_id` on
  adapters declared in TOML.
- **Dict and tuple fields remain TOML-only.** Fields like `channel_mapping`,
  `node_config`, and `auto_join_rooms` require structured data. They cannot be
  set through the flat string format of environment variables. This is the same
  restriction that applies to overrides on existing adapters.

### F. Adapter kind (env-created adapters)

Env-created adapters accept the `ADAPTER_KIND` field to control whether a
live or simulated adapter is built:

| Value  | Description                                                 |
| ------ | ----------------------------------------------------------- |
| `real` | Build the live adapter with optional SDK imports (default). |
| `fake` | Build a simulated adapter without optional SDK imports.     |

```bash
MEDRE_ADAPTER__RADIO_A__TRANSPORT=meshtastic
MEDRE_ADAPTER__RADIO_A__ADAPTER_KIND=fake
MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE=serial
MEDRE_ADAPTER__RADIO_A__SERIAL_PORT=/dev/ttyUSB0
```

`ADAPTER_KIND` defaults to `"real"` when not specified. Invalid values raise
`ConfigValidationError` at startup. This field is only available for
env-created adapters — adapters defined in TOML use the `adapter_kind` key
in their TOML section (see per-transport schema above).

## Env-Driven Route Creation

In addition to creating adapters from env vars, you can create **or
override** routes entirely from environment variables. This lets you
deploy a full bridge without writing any TOML route sections, or
override an existing TOML-defined route's fields. Both creation and
override use the same validation rules (see below).

### Syntax

Route env vars use the prefix `MEDRE_ROUTE__<TOKEN>__`:

```bash
MEDRE_ROUTE__<TOKEN>__SOURCE_ADAPTERS=adapter-a,adapter-b
MEDRE_ROUTE__<TOKEN>__DEST_ADAPTERS=adapter-c
MEDRE_ROUTE__<TOKEN>__DIRECTIONALITY=source_to_dest
MEDRE_ROUTE__<TOKEN>__ENABLED=true
MEDRE_ROUTE__<TOKEN>__SOURCE_CHANNEL=1
```

`<TOKEN>` is an arbitrary uppercase identifier you choose. It must be
unique across all route env tokens. Tokens may contain only letters,
numbers, and underscores (no hyphens, dots, or spaces).

Env route validation follows the same invariants as TOML route validation:

- `source_adapters` and `dest_adapters` must be non-empty
- Source and destination adapters must not overlap
- `source_room` / `source_channel` are aliases and cannot be set to different values
- `dest_room` / `dest_channel` are aliases and cannot be set to different values
- Duplicate entries in `source_adapters` or `dest_adapters` are rejected
- Invalid `directionality` values are rejected
- Unsupported route fields are rejected

### Field Types

| Field             | Type                 | Required | Description                                                               |
| ----------------- | -------------------- | -------- | ------------------------------------------------------------------------- |
| `SOURCE_ADAPTERS` | comma-separated list | yes      | Adapter IDs that originate events. Must not overlap with `DEST_ADAPTERS`. |
| `DEST_ADAPTERS`   | comma-separated list | yes      | Adapter IDs that receive events. Must not overlap with `SOURCE_ADAPTERS`. |
| `DIRECTIONALITY`  | string               | no       | `source_to_dest` (default), `dest_to_source`, or `bidirectional`.         |
| `ENABLED`         | bool                 | no       | `true`/`false`/`yes`/`no`/`1`/`0`. Defaults to `true`.                    |
| `SOURCE_CHANNEL`  | string               | no       | Source channel or conversation ID.                                        |
| `DEST_CHANNEL`    | string               | no       | Destination channel or conversation ID.                                   |
| `SOURCE_ROOM`     | string               | no       | Source Matrix room ID.                                                    |
| `DEST_ROOM`       | string               | no       | Destination Matrix room ID.                                               |
| `ROUTE_ID`        | string               | no       | Explicit route ID. See default derivation below.                          |

### Route ID Default Derivation

When `ROUTE_ID` is not set, the route ID is derived from the token by
lowercasing and replacing underscores with hyphens. This matches the
same convention used for env-created adapter IDs.

| Token                   | Default `route_id`      |
| ----------------------- | ----------------------- |
| `RADIO_A_TO_MATRIX`     | `radio-a-to-matrix`     |
| `MATRIX_PRIMARY_BRIDGE` | `matrix-primary-bridge` |
| `ADMIN_ROUTE`           | `admin-route`           |

### Full Env-Only Example

The following example creates a complete deployment from environment variables
with no adapter or route sections in TOML. This uses fake adapters, which is
the recommended starting point for smoke-testing the pipeline without network.

**Minimal TOML:**

```toml
[runtime]
name = "env-deployed"

[storage]
backend = "sqlite"
path = "/var/medre/medre.db"
```

**Environment variables:**

```bash
# Matrix fake adapter
export MEDRE_ADAPTER__MATRIX_FAKE__TRANSPORT=matrix
export MEDRE_ADAPTER__MATRIX_FAKE__ADAPTER_KIND=fake
export MEDRE_ADAPTER__MATRIX_FAKE__HOMESERVER=https://matrix.example.test
export MEDRE_ADAPTER__MATRIX_FAKE__USER_ID=@bot:example.test
export MEDRE_ADAPTER__MATRIX_FAKE__ACCESS_TOKEN=fake-token
export MEDRE_ADAPTER__MATRIX_FAKE__ROOM_ALLOWLIST=!room:example.test

# Meshtastic fake adapter
export MEDRE_ADAPTER__RADIO_A__TRANSPORT=meshtastic
export MEDRE_ADAPTER__RADIO_A__ADAPTER_KIND=fake
export MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE=fake
export MEDRE_ADAPTER__RADIO_A__MESHNET_NAME=RadioA

# Route between them
export MEDRE_ROUTE__RADIO_TO_MATRIX__SOURCE_ADAPTERS=radio-a
export MEDRE_ROUTE__RADIO_TO_MATRIX__DEST_ADAPTERS=matrix-fake
export MEDRE_ROUTE__RADIO_TO_MATRIX__DIRECTIONALITY=source_to_dest
export MEDRE_ROUTE__RADIO_TO_MATRIX__ENABLED=true
```

Adapter IDs are derived from env tokens: `MATRIX_FAKE` → `matrix-fake`,
`RADIO_A` → `radio-a`.

**For a real-adapter deployment** (live Matrix + real Meshtastic serial),
use `adapter_kind = "real"` (the default) and set real connection fields:

```bash
export MEDRE_ADAPTER__MATRIX_PRIMARY__TRANSPORT=matrix
export MEDRE_ADAPTER__MATRIX_PRIMARY__HOMESERVER=https://matrix.example.com
export MEDRE_ADAPTER__MATRIX_PRIMARY__USER_ID=@bot:example.com
export MEDRE_ADAPTER__MATRIX_PRIMARY__ACCESS_TOKEN=syt_...
export MEDRE_ADAPTER__MATRIX_PRIMARY__ROOM_ALLOWLIST="!bridge:example.com"

export MEDRE_ADAPTER__RADIO_A__TRANSPORT=meshtastic
export MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE=serial
export MEDRE_ADAPTER__RADIO_A__SERIAL_PORT=/dev/ttyACM0

export MEDRE_ROUTE__RADIO_A_TO_MATRIX__SOURCE_ADAPTERS=radio-a
export MEDRE_ROUTE__RADIO_A_TO_MATRIX__DEST_ADAPTERS=matrix-primary
export MEDRE_ROUTE__RADIO_A_TO_MATRIX__DIRECTIONALITY=bidirectional
export MEDRE_ROUTE__RADIO_A_TO_MATRIX__ENABLED=true
```

This creates two adapters (`matrix-primary`, `radio-a`) and one
bidirectional route between them, all from environment variables.

### Limitations

- **Advanced route features still require TOML.** Policy (`[routes.*.policy]`),
  retry (`[routes.*.retry]`), and `filter_hooks` are not expressible through
  env vars. If you need these, define the route in TOML instead.
- **Legacy transport env vars remain unsupported.** The same rejection rules
  that apply to adapter env vars apply here. Use the `MEDRE_ROUTE__<TOKEN>__`
  prefix, not transport-prefixed shortcuts.
- **Routes reference adapter IDs, not env tokens.** The `SOURCE_ADAPTERS` and
  `DEST_ADAPTERS` values must match the resolved `adapter_id` of the target
  adapters (whether those adapters come from TOML or env). For env-created
  adapters, this is the lowercased, hyphenated form of the adapter token.
  For TOML adapters, it is the `adapter_id` field value (or the section key
  if `adapter_id` is not set).

## Environment Variable `.env` Files

`.env` files are a deployment convenience for container runtimes. They are not
part of MEDRE's configuration model — MEDRE reads environment variables from the
process environment, not from `.env` files directly.

With Docker Compose or Podman:

```bash
# .env file
MEDRE_HOME=/opt/medre
MEDRE_LOG_LEVEL=DEBUG
MEDRE_ADAPTER__MAIN__HOMESERVER=https://matrix.example.com
MEDRE_ADAPTER__MAIN__USER_ID=@bot:example.com
MEDRE_ADAPTER__MAIN__ACCESS_TOKEN=syt_...
MEDRE_ADAPTER__MAIN__ENABLED=true
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

- **Access tokens are secrets.** Any env var whose field name matches `TOKEN`, `SECRET`, `PASSWORD`, `KEY`, `AUTH`, or `CREDENTIAL` (case-insensitive) is automatically redacted in provenance logs and diagnostics (`***REDACTED***`). For example, `MEDRE_ADAPTER__MAIN__ACCESS_TOKEN` is redacted.
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
  -e MEDRE_ADAPTER__MAIN__HOMESERVER=https://matrix.example.com \
  -e MEDRE_ADAPTER__MAIN__USER_ID=@bot:example.com \
  -e MEDRE_ADAPTER__MAIN__ACCESS_TOKEN=syt_... \
  -e MEDRE_ADAPTER__MAIN__ENABLED=true \
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
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.config.adapters.matrix import MatrixConfig

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

**Inspect-first product path.** `medre inspect` is the primary read-only
investigation command. For daily operation, the recommended sequence is:
`medre config check` and `medre routes validate` (pre-flight), `medre run`
(start the runtime), then `medre inspect event` and `medre inspect receipts`
(investigate what happened). For deeper investigation, use `medre inspect
event --timeline` (covers `trace event`), `medre inspect event --evidence`
(covers `evidence --event`), or `medre inspect event --recovery` (covers
`recover --event`). The specialized `medre trace`, `medre evidence`, and
`medre recover` commands remain available for standalone output or features
beyond what inspect flags provide. `medre smoke` is local validation tooling
for developers and CI, not a daily operator command. `medre replay` is a
lower-level supported command for recovery scenarios. See the
[Alpha Walkthrough](alpha-walkthrough.md) for the full product path.

```yaml
medre run [--config PATH]
    Start the MEDRE runtime. Loads config, resolves paths, starts adapters.

medre config check [--config PATH]
    Load and validate the config file. Prints config source, paths, and
    adapter status.     Exits with code 2 on config errors.

medre config sample
    Print a complete sample TOML configuration to stdout. Redirect to a
    file to use as a starting point.

medre paths
    Print all resolved MEDRE paths (config, state, data, cache, logs,
    database, matrix store). Useful for debugging path resolution.

medre version
    Print the MEDRE version.

medre adapters
    List available adapter kinds and their SDK dependency status.

medre diagnostics [--config PATH] [--refresh-health]
    Print adapter diagnostics snapshot. Without --refresh-health, reports
    build-time state only (no adapter start, no I/O). With --refresh-health,
    starts adapters, polls health, then stops.

medre routes (validate|topology|list) [--config PATH]
    Route management: validate route config, print topology preview, or
    list configured routes. All require --config.

medre smoke [--config PATH] [--storage-path PATH] [--drill NAME] [--run-session] [--json]
    Run fake bridge smoke test. Uses in-memory storage by default.
    Pass --storage-path to persist evidence to SQLite. --drill runs a
    named failure drill. --run-session runs a full bridge session cycle.

medre inspect (event|receipts|native-ref|replay) [--config PATH] [--storage-path PATH]
    Read-only storage inspection. All subcommands support --storage-path
    to open a SQLite database directly (bypasses config).

    medre inspect event <event_id> --storage-path <db>
    medre inspect event <event_id> --storage-path <db> --timeline
    medre inspect event <event_id> --storage-path <db> --evidence
    medre inspect event <event_id> --storage-path <db> --recovery
    medre inspect receipts --event <event_id> --storage-path <db>
    medre inspect receipts --replay-run <run_id> --storage-path <db>
    medre inspect native-ref --adapter <name> --message <native_id> --storage-path <db>
    medre inspect replay <run_id> --storage-path <db>

medre trace (event|replay) [--config PATH] [--storage-path PATH]
    Specialized chronological timeline assembly. Usually prefer
    `inspect event --timeline` for per-event timelines. Both subcommands
    support --storage-path for direct read-only access to a SQLite database.

    medre trace event <event_id> --storage-path <db> [--json]
    medre trace replay <run_id> --storage-path <db> [--json]

medre evidence [--config PATH] [--storage-path PATH] [--event ID] [--replay-run ID] [--include-refresh-health] [--json]
    Specialized support bundle collection. Usually prefer
    `inspect event --evidence` for per-event bundles. Supports --storage-path
    for direct read-only access to a SQLite database. --include-refresh-health
    starts adapters to poll live health.

medre replay --mode MODE --config PATH [--event ID] [--json]
    Execute a replay operation. Requires --config (rejects --storage-path).
    Modes: dry_run, re_route, best_effort. Replay needs config to resolve
    routes and adapters for replay targets.

medre recover --config PATH [--event ID] [--failed-only] [--dry-run] [--json]
    Specialized recovery classification. Usually prefer
    `inspect event --recovery` for per-event runbook. Requires --config
    to load storage and route context.
```

All commands that accept `--config` follow the
[Configuration Search Order](#configuration-search-order) when the flag is
omitted.

`inspect` is the primary read-only investigation command. It supports
`--storage-path` to open a SQLite database directly in read-only mode,
bypassing config file loading entirely. This is useful for inspecting smoke
databases or post-run evidence without maintaining a config file.
`trace`, `evidence`, and `recover` are specialized commands that also support
`--storage-path` (except `recover`, which requires `--config`). Commands that
need route or adapter context for write operations (`replay`) require
`--config`.

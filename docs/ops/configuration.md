# Configuration Reference

MEDRE uses YAML configuration files as the primary configuration source. Environment variables provide overrides for every config field and are useful for secrets and container deployments. Path defaults follow the XDG Base Directory Specification, with a single-directory `MEDRE_HOME` mode for Docker and Kubernetes.

The configuration system is only used by the MEDRE runtime (`medre run`). Library consumers construct adapter configs directly in Python — no config file needed.

MEDRE accepts a boring subset of YAML: explicit mappings and sequences only.
No anchors, aliases, merge keys, or custom tags are supported. Values that
YAML could misread must be quoted — Matrix room IDs (`"!room:server"`), MXIDs
(`"@user:server"`), channel IDs where string semantics matter (`"0"`), and
path placeholders like `"{state}/medre.sqlite"`.

## Configuration Search Order

The runtime locates its config file by searching in this order:

1. `--config` CLI flag — explicit path. Exits with error if file does not exist.
2. `MEDRE_CONFIG` environment variable — full path to a YAML file.
3. `$MEDRE_HOME/config.yaml` — if `MEDRE_HOME` is set.
4. XDG config path — `~/.config/medre/config.yaml` (or `$XDG_CONFIG_HOME/medre/config.yaml`).
5. `./medre.yaml` — fallback in the current working directory.

The loader accepts `.yaml` and `.yml` extensions and rejects `.toml` with a
clear error. The first file found wins. If no file is found, the runtime exits with `ConfigNotFoundError`.

Use `medre config check` to verify which file is loaded and whether it parses correctly.

## Pre-flight Validation with `medre config check`

`medre config check --config <path>` loads and validates a config file
without starting the runtime. Run it before `medre run` to surface config
errors fast — a clean `Config valid` means the file parses, every section
passes its validators, and every route adapter reference resolves.

### What it validates

- **Strict YAML parsing** — anchors, aliases, merge keys, duplicate keys,
  exotic tags, and multi-document streams are all rejected. Errors include
  `path:line:column:` locations and never echo raw file content.
- **Unknown keys** — rejected at the root, adapter-instance, route,
  section, and nested-section levels. Error messages name the offending key
  and list the accepted keys.
- **Section types and shapes** — `runtime`, `logging`, `storage`, `retry`,
  `adapters`, `routes`, and `runtime.limits` must each be a mapping;
  transport groups and adapter instances must be mappings. Unknown transport
  groups (e.g. `adapters.matrixx`) are rejected.
- **Adapter shapes** — every adapter config runs its transport-specific
  `validate()` (e.g. `MatrixConfig.validate()` rejects a `homeserver` that
  does not start with `http://` or `https://`).
- **Route adapter references** — every `source_adapters` / `dest_adapters`
  entry must resolve to a configured adapter ID. Dangling references surface
  here as validation errors, rather than passing and failing later at
  `medre run` startup.
- **Runtime limits** — `runtime.limits` range checks run via
  `RuntimeLimits.validate()`.

### Exit codes

| Code | Meaning                                                                                                       |
| ---- | ------------------------------------------------------------------------------------------------------------- |
| `0`  | Valid — loaded, parsed, and all validators passed.                                                            |
| `2`  | Config error — YAML parse, unknown key, type/shape, adapter `validate()`, route ref, or limits-range failure. |

On error, MEDRE prints `Config error: <message>` to stderr — a single line,
no traceback. Raw secrets are not echoed; diagnostics may include safe IDs, key names, type names, and `section_path` locations.

### What it does not check

- **SDK availability** — whether optional transport SDKs (`mindroom-nio`,
  `mtjk`, etc.) are importable is checked at `medre run` startup, not here.
  A fake-only config validates regardless of installed SDKs; a `real`
  adapter config validates structurally even when the SDK is missing.
- **Storage path writability** — the storage directory is created at
  `medre run` startup, not at `medre config check`.

For interactive route debugging, `medre routes validate [--config PATH]`
prints a topology preview alongside the same route validation.

### Collecting a support bundle

`medre support bundle --config <path> --output medre-support.zip`
collects an offline, redacted support bundle for issue reports. The ZIP
is built from config discovery, `config check`, the route plan, the
adapter inventory, config/adapter schema presence, environment metadata,
and a redacted copy of the parsed config. It is offline by design: no
adapters start, no live health is refreshed, and no network or hardware
I/O is performed.

The support bundle is **observational, not delivery evidence**. It does
not include live probe results, adapter health refresh, storage contents
(events, receipts, outbox rows), delivery evidence, runtime logs, or any
hardware/network checks. For storage-backed delivery evidence use
`medre inspect event --evidence` (per-event) or `medre evidence
--storage-path` (storage-backed evidence report) — these are separate
paths, not parts of the bundle. See [troubleshooting.md](troubleshooting.md)
for the full bundle contents, the complete not-included list, and the
redaction scope.

### Route Topology Preview with `medre routes plan`

`medre routes plan --config <path>` renders the expanded route topology
the runtime will build, **without performing any live network or hardware
I/O**. No adapter is started, no SDK is imported, no transport is
contacted — the plan is computed purely from the parsed config plus the
configured adapter platforms.

What the plan shows:

- The configured adapters (id, transport, kind, `origin_label`).
- Every expanded route leg, one row per leg, including the legs produced
  by `channel_room_map` expansion and bidirectional reverse legs.
- The direction of each leg (forward / reverse, and the platform pair).
- The resolved `origin_label` per leg and its provenance — per-entry,
  route-level, adapter fallback, or unset — so the relay-prefix
  precedence chain is visible end-to-end.
- Fan-in decisions: when a `channel_room_map` maps multiple Meshtastic
  channels into one Matrix room and the route only creates
  Meshtastic→Matrix legs, the plan annotates the fan-in as allowed.

The plan is **observational, not delivery evidence**. It describes the
shape the router will receive; it does not record, prove, or guarantee
any delivery. Use `medre config check` for general validation (YAML,
unknown keys, adapter shapes, route adapter references) and
`medre routes plan` for the expanded-topology preview. The two commands
are complementary: `config check` answers "is this config well-formed?",
`routes plan` answers "what will MEDRE actually build from it?".

### Docker `${VAR}` configs cannot be loaded directly

Docker bridge configs (`examples/configs/docker-matrix-bridge.yaml`,
`examples/configs/docker-meshtastic-bridge.yaml`) use `${MEDRE_HOMESERVER}`,
`${MEDRE_ACCESS_TOKEN}`, and similar placeholders. These are resolved by
Docker Compose from an `env_file`, not by the MEDRE config loader. The
loader treats `{...}` as a [path placeholder](#path-placeholders) and
rejects `${VAR}` as an invalid placeholder, so these configs cannot be
loaded by `medre config check` or `medre run` directly. Use `medre smoke`,
the Docker Compose workflow (see [running-medre.md](running-medre.md)), or
pre-resolve the placeholders before validation.

## YAML Schema Reference

### `runtime`

Top-level runtime behaviour.

```yaml
runtime:
  name: medre # instance name (informational)
  shutdown_timeout_seconds: 10 # graceful shutdown deadline in seconds
```

| Field                      | Type   | Default   | Description                                                       |
| -------------------------- | ------ | --------- | ----------------------------------------------------------------- |
| `name`                     | string | `"medre"` | Instance name used in logs and diagnostics.                       |
| `shutdown_timeout_seconds` | int    | `10`      | Maximum seconds to wait for adapters to stop before forcing exit. |

### `logging`

```yaml
logging:
  level: INFO # INFO, DEBUG, WARNING, ERROR
  format: text # text or json
```

| Field    | Type   | Default  | Description                                                                               |
| -------- | ------ | -------- | ----------------------------------------------------------------------------------------- |
| `level`  | string | `"INFO"` | Log level for the `medre.*` logger namespace. One of `INFO`, `DEBUG`, `WARNING`, `ERROR`. |
| `format` | string | `"text"` | Output format. `text` for human-readable, `json` for structured logging.                  |

`level` controls MEDRE logs only — dependency libraries (nio, meshtastic, aiohttp, etc.) inherit the root logger level (`WARNING`) unless explicitly configured via `logging.overrides`.

#### `logging.overrides` — Troubleshooting Escape Hatch

Per-logger level overrides for dependency libraries. Each key is a Python logger name and the value is a log level string. This is a troubleshooting tool — add overrides only when debugging an integration issue, then remove them.

**Default dependency log levels** (applied automatically):

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

Example troubleshooting config (remove after debugging):

```yaml
logging:
  overrides:
    aiohttp: INFO
    meshtastic: DEBUG
    nio: DEBUG
    "nio.crypto.log": WARNING
```

### `storage`

```yaml
storage:
  backend: sqlite
  path: "{state}/medre.sqlite"
```

| Field     | Type   | Default    | Description                                                                                               |
| --------- | ------ | ---------- | --------------------------------------------------------------------------------------------------------- |
| `backend` | string | `"sqlite"` | Storage backend. Currently only `sqlite` is supported.                                                    |
| `path`    | string | `None`     | Database file path. Supports [path placeholders](#path-placeholders). Defaults to `{state}/medre.sqlite`. |

MEDRE uses a single configured storage backend holding canonical events, delivery receipts, native references, replay state, and cross-adapter relationships. There is no per-adapter database. Transport-owned local files (Matrix crypto stores, LXMF identities) live under adapter state roots.

### `adapters.matrix.<instance_name>`

Each Matrix adapter instance is a separate mapping entry under `adapters.matrix`.
The `<instance_name>` key becomes the `adapter_id` unless overridden.

```yaml
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: real
      adapter_id: main
      homeserver: "https://matrix.example.com"
      user_id: "@bot:example.com"
      access_token: "<matrix-access-token>"
      room_allowlist:
        - "!room:example.com"
      sync_timeout_ms: 30000
      encryption_mode: plaintext
```

| Field                     | Type           | Default       | Description                                                                               |
| ------------------------- | -------------- | ------------- | ----------------------------------------------------------------------------------------- |
| `enabled`                 | bool           | `true`        | Whether this adapter instance is active.                                                  |
| `adapter_kind`            | string         | `"real"`      | `"real"` builds the live adapter; `"fake"` builds a simulated adapter.                    |
| `adapter_id`              | string         | instance name | Unique identifier. Defaults to the mapping key.                                           |
| `homeserver`              | string         | _(required)_  | Matrix homeserver URL. Must start with `http://` or `https://`.                           |
| `user_id`                 | string         | _(required)_  | Fully-qualified Matrix user ID.                                                           |
| `access_token`            | string         | `""`          | Access token. Treat as a secret.                                                          |
| `room_allowlist`          | list of string | `None`        | Room IDs to accept. `None` means all rooms.                                               |
| `metadata_embedding_mode` | string         | `"safe"`      | How metadata is embedded in messages.                                                     |
| `sync_timeout_ms`         | int            | `30000`       | Long-polling sync timeout in milliseconds.                                                |
| `encryption_mode`         | string         | `"plaintext"` | `plaintext`, `e2ee_required`, or `e2ee_optional`.                                         |
| `require_encrypted_rooms` | bool           | `false`       | When `true`, only operate in encrypted rooms. Invalid with `encryption_mode="plaintext"`. |

`device_id` and `store_path` are not operator-facing — the runtime derives them automatically.

### `adapters.meshtastic.<instance_name>`

```yaml
adapters:
  meshtastic:
    radio:
      enabled: false
      adapter_kind: real
      adapter_id: radio
      connection_type: serial
      serial_port: /dev/ttyACM0
      host: meshtastic.local
      port: 4403
      ble_address: ""
      origin_label: MyMesh
      default_channel: 0
      channel_mapping:
        "0": general
        "1": admin
      message_delay_seconds: 0.5
      startup_backlog_suppress_seconds: 5.0
      sync_timeout_ms: 30000
      outbound_mode: enabled
```

| Field                              | Type               | Default       | Description                                                                           |
| ---------------------------------- | ------------------ | ------------- | ------------------------------------------------------------------------------------- |
| `enabled`                          | bool               | `true`        | Active status.                                                                        |
| `adapter_kind`                     | string             | `"real"`      | `"real"` or `"fake"`.                                                                 |
| `adapter_id`                       | string             | instance name | Unique identifier.                                                                    |
| `connection_type`                  | string             | `"fake"`      | `fake`, `tcp`, `serial`, or `ble`.                                                    |
| `host`                             | string             | `None`        | Hostname or IP for TCP. Required when `connection_type="tcp"`.                        |
| `port`                             | int                | `None`        | Port for TCP.                                                                         |
| `serial_port`                      | string             | `None`        | Serial device path. Required when `connection_type="serial"`.                         |
| `ble_address`                      | string             | `None`        | BLE MAC address. Required when `connection_type="ble"`.                               |
| `origin_label`                     | string             | `""`          | Platform-neutral source label for relay prefixes.                                     |
| `default_channel`                  | int                | `0`           | Default radio channel index for outbound messages.                                    |
| `channel_mapping`                  | dict of int→string | `{}`          | Maps channel indices to human-readable names.                                         |
| `message_delay_seconds`            | float              | `0.5`         | Minimum delay between outbound messages (pacing).                                     |
| `startup_backlog_suppress_seconds` | float              | `5.0`         | Seconds after start to suppress stale backlog packets.                                |
| `sync_timeout_ms`                  | int                | `30000`       | Timeout for sync operations in milliseconds.                                          |
| `max_text_bytes`                   | int                | `227`         | Maximum UTF-8 byte budget for final radio text.                                       |
| `outbound_mode`                    | string             | `"enabled"`   | `"enabled"` allows RF transmission; `"listen_only"` suppresses all outbound delivery. |

#### Outbound Gate Semantics (Meshtastic)

| Value           | Inbound | Outbound                           | Delivery receipt                        |
| --------------- | ------- | ---------------------------------- | --------------------------------------- |
| `"enabled"`     | Normal  | Normal                             | Normal                                  |
| `"listen_only"` | Normal  | Suppressed — non-retryable failure | `outbound suppressed: listen_only mode` |

### `adapters.meshcore.<instance_name>`

```yaml
adapters:
  meshcore:
    radio:
      enabled: false
      adapter_kind: real
      adapter_id: radio
      connection_type: serial
      serial_port: /dev/ttyUSB0
      serial_baudrate: 115200
      host: meshcore.local
      port: 4000
      ble_address: ""
      origin_label: ""
      default_channel: 0
      message_delay_seconds: 0.5
      identity: my-node
      pubkey: abcdef0123456789
      node_config: {}
```

| Field                                           | Type       | Default       | Description                                                        |
| ----------------------------------------------- | ---------- | ------------- | ------------------------------------------------------------------ |
| `enabled`                                       | bool       | `true`        | Active status.                                                     |
| `adapter_kind`                                  | string     | `"real"`      | `"real"` or `"fake"`.                                              |
| `adapter_id`                                    | string     | instance name | Unique identifier.                                                 |
| `connection_type`                               | string     | `"fake"`      | `fake`, `tcp`, `serial`, or `ble`.                                 |
| `host` / `port` / `serial_port` / `ble_address` | string/int | `None`        | Connection parameters. TCP port defaults to 4000 when `port=None`. |
| `origin_label`                                  | string     | `""`          | Platform-neutral source label for relay prefixes.                  |
| `default_channel`                               | int        | `0`           | Default outbound channel.                                          |
| `message_delay_seconds`                         | float      | `0.5`         | Pacing.                                                            |
| `identity`                                      | string     | `None`        | MeshCore node identity string.                                     |
| `pubkey`                                        | string     | `None`        | Public key as hex string.                                          |
| `max_text_bytes`                                | int        | `512`         | Maximum UTF-8 byte budget for rendered radio text.                 |
| `serial_baudrate`                               | int        | `115200`      | Baud rate for serial connection.                                   |
| `node_config`                                   | dict       | `{}`          | Opaque node-specific settings. No secret keys.                     |

### `adapters.lxmf.<instance_name>`

```yaml
adapters:
  lxmf:
    local:
      enabled: false
      adapter_kind: real
      adapter_id: local
      connection_type: reticulum
      display_name: MEDRE
      stamp_cost: 8
      default_delivery_method: direct
      origin_label: ""
      default_channel: 0
      message_delay_seconds: 0.5
      metadata_embedding: true
      identity_path: "{state}/lxmf/identity"
      # storage_path: "{state}/lxmf/router"  # required for reticulum mode
```

| Field                     | Type   | Default       | Description                                                                                        |
| ------------------------- | ------ | ------------- | -------------------------------------------------------------------------------------------------- |
| `enabled`                 | bool   | `true`        | Active status.                                                                                     |
| `adapter_kind`            | string | `"real"`      | `"real"` or `"fake"`.                                                                              |
| `adapter_id`              | string | instance name | Unique identifier.                                                                                 |
| `connection_type`         | string | `"fake"`      | `fake` or `reticulum`.                                                                             |
| `display_name`            | string | `""`          | Display name for LXMF announces.                                                                   |
| `stamp_cost`              | int    | `8`           | Stamp cost. `0` means no stamp required.                                                           |
| `default_delivery_method` | string | `"direct"`    | `direct`, `opportunistic`, `propagated`, or `paper`.                                               |
| `origin_label`            | string | `""`          | Platform-neutral source label for relay prefixes.                                                  |
| `default_channel`         | int    | `0`           | Default outbound channel.                                                                          |
| `message_delay_seconds`   | float  | `0.5`         | Pacing.                                                                                            |
| `metadata_embedding`      | bool   | `true`        | Whether to embed MEDRE metadata in LXMF fields.                                                    |
| `identity_path`           | string | `None`        | Path to Reticulum identity file. Supports path placeholders.                                       |
| `storage_path`            | string | `None`        | Required when connection_type="reticulum". Path for LXMRouter storage. Supports path placeholders. |

### `routes.<route_id>`

Routes define named bridges between adapters. `route_id` contains only alphanumeric characters, underscores, or hyphens, and must be unique across the entire configuration.

Routes reference **adapter IDs** (the resolved `adapter_id` value), not the mapping key.

```yaml
routes:
  matrix_radio_bridge:
    source_adapters:
      - main
    dest_adapters:
      - radio
    directionality: bidirectional
    enabled: true
    source_room: "!room:example.com"
    dest_channel: "1"
    policy:
      allowed_event_types:
        - message.created
```

| Field                                                           | Type           | Default            | Description                                                                                             |
| --------------------------------------------------------------- | -------------- | ------------------ | ------------------------------------------------------------------------------------------------------- |
| `source_adapters`                                               | list of string | _(required)_       | Adapter IDs that originate events. No overlap with `dest_adapters`.                                     |
| `dest_adapters`                                                 | list of string | _(required)_       | Adapter IDs that receive events. No overlap with `source_adapters`.                                     |
| `directionality`                                                | string         | `"source_to_dest"` | `source_to_dest`, `dest_to_source`, or `bidirectional`.                                                 |
| `enabled`                                                       | bool           | `true`             | Active at startup. Disabled routes are validated but not registered.                                    |
| `source_room` / `dest_room` / `source_channel` / `dest_channel` | string         | `None`             | Room/channel targeting. `source_room` is an alias for `source_channel`, `dest_room` for `dest_channel`. |

#### Route Policy (`routes.<route_id>.policy`)

Optional static allowlist policy. A policy denial produces a `status="suppressed"` receipt with `failure_kind="policy_suppressed"` — not retryable. All policy fields are config-file-only (not settable via environment variables).

| Field                     | Type           | Default | Description                                         |
| ------------------------- | -------------- | ------- | --------------------------------------------------- |
| `allowed_event_types`     | list of string | `[]`    | Event kinds this route permits. Empty = all events. |
| `allowed_source_adapters` | list of string | `[]`    | Source adapter names to permit. Empty = any.        |
| `allowed_dest_adapters`   | list of string | `[]`    | Destination adapter names to permit. Empty = any.   |
| `sender_allowlist`        | list of string | `[]`    | Permitted sender identities. Empty = any sender.    |
| `room_allowlist`          | list of string | `[]`    | Permitted room identifiers. Empty = any room.       |
| `channel_allowlist`       | list of string | `[]`    | Permitted channel identifiers. Empty = any channel. |

Unknown keys are rejected at config load time. Allowlist values must be arrays of strings.

#### Route Retry (`routes.<route_id>.retry`)

Optional per-route retry policy for transient delivery failures. Both the route retry and the global `retry` section need to be enabled for automatic retry.

```yaml
routes:
  matrix_radio_bridge:
    retry:
      enabled: true
      max_attempts: 3
      backoff_base: 2.0
      max_delay_seconds: 60.0
      jitter: false
```

| Field               | Type  | Default | Description                                          |
| ------------------- | ----- | ------- | ---------------------------------------------------- |
| `enabled`           | bool  | `true`  | Retry scheduling active for this route.              |
| `max_attempts`      | int   | `3`     | Maximum total delivery attempts (including initial). |
| `backoff_base`      | float | `2.0`   | Base delay in seconds for exponential backoff.       |
| `max_delay_seconds` | float | `60.0`  | Upper bound for backoff delay.                       |
| `jitter`            | bool  | `false` | Whether to add jitter to backoff.                    |

#### channel_room_map Shorthand

For Matrix↔Meshtastic bridges, `channel_room_map` expands a single route into N channel→room pairs:

```yaml
routes:
  multi_channel_bridge:
    source_adapters:
      - main
    dest_adapters:
      - radio
    directionality: bidirectional
    enabled: true
    channel_room_map:
      "0": "!general:example.com"
      "1": "!admin:example.com"
```

Limitations:

- Room IDs must be canonical (`!` prefix). Aliases not supported.
- Mutually exclusive with `source_room`, `dest_room`, `source_channel`, `dest_channel`.
- Route must have exactly one source and one destination adapter.
- Channel keys 0–7 only (Meshtastic supports up to 8 channels).
- Two or more entries can share the same Matrix room for Meshtastic→Matrix
  fan-in (multiple radio channels relaying into one room). Duplicate rooms
  are rejected at route expansion when the route also creates a
  Matrix→Meshtastic leg — a Matrix event from a shared room is ambiguous
  across channels. See the Routing and Delivery Specification §17.6 for the
  full directionality decision matrix.

Fan-in example — two Meshtastic channels delivering into one Matrix room,
each with its own `source_origin_label` so the relay prefix distinguishes
them. This shape is valid only when the route does not also create a
Matrix→Meshtastic leg (here `source_to_dest` with a Meshtastic source and a
Matrix destination):

```yaml
routes:
  radio_fan_in:
    source_adapters:
      - radio
    dest_adapters:
      - main
    directionality: source_to_dest
    enabled: true
    channel_room_map:
      "0":
        room: "!room:example.com"
        source_origin_label: "LongFast"
      "1":
        room: "!room:example.com"
        source_origin_label: "ShortFast"
```

#### Per-entry origin labels

Each `channel_room_map` entry can carry its own origin labels, so two
channels bridged by the same route can show different attribution text
in the relay prefix (for example, the channel name). An entry is either
a bare room-ID string (no per-entry labels) or a structured table with
`room` plus optional `source_origin_label` / `dest_origin_label`:

```yaml
routes:
  radio_matrix:
    source_adapters:
      - main
    dest_adapters:
      - ops
    directionality: bidirectional
    enabled: true
    channel_room_map:
      "0":
        room: "!longfast:example.com"
        source_origin_label: "LongFast"
        dest_origin_label: "Matrix Ops"
      "1":
        room: "!shortfast:example.com"
        source_origin_label: "ShortFast"
```

Here channel 0 bridges to `!longfast:example.com` and tags both legs of
that mapping; channel 1 bridges to `!shortfast:example.com` and tags
only its forward leg, leaving the reverse leg to inherit the
route-level `dest_origin_label` (or the adapter `origin_label`). Both
shapes can be mixed in the same map.

How labels resolve for each expanded leg, from most to least specific:

1. The per-entry `source_origin_label` (forward leg) or
   `dest_origin_label` (reverse leg), when set.
2. The route-level `source_origin_label` / `dest_origin_label`, when set.
3. The source adapter's `origin_label`.
4. Empty string (no label rendered).

An empty string (`""`) at the per-entry or route level suppresses the
fallback below it: the `{origin_label}` template variable renders empty
for that leg. Leaving a label unset (or omitting the key) falls through
to the next level.

Keep in mind:

- The bare-string shape still works exactly as before — no labels are
  attached and the route-level / adapter labels apply uniformly.
- `origin_label` is human-readable attribution only. It is not a routing
  key, not a transport identity, and not delivery evidence. It never
  affects which route matches an event.
- Use separate routes when the `channel_room_map` shape cannot express
  the targeting you need (for example, distinct fanout across multiple
  destinations). Per-entry labels do not change targeting — they only
  label the legs the map already expands.

### `runtime.limits`

Controls concurrency and drain behaviour for the pipeline and replay engine. If absent, all limits use their defaults.

```yaml
runtime:
  limits:
    max_inflight_deliveries: 100 # max concurrent delivery coroutines (default: 100)
    max_inflight_replay_events: 100 # max concurrent replay event deliveries (default: 100)
    shutdown_drain_timeout_seconds: 10 # seconds to drain in-flight deliveries on shutdown (default: 10)
    delivery_acquire_timeout_seconds: 1.0 # seconds to wait for a delivery slot (default: 1.0)
```

| Field                              | Type  | Default | Description                                                                             |
| ---------------------------------- | ----- | ------- | --------------------------------------------------------------------------------------- |
| `max_inflight_deliveries`          | int   | `100`   | Maximum concurrent adapter `deliver()` calls. Capacity is acquired per delivery target. |
| `max_inflight_replay_events`       | int   | `100`   | Maximum concurrent replay event deliveries.                                             |
| `shutdown_drain_timeout_seconds`   | int   | `10`    | Seconds to wait for in-flight work to complete during shutdown.                         |
| `delivery_acquire_timeout_seconds` | float | `1.0`   | Seconds to wait for a delivery semaphore slot before rejecting.                         |

When capacity is exhausted, new deliveries are permanently rejected with `error="delivery_capacity_exceeded"` — no retry.

### `retry`

Controls the background RetryWorker that polls for due retry receipts.

```yaml
retry:
  enabled: true
  interval_seconds: 10.0
  batch_size: 20
  max_attempts: 3
```

| Field              | Type  | Default | Description                                                         |
| ------------------ | ----- | ------- | ------------------------------------------------------------------- |
| `enabled`          | bool  | `false` | Whether the RetryWorker runs.                                       |
| `interval_seconds` | float | `10.0`  | Polling interval.                                                   |
| `batch_size`       | int   | `20`    | Max receipts processed per cycle.                                   |
| `max_attempts`     | int   | `3`     | Global max attempts before dead-lettering. Each route may override. |

Both the route retry and the global retry need to be enabled for automatic retry to occur.

## XDG Default Paths

When `MEDRE_HOME` is not set:

```text
Config:    $XDG_CONFIG_HOME/medre/    or  ~/.config/medre/
State:     $XDG_STATE_HOME/medre/     or  ~/.local/state/medre/
Data:      $XDG_DATA_HOME/medre/      or  ~/.local/share/medre/
Cache:     $XDG_CACHE_HOME/medre/     or  ~/.cache/medre/
```

Runtime paths derived from the resolved state directory (`{state}`):

| Path                                          | Description                   |
| --------------------------------------------- | ----------------------------- |
| `{state}/medre.sqlite`                        | Single global storage backend |
| `{state}/logs/medre.log`                      | Global log file               |
| `{state}/adapters/{adapter_id}/`              | Per-adapter state root        |
| `{state}/adapters/{adapter_id}/matrix/store/` | Matrix E2EE crypto store      |
| `{state}/adapters/{adapter_id}/meshtastic/`   | Meshtastic transport state    |
| `{state}/adapters/{adapter_id}/meshcore/`     | MeshCore transport state      |
| `{state}/adapters/{adapter_id}/lxmf/`         | LXMF transport state          |

There are no per-adapter databases. Adapter-local filesystem state is transport-owned.

## MEDRE_HOME (Single-Directory Mode)

Setting `MEDRE_HOME` switches to single-directory mode — all paths resolve under one root:

```text
MEDRE_HOME=/opt/medre
Config:    /opt/medre/config.yaml
State:     /opt/medre/state/
Data:      /opt/medre/data/
Cache:     /opt/medre/cache/
```

Use this mode for Docker, Kubernetes, portable deployments, and development. See [running-medre.md](running-medre.md) for Docker deployment details.

```bash
export MEDRE_HOME=/opt/medre
medre paths    # verify paths
medre run      # reads /opt/medre/config.yaml
```

## Path Placeholders

YAML config values for filesystem paths support placeholder expansion:

| Placeholder | Expands to       |
| ----------- | ---------------- |
| `{config}`  | Config directory |
| `{state}`   | State directory  |
| `{data}`    | Data directory   |
| `{cache}`   | Cache directory  |
| `{logs}`    | Log directory    |

Example:

```yaml
storage:
  path: "{state}/medre.sqlite"

adapters:
  lxmf:
    local:
      identity_path: "{state}/lxmf/identity"
```

Unrecognized placeholders cause a `MedrePathsError` at startup.

## Environment Variable Overrides

Environment variables always override YAML values. The original YAML config is never mutated — a new frozen config is returned with overrides applied.

Three categories:

1. **Core env vars** — global runtime behaviour (paths, logging, limits).
2. **Instance-scoped adapter overrides** — `MEDRE_ADAPTER__<TOKEN>__<FIELD>`.
3. **Instance-scoped route overrides** — `MEDRE_ROUTE__<TOKEN>__<FIELD>`.

### Core Environment Variables

| Variable                                         | Maps to                                   | Default                |
| ------------------------------------------------ | ----------------------------------------- | ---------------------- |
| `MEDRE_HOME`                                     | Single-directory root path                | _(not set)_            |
| `MEDRE_CONFIG`                                   | Config file path                          | _(not set)_            |
| `MEDRE_DB_PATH`                                  | `storage.path`                            | `{state}/medre.sqlite` |
| `MEDRE_LOG_LEVEL`                                | `logging.level`                           | `INFO`                 |
| `MEDRE_RUNTIME_MAX_INFLIGHT_DELIVERIES`          | `limits.max_inflight_deliveries`          | _(YAML default)_       |
| `MEDRE_RUNTIME_MAX_INFLIGHT_REPLAY_EVENTS`       | `limits.max_inflight_replay_events`       | _(YAML default)_       |
| `MEDRE_RUNTIME_SHUTDOWN_DRAIN_TIMEOUT_SECONDS`   | `limits.shutdown_drain_timeout_seconds`   | _(YAML default)_       |
| `MEDRE_RUNTIME_DELIVERY_ACQUIRE_TIMEOUT_SECONDS` | `limits.delivery_acquire_timeout_seconds` | _(YAML default)_       |

### Retry Config via Env

```text
MEDRE_RETRY__ENABLED=true|false
MEDRE_RETRY__MAX_ATTEMPTS=5
MEDRE_RETRY__INTERVAL_SECONDS=10.0
MEDRE_RETRY__BATCH_SIZE=20
```

### Adapter Overrides

Format: `MEDRE_ADAPTER__<TOKEN>__<FIELD>=<value>`

- `<TOKEN>` is the uppercased, normalised `adapter_id` (non-alphanumeric → `_`, consecutive underscores collapsed).
- `<FIELD>` is a config dataclass field name (case-insensitive).
- If no adapter matches the token, MEDRE raises `ConfigValidationError` at startup.

#### Token Normalisation

| `adapter_id`     | Normalised token |
| ---------------- | ---------------- |
| `main`           | `MAIN`           |
| `matrix-primary` | `MATRIX_PRIMARY` |
| `radio.a`        | `RADIO_A`        |

Token collisions are detected at startup and raise `ConfigValidationError`.

#### Value Types

- **Boolean**: `1`, `true`, `yes` / `0`, `false`, `no` (case-insensitive).
- **Set** fields (e.g. `room_allowlist`): comma-separated values.
- All others: strings.

#### Per-Transport Field Reference

**Matrix** (`MatrixConfig`):

| Field             | Env var example                                                    |
| ----------------- | ------------------------------------------------------------------ |
| `enabled`         | `MEDRE_ADAPTER__MAIN__ENABLED=true`                                |
| `homeserver`      | `MEDRE_ADAPTER__MAIN__HOMESERVER=https://matrix.example.com`       |
| `user_id`         | `MEDRE_ADAPTER__MAIN__USER_ID=@bot:example.com`                    |
| `access_token`    | `MEDRE_ADAPTER__MAIN__ACCESS_TOKEN=<matrix-access-token>`          |
| `room_allowlist`  | `MEDRE_ADAPTER__MAIN__ROOM_ALLOWLIST=!room:example.com,!other:...` |
| `encryption_mode` | `MEDRE_ADAPTER__MAIN__ENCRYPTION_MODE=plaintext`                   |
| `sync_timeout_ms` | `MEDRE_ADAPTER__MAIN__SYNC_TIMEOUT_MS=30000`                       |

**Meshtastic** (`MeshtasticConfig`):

| Field             | Env var example                                   |
| ----------------- | ------------------------------------------------- |
| `enabled`         | `MEDRE_ADAPTER__RADIO__ENABLED=true`              |
| `connection_type` | `MEDRE_ADAPTER__RADIO__CONNECTION_TYPE=tcp`       |
| `host`            | `MEDRE_ADAPTER__RADIO__HOST=meshtastic.local`     |
| `port`            | `MEDRE_ADAPTER__RADIO__PORT=4403`                 |
| `serial_port`     | `MEDRE_ADAPTER__RADIO__SERIAL_PORT=/dev/ttyACM0`  |
| `outbound_mode`   | `MEDRE_ADAPTER__RADIO__OUTBOUND_MODE=listen_only` |
| `origin_label`    | `MEDRE_ADAPTER__RADIO__ORIGIN_LABEL=MyMesh`       |

**MeshCore** (`MeshCoreConfig`):

| Field             | Env var example                                 |
| ----------------- | ----------------------------------------------- |
| `enabled`         | `MEDRE_ADAPTER__RADIO__ENABLED=true`            |
| `connection_type` | `MEDRE_ADAPTER__RADIO__CONNECTION_TYPE=tcp`     |
| `host`            | `MEDRE_ADAPTER__RADIO__HOST=meshcore.local`     |
| `port`            | `MEDRE_ADAPTER__RADIO__PORT=4000`               |
| `identity`        | `MEDRE_ADAPTER__RADIO__IDENTITY=my-node`        |
| `pubkey`          | `MEDRE_ADAPTER__RADIO__PUBKEY=abcdef0123456789` |
| `origin_label`    | `MEDRE_ADAPTER__RADIO__ORIGIN_LABEL=MyMesh`     |

**LXMF** (`LxmfConfig`):

| Field             | Env var example                                             |
| ----------------- | ----------------------------------------------------------- |
| `enabled`         | `MEDRE_ADAPTER__LOCAL__ENABLED=true`                        |
| `connection_type` | `MEDRE_ADAPTER__LOCAL__CONNECTION_TYPE=reticulum`           |
| `display_name`    | `MEDRE_ADAPTER__LOCAL__DISPLAY_NAME=MEDRE`                  |
| `identity_path`   | `MEDRE_ADAPTER__LOCAL__IDENTITY_PATH={state}/lxmf/identity` |
| `origin_label`    | `MEDRE_ADAPTER__LOCAL__ORIGIN_LABEL=MyMesh`                 |

Dict fields such as Meshtastic `channel_mapping` and MeshCore `node_config`, plus tuple fields such as Matrix `auto_join_rooms`, cannot be set via env vars — use YAML instead.

### Unsupported Legacy Prefixes

These are **rejected at startup**:

| Rejected pattern     | Correct form                      |
| -------------------- | --------------------------------- |
| `MEDRE_MATRIX_*`     | `MEDRE_ADAPTER__<TOKEN>__<FIELD>` |
| `MEDRE_MESHTASTIC_*` | `MEDRE_ADAPTER__<TOKEN>__<FIELD>` |
| `MEDRE_MESHCORE_*`   | `MEDRE_ADAPTER__<TOKEN>__<FIELD>` |
| `MEDRE_LXMF_*`       | `MEDRE_ADAPTER__<TOKEN>__<FIELD>` |

Migration example:

```bash
# Old (rejected):
export MEDRE_MATRIX_ACCESS_TOKEN="<matrix-access-token>"
# New:
export MEDRE_ADAPTER__MAIN__ACCESS_TOKEN="<matrix-access-token>"
```

### Env-First Adapter Creation

You can create entirely new adapters from environment variables when the token does not match any YAML adapter:

```bash
# Matrix adapter — created from env
MEDRE_ADAPTER__MATRIX_PRIMARY__TRANSPORT=matrix
MEDRE_ADAPTER__MATRIX_PRIMARY__HOMESERVER=https://matrix.example.com
MEDRE_ADAPTER__MATRIX_PRIMARY__USER_ID=@bot:example.com
MEDRE_ADAPTER__MATRIX_PRIMARY__ACCESS_TOKEN="<matrix-access-token>"

# Meshtastic adapter — created from env
MEDRE_ADAPTER__RADIO_A__TRANSPORT=meshtastic
MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE=serial
MEDRE_ADAPTER__RADIO_A__SERIAL_PORT=/dev/ttyACM0
```

`TRANSPORT` is required for env-created adapters. Accepted values: `matrix`, `meshtastic`, `meshcore`, `lxmf`. The token is lowercased and underscores replaced with hyphens for the default `adapter_id` (e.g. `RADIO_A` → `radio-a`).

`ADAPTER_KIND` accepts `"real"` (default) or `"fake"` for env-created adapters.

### Env-Driven Route Creation

Routes can also be created from env vars:

```bash
MEDRE_ROUTE__RADIO_TO_MATRIX__SOURCE_ADAPTERS=radio-a
MEDRE_ROUTE__RADIO_TO_MATRIX__DEST_ADAPTERS=matrix-fake
MEDRE_ROUTE__RADIO_TO_MATRIX__DIRECTIONALITY=source_to_dest
MEDRE_ROUTE__RADIO_TO_MATRIX__ENABLED=true
```

Token is an arbitrary uppercase identifier. Route ID defaults to the lowercased, hyphenated token. Advanced route features (policy, retry) still require YAML.

### Full Env-Only Example

Minimal YAML + all adapters and routes from env:

```yaml
runtime:
  name: env-deployed

storage:
  backend: sqlite
  path: /var/medre/medre.db
```

```bash
# Matrix fake adapter
export MEDRE_ADAPTER__MATRIX_FAKE__TRANSPORT=matrix
export MEDRE_ADAPTER__MATRIX_FAKE__ADAPTER_KIND=fake
export MEDRE_ADAPTER__MATRIX_FAKE__HOMESERVER=https://matrix.example.test
export MEDRE_ADAPTER__MATRIX_FAKE__USER_ID=@bot:example.test
export MEDRE_ADAPTER__MATRIX_FAKE__ACCESS_TOKEN=fake-token

# Meshtastic fake adapter
export MEDRE_ADAPTER__RADIO_A__TRANSPORT=meshtastic
export MEDRE_ADAPTER__RADIO_A__ADAPTER_KIND=fake
export MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE=fake

# Route
export MEDRE_ROUTE__RADIO_TO_MATRIX__SOURCE_ADAPTERS=radio-a
export MEDRE_ROUTE__RADIO_TO_MATRIX__DEST_ADAPTERS=matrix-fake
export MEDRE_ROUTE__RADIO_TO_MATRIX__DIRECTIONALITY=source_to_dest
export MEDRE_ROUTE__RADIO_TO_MATRIX__ENABLED=true
```

## Secrets Management

### Principles

- Environment variables for secrets — not command-line arguments, not config files checked into version control, not hardcoded strings.
- Never commit credentials. Files containing tokens, private keys, or identity data belong in `.gitignore`.
- Store secret files outside the repo tree. If a secret lives as a file (e.g. LXMF identity), keep it in a path excluded by `.gitignore`.
- Never log tokens or private keys. Diagnostic output and error messages exclude raw credentials.

### Per-Transport Guidance

**Matrix:**

| Secret       | Handling                                                                            |
| ------------ | ----------------------------------------------------------------------------------- |
| Access token | Set via `MEDRE_ADAPTER__MAIN__ACCESS_TOKEN` env var. Never logged. Never committed. |

The runtime derives device ID via `whoami()` and uses an internal store path. The crypto store directory contains sensitive key material — exclude it from version control.

**Token rotation procedure:**

1. Generate a new token from the Matrix client (Element → Settings → Help & About → Access Token).
2. Update the environment variable.
3. Restart MEDRE to pick up the new token.

**Using `medre adapter matrix auth login` for token acquisition:**

Interactive (preferred for operators — prompts for the password securely and
keeps it out of shell history):

```bash
medre adapter matrix auth login \
  --homeserver https://matrix.example.com \
  --user @bot:example.com
```

Non-interactive via stdin (preferred for automation — read from a file or a
secret manager so the password never appears on the command line):

```bash
medre adapter matrix auth login \
  --homeserver https://matrix.example.com \
  --user @bot:example.com \
  --password-stdin < /run/secrets/matrix_password
```

The command keeps the token out of terminal output and saves credentials to
the Matrix sidecar JSON file. The runtime reads credentials from this sidecar
at startup. Accepted flags: `--homeserver`, `--user`, `--password`,
`--password-stdin`.

`--password` reads the password from the command line. It is supported for
automation that cannot pipe stdin, but the value is visible in shell history,
process listings, and audit logs — prefer interactive login or
`--password-stdin` whenever the caller can read from a file or pipe.

**If a token is leaked** (pasted in chat, committed to git, appeared in logs):

1. Revoke the token immediately via the Matrix client or Synapse admin API.
2. Re-run `medre adapter matrix auth login` to obtain a fresh token.
3. Rotate the config file and delete any artifacts containing the old token.

**Bearer token in config files:** When using a YAML config file, the `access_token` field is plaintext. Treat the config file as a secret:

```bash
chmod 600 /path/to/config.yaml
```

Use a dedicated Matrix bot account for MEDRE — never a personal account. Test with a throwaway room before bridging to real rooms. `MatrixConfig.__repr__()` redacts tokens to a short 3-character preview (`syt_…`) to prevent accidental leakage in logs and debug output.

**Meshtastic / MeshCore:** No secrets required. Connection parameters are network addresses, not credentials. Channel pre-shared keys are managed at the firmware level.

**LXMF:**

| Secret        | Handling                                                                                                                                                  |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Identity file | 64-byte private key file. Restrictive permissions (`chmod 600`). Never committed. Never logged. Never copied between instances — each identity is unique. |

```bash
chmod 600 /path/to/identity.key
ls -la /path/to/identity.key
# Expected: -rw------- (600)
```

### Automatic Redaction

Any env var whose field name matches `TOKEN`, `SECRET`, `PASSWORD`, `KEY`, `AUTH`, or `CREDENTIAL` (case-insensitive) is redacted in provenance logs and diagnostic output (`***REDACTED***`). The raw values are still applied to the config.

MeshCore `node_config` rejects keys named `private_key`, `secret`, or `password` at validation time.

### Git Exclusion

```text
*.key
*.pem
*.token
identity*
nio-store/
crypto-store/
.env
.env.*
```

MEDRE's `.gitignore` excludes common patterns. Verify before adding identity or token files.

### Docker and Deployment Secrets

When deploying in containers:

- Inject secrets via environment variables, not build args or baked-in config files.
- Use Docker secrets, Kubernetes secrets, or equivalent orchestrator secret management.
- Never include secret material in container images or build layers.
- Mount LXMF identity files as read-only volumes from a secrets manager.

## CLI Commands

`medre inspect` is the primary read-only investigation command. The recommended sequence for daily operation: `medre config check` and `medre routes validate` (pre-flight), `medre run` (start), then `medre inspect event` and `medre inspect receipts` (investigate).

The `medre support bundle` command and the `medre evidence` command are distinct paths and must not be conflated:

- **`medre support bundle`** writes an offline, redacted ZIP built from config discovery, `config check`, the route plan, the adapter inventory, schemas, environment metadata, and a redacted config copy. It does **not** include live probes, storage contents, delivery evidence, runtime logs, adapter health refresh, or hardware/network checks. See [troubleshooting.md](troubleshooting.md) § "Support Bundles".
- **`medre evidence`** is a read-only, storage-backed evidence report for investigating event and replay-run receipts. Prefer `medre inspect event --evidence` for per-event evidence bundles.

```text
medre run [--config PATH]
    Start the MEDRE runtime.

medre config check [--config PATH]
    Load and validate config. Exits with code 2 on errors.

medre config sample
    Print a complete sample YAML config to stdout.

medre paths
    Print all resolved MEDRE paths.

medre version
    Print the MEDRE version.

medre adapters
    List available adapter kinds and SDK dependency status.

medre diagnostics [--config PATH] [--refresh-health]
    Print adapter diagnostics snapshot. Without --refresh-health, reports
    build-time state only. With --refresh-health, starts adapters, polls
    health, then stops.

medre routes validate [--config PATH]
    Validate route configuration (offline, no network I/O).

medre routes topology [--config PATH]
    Print route topology preview (offline, no network I/O).

medre routes list [--config PATH]
    List configured routes (offline, no network I/O).

medre routes plan [--config PATH]
    Show the expanded route plan (offline, no network I/O). See
    "Route Topology Preview with `medre routes plan`" above.

medre smoke [--config PATH] [--drill NAME] [--run-session] [--json]
    Run fake bridge smoke test. Storage backend determined by config.
    Use a config with storage.backend = "sqlite" for persistent evidence.

medre inspect event --storage-path PATH
    Read-only event inspection. Supports --timeline, --evidence, --recovery flags.

medre inspect receipts --storage-path PATH
    Read-only receipt inspection. Filter with --event or --replay-run.

medre inspect native-ref --storage-path PATH
    Read-only native transport reference inspection.

medre inspect replay --storage-path PATH
    Read-only replay run inspection. Shows replay run metadata and receipt summaries.

    All inspect subcommands require --storage-path for direct SQLite access.

medre trace event --storage-path PATH [--json]
    Specialized chronological event timeline assembly. Prefer
    inspect event --timeline for per-event timelines.

medre trace replay --storage-path PATH [--json]
    Specialized chronological replay-run timeline assembly. Prefer
    inspect event --timeline for per-event timelines.

medre evidence --storage-path PATH [--event ID] [--replay-run ID] [--json]
    Read-only, storage-backed evidence report for event or replay-run
    receipt investigation. Prefer inspect event --evidence for per-event
    evidence bundles. This is distinct from medre support bundle.

medre replay --mode MODE --config PATH [--event ID] [--json]
    Execute a one-shot replay operation. Each invocation processes stored
    events once and exits — replay does not continuously tail the event log.
    Modes: strict, re_render, re_route, dry_run, best_effort.
    Requires --config. Duplicate-risky for best_effort.

medre recover --storage-path PATH [--event ID] [--failed-only] [--dry-run] [--json]
    Specialized recovery classification. Prefer
    inspect event --recovery for per-event runbook.

medre storage status [--storage-path PATH]
    Storage management (read-only). Reports schema health.

medre storage reset [--storage-path PATH]
    Storage management (destructive). Deletes the database; accepts
    --backup and --yes.

medre adapter matrix auth login
    Matrix credential management (no runtime). Authenticates with
    the homeserver and saves credentials to a sidecar JSON file; it never
    prints the access token.

medre adapter matrix auth status
    Matrix credential management (no runtime). Shows credential file
    status without printing secrets.

medre support bundle [--config PATH] [--output PATH]
    Write an offline, redacted support bundle ZIP. Built from config
    discovery, config check, route plan, adapter inventory, schemas,
    environment metadata, and a redacted config copy. Does NOT include
    live probes, storage contents, delivery evidence, runtime logs,
    adapter health refresh, or hardware/network checks. See
    troubleshooting.md § "Support Bundles".
```

## Environment Variable `.env` Files

`.env` files are a deployment convenience for container runtimes. MEDRE reads environment variables from the process environment, not from `.env` files directly. The container runtime loads the file into the process environment before starting MEDRE.

With Docker Compose or Podman:

```bash
# .env file
MEDRE_HOME=/opt/medre
MEDRE_LOG_LEVEL=DEBUG
MEDRE_ADAPTER__MAIN__HOMESERVER=https://matrix.example.com
MEDRE_ADAPTER__MAIN__USER_ID=@bot:example.com
MEDRE_ADAPTER__MAIN__ACCESS_TOKEN=<matrix-access-token>
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

## Docker / Kubernetes Usage

### Recommended Docker Pattern

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
  -e MEDRE_ADAPTER__MAIN__ACCESS_TOKEN="<matrix-access-token>" \
  -e MEDRE_ADAPTER__MAIN__ENABLED=true \
  -v medre-state:/opt/medre \
  medre run
```

### Serial Passthrough

For Meshtastic and MeshCore adapters using serial connections:

```bash
docker run -d \
  --device /dev/ttyACM0:/dev/ttyACM0 \
  -v /srv/medre:/opt/medre \
  medre run
```

The container user needs read/write access to the device. On most Linux distributions the device is owned by the `dialout` group. Options: run as user in `dialout` group, use `--group-add dialout`, or set udev rules on the host.

### Matrix E2EE Store Persistence

The Matrix crypto store is derived automatically under the resolved state directory (`{state}/adapters/{adapter_id}/matrix/store`). It persists across container restarts when the state volume is mounted. With `MEDRE_HOME=/opt/medre`, the store resolves to `/opt/medre/state/adapters/main/matrix/store` for an adapter with `adapter_id="main"`. Ensure the volume is persistent.

### Kubernetes

Use a PersistentVolumeClaim mounted at `MEDRE_HOME`. Inject secrets via Kubernetes Secrets as environment variables.

## Library Usage vs Runtime Usage

### Library (No Config File Needed)

```python
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.config.adapters.matrix import MatrixConfig

config = MatrixConfig(
    adapter_id="my-bot",
    homeserver="https://matrix.example.com",
    user_id="@bot:example.com",
    access_token="<matrix-access-token>",
    room_allowlist={"!room:example.com"},
)
adapter = MatrixAdapter(config)
```

### Runtime (Config File Driven)

```bash
medre config sample > ~/.config/medre/config.yaml
# Edit config, then:
medre run
# or:
medre run --config /path/to/config.yaml
```

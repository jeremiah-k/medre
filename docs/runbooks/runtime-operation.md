# MEDRE Runtime Operation

This runbook covers how to run, configure, deploy, and operate the MEDRE runtime.

## Running MEDRE

```bash
medre run --config config.toml
```

The runtime loads the specified config, validates it, creates required directories, and starts all enabled adapters. See [Configuration](configuration.md) for the full TOML schema.

To verify config without starting:

```bash
medre config check
```

## Exit Codes

`medre run` uses differentiated exit codes so operators and process supervisors can distinguish failure categories without parsing stderr.

| Code | Constant       | Meaning                                                                                                                                                                                                                                                 |
| ---- | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0    | `EXIT_OK`      | Successful run and clean shutdown.                                                                                                                                                                                                                      |
| 2    | `EXIT_CONFIG`  | Config file not found, TOML parse error, validation error, or no adapters enabled.                                                                                                                                                                      |
| 3    | `EXIT_BUILD`   | Runtime build failure — missing optional SDK dependency, invalid storage path, or adapter construction error. Adapters that fail during construction are recorded as build failures; if _all_ adapters fail to build, the runtime exits with this code. |
| 4    | `EXIT_STARTUP` | Total startup failure — zero adapters started successfully (after build). This covers core subsystem failures (storage init, pipeline runner) and total adapter startup failure.                                                                        |

**Degraded startup does NOT exit.** If at least one adapter starts successfully but others fail, the runtime enters `RUNNING` with `DEGRADED` health and continues operating. The boot summary and console output report which adapters failed.

### Diagnosing Degraded Startup

When the runtime starts with `DEGRADED` health, use these surfaces to understand what happened:

1. **CLI startup output** — logs which adapters started and which failed, with adapter ID attribution.

2. **`startup.boot_summary`** (snapshot path: `snapshot.startup.boot_summary`) — carries:
   - `startup_outcome`: `"partial"` for degraded startup.
   - `runtime_health`: `"degraded"`.
   - `adapters_started` / `adapters_failed` / `adapters_total`: counts.
   - `started_adapter_ids`: sorted list of adapters that started successfully.
   - `failed_adapter_ids`: sorted list of adapters that failed to start.
   - `build_failure_ids`: sorted list of adapters that failed during construction (before startup).
   - `route_count`: number of routes registered at startup.
   - Provenance: `scope="startup"`, `live_refresh=false`.

3. **`startup.build_failures`** (snapshot path: `snapshot.startup.build_failures`) — bounded list of adapters that failed during construction. Each entry has `adapter_id` and sanitized `error`.

4. **`routes.startup_readiness`** (snapshot path: `snapshot.routes.startup_readiness`) — shows routes that were **degraded** (some target adapters failed to start) or **skipped** (source adapter or all targets failed). Each entry carries `failed_adapter_ids`. Provenance: `scope="startup"`, `live_refresh=false`.

5. **`diagnostics.runtime_events`** (snapshot path: `snapshot.diagnostics.runtime_events`) — event buffer recording `adapter_start_failed`, `route_skipped`, and `startup_classified` events. Provenance: `scope="process_local"`, `live_refresh=false` (events grow from local runtime state transitions, not external polling).

**Provenance note:** All startup diagnostic surfaces carry `scope="startup"` and `live_refresh=false`. These values do not change after startup completes. For post-startup adapter state, check `lifecycle.adapters.{id}` (process-local).

### Exit Codes by Command

| Command                              | Config error | Build error | Startup error |
| ------------------------------------ | :----------: | :---------: | :-----------: |
| `medre run`                          |      2       |      3      |       4       |
| `medre diagnostics`                  |      2       |      3      |      n/a      |
| `medre diagnostics --refresh-health` |      2       |      3      |       4       |
| `medre config check`                 |      2       |     n/a     |      n/a      |
| `medre routes validate`              |      2       |     n/a     |      n/a      |
| `medre routes topology`              |      2       |     n/a     |      n/a      |
| `medre routes list`                  |      2       |     n/a     |      n/a      |

All commands print a human-readable error message to stderr (no traceback) before exiting nonzero.

## Configuration Overview

MEDRE uses a single TOML config file. The `[adapters.*]` sections define which transport adapters to run. You can declare multiple adapters of the same transport type, each with a unique `adapter_id`.

See [Configuration](configuration.md) for the complete field reference.

### Example 1 — Single Matrix Adapter (Plaintext)

```toml
[runtime]
name = "matrix-bridge"
shutdown_timeout_seconds = 10

[logging]
level = "INFO"
format = "text"

[storage]
backend = "sqlite"

[adapters.matrix.bot]
enabled = true
adapter_kind = "real"
homeserver = "https://matrix.example.com"
user_id = "@bot:example.com"
access_token = "syt_..."
room_allowlist = ["!room:example.com"]
encryption_mode = "plaintext"
```

### Example 2 — Single Matrix Adapter (E2EE)

```toml
[runtime]
name = "matrix-e2ee"
shutdown_timeout_seconds = 10

[logging]
level = "INFO"
format = "text"

[storage]
backend = "sqlite"

[adapters.matrix.securebot]
enabled = true
adapter_kind = "real"
homeserver = "https://matrix.example.com"
user_id = "@securebot:example.com"
access_token = "syt_..."
room_allowlist = ["!secretroom:example.com"]
encryption_mode = "e2ee_required"
```

E2EE mode requires the `mindroom-nio[e2e]` optional dependency. The crypto store is created automatically at `{state}/adapters/securebot/matrix/store/`. Device ID is discovered via the Matrix `whoami()` endpoint on first connect — do not configure it manually.

### Example 3 — Two Meshtastic Radios (LongFast + ShortTurbo)

```toml
[runtime]
name = "dual-radio"
shutdown_timeout_seconds = 15

[logging]
level = "INFO"
format = "json"

[storage]
backend = "sqlite"

[adapters.meshtastic.longfast]
enabled = true
adapter_kind = "real"
connection_type = "serial"
serial_port = "/dev/ttyACM0"
meshnet_name = "LongFast Net"
default_channel = 0
channel_mapping = {0 = "general", 1 = "alerts"}

[adapters.meshtastic.shortturbo]
enabled = true
adapter_kind = "real"
connection_type = "tcp"
host = "meshtastic-turbo.local"
port = 4403
meshnet_name = "ShortTurbo Net"
default_channel = 0
channel_mapping = {0 = "fast"}
```

Each radio is a separate adapter with its own connection. Startup order: `meshtastic.longfast`, then `meshtastic.shortturbo` (sorted by adapter_id).

### Example 4 — Mixed Runtime (Matrix + Meshtastic)

```toml
[runtime]
name = "mixed-bridge"
shutdown_timeout_seconds = 15

[logging]
level = "INFO"
format = "json"

[storage]
backend = "sqlite"

[adapters.matrix.bridge]
enabled = true
adapter_kind = "real"
homeserver = "https://matrix.example.com"
user_id = "@bridge:example.com"
access_token = "syt_..."
room_allowlist = ["!bridge-room:example.com"]
encryption_mode = "plaintext"

[adapters.meshtastic.radio]
enabled = true
adapter_kind = "real"
connection_type = "serial"
serial_port = "/dev/ttyACM0"
meshnet_name = "LocalMesh"
default_channel = 0
channel_mapping = {0 = "general"}
```

Startup order: `matrix.bridge` first (alphabetical by transport), then `meshtastic.radio`.

## Resource Limits

The `[runtime.limits]` section controls concurrency and drain behavior for the pipeline and replay engine. If this section is absent, all limits use their defaults.

### Configuration

```toml
[runtime]
name = "my-bridge"
shutdown_timeout_seconds = 10

[runtime.limits]
max_inflight_deliveries = 100       # max concurrent delivery coroutines (default: 100)
max_inflight_replay_events = 100    # max concurrent replay event deliveries (default: 100)
shutdown_drain_timeout_seconds = 10.0  # seconds to drain in-flight deliveries on shutdown (default: 10)
delivery_acquire_timeout_seconds = 1.0   # seconds to wait for a delivery slot (default: 1.0)
```

### How Delivery Limiting Works

The pipeline runner uses an `asyncio.Semaphore` to bound the number of concurrent adapter `deliver()` calls. Capacity is acquired **per delivery target** — each target in a fan-out independently acquires and releases a slot. When a per-target delivery is about to start:

1. The per-target coroutine attempts to acquire a semaphore slot.
2. If a slot is available immediately, the delivery proceeds.
3. If all slots are occupied, the coroutine waits up to `delivery_acquire_timeout_seconds`.
4. If the wait times out, the delivery fails with `status="permanent_failure"` and `error="delivery_capacity_exceeded"` (or `error="delivery_rejected_shutdown"` during shutdown). A diagnostic counter is incremented. **No retry** — capacity timeout is a backpressure signal.

This prevents unbounded memory growth from concurrent deliveries. Fan-out is correct: if 10 targets are matched and `max_inflight_deliveries=1`, only one target acquires capacity at a time while the rest wait on the semaphore.

### How Replay Limiting Works

The replay engine has a separate semaphore (`max_inflight_replay_events`) that bounds how many replay events can be in their **delivery phase** concurrently. This limits the number of replay deliveries actively executing at once, not all replay event processing (re-routing, re-rendering, and dry-run modes do not consume replay capacity). This prevents replay from consuming the entire delivery budget and starving real-time traffic. Replay deliveries that pass the replay limiter still acquire a slot on the delivery semaphore via the pipeline runner's per-target capacity guard.

### Diagnostics

Run `medre diagnostics` to see resource limit gauges:

| Counter                               | Description                                       |
| ------------------------------------- | ------------------------------------------------- |
| `inbound_accepted`                    | Inbound events accepted into the pipeline         |
| `outbound_delivered`                  | Outbound deliveries that succeeded                |
| `outbound_failed`                     | Outbound deliveries that failed                   |
| `loop_prevented`                      | Events blocked by the self-loop guard             |
| `capacity_rejections`                 | Operations rejected by the capacity controller    |
| `delivery_current` / `delivery_limit` | Current / max concurrent delivery semaphore slots |
| `replay_current` / `replay_limit`     | Current / max concurrent replay semaphore slots   |

### Example Configurations

**Conservative (low-resource device):**

```toml
[runtime.limits]
max_inflight_deliveries = 8
max_inflight_replay_events = 4
delivery_acquire_timeout_seconds = 10.0
shutdown_drain_timeout_seconds = 3.0
```

**High-throughput (server):**

```toml
[runtime.limits]
max_inflight_deliveries = 128
max_inflight_replay_events = 64
delivery_acquire_timeout_seconds = 60.0
shutdown_drain_timeout_seconds = 10.0
```

## Shutdown Behavior

MEDRE shuts down in reverse dependency order: adapters → pipeline runner → storage. The runtime state transitions from `RUNNING` to `STOPPING` when `stop()` is called, and from `STOPPING` to either `STOPPED` or `FAILED` when complete. There are no substates for individual shutdown phases.

### Shutdown Sequence

1. **Stop accepting new work** — `CapacityController.stop_accepting()` blocks new delivery and replay acquire calls.
2. **Drain in-flight work** — Poll `CapacityController.snapshot()` until both `delivery_current` and `replay_current` reach 0, or `shutdown_drain_timeout_seconds` expires.
3. **Signal shutdown** — `shutdown_event.set()` notifies adapters and waiters.
4. **Stop adapters** — Reverse start order, each with `shutdown_timeout_seconds` from the `[runtime]` section.
5. **Stop pipeline runner** — Remove middleware, release resources.
6. **Close storage** — Flush and release SQLite resources.

### Drain Phase

When shutdown begins (SIGTERM, SIGINT, or programmatic):

1. `CapacityController.stop_accepting()` blocks new delivery and replay work.
2. In-flight work is drained by polling capacity counters until both reach zero, or `shutdown_drain_timeout_seconds` (from `[runtime.limits]`) expires.
3. `shutdown_event` is set — signals all adapters and waiters.
4. Adapters are stopped in reverse start order. Each adapter's `stop()` is called with `shutdown_timeout_seconds`.

### What Gets Drained vs Cancelled

| Category                              | Behavior                                                                                                                                                                                                           |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| In-flight adapter deliveries          | **Drained** — awaited up to `shutdown_drain_timeout_seconds`, then cancelled                                                                                                                                       |
| Abandoned in-flight deliveries        | **Evidence persisted** — each abandoned delivery gets a `status="suppressed"` receipt with `failure_kind=shutdown_rejection`, `error="shutdown_drain_timeout"`, and `failure_kind_detail="shutdown_drain_timeout"` |
| Adapter receive loops                 | Cancelled immediately on adapter `stop()`                                                                                                                                                                          |
| Replay events                         | Cancelled; completed delivery receipts are preserved                                                                                                                                                               |
| Route statistics, diagnostic counters | **Lost** — in-memory only                                                                                                                                                                                          |

### Drain-Abandoned Evidence

When the drain deadline expires with in-flight deliveries still active, the runtime persists structured abandonment evidence before continuing shutdown. Each abandoned delivery produces a `DeliveryReceipt` with:

| Field                 | Value                                                           |
| --------------------- | --------------------------------------------------------------- |
| `status`              | `suppressed`                                                    |
| `failure_kind`        | `shutdown_rejection` (reuses existing enum)                     |
| `error`               | `shutdown_drain_timeout`                                        |
| `failure_kind_detail` | `shutdown_drain_timeout` (derived from error by `reporting.py`) |
| `attempt_number`      | `1`                                                             |

Each receipt includes the `event_id`, `route_id`, `target_adapter`, `target_channel`, and `delivery_plan_id` of the abandoned delivery. Receipts are persisted to SQLite storage and survive shutdown — they are retrievable via `medre inspect receipts` after the runtime exits.

**Post-shutdown inspection:**

```bash
# Find events abandoned during drain timeout
medre inspect receipts --event <event_id> --storage-path /path/to/medre.sqlite

# SQL to find all drain-abandoned receipts
sqlite3 /path/to/medre.sqlite \
  "SELECT event_id, route_id, target_adapter, created_at
   FROM delivery_receipts
   WHERE status = 'suppressed' AND error = 'shutdown_drain_timeout'
   ORDER BY created_at DESC;"
```

### Shutdown Timeout

The overall shutdown budget is `shutdown_timeout_seconds` from `RuntimeConfig`. Individual subsystem timeouts share this budget:

```text
Total budget: shutdown_timeout_seconds
├── Adapter stops (reverse order, each uses the full timeout)
├── Pipeline runner stop (drain timeout = shutdown_drain_timeout_seconds)
└── Storage close (uses remaining budget)
```

If the overall budget is exceeded, `RuntimeShutdownError` is raised with a summary of which subsystems failed.

### What Shutdown Does NOT Do

- **No per-adapter restart.** Shutdown stops the entire runtime. Individual adapters cannot be restarted independently.
- **No graceful connection drain.** Adapters do not wait for pending transport-level operations (e.g., Matrix sync responses, Meshtastic pending packets) before disconnecting.
- **No replay deduplication on restart.** If the runtime restarts, replayed events may be delivered again.
- **No persistent adapter-local queue.** Adapter-local queues (e.g., Meshtastic outbound deque) are in-memory and lost on shutdown. The delivery outbox persists operational work state across restart. Outbox items with expired `in_progress` leases are re-claimable by the RetryWorker.
- **No distributed coordination.** Shutdown is local to the process.

See Contract 54 (Runtime Shutdown), Contract 59 (Runtime Durability), and Contract 60 (Runtime Cancellation) for full specifications.

## Docker Deployment

### Using docker.env.example

A Docker environment template is provided at `examples/env/docker.env.example`. Copy it to `.env` and replace placeholder values:

```bash
cp examples/env/docker.env.example .env
# Edit .env with your homeserver, user ID, access token, etc.
```

Key variables:

| Variable                                | Purpose                                             |
| --------------------------------------- | --------------------------------------------------- |
| `MEDRE_HOME`                            | Root data directory inside container (`/opt/medre`) |
| `MEDRE_LOG_LEVEL`                       | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`  |
| `MEDRE_ADAPTER__MAIN__ENABLED`          | Enable Matrix adapter                               |
| `MEDRE_ADAPTER__MAIN__HOMESERVER`       | Matrix homeserver URL                               |
| `MEDRE_ADAPTER__MAIN__USER_ID`          | Matrix user ID                                      |
| `MEDRE_ADAPTER__MAIN__ACCESS_TOKEN`     | Matrix access token                                 |
| `MEDRE_ADAPTER__MAIN__ROOM_ALLOWLIST`   | Comma-separated room IDs                            |
| `MEDRE_ADAPTER__RADIO__ENABLED`         | Enable Meshtastic adapter                           |
| `MEDRE_ADAPTER__RADIO__CONNECTION_TYPE` | Connection mode: `serial`, `tcp`, `ble`, `fake`     |
| `MEDRE_ADAPTER__RADIO__SERIAL_PORT`     | Serial device path                                  |

Mount a volume at `MEDRE_HOME` for persistent state:

```bash
docker run -d \
  --name medre \
  --env-file .env \
  --device /dev/ttyACM0 \
  -v medre-data:/opt/medre \
  medre:latest
```

### MEDRE_HOME in Docker

Inside the container, `MEDRE_HOME=/opt/medre`. All paths resolve under this root:

```text
/opt/medre/
├── config.toml
├── state/
│   ├── medre.sqlite
│   └── adapters/
│       ├── bot/
│       │   └── matrix/
│       │       └── store/
│       └── radio/
│           └── meshtastic/
├── data/
├── cache/
└── logs/
    └── medre.log
```

## MEDRE_HOME Layout

When `MEDRE_HOME` is set (or using XDG defaults), the runtime creates this layout:

| Path                                          | Description                                                                         |
| --------------------------------------------- | ----------------------------------------------------------------------------------- |
| `{config}/config.toml`                        | Primary configuration file                                                          |
| `{state}/medre.sqlite`                        | Single global database                                                              |
| `{log_dir}/medre.log`                         | Global log file (`{state}/logs` in XDG mode, `$MEDRE_HOME/logs` in MEDRE_HOME mode) |
| `{state}/adapters/{adapter_id}/`              | Per-adapter state root                                                              |
| `{state}/adapters/{adapter_id}/matrix/store/` | Matrix E2EE crypto store (non-plaintext only)                                       |
| `{state}/adapters/{adapter_id}/meshtastic/`   | Meshtastic transport state                                                          |
| `{state}/adapters/{adapter_id}/meshcore/`     | MeshCore transport state                                                            |
| `{state}/adapters/{adapter_id}/lxmf/`         | LXMF transport state                                                                |
| `{data}/`                                     | Data directory                                                                      |
| `{cache}/`                                    | Cache directory                                                                     |

See Contract 46 for the authoritative path model.

To inspect resolved paths:

```bash
medre paths
```

## Container Deployment Hardening

This section covers path, storage, device, and isolation guarantees relevant to container deployments. It is platform-agnostic — it applies to any container runtime (Docker, Podman, containerd, etc.) that sets `MEDRE_HOME`.

### Path Resolution Modes

MEDRE supports two path resolution modes controlled by environment variables:

**MEDRE_HOME mode** (recommended for containers): Set `MEDRE_HOME` to a single directory. All paths resolve under it. This produces a deterministic layout regardless of user or distribution.

**XDG mode** (default): Each path category resolves independently against `XDG_*_HOME` variables with spec-defined fallbacks. Intended for interactive desktop use, not containers.

Path resolution is pure computation — no filesystem I/O occurs during config loading. `MedrePaths` is an immutable, frozen dataclass returned by `resolve()`. See Contract 46 (Runtime Storage and Path Model) for the authoritative specification.

### MEDRE_HOME Bind Mount Strategy

When running in a container, set `MEDRE_HOME=/opt/medre` (or any absolute path) and mount a single volume at that path. The runtime creates all subdirectories on startup via `_ensure_dirs()`.

```bash
docker run -d \
  --env MEDRE_HOME=/opt/medre \
  -v medre-data:/opt/medre \
  medre:latest
```

The volume must be writable by the container's runtime user. The runtime does not manage file ownership or permissions — ensure the container user has read/write access to the mounted volume.

### Directory Creation at Startup

`MedreApp._ensure_dirs()` creates the following directories during startup (before any adapter starts):

1. `state_dir` — mutable application state
2. `data_dir` — persistent application data
3. `cache_dir` — disposable cached data
4. `log_dir` — log files
5. Database parent directory (parent of the SQLite database path)
6. Per-adapter state roots: `{state_dir}/adapters/{adapter_id}/` for every enabled adapter
7. Matrix store directories: `{state_dir}/adapters/{adapter_id}/matrix/store/` for enabled Matrix adapters with non-plaintext `encryption_mode`

All directories are created with `mkdir(parents=True, exist_ok=True)`. Creation is idempotent — restarting with an existing volume is safe.

### SQLite Persistence

MEDRE uses a single SQLite database at `{state_dir}/medre.sqlite`. This is the authoritative persistent state:

- Uses WAL (Write-Ahead Logging) journal mode for crash consistency.
- Holds canonical events, delivery receipts, replay state, cross-adapter relationships, and route attribution.
- There are **no per-adapter databases**. Transport-local state (crypto stores, identity files) lives on the filesystem under each adapter's state root, not in SQLite.
- The database file must be on a writable, non-memory filesystem for persistence across container restarts. Mount the volume at `MEDRE_HOME` (or at least at `{state_dir}`) to preserve it.

### Matrix Store Persistence

When a Matrix adapter uses non-plaintext `encryption_mode`, the runtime derives a crypto store path:

```json
{state_dir}/adapters/{adapter_id}/matrix/store/
```

This path is derived by `RuntimeBuilder` from `MedrePaths.adapter_transport_state_dir(adapter_id, "matrix") / "store"` when `store_path` is not explicitly configured. The builder does not override an explicit `store_path`.

The store contains Olm/Megolm session keys and device keys managed by the nio library. It must persist across container restarts to avoid E2EE session loss and mandatory re-verification. Mount the volume at `MEDRE_HOME` to preserve it.

### Serial Device Passthrough

For Meshtastic adapters using serial connections, pass the host device into the container:

```bash
docker run -d \
  --device /dev/ttyACM0 \
  -v medre-data:/opt/medre \
  medre:latest
```

The container's runtime user must have read/write access to the device. On most Linux distributions, the device is owned by the `dialout` group. Options:

- Run the container process as a user in the `dialout` group.
- Use `--group-add dialout` if the container runtime supports it.
- Set `udev` rules on the host to adjust permissions.

The serial port path is configured via `MEDRE_ADAPTER__RADIO__SERIAL_PORT` (default: `/dev/ttyACM0`). The path inside the container must match the `--device` mapping.

### Deterministic Path Resolution

Paths are fully deterministic given the same environment variables:

- In MEDRE_HOME mode, all paths are computed relative to `MEDRE_HOME` with no external state lookups.
- In XDG mode, paths are computed relative to `XDG_*_HOME` variables (or their defaults).
- `resolve()` reads environment variables once and returns an immutable `MedrePaths`. There is no subsequent filesystem access during resolution.
- Two containers with the same `MEDRE_HOME` value produce identical path layouts.

### No Cross-Adapter State Collision

Each adapter receives an isolated state root at `{state_dir}/adapters/{adapter_id}/`. The `adapter_id` acts as a namespace:

- `MedrePaths.adapter_state_dir(adapter_id)` validates that `adapter_id` is non-empty and contains no path separators, preventing path traversal.
- Two adapters with different IDs never share a state directory, even if they use the same transport.
- Two adapters of different transports with the same ID would collide at the adapter root — the config system enforces unique `adapter_id` values across all adapters.
- Transport-specific subdirectories (e.g., `matrix/`, `meshtastic/`) are nested inside the adapter root, so a multi-transport adapter (if it existed) would also be isolated at the transport level.

### Container Checklist

| Concern                   | Mechanism                                                  |
| ------------------------- | ---------------------------------------------------------- |
| Persistent state          | Mount volume at `MEDRE_HOME`                               |
| SQLite durability         | WAL mode, file on mounted volume                           |
| Matrix crypto persistence | Auto-derived store path under adapter state root           |
| Log persistence           | `{log_dir}/medre.log` on mounted volume                    |
| Serial device access      | `--device` passthrough, correct permissions                |
| Deterministic paths       | `MEDRE_HOME` set to fixed absolute path                    |
| Adapter isolation         | Unique `adapter_id` per adapter, path separator validation |
| Idempotent startup        | `_ensure_dirs()` uses `exist_ok=True`                      |
| Config injection          | Environment variables or mounted `config.toml`             |

## Expected Startup Output

A successful startup with the mixed runtime (Example 4) produces output similar to:

```console
INFO  medre.cli: Loading config from /opt/medre/config.toml
INFO  medre.runtime: Starting 2 adapters
INFO  medre.adapters.matrix.bridge: adapter_starting transport=matrix adapter_id=bridge
INFO  medre.adapters.matrix.bridge: adapter_started transport=matrix adapter_id=bridge duration_ms=312
INFO  medre.adapters.meshtastic.radio: adapter_starting transport=meshtastic adapter_id=radio
INFO  medre.adapters.meshtastic.radio: adapter_started transport=meshtastic adapter_id=radio duration_ms=145
INFO  medre.runtime: Assembly complete: 2/2 adapters started in 457ms
INFO  medre.runtime: Resource limits: max_inflight_deliveries=100 max_inflight_replay=100 drain_timeout=10s delivery_acquire_timeout=1.0s
```

Adapters start in deterministic order: sorted by `(transport, adapter_id)`. Resource limits are logged at startup with their resolved values (explicit or default). Capacity bounds are enforced by the `CapacityController` — delivery concurrency is bounded by `max_inflight_deliveries`, replay concurrency by `max_inflight_replay_events`, and adapter-level queues (e.g., Meshtastic outbound queue) apply their own `maxlen` bounds.

## Expected Shutdown Output

On SIGTERM or SIGINT, the runtime shuts down in reverse start order:

```console
INFO  medre.runtime: Shutting down 2 adapters (timeout=10s drain=5.0s)
INFO  medre.adapters.meshtastic.radio: adapter_stopping transport=meshtastic adapter_id=radio
INFO  medre.adapters.meshtastic.radio: adapter_stopped transport=meshtastic adapter_id=radio duration_ms=42
INFO  medre.adapters.matrix.bridge: adapter_stopping transport=matrix adapter_id=bridge
INFO  medre.adapters.matrix.bridge: adapter_stopped transport=matrix adapter_id=bridge duration_ms=28
INFO  medre.runtime: Pipeline draining in-flight deliveries (timeout=5.0s)
INFO  medre.runtime: Pipeline drain complete: 0 deliveries in-flight
INFO  medre.runtime: Pipeline runner stopped
INFO  medre.runtime: Storage closed
INFO  medre.runtime: Shutdown complete in 70ms
```

## Long-Running Run: Operator Observability

This section covers what operators see when running `medre run` in a terminal for an extended period — startup evidence, shutdown evidence, signal handling, and post-run inspection. For short-lived commands (`medre smoke`, `medre diagnostics`, `medre evidence`, `medre trace`, `medre recover`), see the respective sections above. `inspect` is the primary read-only investigation command for post-run evidence.

### Long-Running Run: Startup Evidence

When `medre run` starts, the console prints a structured summary of the runtime state. Operators should check these elements to confirm the runtime is healthy before walking away:

**What you see on a successful startup:**

```yaml
Runtime starting with 2 adapter(s): bridge, radio
  Routes: 1 enabled, 0 disabled (1 total)
  Storage: sqlite
  Limits: max_inflight_deliveries=100, max_inflight_replay_events=100, drain_timeout=10s
INFO  medre.cli: MEDRE starting — config source: cli_arg
INFO  medre.cli: Config path: /opt/medre/config.toml
INFO  medre.cli: State dir:   /opt/medre/state
INFO  medre.runtime: Starting 2 adapters
INFO  medre.adapters.matrix.bridge: adapter_starting transport=matrix adapter_id=bridge
INFO  medre.adapters.matrix.bridge: adapter_started transport=matrix adapter_id=bridge duration_ms=312
INFO  medre.adapters.meshtastic.radio: adapter_starting transport=meshtastic adapter_id=radio
INFO  medre.adapters.meshtastic.radio: adapter_started transport=meshtastic adapter_id=radio duration_ms=145
INFO  medre.runtime: Assembly complete: 2/2 adapters started in 457ms
Runtime started — 2 adapter(s) in 457ms
```

**Startup checklist for operators:**

| Element          | Where to look                      | Healthy sign                  | Problem sign                         |
| ---------------- | ---------------------------------- | ----------------------------- | ------------------------------------ |
| Adapter count    | Console first line                 | `N adapter(s)` matches config | Fewer than expected (build failures) |
| Build failures   | Console `Build failures (N)` block | No block printed              | Block present with `✗` entries       |
| Routes           | Console `Routes:` line             | Expected count enabled        | Zero enabled or validation errors    |
| Storage backend  | Console `Storage:` line            | `sqlite` for production       | `memory` (no persistence)            |
| Limits           | Console `Limits:` line             | As configured                 | Unexpected defaults                  |
| Per-adapter logs | `adapter_started` lines            | All adapters logged `started` | Any `adapter_failed` entries         |
| Assembly summary | `Assembly complete` line           | `N/N adapters started`        | `N/M adapters started, K failed`     |

**Degraded startup indicators:**

```yaml
Runtime starting with 3 adapter(s): bot1, bot2, radio
  Build failures (1):
    ✗ matrix.bot2: authentication failed
  Routes: 1 enabled, 0 disabled (1 total)
  Storage: sqlite
  Limits: max_inflight_deliveries=100, max_inflight_replay_events=100, drain_timeout=10s
INFO  medre.runtime: Starting 2 adapters
INFO  medre.adapters.matrix.bot1: adapter_started transport=matrix adapter_id=bot1 duration_ms=210
INFO  medre.adapters.meshtastic.radio: adapter_started transport=meshtastic adapter_id=radio duration_ms=98
WARNING medre.runtime: Assembly complete: 2/3 adapters started, 1 failed in 308ms
  ⚠ Runtime is DEGRADED: 2/3 adapter(s) started
    Failed adapters: bot2
Runtime started — 2 adapter(s) in 308ms
```

The runtime does **not** exit on degraded startup. It continues operating with the adapters that started successfully. Routes referencing failed adapters are skipped or degraded. See [Diagnosing Degraded Startup](#diagnosing-degraded-startup) for diagnostic surfaces.

### Long-Running Run: Shutdown Evidence

On shutdown (triggered by Ctrl-C, SIGTERM, or programmatic stop), the runtime prints a summary of the shutdown sequence. Operators should verify these elements to confirm clean termination:

**What you see on a clean shutdown:**

```console
Runtime shutting down
INFO  medre.runtime: Shutting down 2 adapters (timeout=10s drain=5.0s)
INFO  medre.adapters.meshtastic.radio: adapter_stopping transport=meshtastic adapter_id=radio
INFO  medre.adapters.meshtastic.radio: adapter_stopped transport=meshtastic adapter_id=radio duration_ms=42
INFO  medre.adapters.matrix.bridge: adapter_stopping transport=matrix adapter_id=bridge
INFO  medre.adapters.matrix.bridge: adapter_stopped transport=matrix adapter_id=bridge duration_ms=28
  stopped radio
  stopped bridge
  Drain completed (timeout=10s)
Shutdown complete — 2 adapter(s) stopped in 70ms, 0 error(s)
INFO  medre.runtime: Shutdown complete in 70ms
```

**Shutdown checklist for operators:**

| Element              | Where to look                       | Clean sign               | Problem sign                     |
| -------------------- | ----------------------------------- | ------------------------ | -------------------------------- |
| Adapter stop order   | Log lines                           | Reverse of startup order | Missing adapter stop lines       |
| Drain outcome        | Console `Drain completed/timed out` | `completed`              | `timed out` with abandoned count |
| Error count          | Console `Shutdown complete` line    | `0 error(s)`             | Non-zero error count             |
| Per-adapter duration | `adapter_stopped` log lines         | Milliseconds             | Timeout or missing               |

**Shutdown accounting counters:**

The shutdown summary reports the number of adapters stopped and any errors encountered. Runtime accounting counters (`inbound_accepted`, `outbound_delivered`, `outbound_failed`, `loop_prevented`, `capacity_rejections`) **are printed at shutdown** as a compact one-line summary alongside the adapter stop and error counts. To capture the full counter breakdown (including per-route stats and capacity gauges) to a file, use `--snapshot-on-shutdown` (see below).

### Shutdown Snapshot (`--snapshot-on-shutdown`)

The `--snapshot-on-shutdown` flag writes a runtime snapshot JSON file to disk after the graceful shutdown sequence completes. This captures the final state of the runtime, including accounting counters, capacity gauges, and adapter lifecycle state.

**Usage:**

```bash
medre run --config config.toml --snapshot-on-shutdown
```

**Output artifact:**

The snapshot is written to `{state_dir}/shutdown-snapshot.json` (resolved according to the active path mode — see [MEDRE_HOME Layout](#medre_home-layout)). The file is a standard runtime snapshot with the same schema as `medre diagnostics` output, plus live lifecycle state captured at shutdown time.

**What the shutdown snapshot contains:**

| Section                      | Content                                                                    |
| ---------------------------- | -------------------------------------------------------------------------- |
| `lifecycle.runtime_state`    | `"stopped"` (captured after graceful shutdown completes)                   |
| `lifecycle.adapters.{id}`    | Per-adapter lifecycle state at shutdown time                               |
| `accounting`                 | Final `RuntimeAccounting` counters (inbound/outbound counts)               |
| `capacity`                   | Final `CapacityController` gauges (delivery_current, timeouts, rejections) |
| `startup.boot_summary`       | Frozen startup classification (unchanged from startup)                     |
| `diagnostics.runtime_events` | Bounded event buffer accumulated during the run                            |
| `routes.stats`               | Per-route delivery statistics accumulated during the run                   |

**Important caveats:**

- The snapshot is captured **after** adapters are stopped. `lifecycle.runtime_state` will be `"stopped"`. Adapter health reflects the stopped state.
- Counters and stats in the snapshot are **process-local and non-durable**. They represent the in-memory state at the moment of capture. The same counters reset to zero on the next startup.
- Runtime events (`diagnostics.runtime_events`) are **process-local**. They are not persisted to SQLite and do not survive the process exiting. The shutdown snapshot is the only way to capture them.
- The RetryWorker is an opt-in background task for transient delivery failures, but there is **no final ACK guarantee** for any transport. The snapshot records what the runtime observed, not what the remote side confirmed.

### Signal Handling (Ctrl-C / SIGTERM)

`medre run` installs signal handlers for `SIGINT` and `SIGTERM` that initiate a graceful shutdown sequence.

**First interrupt (Ctrl-C or SIGTERM):**

1. The signal handler sets an internal flag requesting shutdown.
2. The main event loop detects the flag on its next poll cycle (1-second interval).
3. The runtime transitions to the shutdown sequence:
   - Stop accepting new delivery and replay work.
   - Drain in-flight work up to `shutdown_drain_timeout_seconds`.
   - Stop adapters in reverse start order.
   - Close storage.
4. The process exits with code 0 if shutdown completes cleanly.

**What is preserved on clean shutdown:**

| Data                          | Preserved? | Why                                 |
| ----------------------------- | ---------- | ----------------------------------- |
| Events in SQLite              | Yes        | Written before delivery begins      |
| Delivery receipts             | Yes        | Written after each delivery attempt |
| Route attribution on receipts | Yes        | Persisted with the receipt          |
| E2EE crypto stores            | Yes        | On disk, managed by SDK             |
| Log history                   | Yes        | Append-only file                    |

**What is lost on shutdown:**

| Data                                     | Lost?     | Why                                                                                        |
| ---------------------------------------- | --------- | ------------------------------------------------------------------------------------------ |
| In-flight deliveries (not yet completed) | Partially | Evidence receipt persisted with `shutdown_drain_timeout` detail; actual delivery abandoned |
| Runtime accounting counters              | Yes       | Process-local; not persisted                                                               |
| RouteStats per-route counters            | Yes       | Process-local; not persisted                                                               |
| CapacityController gauges                | Yes       | Process-local; reset on startup                                                            |
| Active replay runs                       | Yes       | Must re-initiate manually                                                                  |
| Runtime events buffer                    | Yes       | Process-local; use `--snapshot-on-shutdown` to capture                                     |

**Second interrupt (repeated Ctrl-C):**

If a second `SIGINT` arrives while graceful shutdown is already in progress, the runtime escalates to a hard kill — the process exits immediately without completing the drain phase or adapter stop sequence. This is a safety valve to prevent an unresponsive shutdown from hanging indefinitely.

**Signal handler reset:**

Signal handlers are reset/reinstalled at the start of each `medre run` invocation. The `shutdown_requested` flag is cleared to `False` before the runtime begins, ensuring that a previous invocation's signal state does not leak into the next run. This is important in scenarios where the runtime is restarted programmatically within the same process.

**Hard kill (SIGKILL / `kill -9`):**

No graceful shutdown occurs. No shutdown logs are emitted. No shutdown snapshot is written. SQLite data on disk is preserved (WAL mode). In-flight deliveries are lost without receipts, but outbox items with expired leases are re-claimable on restart. See [Crash Recovery](#crash-recovery) for the full recovery procedure.

**No active restart.** After shutdown (graceful or hard), the runtime does not restart automatically. Operators must re-run `medre run` manually or use an external process supervisor (systemd, Docker restart policy, etc.). MEDRE does not provide its own supervision.

### Post-Run Evidence Inspection

After a `medre run` session ends (clean shutdown or crash), operators can inspect persisted evidence using CLI commands. These commands read from the SQLite database. `inspect` is the primary read-only investigation command; start with it and escalate to specialized commands when needed.

**Inspect a specific event (inspect-first path):**

```bash
# View a stored event (primary investigation command)
medre inspect event <event_id> --storage-path /path/to/medre.sqlite

# View with chronological timeline (covers trace event output)
medre inspect event <event_id> --storage-path /path/to/medre.sqlite --timeline

# View with evidence bundle (covers evidence --event output)
medre inspect event <event_id> --storage-path /path/to/medre.sqlite --evidence

# View with recovery runbook (covers recover --event output)
medre inspect event <event_id> --storage-path /path/to/medre.sqlite --recovery
```

**View delivery receipts:**

```bash
# View delivery receipts for an event
medre inspect receipts --event <event_id> --storage-path /path/to/medre.sqlite

# View receipts from a specific replay run
medre inspect receipts --replay-run <run_id> --storage-path /path/to/medre.sqlite
```

**Resolve a native message reference:**

```bash
medre inspect native-ref --adapter <name> --message <native_id> --storage-path /path/to/medre.sqlite
```

**Specialized commands for deeper investigation:**

```bash
# Standalone timeline (equivalent to inspect event --timeline)
medre trace event <event_id> --storage-path /path/to/medre.sqlite

# Full bridge evidence bundle with optional live health refresh
medre evidence --config config.toml --json
```

**Find events without receipts (orphaned by a crash):**

```bash
medre inspect receipts --event <event_id> --config config.toml
```

If the event exists but has no receipts, it was stored but delivery was never completed (crash during delivery). Check `delivery_outbox` for surviving operational state before concluding the event is unrecoverable. Use SQL for bulk detection:

```sql
SELECT e.event_id, e.source_adapter, e.created_at
FROM canonical_events e
LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
WHERE r.event_id IS NULL
ORDER BY e.created_at DESC;
```

**Caveats for post-run evidence:**

- `medre inspect` and `medre trace` require `[storage] backend = "sqlite"` in the config. They exit with code 2 if the config uses `backend = "memory"` or the database file does not exist.
- Receipts record what the adapter reported, not what the remote side confirmed. Radio transport receipts show `sent` (local node acceptance), not delivered. See [Per-Transport Delivery Semantics](bridge-operation.md#2-per-transport-delivery-semantics).
- Replay is manual and duplicate-risky. `BEST_EFFORT` replay produces real outbound messages without deduplication. Use `DRY_RUN` first to preview.
- Runtime events and counters are process-local. They are not in SQLite and cannot be inspected post-run unless captured via `--snapshot-on-shutdown`.

## Restart Expectations

### State Persists Across Restarts

- **Global database** (`{state}/medre.sqlite`) survives restarts. All events, delivery receipts, and replay state are retained.
- **Crypto stores** (Matrix Olm/Megolm keys at `{state}/adapters/{adapter_id}/matrix/store/`) survive restarts. E2EE sessions resume without re-verification.
- **Transport identity files** (LXMF identities, etc.) survive restarts.
- **Logs** are appended to `{log_dir}/medre.log`, not rotated by MEDRE itself.

### No Manual Cleanup Required

After a clean shutdown, restarting with the same config resumes normal operation. No state reset, cache clear, or manual intervention is needed.

## Persistence and Crash Semantics

This section summarizes what MEDRE state survives restarts and what is lost. For the full contracts, see Contract 55 (Runtime Persistence) and Contract 59 (Runtime Durability).

### What Is Persisted (Survives Crash and Restart)

| State                         | Location                              | Notes                                         |
| ----------------------------- | ------------------------------------- | --------------------------------------------- |
| Canonical events              | SQLite (`{state}/medre.sqlite`)       | Written before delivery begins                |
| Delivery receipts             | SQLite                                | Written after each delivery attempt completes |
| Route attribution on receipts | SQLite (`receipt.route_id`)           | Persists with the receipt                     |
| Matrix E2EE crypto keys       | `{state}/adapters/{id}/matrix/store/` | SDK-managed                                   |
| LXMF identities               | `{state}/adapters/{id}/lxmf/`         | Transport-managed                             |
| Log history                   | `{state}/logs/medre.log`              | Append-only                                   |
| Configuration                 | Operator-managed file                 | Unchanged                                     |

### What Is NOT Persisted (Lost on Process Termination)

| State                                       | Nature                                                                                                                      | Impact                                                                                                                   |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| In-flight deliveries                        | Evidence persisted as `suppressed` receipts with `failure_kind_detail=shutdown_drain_timeout`; delivery itself is abandoned | No retry of deliveries without outbox rows; `in_progress` outbox rows with expired leases are reclaimable by RetryWorker |
| Active replay runs                          | Lost on crash or shutdown                                                                                                   | Must re-initiate manually                                                                                                |
| Runtime counters (`inbound_accepted`, etc.) | Process-local only                                                                                                          | Reset to zero on every startup                                                                                           |
| RouteStats per-route counters               | Process-local only                                                                                                          | No historical route statistics                                                                                           |
| CapacityController gauges                   | Process-local only                                                                                                          | Reset on startup                                                                                                         |
| Adapter health/connection state             | Process-local only                                                                                                          | Adapters reconnect from scratch                                                                                          |

### Crash Recovery

On hard crash (`kill -9`, OOM, power loss):

1. No graceful shutdown. No shutdown logs.
2. SQLite database is preserved (WAL mode). Events and committed receipts survive.
3. In-flight deliveries are lost without receipts, but `in_progress` outbox items survive with expired leases and are re-claimable by the RetryWorker on restart. Deliveries that never created an outbox item are lost.
4. All runtime counters are lost.
5. Restart with the same config. Adapters reconnect autonomously.
6. Adapters may replay or suppress stale messages based on their `startup_backlog_suppress_seconds` setting.

To identify events that were stored but never delivered (orphaned by a crash):

```sql
SELECT e.event_id, e.source_adapter, e.created_at
FROM canonical_events e
LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
WHERE r.event_id IS NULL
ORDER BY e.created_at DESC;
```

### Persistence Is Single-Machine

MEDRE persists state to a local SQLite database and local filesystem. There is no replication, no remote backup, and no distributed coordination. Operators are responsible for:

- Database backup and disaster recovery.
- Log rotation and log aggregation.
- External monitoring of disk space (a full disk stops event persistence).

See Contract 55 (Runtime Persistence) and Contract 59 (Runtime Durability) for the complete specifications.

## Failure Expectations

### Adapter Failure During Startup

If one adapter fails to start, the runtime:

1. Logs the failure with `adapter_id` and error summary.
2. Continues starting remaining adapters.
3. Reports the failure in the assembly summary.
4. Successfully started adapters continue running.

Example output with a partial failure:

```console
INFO  medre.runtime: Starting 3 adapters
INFO  medre.adapters.matrix.bot1: adapter_starting transport=matrix adapter_id=bot1
INFO  medre.adapters.matrix.bot1: adapter_started transport=matrix adapter_id=bot1 duration_ms=210
INFO  medre.adapters.matrix.bot2: adapter_starting transport=matrix adapter_id=bot2
ERROR medre.adapters.matrix.bot2: adapter_failed transport=matrix adapter_id=bot2 error="authentication failed"
INFO  medre.adapters.meshtastic.radio: adapter_starting transport=meshtastic adapter_id=radio
INFO  medre.adapters.meshtastic.radio: adapter_started transport=meshtastic adapter_id=radio duration_ms=98
WARNING medre.runtime: Assembly complete: 2/3 adapters started, 1 failed in 308ms
```

### Adapter Crash at Runtime

If an adapter crashes after successful start:

1. The crash is logged at ERROR level with `(transport, adapter_id)`.
2. The adapter enters a `failed` or `degraded` health state.
3. Other adapters are unaffected.
4. The adapter's reconnect policy (if applicable) attempts recovery autonomously.

### Runtime Crash

If the entire runtime process crashes (OOM, kill -9, power loss):

1. No graceful shutdown occurs. No shutdown logs are emitted.
2. State on disk is preserved: database, crypto stores, identity files.
3. Restart with the same config to resume operation.
4. Adapters may replay or suppress stale messages based on their `startup_backlog_suppress_seconds` setting.

## Runtime Diagnostics

### Inspecting Build-Time Runtime State

```bash
medre diagnostics
```

This command builds the runtime from configuration but **does not start adapters, storage, or any I/O**. It produces a pre-flight JSON snapshot showing what the runtime _would_ look like at build time. All values are build-time snapshots — no adapter startup occurs, no connections are made.

The output is structured into the same sections documented in Contract 63 (Runtime Snapshot Schema). Key adapter-level fields per entry:

- `adapter_id`: Unique adapter identifier.
- `health`: Startup-derived health from `_last_health` (typically `"unknown"` at build time — set during startup, not during build).
- `capabilities`: Adapter capabilities from build-time construction.
- `platform`, `role`, `version`: Static adapter metadata.
- `provenance`: Always `"startup"` — indicates the metadata source is build/startup, not actively refreshed.

**No live connectivity state is included.** Fields like `connected`, `reconnecting`, `reconnect_attempts`, and `last_error` do not appear in the snapshot. The adapter `health` value is startup-derived, not polled from a running adapter. For current adapter lifecycle state after startup, check `lifecycle.adapters.{id}` in the running runtime's snapshot (process-local, not available via `medre diagnostics`).

See Contract 63 for the complete snapshot schema, and Contract 56 for health classification semantics.

### Live Health Refresh

```bash
medre diagnostics --refresh-health
```

This command builds the runtime from configuration, **starts all adapters**, refreshes each adapter's `health_check()` once, prints a snapshot with live health data, and stops the runtime cleanly. It is the operator-facing interface for manual health refresh — there is no background polling, scheduler, or automatic refresh.

**What it does:**

1. Load config, build runtime via `RuntimeBuilder` (same path as `medre run`).
2. Start all enabled adapters (opens real connections to configured transports).
3. Call `app.refresh_live_health()` — refreshes each adapter's `health_check()` once in deterministic order.
4. Build and print the runtime snapshot JSON with `health.live_health` populated. The snapshot is captured **before** `app.stop()`, so `lifecycle.runtime_state` reflects `"running"`.
5. Stop the runtime cleanly in a `finally` block (reverse adapter order, drain, storage close).

**Output shape:**

The snapshot has the same structure as `medre diagnostics` but with key differences:

| Section                   | `medre diagnostics`            | `medre diagnostics --refresh-health`                                                    |
| ------------------------- | ------------------------------ | --------------------------------------------------------------------------------------- |
| `health.live_health`      | `null`                         | `LiveHealthSnapshot` dict with per-adapter live health, `poll_count=1`, real timestamps |
| `health.live_refresh`     | `false`                        | `true`                                                                                  |
| `health.scope`            | `"startup"`                    | `"live"`                                                                                |
| `startup.startup_health`  | Frozen startup classification  | Frozen startup classification (unchanged, separate from live)                           |
| `lifecycle.runtime_state` | `"initialized"`                | `"running"` (snapshot captured before runtime stop)                                     |
| `lifecycle.adapters.{id}` | `{}` (empty — never started)   | Current adapter lifecycle state at snapshot time                                        |
| Timestamps                | Fixed (`2026-01-01T00:00:00Z`) | Real wall-clock and monotonic timestamps                                                |

**`health.live_health` fields (per adapter):**

| Field                      | Meaning                                                                             |
| -------------------------- | ----------------------------------------------------------------------------------- |
| `adapter_id`               | Unique adapter identifier                                                           |
| `health`                   | One of: `healthy`, `degraded`, `failed`, `unknown`, `starting`, `stopping`          |
| `adapter_state`            | Lifecycle state derived from the health poll                                        |
| `fake_or_live`             | `"fake"` for fake adapters, `"live"` for real adapters, `"unknown"` if undetermined |
| `poll_timestamp_wall`      | ISO-8601 UTC when this adapter was polled                                           |
| `poll_timestamp_monotonic` | Monotonic timestamp for ordering                                                    |
| `error`                    | Error string if `health_check()` raised, `null` otherwise                           |

**Key semantics:**

- **Manual only.** There is no background polling, scheduler, or automatic refresh. The operator must invoke this command explicitly.
- **Process-local, non-durable.** Live health data exists only for the duration of the command. It is not persisted. Running the command again starts fresh adapters from scratch.
- **`startup.startup_health` remains frozen.** The live health refresh populates `health.live_health` and changes `health.scope` to `"live"`. The `startup.startup_health` value is a separate, frozen snapshot from startup time and is not affected by the refresh.
- **No automatic restart or remediation.** If an adapter reports `failed` health, the operator must diagnose and fix the issue manually. MEDRE does not restart adapters or routes based on health state.
- **Exits 0 on success** even if runtime health is `degraded` or `failed` — operators read the JSON output. Exits nonzero only for command-level failures (config, build, startup).
- **Starts real adapters.** Real Matrix adapters connect to homeservers, real Meshtastic adapters open serial/TCP ports. This is intentional — the purpose is to verify real connectivity.

**Exit codes for `--refresh-health`:**

| Code | Constant       | Meaning                                                                       |
| ---- | -------------- | ----------------------------------------------------------------------------- |
| 0    | `EXIT_OK`      | Runtime started, health refreshed, snapshot printed. Runtime may be degraded. |
| 2    | `EXIT_CONFIG`  | Config parse/validation error, or no adapters enabled.                        |
| 3    | `EXIT_BUILD`   | Runtime build failure — all adapters failed to construct.                     |
| 4    | `EXIT_STARTUP` | Total startup failure — zero adapters started.                                |

### Config Validation

```bash
medre config check
```

Validates the config file without starting the runtime. Checks for:

- TOML syntax errors
- Duplicate adapter IDs
- Required fields per adapter type
- Conflicting paths

### Smoke vs. Diagnostics vs. Inspect vs. Evidence vs. Trace vs. Recover

These commands serve different purposes. `inspect` is the primary read-only
investigation command. Operators should understand the boundaries:

| Command                              | Storage                                            | Starts adapters                                     | Output                    | Persistence                                                 |
| ------------------------------------ | -------------------------------------------------- | --------------------------------------------------- | ------------------------- | ----------------------------------------------------------- |
| `medre inspect *`                    | Opens existing SQLite (read-only)                  | No                                                  | Queried data              | Reads existing DB                                           |
| `medre smoke`                        | In-memory by default; SQLite with `--storage-path` | Yes (fake only)                                     | passed/failed JSON report | Ephemeral by default; SQLite persists with `--storage-path` |
| `medre evidence`                     | Per config (memory or SQLite)                      | Fake only (or real with `--include-refresh-health`) | Full evidence bundle JSON | Per config                                                  |
| `medre diagnostics`                  | None (build-time)                                  | No                                                  | Build-time snapshot       | N/A (no data written)                                       |
| `medre diagnostics --refresh-health` | None                                               | Yes (real or fake)                                  | Live health snapshot      | Ephemeral — lost on exit                                    |
| `medre run`                          | Per config (SQLite or memory)                      | Yes (real or fake)                                  | Logs only                 | SQLite persists if configured                               |
| `medre trace`                        | Opens existing SQLite (read-only)                  | No                                                  | Chronological timeline    | Reads existing DB                                           |
| `medre recover`                      | Per config                                         | No                                                  | Recovery runbook          | Reads existing DB                                           |

`inspect event --timeline` covers `trace event` output. `inspect event
--evidence` covers `evidence --event` output. `inspect event --recovery`
covers `recover --event` output. Use `inspect` first; reach for the
specialized commands when you need standalone output or features beyond
inspect flags.

For durable post-run investigation of events, receipts, and native refs, use
`medre run` with `[storage] backend = "sqlite"`, then inspect with
`medre inspect` subcommands (primary path):

```bash
# Inspect a specific event (primary investigation command)
medre inspect event <event_id> --storage-path /path/to/medre.sqlite

# With timeline, evidence, or recovery (covers trace/evidence/recover output)
medre inspect event <event_id> --storage-path /path/to/medre.sqlite --timeline
medre inspect event <event_id> --storage-path /path/to/medre.sqlite --evidence
medre inspect event <event_id> --storage-path /path/to/medre.sqlite --recovery

# Inspect delivery receipts for an event
medre inspect receipts --event <event_id> --storage-path /path/to/medre.sqlite

# Inspect receipts from a replay run
medre inspect receipts --replay-run <run_id> --storage-path /path/to/medre.sqlite

# Resolve a native message ref
medre inspect native-ref --adapter bot --message '$event_id' --storage-path /path/to/medre.sqlite
```

`medre inspect` exits with code 2 if the config uses `backend = "memory"`
— there is no persistent data to inspect. It also exits 2 if the database
file does not exist or cannot be opened.

### Evidence Command (Specialized)

The `evidence` command is a specialized support bundle command. For per-event
investigation, `inspect event --evidence` is usually the right choice. Use `evidence`
when you need a full bridge evidence bundle with optional live health refresh.

```bash
# Full evidence bundle: config summary + route validation + diagnostics + storage
medre evidence --config my-bridge.toml --json

# Include live health refresh (starts real adapters)
medre evidence --config my-bridge.toml --include-refresh-health --json

# Target a specific stored event
medre evidence --config my-bridge.toml --event <event_id> --json
```

`medre evidence` collects config summary, route validation, diagnostics
snapshot, optional live health, and storage inspection into a single JSON
report. It is the recommended operator command for pre-runtime validation
and bug report attachments. Exit codes: 0 (passed/partial), 2 = config error.

With `--include-refresh-health`, the command starts all enabled adapters,
polls health once, captures the live snapshot, and stops. This opens real
connections for real adapters — the purpose is to verify real connectivity as
part of the evidence bundle.

See [Bridge Evidence Bundle](bridge-evidence-bundle.md) for the full report
shape, interpretation guidance, and bug report attachment checklist.

See [Fake Bridge Smoke Runbook](fake-bridge-smoke-runbook.md#smoke-persistence-caveat)
for the smoke persistence caveat, [Bridge Failure Drills](bridge-failure-drills.md)
for failure interpretation guidance, and [Bridge Evidence Bundle](bridge-evidence-bundle.md)
for the full evidence collection workflow using `medre evidence`.

### Per-Adapter Diagnostics

Diagnostics are per-adapter. Each adapter's snapshot is isolated from other adapters. See Contract 29 for the complete diagnostics schema.

### Delivery Outbox

The delivery outbox persists pending and retryable delivery work.
Operators can inspect outbox state via:

- **Runtime snapshot**: The `outbox` section shows status counts from storage
  (`pending`, `retry_wait`, `in_progress`, `dead_lettered`, etc.)
- **Storage queries**: Outbox items are in the `delivery_outbox` SQLite table
  (see the storage contract for schema).
- **Live delivery protection**: Items created by the live pipeline are
  `in_progress` with a pipeline lease — they are not claimable by the
  RetryWorker until the live attempt finishes or the lease expires.

**Automatic recovery**: When `[retry] enabled = true`, the RetryWorker
automatically claims and re-attempts due items on each cycle.

**Crash recovery:**

- Deliveries that never created an outbox row are lost on crash (no durable state exists).
- Deliveries with a persisted outbox row survive the crash.
- Expired `in_progress` rows become reclaimable by the RetryWorker after restart.
- Adapter-local queue contents (e.g., Meshtastic in-memory deque) may still be lost.
- `queued` outbox rows after a crash are ambiguous — the adapter may have sent
  the message before crashing or not. These items are NOT auto-retried.

**Dead-lettered items**: Outbox items with status `dead_lettered` require
explicit operator action. Query the `delivery_outbox` table to inspect
them, then decide whether to re-deliver, re-route, or discard.

### Sample Output

The following compact excerpts illustrate the key structural differences between plain diagnostics and `--refresh-health` output. Values are illustrative, not from a real run.

**`medre diagnostics` (build-time, no adapter start):**

```json
{
  "lifecycle": {
    "runtime_state": "initialized",
    "adapters": {}
  },
  "health": {
    "scope": "startup",
    "live_refresh": false,
    "live_health": null
  },
  "startup": {
    "startup_health": "degraded",
    "boot_summary": {
      "startup_outcome": "partial",
      "adapters_started": 1,
      "adapters_failed": 1
    }
  },
  "routes": {
    "startup_readiness": [
      {
        "route_id": "matrix-to-radio",
        "readiness": "degraded",
        "failed_adapter_ids": ["radio"]
      }
    ]
  }
}
```

**`medre diagnostics --refresh-health` (live health refresh, snapshot before stop):**

```json
{
  "lifecycle": {
    "runtime_state": "running",
    "adapters": {
      "bridge": { "state": "started", "health": "healthy" }
    }
  },
  "health": {
    "scope": "live",
    "live_refresh": true,
    "live_health": {
      "poll_count": 1,
      "runtime_health": "healthy",
      "adapters": [
        {
          "adapter_id": "bridge",
          "health": "healthy",
          "poll_timestamp_wall": "2026-05-14T10:30:00Z"
        }
      ]
    }
  },
  "startup": {
    "startup_health": "degraded",
    "boot_summary": { "startup_outcome": "partial" }
  },
  "diagnostics": {
    "runtime_events": [
      { "event": "health_refreshed", "timestamp": "2026-05-14T10:30:00Z" }
    ]
  }
}
```

Note: `startup.startup_health` is the frozen startup classification in both outputs. `health.live_health` is only populated by `--refresh-health`. `lifecycle.runtime_state` is `"running"` in the `--refresh-health` snapshot because the snapshot is captured before the runtime stops.

### How to Interpret Live Health

When reading `health.live_health.runtime_health` from `--refresh-health` output:

| Value      | Meaning                                                                      |
| ---------- | ---------------------------------------------------------------------------- |
| `healthy`  | All started adapters report healthy/operational.                             |
| `degraded` | Some adapters report degraded or failed health. Runtime may still be usable. |
| `failed`   | All adapters failed or health checks could not complete.                     |
| `unknown`  | No live health available, or an adapter returned unknown status.             |

**`startup_health` vs `live_health`:** `startup.startup_health` is frozen at startup time and never changes. `health.live_health` reflects the current state at the moment of the health refresh. They can differ — an adapter that started successfully may fail its live health check, or a degraded startup may recover by the time you run `--refresh-health`.

**Failed health does not trigger automatic remediation.** MEDRE does not restart adapters, routes, or the runtime based on health state.

**Manual next steps when health is not `healthy`:**

1. Check `health.live_health.adapters[].error` and `health.live_health.adapters[].health` per adapter.
2. Review adapter diagnostics and logs for the affected adapter.
3. Verify transport connectivity (network, serial device, credentials).
4. Fix the environment or config, then run `medre diagnostics --refresh-health` again to confirm recovery.

## Log File Location

```json
{log_dir}/medre.log
```

The resolved log directory depends on the active path mode:

- **XDG mode** (default): `~/.local/state/medre/logs/medre.log` (log directory is `{state}/logs`)
- **MEDRE_HOME mode** (container): `$MEDRE_HOME/logs/medre.log` (log directory is a direct child of `MEDRE_HOME`, not under `state/`)

This is the single global log file. All adapter and runtime events are written here. Log format is controlled by `[logging]` configuration:

- `format = "text"` — human-readable for development.
- `format = "json"` — structured for log aggregation (ELK, Loki, etc.).

There are **no per-adapter log files** today. Per-adapter log file support is a future consideration. When introduced, the global log file will remain the authoritative source.

### Log Rotation

MEDRE does not rotate logs internally. Use external log rotation (logrotate, Docker logging drivers, or Kubernetes log management) for production deployments.

## Recovery Procedures

### Runtime Crash Recovery

1. Restart the runtime with the same config: `medre run --config config.toml`
2. State persists on disk — no cleanup needed.
3. Crypto stores survive — E2EE sessions resume.
4. Check logs for the crash cause: `grep ERROR {log_dir}/medre.log`

For a detailed crash recovery workflow including orphan detection and replay
procedures, see [Bridge Recovery](bridge-recovery.md). For tracing events
through the pipeline, see [Event Tracing](event-tracing.md). For the replay
workflow, see [Replay Operation](replay-operation.md).

### Adapter Not Connecting

1. Check adapter health: `medre diagnostics`
2. Look for WARNING/ERROR entries for the adapter in the log file.
3. Verify transport connectivity (network, serial device, BLE pairing).
4. Restart the runtime — the adapter will attempt to reconnect from scratch.

### Config Error

1. Run `medre config check` to identify the issue.
2. Fix the config file.
3. Restart the runtime.

### Corrupted State

1. Stop the runtime.
2. Back up `{state}/medre.sqlite`.
3. If the database is corrupted, delete it and restart. The runtime creates a fresh database.
4. **Note:** This loses all event history. Crypto stores (under `{state}/adapters/`) are separate and unaffected.

## Queue Discipline

The MEDRE pipeline uses bounded capacity to prevent unbounded memory accumulation. This section describes how capacity bounds work, what happens when capacity is exhausted, and what operators should expect.

### Capacity Bounding

The `CapacityController` (see Contract 53, §15) manages two independent semaphores:

| Stream   | Config field                 | Default bound | What it limits                                           |
| -------- | ---------------------------- | ------------- | -------------------------------------------------------- |
| Delivery | `max_inflight_deliveries`    | 100           | Concurrent adapter `deliver()` calls across all adapters |
| Replay   | `max_inflight_replay_events` | 100           | Concurrent replay event deliveries                       |

When a delivery or replay event cannot acquire a slot within `delivery_acquire_timeout_seconds` (default 1.0s), the operation is **rejected** — it returns a failure outcome with diagnostics incremented. No retry is attempted. Capacity timeout is a backpressure signal, not a transient error.

### Adapter-Level Queue Bounds

Some adapters maintain their own bounded internal queues in addition to the global capacity semaphores:

| Adapter    | Queue mechanism                           | Default bound        | Overflow policy                                                          |
| ---------- | ----------------------------------------- | -------------------- | ------------------------------------------------------------------------ |
| Meshtastic | unbounded deque with explicit enqueue cap | 1024 items (default) | Explicit rejection when full, `queue_total_rejected` counter incremented |

Other adapters (Matrix, LXMF, MeshCore) rely on the `CapacityController` semaphore and their transport's own flow control.

### What Rejection Looks Like

When capacity is exhausted:

1. The delivery or replay acquire fails.
2. The outcome records `status="permanent_failure"` with `error="delivery_capacity_exceeded"` (delivery) or `error="delivery_rejected_shutdown"` (delivery during shutdown). For replay, the result records `status="error"` with `error="replay_capacity_exceeded"` or `error="replay_rejected_shutdown"` (replay during shutdown).
3. A diagnostic counter is incremented (`inbound_accepted`, `outbound_delivered`, `outbound_failed`, `loop_prevented`, or `capacity_rejections`).
4. A WARNING log is emitted with the current vs. limit counts.
5. **No retry.** The delivery is abandoned. The operator can re-trigger replay manually if needed.

### Monitoring Queue Pressure

Capacity counters are available in the `capacity` section of the runtime snapshot from a **running** runtime. The `medre diagnostics` command produces a build-time snapshot where these counters are zero/default — it does not start adapters and cannot show live capacity state. To inspect live capacity, consume the snapshot from a running `MedreApp` instance (e.g., via a future admin endpoint).

Snapshot capacity fields (process-local, actively refreshed, bounded, non-durable):

| Counter               | What it tells you                                            |
| --------------------- | ------------------------------------------------------------ |
| `delivery_current`    | How many deliveries are in-flight right now                  |
| `inbound_accepted`    | How many inbound events were accepted into the pipeline      |
| `outbound_delivered`  | How many outbound deliveries succeeded                       |
| `outbound_failed`     | How many outbound deliveries failed                          |
| `loop_prevented`      | How many events were blocked by the self-loop guard          |
| `capacity_rejections` | How many operations were rejected by the capacity controller |
| `replay_current`      | How many replay events are in-flight right now               |

Sustained growth in `capacity_rejections` indicates the runtime is under more delivery pressure than its configured limits can handle. Consider increasing `max_inflight_deliveries` or reducing the number of active routes.

**Important:** Queue bounds prevent unbounded memory accumulation but do **not** prevent data loss under extreme pressure. MEDRE remains best-effort. No exactly-once guarantees. No transactional delivery guarantees.

## Soak Expectations

The soak harness (`tests/test_soak_harness.py`) validates stability patterns within a bounded timeframe suitable for CI. It is **not** a multi-hour or multi-day soak test.

### What the Soak Harness Validates

The `SoakRuntime` test helper builds a fully-wired `MedreApp` with one fake adapter per transport (Matrix, Meshtastic, MeshCore, LXMF) using `adapter_kind="fake"` and in-memory storage. It exercises:

- **Start/stop cycling:** Repeatedly starting and stopping the runtime to verify clean lifecycle transitions with no resource leaks.
- **Replay cycling:** Running replay operations across multiple start/stop cycles to verify replay engine stability.
- **Pressure testing:** Pumping fake inbound events through adapters to validate delivery pipeline behavior under load.
- **Long-running stability:** A configurable iteration count (default 50, max 200, via `SOAK_HARNESS_ITERATIONS` env var) that exercises all the above in a single sustained run.

### What the Soak Harness Does NOT Validate

- **Live transport behavior.** All adapters are fake. No real Matrix homeserver, radio, or Reticulum network is involved.
- **Multi-hour stability.** The harness runs in seconds, not hours. Long-duration soak testing is an operational activity conducted with live transports.
- **Radio transport pressure.** The harness cannot exercise the physical constraints of LoRa (low bandwidth, serial write blocking, packet loss).
- **Exactly-once delivery.** MEDRE does not provide this guarantee. The harness validates pattern correctness, not delivery completeness.

### Running the Soak Harness

```bash
# Default: 50 iterations
pytest tests/test_soak_harness.py

# Deeper local run: 200 iterations
SOAK_HARNESS_ITERATIONS=200 pytest tests/test_soak_harness.py
```

### Soak Harness Design Principles

Every test in the harness:

- Uses **fake adapters** only — no live transports or SDKs required.
- Uses **in-memory storage** — no filesystem I/O beyond temp dirs.
- Runs within **<10 seconds** for default iteration counts.
- Is **deterministic** — no sleeps or wall-clock dependencies beyond what the event loop needs for async scheduling.

## Operational Caveats

### Radio Transport Pressure

Radio transports (Meshtastic, MeshCore) are inherently constrained:

- **Low bandwidth:** LoRa PHY is extremely slow (hundreds of bytes per second on LongFast).
- **No flow control from the mesh:** The radio accepts packets as fast as the serial link allows, but the mesh itself may drop them.
- **Serial write blocking:** Writing to a serial port blocks until the kernel buffer accepts the data.
- **Packet loss is normal.** Do not alert on individual packet failures. Monitor trends, not individual events.

Under sustained radio pressure, the Meshtastic outbound queue drops the oldest items (see Queue Discipline section). This is by design — the runtime prioritizes stability over delivery completeness.

### No Exactly-Once Guarantees

MEDRE does not provide exactly-once delivery semantics for any transport:

- **Radio transports (Meshtastic, MeshCore):** Probabilistic fire-and-forget. Packet loss is expected. Duplicate sends are normal operational practice.
- **Matrix:** At-least-once. Retries after connection loss may produce duplicates.
- **LXMF (Reticulum):** At-least-once with eventual delivery. Propagation delays range from seconds to hours.

The runtime records what adapters report honestly. It never upgrades receipt states based on assumptions.

### No Transactional Delivery Guarantees

When a single inbound event routes to multiple targets (fan-out), each target gets an independent delivery. There is no transactional coordination:

- A failure on one target does not affect the other.
- A success on one target does not guarantee the other.
- Partial delivery is a normal outcome, not an error.

### Capacity Bounds Are Not Delivery Guarantees

Capacity bounds (semaphores, adapter-level queues) prevent unbounded memory accumulation. They do **not** prevent data loss:

- When the capacity semaphore is exhausted, new deliveries are rejected (permanently failed).
- When an adapter-level queue is full, new enqueue attempts are explicitly rejected (the caller receives a transient `MeshtasticSendError`).
- Under extreme pressure, the runtime sheds load to protect process stability.

This is an explicit design tradeoff: runtime stability over delivery completeness. Operators must monitor capacity timeout counters and tune limits accordingly.

### MEDRE Is Best-Effort

The entire MEDRE runtime is best-effort:

- No replay deduplication. Replayed events may be delivered again.
- No persistent adapter-local queue. Adapter-local queue contents (Meshtastic) are lost on shutdown. The delivery outbox persists operational work state; expired `in_progress` outbox items are re-claimable on restart. Deliveries cancelled during shutdown drain produce `suppressed` receipts as evidence.
- No distributed coordination. State is local to the process.
- No per-adapter restart. Only full runtime stop/start is supported.

These are not bugs — they are documented design boundaries. See Contract 53 and Contract 54 for the complete non-guarantee enumeration.

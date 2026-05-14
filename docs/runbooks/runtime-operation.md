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

| Code | Constant | Meaning |
|------|----------|---------|
| 0 | `EXIT_OK` | Successful run and clean shutdown. |
| 2 | `EXIT_CONFIG` | Config file not found, TOML parse error, validation error, or no adapters enabled. |
| 3 | `EXIT_BUILD` | Runtime build failure — missing optional SDK dependency, invalid storage path, or adapter construction error. Adapters that fail during construction are recorded as build failures; if *all* adapters fail to build, the runtime exits with this code. |
| 4 | `EXIT_STARTUP` | Total startup failure — zero adapters started successfully (after build). This covers core subsystem failures (storage init, pipeline runner) and total adapter startup failure. |

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

| Command | Config error | Build error | Startup error |
|---------|:---:|:---:|:---:|
| `medre run` | 2 | 3 | 4 |
| `medre diagnostics` | 2 | 3 | n/a |
| `medre diagnostics --refresh-health` | 2 | 3 | 4 |
| `medre config check` | 2 | n/a | n/a |
| `medre routes validate` | 2 | n/a | n/a |
| `medre routes topology` | 2 | n/a | n/a |
| `medre routes list` | 2 | n/a | n/a |

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

| Counter | Description |
|---------|-------------|
| `delivery_rejections` | Delivery acquire attempts that failed immediately (shutdown or capacity exhausted) |
| `delivery_timeouts` | Delivery acquire attempts that timed out waiting for a slot |
| `delivery_current` / `delivery_limit` | Current / max concurrent delivery semaphore slots |
| `replay_rejections` | Replay acquire attempts that failed immediately |
| `replay_timeouts` | Replay acquire attempts that timed out waiting for a slot |
| `replay_current` / `replay_limit` | Current / max concurrent replay semaphore slots |

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

| Category | Behavior |
|----------|----------|
| In-flight adapter deliveries | **Drained** — awaited up to `shutdown_drain_timeout_seconds`, then cancelled |
| Adapter receive loops | Cancelled immediately on adapter `stop()` |
| Replay events | Cancelled; completed delivery receipts are preserved |
| Route statistics, diagnostic counters | **Lost** — in-memory only |

### Shutdown Timeout

The overall shutdown budget is `shutdown_timeout_seconds` from `RuntimeConfig`. Individual subsystem timeouts share this budget:

```
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
- **No persistent queue.** Delivery state is in-memory only. In-flight deliveries that are cancelled on shutdown are lost.
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

| Variable | Purpose |
|----------|---------|
| `MEDRE_HOME` | Root data directory inside container (`/opt/medre`) |
| `MEDRE_LOG_LEVEL` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `MEDRE_MATRIX_ENABLED` | Enable Matrix adapter |
| `MEDRE_MATRIX_HOMESERVER` | Matrix homeserver URL |
| `MEDRE_MATRIX_USER_ID` | Matrix user ID |
| `MEDRE_MATRIX_ACCESS_TOKEN` | Matrix access token |
| `MEDRE_MATRIX_ROOM_ALLOWLIST` | Comma-separated room IDs |
| `MEDRE_MESHTASTIC_ENABLED` | Enable Meshtastic adapter |
| `MEDRE_MESHTASTIC_CONNECTION_TYPE` | Connection mode: `serial`, `tcp`, `ble`, `fake` |
| `MEDRE_MESHTASTIC_SERIAL_PORT` | Serial device path |

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

```
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

| Path | Description |
|------|-------------|
| `{config}/config.toml` | Primary configuration file |
| `{state}/medre.sqlite` | Single global database |
| `{log_dir}/medre.log` | Global log file (`{state}/logs` in XDG mode, `$MEDRE_HOME/logs` in MEDRE_HOME mode) |
| `{state}/adapters/{adapter_id}/` | Per-adapter state root |
| `{state}/adapters/{adapter_id}/matrix/store/` | Matrix E2EE crypto store (non-plaintext only) |
| `{state}/adapters/{adapter_id}/meshtastic/` | Meshtastic transport state |
| `{state}/adapters/{adapter_id}/meshcore/` | MeshCore transport state |
| `{state}/adapters/{adapter_id}/lxmf/` | LXMF transport state |
| `{data}/` | Data directory |
| `{cache}/` | Cache directory |

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

```
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

```
{state_dir}/adapters/{adapter_id}/matrix/store/
```

This path is derived by `RuntimeBuilder` from `MedrePaths.adapter_transport_state_dir(adapter_id, "matrix") / "store"` when `store_path` is not explicitly configured. The builder does not override an explicit `store_path`.

The store contains Olm/Megolm session keys and device keys managed by the nio library. It must persist across container restarts to avoid E2EE session loss and mandatory re-verification. Mount the volume at `MEDRE_HOME` to preserve it.

### Serial Device Passthrough

For Meshtastic adapters using serial connections, pass the host device into the container:

```
docker run -d \
  --device /dev/ttyACM0 \
  -v medre-data:/opt/medre \
  medre:latest
```

The container's runtime user must have read/write access to the device. On most Linux distributions, the device is owned by the `dialout` group. Options:

- Run the container process as a user in the `dialout` group.
- Use `--group-add dialout` if the container runtime supports it.
- Set `udev` rules on the host to adjust permissions.

The serial port path is configured via `MEDRE_MESHTASTIC_SERIAL_PORT` (default: `/dev/ttyACM0`). The path inside the container must match the `--device` mapping.

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

| Concern | Mechanism |
|---------|-----------|
| Persistent state | Mount volume at `MEDRE_HOME` |
| SQLite durability | WAL mode, file on mounted volume |
| Matrix crypto persistence | Auto-derived store path under adapter state root |
| Log persistence | `{log_dir}/medre.log` on mounted volume |
| Serial device access | `--device` passthrough, correct permissions |
| Deterministic paths | `MEDRE_HOME` set to fixed absolute path |
| Adapter isolation | Unique `adapter_id` per adapter, path separator validation |
| Idempotent startup | `_ensure_dirs()` uses `exist_ok=True` |
| Config injection | Environment variables or mounted `config.toml` |


## Expected Startup Output

A successful startup with the mixed runtime (Example 4) produces output similar to:

```
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

```
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

| State | Location | Notes |
|-------|----------|-------|
| Canonical events | SQLite (`{state}/medre.sqlite`) | Written before delivery begins |
| Delivery receipts | SQLite | Written after each delivery attempt completes |
| Route attribution on receipts | SQLite (`receipt.route_id`) | Persists with the receipt |
| Matrix E2EE crypto keys | `{state}/adapters/{id}/matrix/store/` | SDK-managed |
| LXMF identities | `{state}/adapters/{id}/lxmf/` | Transport-managed |
| Log history | `{state}/logs/medre.log` | Append-only |
| Configuration | Operator-managed file | Unchanged |

### What Is NOT Persisted (Lost on Process Termination)

| State | Nature | Impact |
|-------|--------|--------|
| In-flight deliveries | Lost on crash or cancellation | No receipt, no retry, no recovery |
| Active replay runs | Lost on crash or shutdown | Must re-initiate manually |
| Runtime counters (`delivery_timeouts`, etc.) | Process-local only | Reset to zero on every startup |
| RouteStats per-route counters | Process-local only | No historical route statistics |
| CapacityController gauges | Process-local only | Reset on startup |
| Adapter health/connection state | Process-local only | Adapters reconnect from scratch |

### Crash Recovery

On hard crash (`kill -9`, OOM, power loss):

1. No graceful shutdown. No shutdown logs.
2. SQLite database is preserved (WAL mode). Events and committed receipts survive.
3. In-flight deliveries are lost — the corresponding events exist in SQLite but have no receipt.
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

```
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


## Diagnostics

### Inspecting Build-Time Runtime State

```bash
medre diagnostics
```

This command builds the runtime from configuration but **does not start adapters, storage, or any I/O**. It produces a pre-flight JSON snapshot showing what the runtime *would* look like at build time. All values are build-time snapshots — no adapter startup occurs, no connections are made.

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

This command builds the runtime from configuration, **starts all adapters**, polls each adapter's `health_check()` once, prints a snapshot with live health data, and stops the runtime cleanly. It is the operator-facing interface for manual health refresh — there is no background polling, scheduler, or automatic refresh.

**What it does:**

1. Load config, build runtime via `RuntimeBuilder` (same path as `medre run`).
2. Start all enabled adapters (opens real connections to configured transports).
3. Call `app.refresh_live_health()` — polls each adapter's `health_check()` in deterministic order.
4. Build and print the runtime snapshot JSON with `health.live_health` populated.
5. Stop the runtime cleanly (reverse adapter order, drain, storage close).

**Output shape:**

The snapshot has the same structure as `medre diagnostics` but with key differences:

| Section | `medre diagnostics` | `medre diagnostics --refresh-health` |
|---------|---------------------|--------------------------------------|
| `health.live_health` | `null` | `LiveHealthSnapshot` dict with per-adapter live health, `poll_count=1`, real timestamps |
| `health.live_refresh` | `false` | `true` |
| `health.scope` | `"startup"` | `"live"` |
| `startup.startup_health` | Frozen startup classification | Frozen startup classification (unchanged, separate from live) |
| `lifecycle.runtime_state` | `"initialized"` | `"stopped"` (runtime has started and stopped) |
| `lifecycle.adapters.{id}` | `{}` (empty — never started) | Current adapter lifecycle state at snapshot time |
| Timestamps | Fixed (`2026-01-01T00:00:00Z`) | Real wall-clock and monotonic timestamps |

**`health.live_health` fields (per adapter):**

| Field | Meaning |
|-------|---------|
| `adapter_id` | Unique adapter identifier |
| `health` | One of: `healthy`, `degraded`, `failed`, `unknown`, `starting`, `stopping` |
| `adapter_state` | Lifecycle state derived from the health poll |
| `fake_or_live` | `"fake"` for fake adapters, `"live"` for real adapters, `"unknown"` if undetermined |
| `poll_timestamp_wall` | ISO-8601 UTC when this adapter was polled |
| `poll_timestamp_monotonic` | Monotonic timestamp for ordering |
| `error` | Error string if `health_check()` raised, `null` otherwise |

**Key semantics:**

- **Manual only.** There is no background polling, scheduler, or automatic refresh. The operator must invoke this command explicitly.
- **Process-local, non-durable.** Live health data exists only for the duration of the command. It is not persisted. Running the command again starts fresh adapters from scratch.
- **`startup.startup_health` remains frozen.** The live health refresh populates `health.live_health` and changes `health.scope` to `"live"`. The `startup.startup_health` value is a separate, frozen snapshot from startup time and is not affected by the refresh.
- **No automatic restart or remediation.** If an adapter reports `failed` health, the operator must diagnose and fix the issue manually. MEDRE does not restart adapters or routes based on health state.
- **Exits 0 on success** even if runtime health is `degraded` or `failed` — operators read the JSON output. Exits nonzero only for command-level failures (config, build, startup).
- **Starts real adapters.** Real Matrix adapters connect to homeservers, real Meshtastic adapters open serial/TCP ports. This is intentional — the purpose is to verify real connectivity.

**Exit codes for `--refresh-health`:**

| Code | Constant | Meaning |
|------|----------|---------|
| 0 | `EXIT_OK` | Runtime started, health refreshed, snapshot printed. Runtime may be degraded. |
| 2 | `EXIT_CONFIG` | Config parse/validation error, or no adapters enabled. |
| 3 | `EXIT_BUILD` | Runtime build failure — all adapters failed to construct. |
| 4 | `EXIT_STARTUP` | Total startup failure — zero adapters started. |

### Config Validation

```bash
medre config check
```

Validates the config file without starting the runtime. Checks for:

- TOML syntax errors
- Duplicate adapter IDs
- Required fields per adapter type
- Conflicting paths

### Per-Adapter Diagnostics

Diagnostics are per-adapter. Each adapter's snapshot is isolated from other adapters. See Contract 29 for the complete diagnostics schema.


## Log File Location

```
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

| Stream | Config field | Default bound | What it limits |
|--------|-------------|---------------|----------------|
| Delivery | `max_inflight_deliveries` | 100 | Concurrent adapter `deliver()` calls across all adapters |
| Replay | `max_inflight_replay_events` | 100 | Concurrent replay event deliveries |

When a delivery or replay event cannot acquire a slot within `delivery_acquire_timeout_seconds` (default 1.0s), the operation is **rejected** — it returns a failure outcome with diagnostics incremented. No retry is attempted. Capacity timeout is a backpressure signal, not a transient error.

### Adapter-Level Queue Bounds

Some adapters maintain their own bounded internal queues in addition to the global capacity semaphores:

| Adapter | Queue mechanism | Default bound | Overflow policy |
|---------|----------------|---------------|-----------------|
| Meshtastic | `deque(maxlen=1024)` | 1024 items | Drop-oldest, `total_dropped` counter incremented |

Other adapters (Matrix, LXMF, MeshCore) rely on the `CapacityController` semaphore and their transport's own flow control.

### What Rejection Looks Like

When capacity is exhausted:

1. The delivery or replay acquire fails.
2. The outcome records `status="permanent_failure"` with `error="delivery_capacity_exceeded"` (delivery) or `error="delivery_rejected_shutdown"` (delivery during shutdown). For replay, the result records `status="error"` with `error="replay_capacity_exceeded"` or `error="replay_rejected_shutdown"` (replay during shutdown).
3. A diagnostic counter is incremented (`delivery_timeouts`, `delivery_rejections`, `replay_timeouts`, or `replay_rejections`).
4. A WARNING log is emitted with the current vs. limit counts.
5. **No retry.** The delivery is abandoned. The operator can re-trigger replay manually if needed.

### Monitoring Queue Pressure

Capacity counters are available in the `capacity` section of the runtime snapshot from a **running** runtime. The `medre diagnostics` command produces a build-time snapshot where these counters are zero/default — it does not start adapters and cannot show live capacity state. To inspect live capacity, consume the snapshot from a running `MedreApp` instance (e.g., via a future admin endpoint).

Snapshot capacity fields (process-local, actively refreshed, bounded, non-durable):

| Counter | What it tells you |
|---------|-------------------|
| `delivery_current` | How many deliveries are in-flight right now |
| `delivery_timeouts` | How many deliveries timed out waiting for a slot (sign of sustained pressure) |
| `delivery_rejections` | How many deliveries were rejected due to shutdown |
| `replay_current` | How many replay events are in-flight right now |
| `replay_timeouts` | How many replay events timed out waiting for a slot |
| `replay_rejections` | How many replay events were rejected due to shutdown |

Sustained growth in `delivery_timeouts` indicates the runtime is under more delivery pressure than its configured limits can handle. Consider increasing `max_inflight_deliveries` or reducing the number of active routes.

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
- When an adapter-level queue overflows, items are dropped silently (drop-oldest).
- Under extreme pressure, the runtime sheds load to protect process stability.

This is an explicit design tradeoff: runtime stability over delivery completeness. Operators must monitor capacity timeout counters and tune limits accordingly.

### MEDRE Is Best-Effort

The entire MEDRE runtime is best-effort:

- No replay deduplication. Replayed events may be delivered again.
- No persistent in-flight recovery. Cancelled deliveries are lost on shutdown.
- No distributed coordination. State is local to the process.
- No per-adapter restart. Only full runtime stop/start is supported.

These are not bugs — they are documented design boundaries. See Contract 53 and Contract 54 for the complete non-guarantee enumeration.

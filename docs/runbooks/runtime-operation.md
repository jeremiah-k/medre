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
max_inflight_deliveries = 64        # max concurrent delivery coroutines (default: 64)
max_inflight_replay_events = 32     # max concurrent replay event deliveries (default: 32)
shutdown_drain_timeout_seconds = 5.0  # seconds to drain in-flight deliveries on shutdown (default: 5.0)
delivery_acquire_timeout_seconds = 30.0  # seconds to wait for a delivery slot (default: 30.0)
```

### How Delivery Limiting Works

The pipeline runner uses an `asyncio.Semaphore` to bound the number of concurrent adapter `deliver()` calls. When a delivery is about to start:

1. The runner attempts to acquire a semaphore slot.
2. If a slot is available immediately, the delivery proceeds.
3. If all slots are occupied, the runner waits up to `delivery_acquire_timeout_seconds`.
4. If the wait times out, the delivery fails with `status="permanent_failure"` and `failure_kind=TIMEOUT`. A diagnostic counter is incremented. **No retry** — capacity timeout is a backpressure signal.

This prevents unbounded memory growth from concurrent deliveries.

### How Replay Limiting Works

The replay engine has a separate semaphore (`max_inflight_replay_events`) that bounds how many replay events can be processed concurrently. This prevents replay from consuming the entire delivery budget and starving real-time traffic. Replay deliveries that pass the replay limiter still acquire a slot on the delivery semaphore via the pipeline runner.

### Diagnostics

Run `medre diagnostics` to see resource limit gauges:

| Counter | Description |
|---------|-------------|
| `capacity_timeouts_total` | Deliveries that timed out waiting for a concurrency slot |
| `inflight_deliveries` | Current number of acquired delivery semaphore slots |
| `inflight_replay_events` | Current number of acquired replay semaphore slots |

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

MEDRE shuts down in reverse dependency order: adapters → pipeline runner → storage.

### Drain Phase

When shutdown begins (SIGTERM, SIGINT, or programmatic):

1. `shutdown_event` is set — signals all adapters and waiters.
2. Adapters are stopped in reverse start order. Each adapter's `stop()` is called with `shutdown_timeout_seconds` from the `[runtime]` section.
3. The pipeline runner stops. It awaits any in-flight delivery tasks for up to `shutdown_drain_timeout_seconds` (from `[runtime.limits]`). Deliveries completing within this window produce normal receipts. After the timeout, remaining deliveries are cancelled.
4. Storage is closed (flushes and releases SQLite resources).

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
│   ├── logs/
│   │   └── medre.log
│   └── adapters/
│       ├── bot/
│       │   └── matrix/
│       │       └── store/
│       └── radio/
│           └── meshtastic/
├── data/
└── cache/
```


## MEDRE_HOME Layout

When `MEDRE_HOME` is set (or using XDG defaults), the runtime creates this layout:

| Path | Description |
|------|-------------|
| `{config}/config.toml` | Primary configuration file |
| `{state}/medre.sqlite` | Single global database |
| `{state}/logs/medre.log` | Global log file |
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
INFO  medre.runtime: Resource limits: max_inflight_deliveries=64 max_inflight_replay=32 drain_timeout=5.0s delivery_acquire_timeout=30.0s
```

Adapters start in deterministic order: sorted by `(transport, adapter_id)`. Resource limits are logged at startup with their resolved values (explicit or default).


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
- **Logs** are appended to `{state}/logs/medre.log`, not rotated by MEDRE itself.

### No Manual Cleanup Required

After a clean shutdown, restarting with the same config resumes normal operation. No state reset, cache clear, or manual intervention is needed.


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

### Checking Runtime Health

```bash
medre diagnostics
```

This outputs a snapshot of all running adapters, their health states, and key metrics. Each adapter reports:

- `health`: `healthy`, `degraded`, `failed`, or `stopped`
- `connected`: whether the transport is connected
- `reconnecting`: whether the adapter is in a reconnect loop
- `reconnect_attempts`: current reconnect attempt count
- `last_error`: summary of the last error (sanitized — no secrets)

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
{state}/logs/medre.log
```

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
4. Check logs for the crash cause: `grep ERROR {state}/logs/medre.log`

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

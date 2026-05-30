# Running MEDRE

Starting, stopping, and operating the MEDRE runtime — including lifecycle, exit codes, Docker/Podman deployment, and delivery semantics.

## Starting MEDRE

```bash
medre run --config config.toml
```

The runtime loads the config, validates it, creates required directories, and starts all enabled adapters. Adapters start in deterministic order: sorted by `(transport, adapter_id)`.

To verify config without starting:

```bash
medre config check
```

### Expected Startup Output

```console
Runtime starting with 2 adapter(s): bridge, radio
  Routes: 1 enabled, 0 disabled (1 total)
  Storage: sqlite
  Limits: max_inflight_deliveries=100, max_inflight_replay_events=100, drain_timeout=10s
INFO  medre.runtime: Starting 2 adapters
INFO  medre.adapters.matrix.bridge: adapter_starting transport=matrix adapter_id=bridge
INFO  medre.adapters.matrix.bridge: adapter_started transport=matrix adapter_id=bridge duration_ms=312
INFO  medre.adapters.meshtastic.radio: adapter_starting transport=meshtastic adapter_id=radio
INFO  medre.adapters.meshtastic.radio: adapter_started transport=meshtastic adapter_id=radio duration_ms=145
INFO  medre.runtime: Assembly complete: 2/2 adapters started in 457ms
Runtime started — 2 adapter(s) in 457ms
```

### Startup Checklist

| Element          | Where to look                      | Healthy sign           | Problem sign              |
| ---------------- | ---------------------------------- | ---------------------- | ------------------------- |
| Adapter count    | Console first line                 | Matches config         | Fewer (build failures)    |
| Build failures   | Console `Build failures (N)` block | No block               | `✗` entries present       |
| Routes           | Console `Routes:` line             | Expected count enabled | Zero enabled or errors    |
| Storage backend  | Console `Storage:` line            | `sqlite`               | `memory` (no persistence) |
| Limits           | Console `Limits:` line             | As configured          | Unexpected defaults       |
| Per-adapter logs | `adapter_started` lines            | All started            | Any `adapter_failed`      |
| Assembly summary | `Assembly complete` line           | `N/N adapters started` | `N/M started, K failed`   |

### Degraded Startup

If at least one adapter starts but others fail, the runtime enters `RUNNING` with `DEGRADED` health and continues operating. Routes referencing failed adapters are skipped or degraded.

Diagnostic surfaces for degraded startup:

- **Console output** — logs which adapters started and which failed.
- **`startup.boot_summary`** — carries `startup_outcome: "partial"`, counts of started/failed adapters, lists of failed adapter IDs.
- **`startup.build_failures`** — bounded list of construction failures.
- **`routes.startup_readiness`** — shows routes that are degraded or skipped.
- **`diagnostics.runtime_events`** — event buffer recording adapter failures and route skips.

## Exit Codes

| Code | Constant       | Meaning                                                                                                                                            |
| ---- | -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0    | `EXIT_OK`      | Successful run and clean shutdown.                                                                                                                 |
| 2    | `EXIT_CONFIG`  | Config not found, TOML parse error, validation error, or no adapters enabled.                                                                      |
| 3    | `EXIT_BUILD`   | Runtime build failure — missing SDK, invalid storage path, or adapter construction error. Exits with this code only if all adapters fail to build. |
| 4    | `EXIT_STARTUP` | Total startup failure — zero adapters started successfully.                                                                                        |

Degraded startup does not exit — the runtime continues with `DEGRADED` health.

### Exit Codes by Command

| Command                              | Config error | Build error | Startup error |
| ------------------------------------ | ------------ | ----------- | ------------- |
| `medre run`                          | 2            | 3           | 4             |
| `medre diagnostics`                  | 2            | 3           | n/a           |
| `medre diagnostics --refresh-health` | 2            | 3           | 4             |
| `medre config check`                 | 2            | n/a         | n/a           |
| `medre routes validate`              | 2            | n/a         | n/a           |

All commands print a human-readable error to stderr before exiting nonzero.

## Shutdown Behavior

MEDRE shuts down in reverse dependency order: adapters → pipeline runner → storage.

### Shutdown Sequence

1. **Stop accepting new work** — `CapacityController.stop_accepting()`.
2. **Drain in-flight work** — poll capacity counters until both reach zero, or `shutdown_drain_timeout_seconds` expires.
3. **Signal shutdown** — `shutdown_event.set()`.
4. **Stop adapters** — reverse start order, each with `shutdown_timeout_seconds`.
5. **Stop pipeline runner** — remove middleware, release resources.
6. **Close storage** — flush and release SQLite.

### Expected Shutdown Output

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
```

### Shutdown Checklist

| Element            | Clean sign         | Problem sign                     |
| ------------------ | ------------------ | -------------------------------- |
| Adapter stop order | Reverse of startup | Missing stop lines               |
| Drain outcome      | `completed`        | `timed out` with abandoned count |
| Error count        | `0 error(s)`       | Non-zero                         |

### What Gets Drained vs Cancelled

| Category                              | Behaviour                                                                          |
| ------------------------------------- | ---------------------------------------------------------------------------------- |
| In-flight adapter deliveries          | Drained up to `shutdown_drain_timeout_seconds`, then cancelled                     |
| Abandoned in-flight deliveries        | Evidence persisted as `suppressed` receipts with `failure_kind=shutdown_rejection` |
| Adapter receive loops                 | Cancelled immediately on adapter `stop()`                                          |
| Replay events                         | Cancelled; completed receipts preserved                                            |
| Route statistics, diagnostic counters | Lost — in-memory only                                                              |
| Pending retry receipts                | Not cancelled — remain in storage, processed on next startup                       |
| Pending outbox items                  | Not cancelled — remain in storage, reclaimable on next startup                     |

Pending retry receipts and outbox items survive shutdown in SQLite. On next startup, the RetryWorker discovers and processes due retry receipts (if retry is enabled). This means retries may be re-attempted for deliveries that were pending when the runtime stopped. A distinct `cancelled` failure kind for shutdown-cancelled deliveries is planned but not yet implemented.

### Signal Handling

- **First Ctrl-C / SIGTERM**: Initiates graceful shutdown. Process exits with code 0 if clean.
- **Second Ctrl-C**: Hard kill — exits immediately without drain or adapter stop.
- **SIGKILL**: No graceful shutdown. No logs. SQLite on disk is preserved (WAL mode). Deliveries without outbox rows are lost.

Signal handlers are reset at the start of each `medre run` invocation.

### Shutdown Snapshot (`--snapshot-on-shutdown PATH`)

```bash
medre run --config config.toml --snapshot-on-shutdown snapshot.json
```

Writes a runtime snapshot JSON to the specified PATH after graceful shutdown. Contains:

| Section                      | Content                                         |
| ---------------------------- | ----------------------------------------------- |
| `lifecycle`                  | Runtime and adapter lifecycle state at shutdown |
| `accounting`                 | Final delivery counters                         |
| `capacity`                   | Final capacity gauges                           |
| `startup.boot_summary`       | Frozen startup classification                   |
| `diagnostics.runtime_events` | Bounded event buffer from the run               |
| `routes.stats`               | Per-route delivery statistics                   |

Counters and events are process-local — without this flag, they are lost on exit.

## Per-Transport Delivery Semantics

Each transport has fundamentally different delivery guarantees. Understanding these differences is essential for interpreting receipts.

### Matrix

| Property               | Value                                                                |
| ---------------------- | -------------------------------------------------------------------- |
| Server acknowledgment  | Yes — Synapse returns `event_id` on successful `room_send`           |
| Delivery confirmation  | Server-level only (not per-recipient)                                |
| Duplicate risk         | Low — retries after connection loss may produce duplicates           |
| Receipt interpretation | `sent` with `adapter_message_id` means homeserver accepted the event |

Matrix is the only transport where `sent` implies server-verified persistence.

### Meshtastic

| Property               | Value                                                                                  |
| ---------------------- | -------------------------------------------------------------------------------------- |
| Server acknowledgment  | None beyond local-node acceptance                                                      |
| Delivery confirmation  | None — whether any remote node received the packet is unknown                          |
| Duplicate risk         | High — radio environments cause packet loss; duplicates are normal practice            |
| Receipt interpretation | `sent` means local node accepted for transmission, not that any other node received it |

### MeshCore

Same discipline as Meshtastic — radio best-effort, no confirmation, duplicates are normal.

### LXMF (Reticulum)

| Property               | Value                                                                                                 |
| ---------------------- | ----------------------------------------------------------------------------------------------------- |
| Delivery confirmation  | Eventual — propagation delays from seconds to hours                                                   |
| Duplicate risk         | Low                                                                                                   |
| Receipt interpretation | `sent` means local router accepted for propagation; delivery to destination may take significant time |

## Delivery Receipt States

Receipts progress through these states:

```text
queued → sent
       ↘ failed → dead_lettered
       ↘ suppressed
```

| Status          | Meaning                                                                                        |
| --------------- | ---------------------------------------------------------------------------------------------- |
| `queued`        | Delivery plan created, waiting for adapter execution.                                          |
| `sent`          | Adapter reported successful handoff to transport. See per-transport table for what this means. |
| `failed`        | Adapter reported delivery failure. Classified by `failure_kind`.                               |
| `dead_lettered` | Exhausted all retries. Permanently failed.                                                     |
| `suppressed`    | Terminal — delivery denied by policy (loop prevention, route policy, capacity, shutdown).      |

Each receipt carries `attempt_number` and `parent_receipt_id` forming a retry lineage. The `source` column distinguishes origin: `"live"`, `"retry"`, or `"replay"`.

## Configuration Examples

### Single Matrix Adapter (Plaintext)

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
access_token = "<matrix-access-token>"
room_allowlist = ["!room:example.com"]
encryption_mode = "plaintext"
```

### Single Matrix Adapter (E2EE)

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
access_token = "<matrix-access-token>"
room_allowlist = ["!secretroom:example.com"]
encryption_mode = "e2ee_required"
```

E2EE mode requires the `mindroom-nio[e2e]` optional dependency. The crypto store is created automatically at `{state}/adapters/securebot/matrix/store/`. Device ID is discovered via the Matrix `whoami()` endpoint on first connect — do not configure it manually.

### Two Meshtastic Radios (LongFast + ShortTurbo)

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

### Mixed Runtime (Matrix + Meshtastic)

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
access_token = "<matrix-access-token>"
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

```toml
[runtime.limits]
max_inflight_deliveries = 100
max_inflight_replay_events = 100
shutdown_drain_timeout_seconds = 10
delivery_acquire_timeout_seconds = 1.0
```

The pipeline uses semaphores to bound concurrent delivery and replay. When capacity is exhausted, new deliveries are rejected with `error="delivery_capacity_exceeded"` — no retry.

### Diagnostics Counters

| Counter               | Description                                |
| --------------------- | ------------------------------------------ |
| `inbound_accepted`    | Inbound events accepted                    |
| `outbound_delivered`  | Outbound successes                         |
| `outbound_failed`     | Outbound failures                          |
| `loop_prevented`      | Events blocked by self-loop guard          |
| `capacity_rejections` | Operations rejected by capacity controller |

### Example Configurations

**Conservative (low-resource device):**

```toml
[runtime.limits]
max_inflight_deliveries = 8
max_inflight_replay_events = 4
delivery_acquire_timeout_seconds = 10.0
shutdown_drain_timeout_seconds = 3
```

**High-throughput (server):**

```toml
[runtime.limits]
max_inflight_deliveries = 128
max_inflight_replay_events = 64
delivery_acquire_timeout_seconds = 60.0
shutdown_drain_timeout_seconds = 10
```

## Retry

Retry is **opt-in** — disabled by default. Two levels need to be enabled:

1. **Per-route retry** (`[routes.<id>.retry]`) — controls whether retry receipts are scheduled.
2. **Global retry** (`[retry] enabled = true`) — controls whether the RetryWorker processes them.

Retry uses frozen target semantics — retries target the originally recorded adapter and channel, not the current route config. The retry policy is captured at first failure and does not change with route config updates.

See [configuration.md](configuration.md) for retry configuration fields.

## Replay

Replay re-processes historical events through pipeline stages. Five modes:

| Mode          | Delivers? | Side effects          | Use case                                       |
| ------------- | --------- | --------------------- | ---------------------------------------------- |
| `strict`      | No        | None                  | Validate events against current schema only    |
| `re_render`   | No        | None                  | Re-run rendering for existing events           |
| `re_route`    | No        | None                  | Re-evaluate route matching after config change |
| `dry_run`     | No        | None                  | Preview what replay would do                   |
| `best_effort` | Yes       | Real adapter delivery | Re-deliver historical events                   |

Always run `dry_run` first. `best_effort` produces real outbound messages without deduplication.

```bash
# Preview
medre replay --mode dry_run --config bridge.toml --json

# Re-deliver
medre replay --mode best_effort --config bridge.toml --json
```

Replay receipts carry `source="replay"` and `replay_run_id` for audit.

## Route Loop Prevention

Multiple layers prevent routing loops:

1. **Direct loop detection (startup)** — detects cycles in route configuration. Logged as warnings; startup is not blocked.
2. **Self-loop guard (per-delivery)** — if `target_adapter == event.source_adapter`, delivery is skipped.
3. **Native-ref duplicate suppression (per-ingress)** — checks inbound event's `source_native_ref` against stored refs. Duplicates are dropped before routing.
4. **Route-trace guard (per-delivery)** — if a route ID appears more than once in the event's route trace, delivery is skipped.

What loop prevention does not cover:

- Cross-instance loops (two separate MEDRE instances bridging the same transports).
- Application-level loops (user commands triggering replies — normal bidirectional operation).

## Persistence and Crash Semantics

### What Persists Across Restarts

| State                         | Location                              |
| ----------------------------- | ------------------------------------- |
| Canonical events              | SQLite (`{state}/medre.sqlite`)       |
| Delivery receipts             | SQLite                                |
| Route attribution on receipts | SQLite                                |
| Matrix E2EE crypto keys       | `{state}/adapters/{id}/matrix/store/` |
| LXMF identities               | `{state}/adapters/{id}/lxmf/`         |
| Log history                   | `{state}/logs/medre.log`              |

### What Is Lost on Process Termination

| State                                                      | Nature                                                 |
| ---------------------------------------------------------- | ------------------------------------------------------ |
| In-flight deliveries (no outbox row)                       | Fully lost — no receipt                                |
| In-flight deliveries (with expired in_progress outbox row) | Reclaimable by RetryWorker                             |
| Active replay runs                                         | Must re-initiate manually                              |
| Runtime counters                                           | Reset to zero on startup                               |
| RouteStats per-route counters                              | Reset to zero                                          |
| CapacityController gauges                                  | Reset to zero                                          |
| Runtime events buffer                                      | Lost unless captured via `--snapshot-on-shutdown PATH` |

### Crash Recovery Procedure

1. Restart with the same config: `medre run --config config.toml`
2. Adapters reconnect autonomously. No cleanup needed.
3. Check logs: `grep ERROR {log_dir}/medre.log`
4. Find orphaned events (stored but never delivered):

```sql
SELECT e.event_id, e.source_adapter, e.created_at
FROM canonical_events e
LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
WHERE r.event_id IS NULL
ORDER BY e.created_at DESC;
```

5. Decide whether to replay orphaned events. Use `dry_run` first.

### Persistence Is Single-Machine

MEDRE persists to local SQLite and local filesystem. No replication, no remote backup, no distributed coordination. Operators are responsible for database backup, log rotation, and disk space monitoring.

## Docker / Podman Deployment

### Container Image

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
docker build -t medre .
```

### Running with Config File

```bash
docker run -d \
  --name medre \
  -v /srv/medre:/opt/medre \
  medre run
```

### Running with Environment Variables

```bash
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

### Serial Device Passthrough

For Meshtastic and MeshCore with serial connections:

```bash
docker run -d \
  --device /dev/ttyACM0:/dev/ttyACM0 \
  -v /srv/medre:/opt/medre \
  medre run
```

The container user needs read/write access to the device. Options: run as user in `dialout` group, use `--group-add dialout`, or set udev rules.

### MEDRE_HOME Layout Inside Container

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

### Volume Ownership

The container process needs read/write access to the mounted volume:

```bash
mkdir -p /host/medre-data
chown 1000:1000 /host/medre-data

docker run --user 1000:1000 \
  --env MEDRE_HOME=/opt/medre \
  --volume /host/medre-data:/opt/medre \
  medre
```

### Container Startup Sequence

1. Container runtime sets `MEDRE_HOME` and adapter variables.
2. Path resolution — pure computation, no I/O.
3. Config loaded from `$MEDRE_HOME/config.toml`.
4. Runtime built — Matrix store paths derived here.
5. `app.start()` — directories created (idempotent), storage initialized, adapters started in sorted order.
6. Running — pipeline processes events.
7. Shutdown — on `SIGTERM`/`SIGINT`, graceful shutdown in reverse order. State persists in volume.

### Container Checklist

| Concern                   | Mechanism                                                  |
| ------------------------- | ---------------------------------------------------------- |
| Persistent state          | Mount volume at `MEDRE_HOME`                               |
| SQLite durability         | WAL mode, file on mounted volume                           |
| Matrix crypto persistence | Auto-derived store path under adapter root                 |
| Log persistence           | `{log_dir}/medre.log` on mounted volume                    |
| Serial device access      | `--device` passthrough, correct permissions                |
| Deterministic paths       | `MEDRE_HOME` set to fixed absolute path                    |
| Adapter isolation         | Unique `adapter_id` per adapter, path separator validation |
| Idempotent startup        | `_ensure_dirs()` uses `exist_ok=True`                      |
| Config injection          | Environment variables or mounted `config.toml`             |

### Operational Procedures

**First run (fresh volume):**

```bash
mkdir -p /host/medre-data
cp config.toml /host/medre-data/
docker run -d --name medre \
  --env MEDRE_HOME=/opt/medre \
  --volume /host/medre-data:/opt/medre \
  --device /dev/ttyACM0:/dev/ttyACM0 \
  medre
docker logs medre  # verify startup
```

**Restart (existing volume):**

```bash
docker start medre
```

**Upgrade (new container, same volume):**

```bash
docker stop medre && docker rm medre
docker run -d --name medre \
  --env MEDRE_HOME=/opt/medre \
  --volume /host/medre-data:/opt/medre \
  --device /dev/ttyACM0:/dev/ttyACM0 \
  medre:new-version
```

**Backup:**

```bash
docker stop medre
tar czf medre-backup-$(date +%Y%m%d).tar.gz /host/medre-data/
# or just the critical state:
tar czf medre-state-backup.tar.gz /host/medre-data/state/
```

## Explicit Non-Guarantees

MEDRE is best-effort. It explicitly does not provide:

1. **Exactly-once delivery** — radio transports are probabilistic, Matrix is at-least-once, LXMF is at-least-once with eventual delivery.
2. **Replay deduplication** — replayed events may be delivered again.
3. **Durable adapter-local queue** — Meshtastic outbound queue is in-memory, lost on shutdown.
4. **Per-adapter restart** — only full runtime stop/start.
5. **Distributed coordination** — state is local to the process.
6. **Transactional fan-out** — each target in a fan-out is independent; partial delivery is normal.
7. **Delivery completeness under pressure** — capacity bounds prevent unbounded memory growth but do not prevent data loss.

## Log File Location

```text
{log_dir}/medre.log
```

The resolved log directory depends on the active path mode:

- **XDG mode** (default): `~/.local/state/medre/logs/medre.log` (log directory is `{state}/logs`)
- **MEDRE_HOME mode** (container): `$MEDRE_HOME/logs/medre.log` (log directory is a direct child of `MEDRE_HOME`)

Single global log file. No per-adapter log files. Log format controlled by `[logging]`:

- `format = "text"` — human-readable.
- `format = "json"` — structured for log aggregation.

MEDRE does not rotate logs internally. Use external log rotation (logrotate, Docker logging drivers, Kubernetes log management) for production deployments.

## Recovery Procedures

### Adapter Not Connecting

1. Check adapter health: `medre diagnostics`
2. Look for WARNING/ERROR entries for the adapter in the log file.
3. Verify transport connectivity (network, serial device, BLE pairing).
4. Restart the runtime — the adapter reconnects from scratch.

### Config Error

1. Run `medre config check` to identify the issue.
2. Fix the config file.
3. Restart the runtime.

### Corrupted State

1. Stop the runtime.
2. Back up `{state}/medre.sqlite`.
3. If the database is corrupted, delete it and restart. The runtime creates a fresh database.
4. Note: this loses all event history. Crypto stores (under `{state}/adapters/`) are separate and unaffected.

## Queue Discipline

### Capacity Bounding

The `CapacityController` manages two independent semaphores:

| Stream   | Config field                 | Default bound | What it limits                                           |
| -------- | ---------------------------- | ------------- | -------------------------------------------------------- |
| Delivery | `max_inflight_deliveries`    | 100           | Concurrent adapter `deliver()` calls across all adapters |
| Replay   | `max_inflight_replay_events` | 100           | Concurrent replay event deliveries                       |

When a delivery or replay event cannot acquire a slot within `delivery_acquire_timeout_seconds` (default 1.0s), the operation is rejected — permanent failure with diagnostics incremented. No retry.

### Adapter-Level Queue Bounds

| Adapter    | Queue mechanism                           | Default bound | Overflow policy                                                          |
| ---------- | ----------------------------------------- | ------------- | ------------------------------------------------------------------------ |
| Meshtastic | Unbounded deque with explicit enqueue cap | 1024 items    | Explicit rejection when full, `queue_total_rejected` counter incremented |

Other adapters (Matrix, LXMF, MeshCore) rely on the `CapacityController` semaphore and their transport's own flow control.

### Monitoring Queue Pressure

Capacity counters are available in the `capacity` section of the runtime snapshot. The `medre diagnostics` command produces a build-time snapshot where these counters are zero — it does not start adapters. To inspect live capacity, use `--snapshot-on-shutdown PATH` or the runtime snapshot from a running instance.

| Counter               | What it tells you                          |
| --------------------- | ------------------------------------------ |
| `delivery_current`    | In-flight deliveries right now             |
| `inbound_accepted`    | Inbound events accepted                    |
| `outbound_delivered`  | Outbound successes                         |
| `outbound_failed`     | Outbound failures                          |
| `loop_prevented`      | Events blocked by self-loop guard          |
| `capacity_rejections` | Operations rejected by capacity controller |
| `replay_current`      | In-flight replay events                    |

Sustained growth in `capacity_rejections` indicates the runtime is under more pressure than its configured limits can handle. Consider increasing `max_inflight_deliveries` or reducing the number of active routes.

## Bridged Message Appearance

Messages are rendered by transport-specific renderers when bridging between transports.

### Matrix → Meshtastic

`MeshtasticRenderer` produces a plain-text payload. The `text` field is extracted from the event payload's `body` key. No Matrix formatting, HTML, or metadata is preserved in the Meshtastic output. The source adapter label is not included in the radio text.

### Meshtastic → Matrix

`MatrixRenderer` produces an `m.room.message` content dict. The `body` is the decoded text from the Meshtastic packet. A MEDRE provenance envelope is embedded in the `medre.envelope` subtree recording the source adapter and channel. If the event carries a reply relation, the rendered output includes `m.relates_to` with `m.in_reply_to` referencing the original message ID.

### Reply Threading

| Target renderer | Reply support                                                                        |
| --------------- | ------------------------------------------------------------------------------------ |
| Matrix          | Supported — `m.relates_to` with `m.in_reply_to` added; body includes quoted fallback |
| Meshtastic      | Not supported — reply relations ignored; plain text only                             |
| MeshCore        | Not supported — same as Meshtastic                                                   |
| LXMF            | Partial — relations recorded in fields envelope but not used for display             |

### Source Adapter Label

- Included in Matrix renderer output via `medre.envelope.source_adapter`.
- Included in LXMF renderer output via the fields envelope.
- Not included in Meshtastic or MeshCore renderer output (plain text payloads with no metadata envelope in the current release scope).

## Soak and Validation

The soak harness (`tests/test_soak_harness.py`) validates stability patterns within a bounded timeframe suitable for CI. It uses fake adapters, in-memory storage, and no wall-clock sleeps. Iteration count configurable via `SOAK_HARNESS_ITERATIONS` (default 50, max 200).

```bash
# Default: 50 iterations
pytest tests/test_soak_harness.py

# Deeper local run: 200 iterations
SOAK_HARNESS_ITERATIONS=200 pytest tests/test_soak_harness.py
```

What the soak harness validates: start/stop cycling, replay cycling, delivery under pressure, long-running stability.

What the soak harness does not validate: live transport behaviour, multi-hour stability, radio transport pressure, exactly-once delivery.

## Operational Caveats

### Radio Transport Pressure

Radio transports (Meshtastic, MeshCore) are inherently constrained:

- Low bandwidth — LoRa PHY is extremely slow (hundreds of bytes per second on LongFast).
- No flow control from the mesh — the radio accepts packets as fast as the serial link allows, but the mesh itself may drop them.
- Serial write blocking — writing to a serial port blocks until the kernel buffer accepts the data.
- Packet loss is normal — monitor trends, not individual events.

Under sustained radio pressure, the Meshtastic outbound queue rejects new items when full. This is by design — the runtime prioritizes stability over delivery completeness.

### No Exactly-Once Delivery

No transport in MEDRE provides exactly-once semantics:

- Radio transports (Meshtastic, MeshCore): probabilistic fire-and-forget. Duplicate sends are normal practice.
- Matrix: at-least-once. Retries after connection loss may produce duplicates.
- LXMF (Reticulum): at-least-once with eventual delivery. Propagation delays range from seconds to hours.

### No Transactional Fan-Out

When a single inbound event routes to multiple targets, each target gets an independent delivery. No transactional coordination:

- A failure on one target does not affect the other.
- A success on one target does not guarantee the other.
- Partial delivery is a normal outcome, not an error.

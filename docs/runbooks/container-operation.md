# Container Operation Runbook

> Last updated: 2026-05-12
> Tracks: 8, 9 (deployment boundary enforcement, evidence consolidation)
> Status: Procedures documented. No container runtime execution performed.
> Evidence tier: All operational procedures in this document produce R-tier evidence only when executed against a live container. Unexecuted procedures are NOT EXECUTED.
> Evidence schema: `docs/contracts/61-operational-evidence-contract.md`
> Related: `docs/runbooks/deployment-validation.md`, `docs/contracts/46-runtime-storage-and-path-contract.md`

This runbook covers how to deploy and operate MEDRE inside a container runtime (Docker, Podman, etc.). It documents environment variable configuration, volume mounting, path layout, serial passthrough, and operational procedures.

For the underlying path model and validation procedures, see [Deployment Validation](deployment-validation.md). For the authoritative path contract, see Contract 46.

**Boundary enforcement (Track 8):** Container deployment relies on transport-agnostic path resolution and runtime construction. No container-specific SDK coupling exists. All adapter construction goes through the `RuntimeBuilder` abstraction. Container environment variables map to adapter config dataclasses (pure frozen dataclasses, no SDK dependency).


## 1. Container Image Assumptions

MEDRE's container deployment makes the following assumptions about the container environment:

| Assumption               | Detail                                                      |
|--------------------------|-------------------------------------------------------------|
| Python runtime           | MEDRE requires Python 3.11+                                |
| Single process           | One MEDRE runtime per container                             |
| Non-root execution       | MEDRE does not require root; run as non-root user           |
| No privileged ports      | MEDRE does not bind to ports < 1024                         |
| Filesystem is writable   | `MEDRE_HOME` path must be writable by the process user      |
| No init system           | MEDRE handles its own lifecycle; no systemd/supervisord     |
| Single `MEDRE_HOME` mount | One volume mount captures all persistent state              |


## 2. Environment Variables

MEDRE reads environment variables for configuration. It does **not** read `.env` files itself — the container runtime sets the variables.

Reference: `examples/env/docker.env.example`

### 2.1 Core Variables

| Variable              | Default           | Description                           |
|-----------------------|-------------------|---------------------------------------|
| `MEDRE_HOME`          | (unset → XDG)     | Root data directory. Set for containers. |
| `MEDRE_LOG_LEVEL`     | `INFO`            | Log verbosity: DEBUG, INFO, WARNING, ERROR |

### 2.2 Matrix Adapter Variables

| Variable                          | Description                                    |
|-----------------------------------|------------------------------------------------|
| `MEDRE_MATRIX_ENABLED`            | `true` / `false`                              |
| `MEDRE_MATRIX_HOMESERVER`         | Matrix homeserver URL                          |
| `MEDRE_MATRIX_USER_ID`            | Bot user ID (e.g., `@bot:example.com`)        |
| `MEDRE_MATRIX_ACCESS_TOKEN`       | Access token (generate via Matrix API)         |
| `MEDRE_MATRIX_ROOM_ALLOWLIST`     | Comma-separated room IDs                      |
| `MEDRE_MATRIX_ENCRYPTION_MODE`    | `plaintext` (default), `e2ee_required`, `e2ee_optional` |

### 2.3 Meshtastic Adapter Variables

| Variable                              | Description                                    |
|---------------------------------------|------------------------------------------------|
| `MEDRE_MESHTASTIC_ENABLED`            | `true` / `false`                              |
| `MEDRE_MESHTASTIC_CONNECTION_TYPE`    | `serial`, `tcp`, `ble`, `fake`                |
| `MEDRE_MESHTASTIC_SERIAL_PORT`        | Serial device path (e.g., `/dev/ttyACM0`)     |

### 2.4 Path Derivation from MEDRE_HOME

When `MEDRE_HOME=/opt/medre` is set, all paths derive from it:

```python
from medre.config.paths import resolve
paths = resolve()  # reads MEDRE_HOME from environment

# paths.config_file  → /opt/medre/config.toml
# paths.state_dir    → /opt/medre/state
# paths.data_dir     → /opt/medre/data
# paths.cache_dir    → /opt/medre/cache
# paths.log_dir      → /opt/medre/logs
# paths.database_path → /opt/medre/state/medre.sqlite
# paths.config_dir   → None (MEDRE_HOME mode has no config_dir)
```


## 3. Volume Mounting

### 3.1 Single Volume Pattern

Mount a host directory or Docker volume at `MEDRE_HOME`:

```bash
docker run \
  --env MEDRE_HOME=/opt/medre \
  --volume /host/medre-data:/opt/medre \
  medre
```

This captures all persistent state in one mount:

```
/host/medre-data/           →  /opt/medre/
  config.toml               →  /opt/medre/config.toml
  state/                    →  /opt/medre/state/
    medre.sqlite            →  /opt/medre/state/medre.sqlite
    adapters/               →  /opt/medre/state/adapters/
      matrix_main/          →  /opt/medre/state/adapters/matrix_main/
        matrix/store/       →  /opt/medre/state/adapters/matrix_main/matrix/store/
  data/                     →  /opt/medre/data/
  cache/                    →  /opt/medre/cache/
  logs/                     →  /opt/medre/logs/
```

### 3.2 Bind-Mounted State Persistence

The host directory at the bind mount point persists across container restarts and recreation. MEDRE's `MedreApp._ensure_dirs()` is idempotent — it uses `mkdir(parents=True, exist_ok=True)`, so pre-existing directories from a previous run are not errors.

Key persistence guarantees (Contract 55):

- **SQLite database**: WAL mode. Committed transactions survive process kills.
- **Adapter state**: Per-adapter directories and Matrix crypto stores survive restart.
- **Cache**: Disposable. Cleared on container recreation; recreated on startup.
- **Logs**: Rotated by the runtime. Persist as long as the volume exists.

### 3.3 Volume Ownership

The container process must have read/write access to the mounted volume. If running as a non-root user (recommended):

```bash
# Pre-create the volume directory with correct ownership
mkdir -p /host/medre-data
chown 1000:1000 /host/medre-data

docker run --user 1000:1000 \
  --env MEDRE_HOME=/opt/medre \
  --volume /host/medre-data:/opt/medre \
  medre
```


## 4. Serial Device Passthrough

### 4.1 Meshtastic Serial Connection

For Meshtastic adapters with `connection_type=serial`, the host serial device must be passed through to the container:

```bash
docker run \
  --device /dev/ttyACM0:/dev/ttyACM0 \
  --env MEDRE_MESHTASTIC_CONNECTION_TYPE=serial \
  --env MEDRE_MESHTASTIC_SERIAL_PORT=/dev/ttyACM0 \
  medre
```

### 4.2 Requirements

- The device must exist on the host (`/dev/ttyACM0`)
- The container process must have read/write access to the device
- The device path inside the container must match `MEDRE_MESHTASTIC_SERIAL_PORT`
- If the device is not available, the Meshtastic adapter will fail to start (non-fatal — other adapters continue)

### 4.3 Alternative: TCP Connection

To avoid serial passthrough complexity, use TCP connection type:

```bash
--env MEDRE_MESHTASTIC_CONNECTION_TYPE=tcp
--env MEDRE_MESHTASTIC_HOST=meshtastic-radio.local
--env MEDRE_MESHTASTIC_PORT=4403
```

This requires a Meshtastic device with IP connectivity (e.g., via a host serial-to-TCP bridge like `meshtasticd`).


## 5. Startup Sequence in Container

When MEDRE starts inside a container:

1. **Environment loaded** — Container runtime sets `MEDRE_HOME` and adapter variables.
2. **Path resolution** — `medre.config.paths.resolve()` reads env vars, computes all paths. No I/O.
3. **Configuration loaded** — TOML config read from `$MEDRE_HOME/config.toml` or env-var-derived config.
4. **Runtime built** — `RuntimeBuilder` constructs all subsystems. Matrix store paths derived here.
5. **`app.start()` called**:
   - `_ensure_dirs()` creates all required directories (idempotent).
   - Storage initialized (SQLite opened or in-memory).
   - Adapters started in sorted order by `(transport, adapter_id)`.
6. **Running** — Pipeline processes events between adapters.
7. **Shutdown** — On `SIGTERM`/`SIGINT`, graceful shutdown in reverse order. State persists in volume.


## 6. XDG Behavior in Container

Setting `MEDRE_HOME` disables XDG resolution entirely. When `MEDRE_HOME=/opt/medre`:

- `config_dir` is `None` (no XDG config directory)
- `config_file` is `/opt/medre/config.toml` (flat file, not in a directory)
- All `XDG_*` environment variables are ignored

If `MEDRE_HOME` is not set in the container, MEDRE falls back to XDG mode, which resolves relative to the container's home directory. This is generally **not** what you want in a container — always set `MEDRE_HOME`.


## 7. Adapter-State Isolation

### 7.1 Per-Adapter Directory Tree

Each enabled adapter gets its own state root. Two adapters with IDs `matrix_main` and `mesh_radio`:

```
/opt/medre/state/adapters/
  matrix_main/
    matrix/
      store/              # E2EE crypto store
  mesh_radio/
                      # Meshtastic state (reserved, not yet created)
```

### 7.2 Isolation Guarantees

- No two adapters share a state root.
- `adapter_state_dir()` rejects empty IDs and IDs with path separators.
- Adapter state directories never contain `medre.sqlite` (global DB only).
- Disabled adapters do not get state directories.

### 7.3 Matrix Store Path Auto-Derivation

When `MatrixConfig.store_path` is `None` (the default), `RuntimeBuilder` derives it as:

```python
paths.adapter_transport_state_dir(adapter_id, "matrix") / "store"
# → {state}/adapters/{adapter_id}/matrix/store/
```

This derivation happens during `builder.build()`, before adapter construction. Explicit `store_path` overrides are preserved for testing.


## 8. Health Checking

### 8.1 Boot Summary

After startup, inspect `app.boot_summary` for:
- Which adapters started successfully
- Which adapters failed (and why)
- Overall health: `HEALTHY` or `DEGRADED`

### 8.2 Diagnostic Snapshot

The runtime provides `app.diagnostic_snapshot()` with:
- Runtime state (`RUNNING`, `FAILED`, etc.)
- Capacity controller status
- Shutdown drain timeout

### 8.3 Filesystem Health

```bash
# Verify all expected directories exist
test -d /opt/medre/state && echo "state OK" || echo "state MISSING"
test -d /opt/medre/data && echo "data OK" || echo "data MISSING"
test -d /opt/medre/cache && echo "cache OK" || echo "cache MISSING"
test -d /opt/medre/logs && echo "logs OK" || echo "logs MISSING"
test -f /opt/medre/state/medre.sqlite && echo "db OK" || echo "db MISSING"
```


## 9. Operational Procedures

### 9.1 First Run (Fresh Volume)

```bash
# 1. Create volume directory
mkdir -p /host/medre-data

# 2. Place config.toml
cp config.toml /host/medre-data/

# 3. Start container
docker run -d \
  --name medre \
  --env MEDRE_HOME=/opt/medre \
  --volume /host/medre-data:/opt/medre \
  --device /dev/ttyACM0:/dev/ttyACM0 \
  medre

# 4. Verify startup
docker logs medre
# Look for: "Runtime state transition: starting → running"
```

### 9.2 Restart (Existing Volume)

```bash
docker start medre

# Directories already exist from previous run.
# _ensure_dirs() is idempotent — no errors.
```

### 9.3 Upgrade (New Container, Same Volume)

```bash
docker stop medre
docker rm medre

docker run -d \
  --name medre \
  --env MEDRE_HOME=/opt/medre \
  --volume /host/medre-data:/opt/medre \
  --device /dev/ttyACM0:/dev/ttyACM0 \
  medre:new-version
```

State in the volume persists. SQLite migrations run automatically on startup if needed.

### 9.4 Backup

```bash
# Stop runtime for consistent backup
docker stop medre

# Backup entire volume
tar czf medre-backup-$(date +%Y%m%d).tar.gz /host/medre-data/

# Or just the critical state
tar czf medre-state-backup.tar.gz /host/medre-data/state/
```


## 10. Container Execution: NOT EXECUTED

**Status: NOT EXECUTED**

No container runtime was invoked during the preparation of this runbook. All observations are derived from:

- Source code analysis of `src/medre/config/paths.py` (path resolution)
- Source code analysis of `src/medre/runtime/app.py` (`_ensure_dirs()` lifecycle)
- Source code analysis of `src/medre/runtime/builder.py` (adapter construction, Matrix store derivation)
- `examples/env/docker.env.example` (canonical Docker environment variables)
- Contract 46 (Runtime Storage and Path Model)
- Contract 55 (Runtime Persistence Contract)
- Existing test coverage in `test_config_paths.py`, `test_storage_path_validation.py`, `test_runtime_builder.py`

To validate operationally:

1. Build a container image with MEDRE installed
2. Run with `MEDRE_HOME=/opt/medre` and a bind-mounted host directory
3. Follow validation procedures in [Deployment Validation](deployment-validation.md)
4. Verify startup, persistence across restart, and graceful shutdown


## 11. Container Runtime Observation Fields

When recording container operation evidence per Contract 61 §3.6, the following fields apply:

| Field | Required | Description |
|-------|----------|-------------|
| `container_runtime` | Yes | Docker, Podman, or other |
| `container_image_tag` | Yes | MEDRE container image tag used |
| `medre_home_path` | Yes | Value of MEDRE_HOME inside container |
| `volume_mount_verified` | Yes | Whether bind mount persisted data across restart |
| `runtime_duration_seconds` | Yes | Wall-clock duration of container session, or NOT EXECUTED |
| `adapter_start_success` | Yes | Which adapters started successfully inside container |
| `clean_shutdown_observed` | Yes | Whether SIGTERM produced clean shutdown |
| `boundedness_observed` | Yes | Whether resources stayed bounded during observation, or NOT EXECUTED |
| `reconnect_events` | Yes | Number of adapter reconnect events during observation, or NOT EXECUTED |

All fields above are NOT EXECUTED for the current session. No container runtime was invoked.


## 12. Unresolved Risks

| Risk | Status | Mitigation |
|------|--------|------------|
| No live container execution evidence | NOT EXECUTED | Build container image and run validation procedures. Record R-tier evidence per Contract 61. |
| Non-root UID/GID mismatch | Not tested | Container may fail to write to volume if UID/GID does not match host. Pre-create with correct ownership. |
| Serial device permission in container | Not tested | Device passthrough requires matching host permissions. Container user must be in correct group. |
| Timezone handling in container | Not tested | Container inherits host timezone. Log timestamps may differ if container TZ differs from host. |
| Signal handling (SIGTERM/SIGINT) | Source-verified only | MEDRE handles SIGTERM via asyncio signal handlers. Not validated in a live container. |

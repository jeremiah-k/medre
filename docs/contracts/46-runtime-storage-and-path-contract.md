# Contract 46 — Runtime Storage and Path Model

> **Status:** Active
> **Classification:** Normative
> **Authority:** Authoritative specification for MEDRE filesystem layout, database ownership, and path resolution
> **Last reviewed:** 2026-05-24
>
> **Scope:** Authoritative source of truth for MEDRE's filesystem layout, database ownership, and path resolution.
> **Audience:** Runtime builders, adapter authors, test harnesses, documentation agents.

Every agent or document that references MEDRE storage paths must defer to this contract. If another document contradicts this contract, this contract wins.

## 1. XDG Path Model

When `MEDRE_HOME` is not set, MEDRE follows the XDG Base Directory Specification. Each category resolves independently:

| Category | Default Path            | XDG Override              |
| -------- | ----------------------- | ------------------------- |
| Config   | `~/.config/medre/`      | `$XDG_CONFIG_HOME/medre/` |
| State    | `~/.local/state/medre/` | `$XDG_STATE_HOME/medre/`  |
| Data     | `~/.local/share/medre/` | `$XDG_DATA_HOME/medre/`   |
| Cache    | `~/.cache/medre/`       | `$XDG_CACHE_HOME/medre/`  |

Throughout this document, `{state}` means the resolved state directory (XDG or MEDRE_HOME).

## 2. MEDRE_HOME (Single-Directory Override)

When `MEDRE_HOME` is set, all categories resolve under one root:

| Category | Path                      |
| -------- | ------------------------- |
| Config   | `$MEDRE_HOME/config.toml` |
| State    | `$MEDRE_HOME/state/`      |
| Data     | `$MEDRE_HOME/data/`       |
| Cache    | `$MEDRE_HOME/cache/`      |
| Logs     | `$MEDRE_HOME/logs/`       |

Use this mode for Docker, Kubernetes, and portable deployments.

## 3. Global Runtime Storage

### 3.1 Database

One SQLite database at `{state}/medre.sqlite`.

This is the single storage backend. It holds:

- Canonical events
- Delivery receipts
- Native references
- Replay state
- Cross-adapter relationships
- Global runtime metadata

There are **no per-adapter databases**. Adapter-local filesystem state (section 5) is transport-owned, not MEDRE-owned.

### 3.2 Logs

Global log file: `{state}/logs/medre.log`

Per-adapter log files are a future capability; they do not exist today.

## 4. Per-Adapter State Root

Every adapter receives a state root:

```json
{state}/adapters/{adapter_id}/
```

This directory is created at runtime startup by `MedreApp._ensure_dirs()` for every enabled adapter. It may contain transport-specific subdirectories but never a database.

## 5. Transport-Specific State Directories

Each transport owns a subdirectory within its adapter's state root. The pattern is:

```json
{state}/adapters/{adapter_id}/{transport}/
```

### 5.1 Matrix

```json
{state}/adapters/{adapter_id}/matrix/store/
```

Purpose: nio crypto store (Olm/Megolm session keys, device keys).

Created when: `encryption_mode` is non-plaintext and the adapter is enabled.

The `RuntimeBuilder` derives this path from `adapter_transport_state_dir(adapter_id, "matrix") / "store"` when `MatrixConfig.store_path` is `None`.

### 5.2 Meshtastic (future)

```json
{state}/adapters/{adapter_id}/meshtastic/
```

Not yet created at runtime. Reserved for Meshtastic transport state.

### 5.3 MeshCore (future)

```json
{state}/adapters/{adapter_id}/meshcore/
```

Not yet created at runtime. Reserved for MeshCore transport state.

### 5.4 LXMF (future)

```json
{state}/adapters/{adapter_id}/lxmf/
```

Not yet created at runtime. Reserved for LXMF transport state (e.g., Reticulum identity files).

## 6. Path Resolution Helpers

### 6.1 MedrePaths

`MedrePaths` is the central path resolution object. Two key methods:

- `adapter_state_dir(adapter_id)` — returns the per-adapter root: `{state}/adapters/{adapter_id}/`
- `adapter_transport_state_dir(adapter_id, transport)` — returns a transport subdirectory: `{state}/adapters/{adapter_id}/{transport}/`

These methods perform path computation only; they do not create directories.

### 6.2 Config/Path Resolution is No-I/O

Path resolution is a pure computation. No filesystem I/O occurs during config loading or path resolution. Directories are only created at runtime startup via `MedreApp._ensure_dirs()`.

### 6.3 RuntimeBuilder

`RuntimeBuilder` uses `MedrePaths` to derive all adapter-specific paths. For Matrix adapters:

- Store path: `adapter_transport_state_dir(adapter_id, "matrix") / "store"` when `MatrixConfig.store_path` is `None`.

## 7. Runtime Directory Creation

`MedreApp._ensure_dirs()` creates the following at runtime startup:

1. `state_dir`
2. `data_dir`
3. `cache_dir`
4. `log_dir`
5. Database parent directory (parent of `{state}/medre.sqlite`)
6. Per-adapter roots (`{state}/adapters/{adapter_id}/`) for all enabled adapters
7. Matrix store dirs (`{state}/adapters/{adapter_id}/matrix/store/`) for enabled Matrix adapters with non-plaintext encryption mode

## 8. Explicit Overrides

### 8.1 MatrixConfig.store_path

When set explicitly (non-`None`), the provided path is used unchanged. This is preserved for internal and test harness use. The runtime builder does not override an explicit `store_path`.

### 8.2 Environment Variables

Environment variables like `MEDRE_ADAPTER__<TOKEN>__STORE_PATH` and `MEDRE_ADAPTER__<TOKEN>__DEVICE_ID` (formerly `MEDRE_MATRIX_STORE_PATH` and `MEDRE_MATRIX_DEVICE_ID`) are reserved for internal/testing use only. They are not operator-facing configuration.

## 9. Matrix E2EE Identity

- `device_id` is discovered via the Matrix `whoami()` endpoint after session start, not operator-configured.
- `encryption_mode` is the operator-facing policy control: `plaintext` (default), `e2ee_required`, `e2ee_optional`.
- `ignore_unverified_devices` is an internal nio policy setting, not operator configuration.

## 10. Summary Table

```json
{state}/medre.sqlite                                   — Global database (one backend)
{log_dir}/medre.log                                    — Global log file
{state}/adapters/{adapter_id}/                         — Per-adapter state root
{state}/adapters/{adapter_id}/matrix/store/            — Matrix E2EE crypto store
{state}/adapters/{adapter_id}/meshtastic/              — Meshtastic state (future)
{state}/adapters/{adapter_id}/meshcore/                — MeshCore state (future)
{state}/adapters/{adapter_id}/lxmf/                    — LXMF state (future)
```

In XDG mode, `{log_dir}` resolves to `{state}/logs` (i.e., `~/.local/state/medre/logs`). In MEDRE_HOME mode, `{log_dir}` resolves to `$MEDRE_HOME/logs` — a direct child of `MEDRE_HOME`, not under `state/`.

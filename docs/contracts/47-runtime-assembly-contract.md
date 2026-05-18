# Contract 47 — Runtime Assembly Contract

**Status:** Active
**Scope:** Authoritative specification for how the MEDRE runtime assembles, starts, and shuts down a multi-adapter configuration.
**Audience:** Runtime builders, adapter authors, test harnesses, documentation agents.
**References:** Contract 46 (Runtime Storage and Path Model), Contract 31 (Session Boundary).

Every agent or document that references MEDRE runtime assembly, startup ordering, adapter lifecycle, or shutdown semantics must defer to this contract.

## 1. RuntimeBuilder — Single Entry Point

`RuntimeBuilder` is the sole entry point for constructing a MEDRE runtime from a loaded configuration. No other component builds the runtime. The builder takes parsed config and produces a fully assembled `MedreApp` ready for `start()` / `stop()`.

The builder does not read files, parse TOML, or resolve config search paths — those responsibilities belong to the config loader. The builder receives already-resolved config.

## 2. Multi-Adapter Support

### 2.1 Multiple Adapters of the Same Transport

The runtime supports multiple adapter instances of the same transport type. A single configuration may declare:

- Multiple Matrix adapters (e.g., `bot1`, `bot2`)
- Multiple Meshtastic adapters (e.g., `longfast`, `shortturbo`)
- Multiple MeshCore adapters
- Multiple LXMF adapters
- Any combination of the above

### 2.2 Adapter ID as First-Class Identity

Each adapter carries an `adapter_id` that is its first-class runtime identity. Adapter IDs are unique across the entire runtime, regardless of transport.

Adapters are keyed by the tuple `(transport, adapter_id)`. This tuple uniquely identifies every adapter instance.

The `adapter_id` defaults to the TOML table key (`INSTANCE_NAME`) when not explicitly set:

```toml
[adapters.matrix.bot1]       # adapter_id = "bot1"
[adapters.matrix.bot2]       # adapter_id = "bot2"
[adapters.meshtastic.radio]  # adapter_id = "radio"
```

## 3. Startup Ordering

### 3.1 Deterministic Order

Adapter startup order is deterministic. Adapters are sorted by:

1. **Transport name** (alphabetical: `lxmf`, `matrix`, `meshcore`, `meshtastic`)
2. **Adapter ID** (alphabetical within the same transport)

This ordering is stable across restarts and platforms.

### 3.2 Example Order

Given this configuration:

```toml
[adapters.meshtastic.longfast]
[adapters.matrix.bot1]
[adapters.meshtastic.shortturbo]
[adapters.matrix.bot2]
```

Startup order:

1. `matrix.bot1`
2. `matrix.bot2`
3. `meshtastic.longfast`
4. `meshtastic.shortturbo`

## 4. Assembly Failure Handling

### 4.1 Individual Adapter Failures Are Collected

If an adapter fails during assembly (construction or start), the failure is collected. Remaining adapters continue building and starting. The runtime does not abort on the first failure.

### 4.2 Failure Collection

All assembly errors are collected into a structured report. After the assembly pass completes, the runtime reports:

- Which adapters started successfully.
- Which adapters failed, with per-adapter error details.
- The total count of successful and failed adapters.

### 4.3 Partial Startup Cleanup

If some adapters have already started successfully and a subsequent adapter fails (or if the runtime decides to abort after collecting failures):

1. All successfully started adapters are stopped.
2. Stop order is **reverse of start order**.
3. Cleanup errors are logged but do not prevent other adapters from being stopped.

This ensures no orphaned transport connections remain after a failed assembly.

## 5. Shutdown Semantics

### 5.1 Reverse Start Order

Shutdown proceeds in the **reverse** of the startup order. The last adapter started is the first adapter stopped.

### 5.2 Shutdown Timeout

The runtime observes a configurable shutdown timeout (`shutdown_timeout_seconds` from `[runtime]`). If any adapter does not stop within this deadline, the runtime proceeds and logs a warning. The timeout is global, not per-adapter.

### 5.3 Clean Shutdown

Each adapter's `stop()` method is called exactly once during shutdown. Adapters that were never started (disabled or failed during assembly) are not stopped.

## 6. Adapter Lifecycle Isolation

### 6.1 Independent Lifecycle

Each adapter's lifecycle is fully independent. One adapter's start, stop, or runtime crash does not affect any other adapter's operation.

### 6.2 No Cross-Adapter Coordination

Adapters do not coordinate with each other. There is no cross-adapter startup barrier, health dependency, or cascading failure mechanism.

### 6.3 Crash Isolation

If an adapter crashes at runtime (after successful start), the crash is:

1. Logged with the adapter's `(transport, adapter_id)`.
2. Reported in diagnostics.
3. Does **not** cause other adapters or the runtime to stop.

Crashed adapters may enter a `failed` health state. Recovery is adapter-local (reconnect policies, session retry budgets — see Contract 31).

## 7. Disabled Adapters

Adapters with `enabled = false` are skipped entirely during assembly. They are not constructed, not started, and do not appear in the running adapter set. Their configuration is validated but their transport SDK is not imported.

## 8. Adapter Kind — SDK Import Policy

### 8.1 `adapter_kind = "fake"`

Fake adapters bypass all optional SDK imports. No transport-specific packages (e.g., `mindroom-nio`, `mtjk`, `meshcore_py`, `lxmf`, `rns`) are required. Fake adapters are used for:

- Testing without transport hardware or services.
- Development and CI environments.
- Config validation against a mock transport.

### 8.2 `adapter_kind = "real"`

Real adapters import their transport SDK. If the SDK is not installed:

- The runtime raises `RuntimeConfigError` with the `adapter_id` identifying which adapter failed.
- The error message identifies the missing SDK package.
- This is a fatal configuration error, not a runtime retry condition.

## 9. Config Validation — Before Assembly

Config validation occurs **before** any adapter is constructed. The following checks are performed:

### 9.1 Duplicate Adapter IDs

No two adapters may share the same `adapter_id`, even across different transports. Duplicate IDs are rejected with a clear error identifying the conflicting entries.

### 9.2 Conflicting State Paths

No two adapters may share the same state path root (`{state}/adapters/{adapter_id}/`). Since the state root is derived from `adapter_id`, the duplicate-ID check (9.1) implicitly prevents path conflicts. If path overrides are introduced in the future, explicit path-conflict detection will be added.

### 9.3 Validation Failure Behavior

If config validation fails, the runtime exits with an error. No adapters are started. No directories are created. No connections are made.

## 10. Storage Model — Global DB, Adapter-Local State

### 10.1 One Global Database

There is exactly **one** global SQLite database at `{state}/medre.sqlite`. This database holds all canonical events, delivery receipts, native references, replay state, cross-adapter relationships, and runtime metadata.

There are **no per-adapter databases**. This is a design invariant, not an implementation detail.

### 10.2 Adapter-Local Filesystem State

Adapter-local filesystem state exists under `{state}/adapters/{adapter_id}/{transport}/`. This state is **transport-owned**, not MEDRE-owned. Examples:

- Matrix crypto store (Olm/Megolm keys)
- LXMF identity files
- Meshtastic transport state (future)
- MeshCore transport state (future)

The runtime creates directories for adapter-local state but does not manage the contents. Transport SDKs read and write their own state.

### 10.3 Path Model Reference

The complete path model is defined in **Contract 46**. This contract references it without reproducing it. Key paths:

```json
{state}/medre.sqlite                                    — Global database (one)
{state}/logs/medre.log                                  — Global log file
{state}/adapters/{adapter_id}/                          — Per-adapter state root
{state}/adapters/{adapter_id}/{transport}/              — Transport-owned state
```

## 11. Directory Creation

`MedreApp._ensure_dirs()` creates all required directories at runtime startup, after config validation passes but before any adapter starts:

1. Global directories: `state_dir`, `data_dir`, `cache_dir`, `log_dir`.
2. Database parent directory.
3. Per-adapter state roots for all **enabled** adapters.
4. Transport-specific subdirectories for enabled adapters (e.g., Matrix store dirs for non-plaintext encryption).

See Contract 46 § 7 for the full list.

## 12. Assembly Sequence Summary

The complete assembly sequence, from config to running runtime:

```text
1. Load and parse config (config loader, not RuntimeBuilder)
2. Validate config (duplicates, conflicts, required fields)
   → On failure: exit with error, no side effects
3. Resolve paths (MedrePaths — pure computation, no I/O)
4. Create directories (_ensure_dirs)
5. Sort adapters by (transport, adapter_id)
6. For each enabled adapter, in order:
   a. Construct adapter (import SDK if real)
      → On failure: collect error, continue
   b. Start adapter
      → On failure: collect error, continue
7. Report assembly results (successes + failures)
8. If any critical failure: stop successful adapters in reverse order
9. Runtime is running
```

Shutdown sequence:

```text
1. Signal received (SIGTERM, SIGINT, or programmatic stop)
2. For each started adapter, in reverse start order:
   a. Call adapter.stop()
   b. Log result
3. Observe shutdown timeout
4. Runtime is stopped
```

## 13. Error Types

| Error                  | When Raised                                                                                   | Contains                         |
| ---------------------- | --------------------------------------------------------------------------------------------- | -------------------------------- |
| `RuntimeConfigError`   | Duplicate adapter IDs, conflicting paths, missing SDK for real adapter, invalid config values | `adapter_id` or field name       |
| `RuntimeAssemblyError` | Adapter construction or start failure during assembly                                         | `adapter_id`, original exception |
| `AdapterStartError`    | Transport-specific start failure                                                              | `adapter_id`, transport context  |

All errors include the `adapter_id` so operators can identify which adapter failed without reading stack traces.

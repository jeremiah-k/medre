# Contract 56 — Runtime Supervision Contract

**Status:** v1 Implementation — Deterministic health classification and failure severity only
**Scope:** Runtime-level health aggregation, fatal vs non-fatal adapter failure classification, startup outcome semantics, and architectural boundary enforcement for the MEDRE runtime.
**Audience:** Runtime builders, adapter authors, operators, test authors.
**References:** Contract 47 (Runtime Assembly), Contract 48 (Runtime Observability), Contract 54 (Runtime Shutdown), Contract 29 (Diagnostics Contract).

**Non-guarantees (explicit):** This contract does not provide automatic adapter restart, supervisor trees, process respawn, admin API, REST/TUI/daemon management, hot reload, distributed orchestration, or transport redesign. The supervision layer is classification and observability only.

**Post-start supervision scope (explicit):** MEDRE can classify supplied adapter health states after startup — the pure classification functions (`classify_runtime_health`, `classify_adapter_failure_severity`, `runtime_supervision_snapshot`) accept arbitrary `AdapterState` sequences and return deterministic results at any time. Manual live-health refresh is available via `MedreApp.refresh_live_health()` (§8), which calls each adapter's `health_check()` in deterministic order and updates per-adapter lifecycle states. However, **automatic** health refresh polling, background scheduling, and automatic runtime state transitions in response to adapter state changes are **not implemented**. The runtime does not actively detect or react to adapter failures at runtime — refresh is caller-initiated, not scheduled.

## 0. State Layer Model

MEDRE uses four distinct state layers. Each has a different source of truth, granularity, and update mechanism. Confusing them is the most common source of misunderstanding.

| Layer | Enum | Scope | Source of Truth | Updated By |
|-------|------|-------|-----------------|------------|
| **RuntimeState** | `medre.runtime.app.RuntimeState` | Process lifecycle: `INITIALIZED → STARTING → RUNNING → STOPPING → STOPPED`, or `→ FAILED` | `MedreApp.state` | `MedreApp.start()`, `.stop()`, unrecoverable errors |
| **RuntimeHealth** | `medre.core.runtime.supervision.RuntimeHealth` | Aggregate adapter health: `HEALTHY`, `DEGRADED`, `FAILED` | Derived (pure function) | `classify_runtime_health()` — called by operator or snapshot, **not** auto-updated |
| **StartupOutcome** | `medre.core.runtime.supervision.StartupOutcome` | One-time boot result: `SUCCESS`, `PARTIAL`, `TOTAL_FAILURE` | Derived (pure function) | `classify_startup_outcome()` — computed once during `start()` |
| **AdapterState** | `medre.core.lifecycle.states.AdapterState` | Per-adapter lifecycle: `INITIALIZING → READY → DEGRADED / BACKPRESSURED / DISCONNECTED → STOPPING → STOPPED / FAILED` | `MedreApp._adapter_states[adapter_id]` | Build, start, stop, cleanup code paths |

Key distinctions:

1. **RuntimeState has no `DEGRADED` value.** The runtime process is either running or it is not. Degradation is a *health* concept, not a *lifecycle* concept. A runtime in `RUNNING` state can have `DEGRADED` or `FAILED` health.

2. **RuntimeHealth is derived, not stored.** It is a pure projection of adapter states at the time of classification. It is not continuously monitored or auto-refreshed.

3. **StartupOutcome is computed once.** It classifies the boot result and does not change after startup completes. It feeds into `startup_health` in the snapshot.

4. **AdapterState is per-adapter.** Each adapter has its own lifecycle tracked independently. The runtime aggregates them to derive RuntimeHealth.

Concrete examples:
- **Partial startup:** `RuntimeState=RUNNING`, `RuntimeHealth=DEGRADED`, `StartupOutcome=PARTIAL`, some adapters `READY` and some `FAILED`.
- **Total startup failure:** `RuntimeState=FAILED`, `RuntimeHealth=FAILED`, `StartupOutcome=TOTAL_FAILURE`, all adapters `FAILED`.
- **Clean stop:** `RuntimeState=STOPPED`, all adapters `STOPPED`. RuntimeHealth is not meaningful after stop.

## 1. Runtime Health Model

The runtime health is a single enumerated value derived deterministically from the aggregate states of all registered adapters.

### 1.1 RuntimeHealth Enum

| Value | Meaning |
|-------|---------|
| `HEALTHY` | All adapters are in `READY` state. The runtime is fully operational. |
| `DEGRADED` | At least one adapter is operational (`READY`), but at least one adapter is not. The runtime continues with reduced capacity. |
| `FAILED` | All adapters are in `FAILED` state, or zero adapters are registered. The runtime cannot route events. |

### 1.2 Classification Rules

`classify_runtime_health(adapter_states: Sequence[AdapterState]) -> RuntimeHealth`

Priority-ordered rules:

1. **Empty sequence** → `FAILED`. Zero adapters means the runtime has no routing capability.
2. **All states are `READY`** → `HEALTHY`. Every adapter is fully operational.
3. **All states are `FAILED`** → `FAILED`. No routing capability remains.
4. **At least one `READY` and at least one non-`READY` state** → `DEGRADED`. The runtime operates with reduced capacity.
5. **No `READY` states, but at least one operational non-`FAILED` state** (`DEGRADED`, `BACKPRESSURED`, `DISCONNECTED`) → `DEGRADED`. The runtime has partial capability.
6. **`INITIALIZING` states only** → `FAILED`. Adapters not yet started are not operational.
7. **`STOPPING` states** → treated as non-operational (not `READY`, not `FAILED`).

These rules are pure, deterministic, and transport-agnostic. They depend only on `AdapterState` values.

## 2. Adapter Failure Severity

### 2.1 AdapterFailureSeverity Enum

| Value | Meaning |
|-------|---------|
| `FATAL` | The adapter failure makes the runtime inoperable. All adapters are down. |
| `NON_FATAL` | The adapter failure degrades the runtime, but at least one adapter remains operational. |

### 2.2 Classification Rule

`classify_adapter_failure_severity(healthy_count: int, total_count: int) -> AdapterFailureSeverity`

- `healthy_count == 0` and `total_count > 0` → `FATAL`
- `healthy_count > 0` → `NON_FATAL`
- `total_count == 0` → `FATAL` (zero adapters, no capability)

**Key invariant:** When `classify_runtime_health()` is supplied with one `FAILED` and one `READY` adapter state, the result is `DEGRADED` rather than `FAILED`. This classification invariant ensures a single adapter failure is correctly identified as non-fatal when other adapters remain operational. The runtime does not actively transition states in response to adapter failures — callers supply the adapter states to classify.

## 3. Startup Outcome Semantics

### 3.1 StartupOutcome Enum

| Value | Meaning |
|-------|---------|
| `SUCCESS` | All configured adapters started successfully. Runtime is `HEALTHY`. |
| `PARTIAL` | Some adapters started, some failed. Runtime is `DEGRADED`. This is allowed. |
| `TOTAL_FAILURE` | Zero adapters started. Runtime is `FAILED`. This is a startup failure. |

### 3.2 Classification Rule

`classify_startup_outcome(started: int, failed: int, total: int) -> StartupOutcome`

- `started == 0` and `total > 0` → `TOTAL_FAILURE`
- `started > 0` and `failed > 0` → `PARTIAL`
- `started == total` → `SUCCESS`
- `total == 0` → `TOTAL_FAILURE` (nothing configured, nothing started)

**Key invariant:** Partial startup is allowed. The runtime enters `DEGRADED` state. Only total startup failure (zero adapters started) is fatal.

## 4. Adapter Lifecycle State Registry

`MedreApp` owns an authoritative per-adapter lifecycle state registry (`_adapter_states: dict[str, AdapterState]`) that tracks each adapter's lifecycle state through build, start, stop, and cleanup.

### 4.1 State Source of Truth

The registry is the single source of truth for adapter lifecycle states. Runtime health classification (`classify_runtime_health`) consumes registry values rather than constructing ephemeral state lists.

### 4.2 State Mutation Points

| When | State set | Transition |
|------|-----------|------------|
| Before adapter start loop | `INITIALIZING` | (initial) |
| Build failure | `FAILED` | (initial) |
| Successful adapter start | `READY` | `INITIALIZING` → `READY` |
| Adapter start failure | `FAILED` | `INITIALIZING` → `FAILED` |
| Before adapter stop | `STOPPING` | `READY` → `STOPPING` |
| Successful adapter stop | `STOPPED` | `STOPPING` → `STOPPED` |
| Adapter stop failure | `FAILED` | `STOPPING` → `FAILED` |
| Cleanup: successful stop | `STOPPED` | `STOPPING` → `STOPPED` |
| Cleanup: failed stop | `FAILED` | `STOPPING` → `FAILED` |
| Never-started adapter during stop | `FAILED` | `INITIALIZING` → `FAILED` |

All transitions are validated via `require_valid_transition()`. Initial assignments (adapter not yet in registry) bypass validation.

### 4.3 Read-Only Access

The `adapter_states` property returns a shallow copy of the registry. Snapshot consumers read from `_adapter_states` directly.

### 4.4 Snapshot Exposure

Per-adapter lifecycle states are exposed in the snapshot under `lifecycle.adapters` as a sorted `{adapter_id: state_string}` mapping (Contract 63 §5.3).

## 5. Runtime Diagnostics Aggregation

The supervision module provides a diagnostics snapshot hook:

`runtime_supervision_snapshot(adapter_states: Sequence[AdapterState]) -> dict[str, Any]`

Returns a JSON-safe dictionary containing:
- `runtime_health`: The classified `RuntimeHealth` value as a string.
- `adapter_summary`: Counts by state category (`healthy`, `degraded`, `failed`, `other`).
- `startup_fingerprint`: Deterministic description of the adapter state distribution.

This is observational only. It does not trigger restarts, alerts, or state changes.

### 5.1 Snapshot Health Field Semantics

The runtime snapshot exposes health through two explicit top-level fields:

| Field | Value | Meaning |
|-------|-------|---------|
| `startup_health` | `dict \| null` | Startup-derived supervision snapshot from `runtime_supervision_snapshot()`. Set once during `app.start()`. Frozen — not affected by `refresh_live_health()`. |
| `live_health` | `dict \| null` | `null` before the first call to `MedreApp.refresh_live_health()`. After the first successful refresh, contains a `LiveHealthSnapshot` dict with per-adapter live health, aggregate classification, and poll metadata. Populated only by explicit manual refresh; no background polling exists. |

The split ensures operators cannot confuse the one-time startup health assessment with live runtime health. `startup_health` remains frozen at its startup value regardless of live refresh activity. `live_health` transitions from `null` to `dict` on the first successful `refresh_live_health()` call; `scope` transitions from `"startup"` to `"live"` and `live_refresh` transitions from `false` to `true`.

### 5.2 Provenance Metadata in the Snapshot

The runtime snapshot carries explicit provenance metadata (`scope` and `live_refresh`) on operator-facing sections so that operators and tooling can determine whether a value is startup-derived, process-local, or live without consulting external documentation. This follows the existing convention established for `routes.*` sub-sections (Contract 63 §5.4.1).

Section-level provenance:

| Section | `scope` | `live_refresh` | Rationale |
|---------|---------|----------------|-----------|
| `startup` | `"startup"` | `false` | Boot classification computed once during `MedreApp.start()`. |
| `startup.startup_health` | `"startup"` | `false` | Health classification from `runtime_supervision_snapshot()` at startup. |
| `health` | `"startup"` | `false` | Before first refresh: startup-derived health assessment. `live_health` is `null`. After first `refresh_live_health()`: `scope` transitions to `"live"`, `live_refresh` to `true`, `live_health` carries live data. |
| `lifecycle` | `"process_local"` | `false` | In-process state at snapshot time. Not persisted across restarts. |
| `diagnostics` | `"process_local"` | `true` | Event buffer grows during process lifetime. |

Per-adapter provenance:

| Field | Provenance | Meaning |
|-------|-----------|---------|
| `adapters.{id}.health` | `"startup"` (via `provenance` field) | Static `_last_health` from build/startup. Not refreshed. |
| `adapters.{id}.provenance` | Always `"startup"` | Explicit marker that adapter metadata is startup-derived. |
| `lifecycle.adapters.{id}` | `"process_local"` (section-level) | Current `AdapterState` from in-memory registry. |

**Key distinction for operators:** `adapters.{id}.health` (startup-derived) and `lifecycle.adapters.{id}` (process-local) can diverge after startup. The `provenance` field and section-level `scope` make this distinction machine-readable.

### 5.3 Where to Look: Operator Diagnostics Guide

| Operator Question | Look Here | Snapshot Path | Provenance |
|---|---|---|---|
| Did config load? | CLI stderr (exit code 2) | N/A | build |
| Which adapters failed to build? | `startup.build_failures` | `snapshot.startup.build_failures` | startup |
| Did startup succeed? | CLI stdout + `startup.boot_summary` | `snapshot.startup.boot_summary.startup_outcome` | startup |
| Is runtime healthy? | `startup.startup_health.runtime_health` | `snapshot.startup.startup_health.runtime_health` | startup (not live!) |
| Which routes are active? | `routes.eligibility` + `routes.startup_readiness` | `snapshot.routes.eligibility.registered` / `snapshot.routes.startup_readiness.readiness` | build / startup |
| Which adapters are running now? | `lifecycle.adapters` | `snapshot.lifecycle.adapters.{id}` | process-local |
| Is adapter health current? | Check `adapters.{id}.provenance` | `snapshot.adapters.{id}.provenance` → `"startup"` | startup (stale!) |
| How long up? | `lifecycle.uptime_seconds` | `snapshot.lifecycle.uptime_seconds` | process-local |

## 6. Architectural Boundaries

The following boundaries are enforced by tests:

| Module | Must NOT import | May import |
|--------|----------------|------------|
| `medre.core.runtime.supervision` | Transport SDKs (`nio`, `meshtastic`, `meshcore`, `RNS`, `lxmf`), concrete adapter packages | `medre.core.lifecycle.states`, `medre.adapters.base` (protocol types only) |
| `medre.core.runtime.diagnostics` | Transport SDKs, concrete adapter packages | `medre.core.runtime.health`, `medre.core.runtime.supervision` |
| `medre.core.runtime.health` | Transport SDKs, concrete adapter packages | `medre.adapters.base` (protocol types), `medre.core.lifecycle.states` |
| Snapshot code | Transport SDKs, concrete adapter packages | Runtime core modules only |
| Accounting code | Transport SDKs, concrete adapter packages | Runtime core modules only |
| Persistence contract | Transport SDKs, concrete adapter packages | Runtime core modules only |

## 7. Test Coverage Requirements

1. **Classification tests:** `RuntimeHealth` classification for all meaningful combinations of `AdapterState` values.
2. **Failure severity tests:** `FATAL` when all adapters down, `NON_FATAL` when some remain.
3. **Startup outcome tests:** `SUCCESS`, `PARTIAL`, `TOTAL_FAILURE` for edge cases.
4. **Boundary tests:** Import-line analysis ensuring no transport SDK or concrete adapter package leakage.
5. **Fake adapters only:** No live transport dependencies in any supervision test.

## 8. Live Health Manual Refresh

This section documents the manual live health refresh mechanism. `MedreApp.refresh_live_health()` exists and is functional. It is explicitly manual and caller-initiated — there is no background polling, no scheduler, and no automatic refresh.

### 8.1 Types

Two frozen dataclasses are defined in `src/medre/core/runtime/health.py`:

- **`AdapterLiveHealth`** — per-adapter live health result from a single `health_check()` call. Fields: `adapter_id`, `health` (VALID_HEALTH_STRINGS), `adapter_state`, `fake_or_live`, `poll_timestamp_monotonic`, `poll_timestamp_wall`, `error`. JSON-safe via `to_dict()`.
- **`LiveHealthSnapshot`** — aggregate runtime live health from a single refresh cycle. Fields: `runtime_health`, `adapter_summary`, `adapters` (dict of `AdapterLiveHealth`), `poll_timestamp_monotonic`, `poll_timestamp_wall`, `poll_count`. JSON-safe via `to_dict()`.

### 8.2 Runtime Method

`MedreApp.refresh_live_health()` is an async method that:

1. Guards on `RuntimeState.RUNNING` (raises if not running).
2. Iterates `self.adapters` in **deterministic order** (sorted by adapter ID), calling `await adapter.health_check()` on each.
3. Normalizes results via `normalize_adapter_health()`.
4. Maps health strings to `AdapterState` values and updates `self._adapter_states`.
5. Re-classifies via `classify_runtime_health()`.
6. Builds a `LiveHealthSnapshot` and stores it in `self._live_health_state`.
7. Increments `self._live_poll_count`.
8. Returns the snapshot.

**Isolation:** Individual adapter failures during refresh are isolated — a failure on one adapter does not prevent other adapters from being polled. The adapter's error is recorded in its `AdapterLiveHealth.error` field.

**Cancellation:** The method is caller-cancellable via `asyncio.CancelledError`. Cancellation is safe and does not corrupt runtime state.

**No scheduling:** This method performs async I/O but does **not** schedule itself. Callers (operator API, CLI, test harness) are responsible for invocation timing. There is no background task, no polling loop, and no automatic refresh interval.

### 8.3 Snapshot Integration

The snapshot slot is `health.live_health` (Contract 63 §5.1). Before `refresh_live_health()` has been called, it is `null`. After the first successful refresh, `build_runtime_snapshot()` reads `app._live_health_state` and populates the slot. Provenance tags transition: `scope` → `"live"`, `live_refresh` → `true`.

The `null` → `dict` transition is non-breaking per Contract 63 §4.2. No `schema_version` bump is required.

### 8.4 State Transitions

Before the first call to `refresh_live_health()`:

| Field | Value |
|-------|-------|
| `health.live_health` | `null` |
| `health.scope` | `"startup"` |
| `health.live_refresh` | `false` |
| `startup.startup_health` | Frozen dict from startup |

After the first successful call to `refresh_live_health()`:

| Field | Value |
|-------|-------|
| `health.live_health` | `LiveHealthSnapshot` dict (per-adapter health, aggregate classification, poll metadata) |
| `health.scope` | `"live"` |
| `health.live_refresh` | `true` |
| `startup.startup_health` | Unchanged — still frozen from startup |

`startup_health` is **frozen** and is not affected by `refresh_live_health()`. Live health is **process-local** and not durable — it is lost on process restart.

### 8.5 Timestamp Semantics

Live health uses dual timestamps consistent with existing patterns:

- **`poll_timestamp_monotonic`** (`time.monotonic()`, seconds): primary timestamp for ordering, deduplication, and delta computation. Consistent with `RuntimeEvent.timestamp` and `uptime_seconds`.
- **`poll_timestamp_wall`** (ISO-8601 UTC): human-readable for operators and external log correlation. Consistent with `snapshot_at` and `startup_timestamp`.
- **`poll_count`** (integer): monotonically increasing counter per successful refresh cycle, for quick staleness checks.

### 8.6 Event Semantics: HEALTH_REFRESHED

A `HEALTH_REFRESHED` runtime event is emitted into the bounded `EventBuffer` once per **successfully completed** `refresh_live_health()` call. The event is not emitted on cancellation or failure to reach the RUNNING state.

**Emission timing:** The event is emitted after the snapshot is fully constructed, stored in `_live_health_state`, and the poll count incremented. If `asyncio.CancelledError` propagates from any adapter's `health_check()`, the method re-raises immediately — no event is emitted, no poll count is incremented, and `_live_health_state` remains unchanged.

**Event detail shape (always present):**

| Key | Type | Description |
|-----|------|-------------|
| `runtime_health` | `str` | Aggregate `RuntimeHealth` value: `"healthy"`, `"degraded"`, or `"failed"`. |
| `poll_count` | `int` | Monotonically increasing counter, matches `LiveHealthSnapshot.poll_count`. |
| `adapter_summary` | `dict` | Counts: `healthy`, `degraded`, `failed`, `transitional`, `total`. |

**Event detail shape (conditional):**

| Key | Type | Condition |
|-----|------|-----------|
| `failed_adapters` | `list[str]` | Present when ≥1 adapter's `health_check()` raised an exception. Sorted by `adapter_id`. Contains only adapter IDs, not error strings. |
| `changed_adapters` | `list[str]` | Present when a previous snapshot exists and ≥1 adapter's health changed. Sorted by `adapter_id`. Contains only adapter IDs. |

**Determinism and boundedness:**

- Both `failed_adapters` and `changed_adapters` are explicitly `sorted()` to guarantee deterministic order regardless of construction order.
- Event detail passes through `sanitize_diagnostic_mapping()` (Contract 29), which strips secrets, truncates oversized strings, and replaces non-serialisable objects with type-name placeholders.
- The event detail does **not** contain per-adapter error strings. Error text is bounded to 256 characters via `truncate_health_error()` and stored in `AdapterLiveHealth.error` within the `LiveHealthSnapshot`, which is not part of the event detail.

**No background scheduling:** `refresh_live_health()` is purely caller-initiated. No background task, polling loop, timer, or scheduler invokes it automatically. The `HEALTH_REFRESHED` event is only emitted when a caller explicitly calls the method while the runtime is RUNNING.

## 9. Deferred

The following are explicitly out of scope:

- Automatic adapter restart manager
- Supervisor tree / process respawn
- Admin API / REST / TUI / daemon manager
- Hot reload of adapters
- Distributed orchestration
- Transport redesign
- Canonical event redesign
- Health polling / circuit breakers / auto-degrade logic

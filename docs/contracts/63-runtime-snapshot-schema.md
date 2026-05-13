# Contract 63 — Runtime Snapshot Schema

**Status:** Active
**Scope:** Normative specification for the runtime snapshot shape, field semantics, stability classification, versioning policy, and structural guarantees.
**Audience:** Runtime builders, adapter authors, operators, test authors, documentation agents.
**References:** Contract 47 (Runtime Assembly), Contract 48 (Runtime Observability), Contract 56 (Runtime Supervision), Contract 29 (Diagnostics).

Every agent or document that references the MEDRE runtime snapshot shape, field stability, or extension rules must defer to this contract.


## 1. Non-goals

- Adding new snapshot fields or changing runtime behaviour.
- Implementing live health polling, dynamic routing, event persistence, or supervisor restarts.
- Changing adapter, route, or diagnostics behaviour.
- Replacing the diagnostics snapshot (`build_diagnostics_snapshot`) — that is a separate surface documented in Contract 29.


## 2. Schema Version

The snapshot carries a top-level `schema_version` integer. **Current version: `1`.**

`schema_version` is bumped only when a **breaking change** is introduced to the top-level shape (§4). Additive or unstable/debug changes (§6) do not require a bump.


## 3. Top-level Shape

All 18 keys are **always present**. Optional subsystems that are not wired report `null` — keys are never omitted.

Keys appear in **alphabetical order** (deterministic serialisation).

| Key | Type | Stability | Audience | Source |
|-----|------|-----------|----------|--------|
| `accounting` | `dict \| null` | stable | operator | `RuntimeAccounting.snapshot()` |
| `adapters` | `dict` | stable | operator | `_snapshot_adapter()` per adapter |
| `boot_summary` | `dict \| null` | stable | operator | `BootSummary.to_dict()` |
| `build_failures` | `list` | stable | operator | `_snapshot_build_failures()` |
| `capacity` | `dict \| null` | stable | operator | `CapacityController.snapshot()` |
| `delivery_counters` | `dict \| null` | stable | operator | Alias for `routes` content |
| `limits` | `dict` | stable | operator | `RuntimeLimits` dataclass fields |
| `live_health` | `null` | reserved | — | Always `null` until health polling exists |
| `replay` | `dict` | stable | operator | `{"available": bool, "counters": dict|null}` |
| `route_eligibility` | `dict \| null` | stable | operator | §5.2 |
| `routes` | `dict` | stable | operator | `RouteStats.snapshot()`, bounded |
| `runtime_events` | `dict \| null` | unstable | debug/internal | `EventBuffer.snapshot()`, §5.3 |
| `runtime_state` | `str` | stable | operator | Enum `.value` |
| `schema_version` | `int` | stable | programmatic | Constant `SCHEMA_VERSION` |
| `snapshot_at` | `str` | stable | operator | ISO-8601 UTC, injectable clock |
| `startup_health` | `dict \| null` | stable | operator | §5.1 |
| `startup_timestamp` | `str \| null` | stable | operator | ISO-8601 wall-clock or `null` |
| `uptime_seconds` | `float \| null` | stable | operator | Computed from monotonic clock |

Stability labels:
- **stable** — shape and semantics are locked for `schema_version` 1. Changes require a version bump.
- **unstable** — shape may evolve across minor releases without a version bump. Consumers must tolerate added keys and changed detail structure.
- **reserved** — key is allocated but always `null` until the corresponding subsystem is implemented.


## 4. Breaking vs Additive Changes

### 4.1 Changes that require a `schema_version` bump

- Removing a top-level key.
- Changing a top-level key's type (e.g., `null` → `dict` for a key currently documented as `null`-only).
- Changing the meaning of a stable field's value (e.g., redefining how `uptime_seconds` is computed).
- Removing or renaming a key from a stable nested structure (e.g., removing `failed_adapter_ids` from `route_eligibility.skipped` entries).
- Changing the type of a stable nested field.

### 4.2 Changes that do NOT require a bump

- Adding a new top-level key (snapshot consumers must ignore unknown keys).
- Adding a new key to an unstable section (e.g., a new `RuntimeEventType` value, a new key inside `runtime_events.events[].detail`).
- Changing a `null`-only field to a non-null value when the type was already documented as `T | null` (e.g., populating `live_health` when health polling is implemented).
- Adding a new key to a stable section's dict, provided the addition is optional and existing keys retain their semantics.
- Changing internal implementation without affecting the observable shape.


## 5. Field Semantics

### 5.1 `startup_health` vs `live_health`

`startup_health` carries the runtime health state computed during startup classification (Contract 56). It reflects the aggregate adapter health assessment made at boot and is **not automatically refreshed** by post-start health polling.

`live_health` is reserved for a future `RuntimeHealth` aggregate that would be populated by active health polling. Until that integration exists, the field is always `null`.

**Operators must not assume `startup_health` represents real-time adapter health.** Adapter-level health values within the `adapters` dict come from the adapter's `_last_health` attribute (set during build/startup), not from live `health_check()` calls.


### 5.2 `route_eligibility`

`route_eligibility` exposes the outcome of route eligibility analysis performed during startup. It is a static snapshot of which routes were configured, registered, disabled, skipped, or unavailable at assembly time.

**Structure:**

```
{
  "configured":   [str],       // Route IDs declared in config
  "registered":   [str],       // Route IDs with successfully built adapters
  "disabled":     [str],       // Route IDs explicitly disabled
  "skipped": [                   // Routes skipped due to adapter failure
    {
      "failed_adapter_ids": [str],
      "reason":             str,
      "route_id":           str,
    }
  ],
  "unavailable": [               // Routes whose adapter was never built
    {
      "missing_adapter_ids": [str],
      "reason":              str,
      "route_id":            str,
    }
  ],
}
```

**Semantics:**

- `route_eligibility` distinguishes **config validity** (the route was correctly declared) from **operational readiness** (the route's adapters were successfully built and started).
- `skipped` routes had their source adapter fail during build/start — the route is configured but cannot operate.
- `unavailable` routes reference adapters that were never built (e.g., due to missing transport dependency).
- `route_eligibility` **does not imply dynamic routing**. It is a diagnostic surface that explains the static route map established at startup. It does not trigger route reconfiguration, dynamic failover, or runtime routing decisions.
- The field is `null` when no eligibility analysis was performed (e.g., minimal app without route wiring).


### 5.3 `runtime_events`

`runtime_events` exposes the bounded, in-memory event buffer that records runtime lifecycle state transitions.

**Structure:**

```
{
  "count":   int,        // Current number of events in buffer
  "maxlen":  int,        // Maximum buffer capacity (default 256)
  "events": [
    {
      "detail":     {str: any},     // Sorted keys, truncated values
      "event_type": str,            // Lowercase string from RuntimeEventType
      "sequence":   int,            // Monotonically increasing, 0-based
      "timestamp":  float,          // Monotonic seconds (not wall-clock)
    }
  ]
}
```

**Event types** (`RuntimeEventType` enum):

| Value | When emitted |
|-------|-------------|
| `state_transition` | Runtime state changes |
| `adapter_started` | Adapter start succeeds |
| `adapter_start_failed` | Adapter start fails |
| `adapter_stopped` | Adapter stops |
| `startup_classified` | Startup health outcome determined |
| `route_skipped` | Route skipped due to adapter failure |
| `route_unavailable` | Route unavailable due to missing adapter |

**Semantics:**

- `runtime_events` is an **unstable/debug** surface. Its shape, event types, and detail keys may be extended without a `schema_version` bump.
- It is a **bounded in-memory diagnostics buffer** backed by a `collections.deque` with fixed `maxlen` (default 256). When the buffer is full, the oldest events are discarded.
- It is **not** a durable audit log, event bus, or pub/sub system. Events are not persisted across restarts.
- Timestamps are **monotonic** (not wall-clock). Use `snapshot_at` and `startup_timestamp` for wall-clock times.
- Detail values are truncated at 256 characters. Event detail dicts have sorted keys.
- The field is `null` when no event buffer is wired to the app.


## 6. Structural Requirements

### 6.1 Deterministic ordering

Every dict in the snapshot — top-level, adapter entries, route entries, event details, nested sub-dicts — has keys in **alphabetical sorted order**. This is enforced by `_sorted_dict()` and is guaranteed for all stable and unstable fields.

`json.dumps(snapshot, sort_keys=True)` must produce identical output for identical runtime state with identical clock inputs.

### 6.2 JSON-safety

Every value is one of: `dict`, `list`, `str`, `int`, `float`, `bool`, `None`. No SDK objects, no custom types, no secrets. `json.dumps()` must succeed without a custom encoder.

Secret patterns (tokens, API keys, passwords) are stripped by `_sanitize_error()` in build failure entries. Adapter configs are never introspected.

### 6.3 Boundedness

Collections are capped:

| Collection | Cap constant | Value |
|-----------|-------------|-------|
| Adapter entries | `_MAX_ADAPTERS` | 256 |
| Route entries | `_MAX_ROUTES` | 1024 |
| Build failures | `_MAX_BUILD_FAILURES` | 64 |
| Error strings | `_MAX_ERROR_DETAIL_LEN` | 512 |
| Event detail strings | `_MAX_DETAIL_VALUE_LEN` | 256 |
| Runtime events | `DEFAULT_EVENT_BUFFER_MAXLEN` | 256 |

When a collection exceeds its cap, entries beyond the cap (in sorted order for adapters/routes, FIFO for events) are silently excluded.

### 6.4 Graceful degradation

If an optional subsystem (capacity, accounting, replay, health state, boot summary, event buffer, route eligibility) is absent or raises during snapshot, the corresponding field reports `null` rather than propagating the exception. The snapshot always succeeds.


## 7. Test Alignment

The following test suites validate conformance to this contract:

| Test file | Coverage |
|-----------|----------|
| `tests/test_runtime_snapshot.py` | Determinism, JSON-safety, sanitisation, boundedness, schema version, health state tolerance, startup/uptime |
| `tests/test_snapshot_schema_stability.py` | Top-level key set validation, deterministic ordering, bounded exports, malformed adapter resilience, replay/capacity consistency, accounting schema |
| `tests/test_snapshot_stress.py` | Large route/adapter tables, repeated snapshot determinism, failing/partially-initialised adapters, replay pressure, capacity exhaustion, secret safety at scale |
| `tests/test_runtime_events.py` | EventBuffer emit/bound/snapshot, RuntimeEvent frozen/to_dict, RuntimeEventType str-enum, route_eligibility integration, runtime_events integration, deterministic key ordering with new keys |

The authoritative top-level key set is defined in `_EXPECTED_RUNTIME_SNAPSHOT_TOP_KEYS` within `test_snapshot_schema_stability.py`. Any new top-level key must be added there.

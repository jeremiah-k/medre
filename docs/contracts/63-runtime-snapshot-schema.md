# Contract 63 — Runtime Snapshot Schema

**Status:** Active
**Scope:** Normative specification for the runtime snapshot shape, section structure, field semantics, stability classification, versioning policy, and structural guarantees.
**Audience:** Runtime builders, adapter authors, operators, test authors, documentation agents.
**References:** Contract 47 (Runtime Assembly), Contract 48 (Runtime Observability), Contract 56 (Runtime Supervision), Contract 29 (Diagnostics).

Every agent or document that references the MEDRE runtime snapshot shape, field stability, or extension rules must defer to this contract.


## 1. Non-goals

- Adding new snapshot fields or changing runtime behaviour.
- Implementing live health polling, dynamic routing, event persistence, or supervisor restarts.
- Changing adapter, route, or diagnostics behaviour.
- Replacing the diagnostics snapshot (`build_diagnostics_snapshot`) — that is a separate surface documented in Contract 29.
- Preserving flat-schema migration from schema_version 1.
- Changing the `schema_version` constant during pre-release.


## 2. Schema Version

The snapshot carries a top-level `schema_version` integer. **Current version: `1`.**

### Pre-release version lock policy

During the pre-release phase, `schema_version` is **frozen at `1`**. There are no external consumers of the snapshot schema, so version bumps would be noise. Internal breaking changes to the snapshot shape are reflected by updating tests and documentation (including this contract) but **do not** increment `schema_version`.

When MEDRE reaches its first stable release:
- `schema_version` will be set to the shape that ships in that release.
- From that point forward, breaking changes to the top-level shape will require a version bump, following the rules in §4.

Additive or unstable/debug changes (§6) never require a bump, neither during pre-release nor after.


## 3. Top-level Shape

The snapshot is structured into **intentional sections** that separate stable operator-facing data from unstable/debug internals. 15 top-level keys are always present.

Keys appear in **alphabetical order** (deterministic serialisation).

| Key | Type | Stability | Audience | Contents |
|-----|------|-----------|----------|----------|
| `accounting` | `dict \| null` | stable | operator | `RuntimeAccounting.snapshot()` |
| `adapters` | `dict` | stable | operator | `_snapshot_adapter()` per adapter |
| `capacity` | `dict \| null` | stable | operator | `CapacityController.snapshot()` |
| `diagnostics` | `dict` | unstable | debug/internal | §5.5 |
| `health` | `dict` | stable/reserved | operator | §5.1 |
| `identity` | `dict` | reserved | operator | Reserved for future runtime identity metadata (see §5.6) |
| `lifecycle` | `dict` | stable | operator | §5.3 |
| `limits` | `dict` | stable | operator | `RuntimeLimits` dataclass fields |
| `persistence` | `dict` | reserved | operator | Reserved for future durable-storage status (see §5.7) |
| `replay` | `dict` | stable | operator | `{"available": bool, "counters": dict|null}` |
| `routes` | `dict` | stable | operator | §5.4 |
| `schema_version` | `int` | stable | programmatic | Constant `SCHEMA_VERSION` (currently `1`) |
| `snapshot_at` | `str` | stable | operator | ISO-8601 UTC, injectable clock |
| `startup` | `dict` | stable | operator | §5.2 |
| `unstable` | `dict` | unstable/reserved | debug/internal | Reserved for debug/internal data (see §5.8) |

Stability labels:
- **stable** — shape and semantics are locked for the current version. Changes require a version bump (post-release) or test/doc updates (pre-release).
- **unstable** — shape may evolve across minor releases without a version bump. Consumers must tolerate added keys and changed detail structure.
- **reserved** — key/section is allocated but always empty/null until the corresponding subsystem is implemented.


## 4. Breaking vs Additive Changes

### 4.1 Changes that require a `schema_version` bump

- Removing a top-level key or section.
- Removing or renaming a key from a stable nested structure (e.g., removing `failed_adapter_ids` from `routes.eligibility.skipped` entries, or renaming `routes.readiness` to `routes.build_readiness`).
- Changing a top-level or stable nested key's type.
- Changing the meaning of a stable field's value (e.g., redefining how `uptime_seconds` is computed).
- Adding a new required (non-optional) key to a stable section.

### 4.2 Changes that do NOT require a bump

- Adding a new top-level key (snapshot consumers must ignore unknown keys).
- Adding a new key to an unstable section (e.g., a new key inside `diagnostics.runtime_events.events[].detail`).
- Changing a `null`-only field to a non-null value when the type was already documented as `T | null`.
- Adding a new key to a stable section's dict, provided the addition is optional and existing keys retain their semantics.
- Changing internal implementation without affecting the observable shape.


## 5. Section Semantics

### 5.1 `health`

```
{
  "live_health": null,
  "live_refresh": false,
  "scope": "startup"
}
```

- `live_health`: Reserved for a future `RuntimeHealth` aggregate that would be populated by active health polling. Until that integration exists, the field is always `null`.
- `live_refresh`: Always `false`. The health assessment is computed once during startup and not refreshed.
- `scope`: Always `"startup"`. The health assessment is a startup-derived snapshot, not live runtime health.

**Operators must not assume `startup.startup_health` represents real-time adapter health.** The `scope` field makes this explicit: this value was computed during startup and does not reflect post-startup adapter state changes.


### 5.2 `startup`

One-time boot classification and build failures.

```
{
  "boot_summary": {...} | null,
  "build_failures": [...],
  "live_refresh": false,
  "scope": "startup",
  "startup_health": {...} | null,
}
```

- `boot_summary`: From `BootSummary.to_dict()`. Null when no boot summary is wired.
- `build_failures`: Bounded list of adapter build failures (capped at `_MAX_BUILD_FAILURES`). Each entry has `adapter_id` and sanitized `error`.
- `live_refresh`: Always `false`. Startup outcome is computed once and never refreshed.
- `scope`: Always `"startup"`. All data in this section is derived during `MedreApp.start()` and is frozen after startup completes.
- `startup_health`: Carries the runtime health state computed during startup classification (Contract 56). Null when no health state is wired.


### 5.3 `lifecycle`

Runtime state transitions, per-adapter lifecycle states, and timing.

```
{
  "adapters": {adapter_id: str, ...},
  "live_refresh": false,
  "runtime_state": str,
  "scope": "process_local",
  "startup_timestamp": str | null,
  "uptime_seconds": float | null,
}
```

- `adapters`: Per-adapter lifecycle state mapping. Keys are adapter IDs (sorted alphabetically); values are `AdapterState` enum strings (`"initializing"`, `"ready"`, `"degraded"`, `"backpressured"`, `"disconnected"`, `"stopping"`, `"failed"`, `"stopped"`). Empty dict before startup.
- `live_refresh`: `false`. While `runtime_state` and `adapters` reflect the runtime's current in-process state at snapshot time, the scope is `process_local` rather than `live` because there is no periodic health polling loop refreshing these values.
- `runtime_state`: Current `RuntimeState` enum value as lowercase string.
- `scope`: Always `"process_local"`. Values reflect the runtime's in-memory state at the moment of the snapshot call. Not persisted across restarts.
- `startup_timestamp`: ISO-8601 wall-clock time set during `app.start()`, or null.
- `uptime_seconds`: Computed from monotonic clock, rounded to 6 decimal places, clamped to >= 0. Null before startup.


### 5.4 `routes`

Route delivery statistics, eligibility, per-route build readiness, and startup-derived readiness. Each sub-section carries explicit `scope` and `live_refresh` metadata so operators can distinguish build-time facts from startup-time facts from (future) live state.

```
{
  "build_readiness": {...} | null,
  "eligibility": {...} | null,
  "startup_readiness": {...} | null,
  "stats": {route_id: {...}},
}
```

#### 5.4.1 Scope and freshness metadata

Every route sub-section that represents a point-in-time observation carries two metadata fields:

- `scope`: `"build"` or `"startup"`. Indicates *when* the data was captured.
  - `"build"`: Computed during `MedreApp.build()`. Does not change after build. Represents build-time route registration outcomes.
  - `"startup"`: Computed after `MedreApp.start()` completes. Does not change after startup. Reflects adapter lifecycle states at startup time.
- `live_refresh`: Always `false` in the current schema. Reserved for future use when live health polling or dynamic routing is implemented. When `true`, the data would be refreshed from live runtime state rather than a point-in-time snapshot.

Operators **must not** assume that data with `live_refresh=false` reflects current runtime state after shutdown or post-startup adapter failures.

#### 5.4.2 `routes.eligibility`

Exposes the outcome of route eligibility analysis performed during startup. Scope: `build`.

```
{
  "configured":   [str],
  "disabled":     [str],
  "live_refresh": false,
  "registered":   [str],
  "scope":        "build",
  "degraded": [
    {
      "failed_adapter_ids": [str],
      "route_id":           str,
    }
  ],
  "skipped": [
    {
      "failed_adapter_ids": [str],
      "reason":             str,
      "route_id":           str,
    }
  ],
  "unavailable": [
    {
      "missing_adapter_ids": [str],
      "reason":              str,
      "route_id":            str,
    }
  ],
}
```

Semantics:
- `configured`: Config route IDs declared and enabled (not disabled).
- `registered`: Expanded route IDs with all source and target adapters built.
- `disabled`: Config route IDs explicitly disabled.
- `degraded`: Routes registered with partial target loss (some target adapters failed).
- `skipped`: Routes that could not be registered (source failed, or all targets failed).
- `unavailable`: Routes referencing adapters never in the configured set. Empty in normal operation (unknown refs raise `RouteValidationError`).
- `live_refresh`: Always `false`. Data is from build time and is not refreshed.
- `scope`: Always `"build"`. Data reflects build-time route registration outcomes.
- This is a **diagnostic surface**, not a trigger for dynamic routing.

#### 5.4.3 `routes.build_readiness`

Per-route operational state mapping from build time. Scope: `build`.

```
{
  "live_refresh": false,
  "scope":        "build",
  "states": {
    "route_id": "registered" | "disabled" | "degraded" | "skipped" | "unavailable",
    ...
  },
}
```

`states` values come from `RouteOperationalState` enum:

| State | When assigned |
|-------|-------------|
| `configured` | Route is enabled in config (initial state before build) |
| `registered` | Route successfully registered with all adapters built |
| `degraded` | Route registered but some target adapters failed to build, or some expanded routes skipped while others registered |
| `skipped` | Route could not register (source failed or all targets failed) |
| `unavailable` | Route references adapter IDs not in configured set |
| `disabled` | Route is explicitly disabled in configuration |

Keys are deterministically sorted. The mapping covers all config route IDs.

Expanded route IDs are mapped back to config route IDs using explicit provenance (no string-prefix inference). A config route that expands to multiple routes (e.g. `fan_out__0`, `fan_out__1`) maps to the worst state among its expansions.

`live_refresh` is always `false`; `scope` is always `"build"`. The data is frozen at build time and does not reflect post-startup adapter failures.

#### 5.4.4 `routes.startup_readiness`

Startup-derived route readiness based on adapter lifecycle states. This is computed **after** `MedreApp.start()` completes and reflects adapters that built successfully but failed to start. Scope: `startup`.

```
{
  "degraded": [
    {
      "route_id":           str,
      "failed_adapter_ids": [str],
    }
  ],
  "live_refresh": false,
  "readiness": {route_id: str},
  "scope":        "startup",
  "skipped": [
    {
      "failed_adapter_ids": [str],
      "reason":             str,
      "route_id":            str,
    }
  ],
}
```

Semantics:
- `readiness`: Per-config-route operational state derived from adapter lifecycle states. Keys are sorted config route IDs; values are `RouteOperationalState` enum strings.
- `degraded`: Expanded routes where some target adapters failed to start (but source and at least one target survived).
- `skipped`: Expanded routes where the source adapter failed to start (`source_adapter_start_failed`) or all target adapters failed to start (`no_surviving_targets_start_failed`).
- `live_refresh`: Always `false`. Data is frozen at startup time.
- `scope`: Always `"startup"`. Data reflects adapter lifecycle states after startup completes.

Rules:
- **Disabled** routes remain `disabled`.
- Routes already **skipped** at build time remain `skipped` (build eligibility is the source of truth for build failures).
- For routes that were registered or degraded at build time:
  - Source adapter `FAILED` → `skipped` (reason: `source_adapter_start_failed`).
  - Some target adapters `FAILED` but others `READY` → `degraded`.
  - All target adapters `FAILED` → `skipped` (reason: `no_surviving_targets_start_failed`).
  - Source and all targets `READY` → `registered`.
- Adapters in states other than `FAILED` or `READY` (e.g. `DEGRADED`, `BACKPRESSURED`) are treated as surviving.
- This is a **diagnostic surface**, not a trigger for dynamic routing or live health-aware routing.
- `null` before `MedreApp.start()` has been called.

#### 5.4.5 `routes.stats`

Per-route delivery counters from `RouteStats.snapshot()`, bounded at `_MAX_ROUTES`.


### 5.5 `adapters`

Per-adapter static metadata from `_snapshot_adapter()`. Each entry is a sorted dict with:

```
{
  "adapter_id":  str,
  "capabilities": {...},
  "health":       str,
  "platform":     str,
  "provenance":   "startup",
  "role":         str,
  "version":      str,
}
```

- `provenance`: Always `"startup"`. Adapter metadata (including `health`) is captured during build/startup from the adapter's `_last_health` attribute. It is **not** refreshed by live `health_check()` calls at runtime.
- `health`: Startup-derived. The value reflects the adapter's health state at the time of build/startup. Operators must not assume this represents current adapter health — use `lifecycle.adapters.{adapter_id}` for the current `AdapterState` lifecycle value.

**Operators must distinguish between:**
- `adapters.{id}.health` — startup-derived, static (from `_last_health`)
- `lifecycle.adapters.{id}` — process-local, current `AdapterState` (from `_adapter_states`)

These two values can diverge after startup if the adapter's lifecycle state changes but its health attribute is not refreshed.


### 5.6 `diagnostics`

Internal debug/diagnostic surfaces. Shape may change without a schema version bump.

```
{
  "live_refresh": true,
  "runtime_events": {...} | null,
  "scope": "process_local",
}
```

- `live_refresh`: `true`. The runtime event buffer grows as events are emitted during the process lifetime. Each snapshot call returns the current buffer contents.
- `runtime_events` exposes the bounded, in-memory event buffer:
- `scope`: Always `"process_local"`. Events are in-memory only and not persisted across restarts.

```
{
  "count":   int,
  "maxlen":  int,
  "events": [
    {
      "detail":     {str: any},
      "event_type": str,
      "sequence":   int,
      "timestamp":  float,
    }
  ]
}
```

Event types (`RuntimeEventType` enum): `state_transition`, `adapter_started`, `adapter_start_failed`, `adapter_stopped`, `startup_classified`, `route_skipped`, `route_unavailable`.

- Bounded in-memory buffer (`collections.deque`, default maxlen 256).
- Not a durable audit log. Events not persisted across restarts.
- Timestamps are monotonic (not wall-clock).
- Detail values truncated at 256 characters.
- Null when no event buffer is wired.


### 5.7 `identity`

Reserved section. Currently always an empty dict `{}`.

Purpose: future runtime identity metadata — node identity, signing keys, provenance chain, or platform identity material. The section is intentionally kept as a placeholder so that identity can be added without introducing a new top-level key.

The shape will be documented when the identity subsystem is implemented. Until then, consumers must treat this section as opaque and ignore its contents.


### 5.8 `persistence`

Reserved section. Currently always an empty dict `{}`.

Purpose: future durable-storage status — last-persisted event ID, storage health, queue depths, replay cursor positions. The section is intentionally kept as a placeholder so that persistence status can be added without introducing a new top-level key.

The shape will be documented when the persistence subsystem is implemented. Until then, consumers must treat this section as opaque and ignore its contents.


### 5.9 `unstable`

Reserved section for debug/internal data. Currently always an empty dict `{}`.

Purpose: carries unstable diagnostic or internal data that may evolve freely across releases without a schema version bump. Content is JSON-safe, bounded, and not intended for operator reliance.

Guidelines for unstable data:
- Keys must be added via `_sorted_dict()` for deterministic ordering.
- Values must be JSON-safe (no SDK objects, no secrets).
- Collections must be bounded.
- Consumers must tolerate arbitrary key additions and removals.
- No stability guarantee: shape may change at any time.


### 5.10 Provenance Summary

Operators must understand whether each diagnostic value is a one-time startup snapshot, a process-local value, or live-refreshed. The following table summarizes the provenance of each section:

| Section / Field | `scope` | `live_refresh` | Meaning |
|-----------------|---------|----------------|---------|
| `startup` | `"startup"` | `false` | Computed once during `MedreApp.start()`. Frozen after startup. |
| `startup.boot_summary` | `"startup"` | `false` | `BootSummary.to_dict()`. Immutable after creation. |
| `startup.build_failures` | `"startup"` | `false` | Build failures are immutable after build. |
| `startup.startup_health` | `"startup"` | `false` | `runtime_supervision_snapshot()` output from startup. |
| `health` | `"startup"` | `false` | Startup-derived health assessment. `live_health` is always `null`. |
| `health.live_health` | — | — | Always `null`. Reserved for future live health polling. |
| `lifecycle` | `"process_local"` | `false` | In-process state at snapshot time. Not persisted. |
| `lifecycle.adapters.{id}` | `"process_local"` | `false` | Current `AdapterState` from `_adapter_states` registry. |
| `lifecycle.uptime_seconds` | `"process_local"` | `false` | Computed from monotonic clock on each snapshot call. |
| `adapters.{id}.health` | `"startup"` (per-entry `provenance`) | — | Static `_last_health` from build/startup. Not refreshed. |
| `adapters.{id}.provenance` | — | — | Always `"startup"`. Indicates adapter metadata is startup-derived. |
| `diagnostics` | `"process_local"` | `true` | Event buffer grows during runtime. |
| `routes.build_readiness` | `"build"` | `false` | Frozen at build time. |
| `routes.eligibility` | `"build"` | `false` | Frozen at build time. |
| `routes.startup_readiness` | `"startup"` | `false` | Frozen after startup. |
| `routes.stats` | (none) | (none) | Live counters from `RouteStats.snapshot()`. |
| `accounting` | (none) | (none) | Live counters from `RuntimeAccounting.snapshot()`. |
| `capacity` | (none) | (none) | Live gauges from `CapacityController.snapshot()`. |

**Operator guidance:**
- Values with `scope="startup"` and `live_refresh=false` **do not reflect post-startup state changes**. If an adapter crashes after startup, `adapters.{id}.health` and `startup.startup_health` will still show the startup-time values.
- For current adapter lifecycle state, check `lifecycle.adapters.{id}` — this is process-local and reflects the in-memory state registry at snapshot time.
- `diagnostics.runtime_events` carries the event history that recorded state transitions; it is the most complete record of what happened after startup.


## 6. Structural Requirements

### 6.1 Deterministic ordering

Every dict in the snapshot — top-level, section internals, adapter entries, route entries, event details, nested sub-dicts — has keys in **alphabetical sorted order**. This is enforced by `_sorted_dict()` and is guaranteed for all stable and unstable fields.

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

If an optional subsystem (capacity, accounting, replay, health state, boot summary, event buffer, route eligibility) is absent or raises during snapshot, the corresponding field reports `null` or empty rather than propagating the exception. The snapshot always succeeds.


## 7. Route Registration Types

### 7.1 `RouteRegistrationResult`

Frozen dataclass (not a list):

```python
@dataclass(frozen=True)
class RouteRegistrationResult:
    registered_routes: tuple[Route, ...]
    eligibility: RouteEligibility
    provenance: dict[str, str]  # expanded_route_id → config_route_id
```

The `provenance` field provides explicit mapping from expanded route IDs back to their config route origins, replacing any string-prefix inference.

### 7.2 `RouteEligibility`

```python
@dataclass(frozen=True)
class RouteEligibility:
    configured: tuple[str, ...]
    registered: tuple[str, ...]
    disabled: tuple[str, ...]
    degraded: tuple[DegradedRoute, ...]
    skipped: tuple[SkippedRoute, ...]
    unavailable: tuple[UnavailableRoute, ...]
    route_states: dict[str, RouteOperationalState]
```

All tuple fields contain deterministically sorted route IDs. `route_states` keys are sorted.

### 7.3 `RouteOperationalState`

Enum with values: `configured`, `registered`, `degraded`, `skipped`, `unavailable`, `disabled`.


### 7.4 `ExpandedRouteProvenance`

Frozen dataclass carrying the triple `(config_route_id, expanded_route_id, Route)` for explicit expansion mapping.

```python
@dataclass(frozen=True)
class ExpandedRouteProvenance:
    config_route_id: str
    expanded_route_id: str
    route: Route
```


### 7.5 `RouteStartupReadiness`

Frozen dataclass with startup-derived route readiness:

```python
@dataclass(frozen=True)
class RouteStartupReadiness:
    route_states: dict[str, RouteOperationalState]
    degraded: tuple[DegradedRoute, ...]
    skipped: tuple[SkippedRoute, ...]
```

Computed by `compute_startup_readiness()` after `MedreApp.start()` completes. Derives per-route states from adapter lifecycle states, independent of build-time eligibility.


## 8. Test Alignment

The following test suites validate conformance to this contract:

| Test file | Coverage |
|-----------|----------|
| `tests/test_runtime_snapshot.py` | Determinism, JSON-safety, sanitisation, boundedness, schema version, health state tolerance, startup/uptime, section structure |
| `tests/test_snapshot_schema_stability.py` | Top-level key set validation, deterministic ordering, bounded exports, malformed adapter resilience, replay/capacity consistency, accounting schema |
| `tests/test_snapshot_stress.py` | Large route/adapter tables, repeated snapshot determinism, failing/partially-initialised adapters, replay pressure, capacity exhaustion, secret safety at scale |
| `tests/test_runtime_events.py` | EventBuffer emit/bound/snapshot, RuntimeEvent frozen/to_dict, RuntimeEventType str-enum, route_eligibility integration, runtime_events integration, deterministic key ordering |
| `tests/test_route_eligibility.py` | RouteOperationalState enum, DegradedRoute, per-route readiness states, sorted ordering, mixed scenarios, frozen dataclass validation, prefix collision, bidirectional/multi-source provenance, startup readiness (source fail, partial targets, all targets fail, mixed) |
| `tests/test_runtime_builder.py` | Degraded route validation, builder integration with route eligibility |

The authoritative top-level key set is defined in `_EXPECTED_RUNTIME_SNAPSHOT_TOP_KEYS` within `test_snapshot_schema_stability.py`. Any new top-level key must be added there.

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
- Preserving flat-schema compatibility from schema_version 1.


## 2. Schema Version

The snapshot carries a top-level `schema_version` integer. **Current version: `3`.**

`schema_version` is bumped only when a **breaking change** is introduced to the top-level shape (§4). Additive or unstable/debug changes (§6) do not require a bump.

Version 3 is a **breaking rename** from version 2: `routes.readiness` is renamed to `routes.build_readiness` and wrapped in a metadata envelope; `scope` and `live_refresh` metadata fields are added to `routes.eligibility`, `routes.build_readiness`, and `routes.startup_readiness`.

Version 2 was a **breaking restructure** from version 1: the flat key layout was replaced with intentional sections. There was no backward-compatibility mapping.


## 3. Top-level Shape

The snapshot is structured into **intentional sections** that separate stable operator-facing data from unstable/debug internals. 16 top-level keys are always present.

Keys appear in **alphabetical order** (deterministic serialisation).

| Key | Type | Stability | Audience | Contents |
|-----|------|-----------|----------|----------|
| `schema_version` | `int` | stable | programmatic | Constant `SCHEMA_VERSION` (currently `3`) |
| `snapshot_at` | `str` | stable | operator | ISO-8601 UTC, injectable clock |
| `accounting` | `dict \| null` | stable | operator | `RuntimeAccounting.snapshot()` |
| `adapters` | `dict` | stable | operator | `_snapshot_adapter()` per adapter |
| `capacity` | `dict \| null` | stable | operator | `CapacityController.snapshot()` |
| `diagnostics` | `dict` | mixed | debug/internal | §5.5 |
| `health` | `dict` | stable/reserved | operator | §5.1 |
| `identity` | `dict` | stable | operator | Reserved for future identity metadata |
| `lifecycle` | `dict` | stable | operator | §5.3 |
| `limits` | `dict` | stable | operator | `RuntimeLimits` dataclass fields |
| `persistence` | `dict` | stable | operator | Reserved for future durable-storage surface |
| `replay` | `dict` | stable | operator | `{"available": bool, "counters": dict|null}` |
| `routes` | `dict` | stable | operator | §5.4 |
| `startup` | `dict` | stable | operator | §5.2 |
| `unstable` | `dict` | unstable | debug/internal | Reserved for future unstable data |

Stability labels:
- **stable** — shape and semantics are locked for `schema_version` 3. Changes require a version bump.
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
  "live_health": null
}
```

`live_health` is reserved for a future `RuntimeHealth` aggregate that would be populated by active health polling. Until that integration exists, the field is always `null`.

**Operators must not assume `startup.startup_health` represents real-time adapter health.**


### 5.2 `startup`

One-time boot classification and build failures.

```
{
  "boot_summary": {...} | null,
  "build_failures": [...],
  "startup_health": {...} | null,
}
```

- `boot_summary`: From `BootSummary.to_dict()`. Null when no boot summary is wired.
- `build_failures`: Bounded list of adapter build failures (capped at `_MAX_BUILD_FAILURES`). Each entry has `adapter_id` and sanitized `error`.
- `startup_health`: Carries the runtime health state computed during startup classification (Contract 56). Null when no health state is wired.


### 5.3 `lifecycle`

Runtime state transitions, per-adapter lifecycle states, and timing.

```
{
  "adapters": {adapter_id: str, ...},
  "runtime_state": str,
  "startup_timestamp": str | null,
  "uptime_seconds": float | null,
}
```

- `adapters`: Per-adapter lifecycle state mapping. Keys are adapter IDs (sorted alphabetically); values are `AdapterState` enum strings (`"initializing"`, `"ready"`, `"degraded"`, `"backpressured"`, `"disconnected"`, `"stopping"`, `"failed"`, `"stopped"`). Empty dict before startup.
- `runtime_state`: Current `RuntimeState` enum value as lowercase string.
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
| `active` | Reserved for future use — not assigned by current logic |
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


### 5.5 `diagnostics`

Internal debug/diagnostic surfaces. Shape may change without a schema version bump.

```
{
  "runtime_events": {...} | null,
}
```

`runtime_events` exposes the bounded, in-memory event buffer:

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

Enum with values: `configured`, `registered`, `active`, `degraded`, `skipped`, `unavailable`, `disabled`.


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

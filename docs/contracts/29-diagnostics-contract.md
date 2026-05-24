# Diagnostics Contract

> Contract version: 1
> Last updated: 2026-05-09
> Track: 9 (Transport Capability Contracts)
> Supersedes: Nothing. Formalizes findings from contracts 21, 27, 28.
> Status: Contract. Documents the locked-in diagnostics contract for beta.

This document defines the contractual shape, safety guarantees, and behavioral semantics of the diagnostics subsystem across all four adapter families and the runtime snapshot layer. It consolidates what was audited in contract 27 into an explicit contract that beta consumers can rely on.

This is a contract document. No runtime redesign, adapter abstraction, or cross-transport normalization changes are proposed.

## 1. Scope

- Common outer diagnostic keys present on all adapters.
- Adapter-specific nested diagnostics and their safety guarantees.
- Runtime snapshot layer: `RuntimeSnapshot` and `capture_runtime_snapshot`.
- Deterministic serialization contract.
- Observational-only caveat.
- Helper location and API surface.

## 2. Non-goals

- Adding new diagnostic fields or metrics.
- Normalizing transport-specific diagnostics that reflect genuine transport differences.
- Building health polling, circuit breakers, or auto-degrade logic.
- Changing any adapter behavior or diagnostics shape.

## 3. Common Outer Keys

Every adapter exposes `health_check()` returning `AdapterInfo` and `diagnostics()` returning a plain dict. The following keys appear on all four adapters:

| Key                           | Type          | Present On | Notes                                                                                  |
| ----------------------------- | ------------- | ---------- | -------------------------------------------------------------------------------------- |
| `connected`                   | `bool`        | All four   | Directly or nested in session sub-dict                                                 |
| `health`                      | `str`         | All four   | One of: `"healthy"`, `"degraded"`, `"failed"`, `"unknown"`, `"starting"`, `"stopping"` |
| `mode`                        | `str`         | All four   | Transport mode: `"fake"`, `"tcp"`, `"serial"`, `"ble"`, or `"reticulum"` as applicable |
| `reconnecting`                | `bool`        | All four   | Indicates active reconnect loop                                                        |
| `reconnect_attempts`          | `int`         | All four   | Current count, bounded to max 10                                                       |
| `last_error`                  | `str or None` | All four   | `str()` of last exception. `None` when no error.                                       |
| `transient_delivery_failures` | `int`         | All four   | Cumulative since adapter start                                                         |
| `permanent_delivery_failures` | `int`         | All four   | Cumulative since adapter start                                                         |

**Matrix note:** `last_error` appears as `last_sync_error` in the session diagnostics dataclass.

**Meshtastic/MeshCore note:** The session-level diagnostics are exposed via a `session` sub-dict within the adapter-level diagnostics dict.

**LXMF note:** Session diagnostics are exposed directly via the `LxmfSessionDiagnostics` frozen dataclass. The adapter itself does not currently layer its own diagnostics dict on top.

## 4. Adapter-Specific Nested Diagnostics

### 4.1 Matrix

Source: `matrix/adapter.py` `diagnostics()`, `matrix/session.py` `MatrixSessionDiagnostics`

| Key                         | Type            | Notes                                                  |
| --------------------------- | --------------- | ------------------------------------------------------ |
| `logged_in`                 | `bool`          | nio login restoration state                            |
| `sync_task_running`         | `bool`          | Background sync loop alive                             |
| `store_path_configured`     | `bool`          | E2EE crypto store path present                         |
| `device_id_configured`      | `bool`          | E2EE device ID present                                 |
| `encryption_mode`           | `str`           | `"plaintext"`, `"e2ee_optional"`, or `"e2ee_required"` |
| `crypto_enabled`            | `bool`          | vodozemac loaded and crypto active                     |
| `last_crypto_error`         | `str or None`   | Last E2EE failure reason                               |
| `encrypted_room_seen`       | `bool`          | At least one encrypted room encountered                |
| `undecryptable_event_count` | `int`           | Messages that failed decryption                        |
| `sync_running`              | `bool`          | Sync loop state                                        |
| `last_successful_sync`      | `float or None` | Epoch timestamp                                        |
| `crypto_store_loaded`       | `bool`          | Crypto database loaded                                 |
| `encrypted_room_count`      | `int`           | Count only, no room IDs exposed                        |
| `plaintext_room_count`      | `int`           | Count only, no room IDs exposed                        |

### 4.2 Meshtastic

Source: `meshtastic/adapter.py` `diagnostics()`, `meshtastic/session.py` `MeshtasticSessionDiagnostics`

Adapter-level keys:

| Key                  | Type  | Notes                                  |
| -------------------- | ----- | -------------------------------------- |
| `adapter_id`         | `str` | Adapter identifier                     |
| `platform`           | `str` | Always `"meshtastic"`                  |
| `connection_type`    | `str` | `"fake"`, `"tcp"`, `"serial"`, `"ble"` |
| `queue_pending`      | `int` | Outbound queue depth                   |
| `queue_total_sent`   | `int` | Lifetime sends via queue               |
| `queue_total_failed` | `int` | Lifetime failures via queue            |
| `background_tasks`   | `int` | Tracked asyncio tasks                  |

Session sub-dict keys (`session.*`):

| Key                        | Type            | Notes                         |
| -------------------------- | --------------- | ----------------------------- |
| `session.node_id`          | `str or None`   | Local node number             |
| `session.channel_count`    | `int`           | Configured channels           |
| `session.last_packet_time` | `float or None` | Epoch of last received packet |

### 4.3 MeshCore

Source: `meshcore/adapter.py` `diagnostics()`, `meshcore/session.py` `_SessionDiagnostics`

| Key                 | Type          | Notes                                  |
| ------------------- | ------------- | -------------------------------------- |
| `adapter_id`        | `str`         | Adapter identifier                     |
| `platform`          | `str`         | Always `"meshcore"`                    |
| `mode`              | `str`         | `"fake"`, `"tcp"`, `"serial"`, `"ble"` |
| `last_message_time` | `str or None` | ISO 8601 timestamp                     |
| `peer_count`        | `int or None` | Known mesh peers                       |

### 4.4 LXMF

Source: `lxmf/session.py` `LxmfSessionDiagnostics`

| Key                      | Type           | Notes                                |
| ------------------------ | -------------- | ------------------------------------ |
| `router_running`         | `bool`         | LXMRouter is active                  |
| `last_message_time`      | `str or None`  | ISO 8601 timestamp                   |
| `known_path_count`       | `int or None`  | Reticulum path table entries         |
| `propagation_enabled`    | `bool or None` | LXMF propagation node state          |
| `pending_delivery_count` | `int or None`  | Outbound deliveries not yet terminal |
| `mode`                   | `str`          | `"fake"` or `"reticulum"`            |

## 5. Safety Guarantees: No Secrets, No Raw SDK Objects

All four adapters enforce these guarantees through their diagnostics dataclasses and methods:

### 5.1 No Secrets

No adapter exposes access tokens, private keys, identity material, or authentication credentials through any diagnostic path. The specific guarantees per adapter:

| Adapter    | Guarantee                                                                               | Mechanism                                                                                   |
| ---------- | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Matrix     | No secrets, access tokens, keys, or private device material                             | Frozen dataclass; token/key fields never included; room names/IDs excluded from room counts |
| Meshtastic | No secrets, private keys, raw protobuf dumps, or sensitive radio identifiers            | Frozen dataclass; node_id is public info; no packet payloads                                |
| MeshCore   | No secrets, private keys, or raw SDK internals                                          | Plain dict copy; no pubkey material                                                         |
| LXMF       | No secrets, private keys, identity material, raw RNS/LXMF objects, or unsafe peer dumps | Frozen dataclass; identity hashes not included; mode is string                              |

### 5.2 No Raw SDK Objects

No adapter exposes the underlying SDK client object, connection handle, or crypto material through diagnostics. Verified: no protobuf objects, no `LXMessage` instances, no nio client references, no `Event` objects leak through any diagnostic path.

### 5.3 No Binary Wire Formats

All exceptions are converted to `str()` before inclusion. All complex objects are reduced to plain dicts with JSON-safe types (str, int, float, bool, None, nested dicts/lists thereof).

## 6. Deterministic Serialization Contract

### 6.1 Runtime Snapshot Layer

Location: `src/medre/core/supervision/diagnostics.py`

The `RuntimeSnapshot` dataclass and `capture_runtime_snapshot()` pure function enforce deterministic serialization:

- `to_dict()` recursively sorts all keys alphabetically via `_sorted_dict()`.
- Adapter entries are sorted by `adapter_id` before inclusion.
- Output is stable for `json.dumps(sort_keys=True)`.
- Lists of dicts have each dict sorted recursively.

The snapshot contains these top-level sections:

| Section                  | Source                                          |
| ------------------------ | ----------------------------------------------- |
| `adapters`               | Sorted list of normalized adapter health dicts  |
| `renderer_registry`      | `RenderingPipeline.status_summary()`            |
| `event_bus_status`       | `EventBus.status_summary()`                     |
| `storage_backend_status` | Placeholder `{"status": "not_yet_implemented"}` |
| `replay_backend_status`  | Placeholder `{"status": "not_yet_implemented"}` |
| `queue_status`           | Placeholder `{"status": "not_yet_implemented"}` |
| `backpressure_status`    | Placeholder `{"status": "not_yet_implemented"}` |
| `task_status`            | Placeholder `{"status": "not_yet_implemented"}` |

### 6.2 Adapter-Level Serialization

Adapters that return plain dicts from `diagnostics()` (Matrix, Meshtastic, MeshCore) do not enforce key ordering themselves. Deterministic ordering is the responsibility of the `RuntimeSnapshot.to_dict()` layer when adapter diagnostics are aggregated. Individual adapter `diagnostics()` output may have arbitrary key order.

### 6.3 Health Normalization Layer

Location: `src/medre/core/supervision/health.py`

`normalize_adapter_health()` projects `AdapterInfo` + optional `AdapterState` into a JSON-safe dict with a fixed health vocabulary of six strings: `"healthy"`, `"degraded"`, `"failed"`, `"unknown"`, `"starting"`, `"stopping"`. This is a read-only projection. It does not add health polling, circuit breakers, or auto-degrade logic.

## 7. Observational-Only Caveat

**Diagnostics are snapshot observations, not authoritative state.**

This applies to all diagnostic paths: adapter `diagnostics()`, session diagnostics dataclasses, and `RuntimeSnapshot`. The implications:

1. `connected: true` does not guarantee the next operation will succeed. The transport may disconnect between the snapshot and the next operation.
2. `reconnect_attempts: 0` does not mean the connection is stable. It means no reconnect loop is currently running.
3. Delivery failure counters are cumulative since adapter start, not per-message receipts. Use the delivery receipt pipeline (contract 21, `phase-1-limitations.md` Track 3) for authoritative delivery state.
4. Diagnostics are not a substitute for delivery receipts. They serve operational monitoring and debugging only.
5. The `RuntimeSnapshot` is frozen at capture time. It does not update if underlying state changes after construction.

## 8. Helper Location and API Surface

### 8.1 Adapter-Level Diagnostics

All adapters expose `diagnostics() -> dict[str, Any]` defined on `AdapterContract`:

```text
AdapterContract.diagnostics() -> dict[str, Any]   # abstract
```

Each adapter implements this by composing session-level diagnostics with adapter-level counters.

### 8.2 Session-Level Diagnostics

All four sessions expose `diagnostics()` returning either a frozen dataclass or a plain dict copy:

| Session           | Return Type                                       | Safety                    |
| ----------------- | ------------------------------------------------- | ------------------------- |
| MatrixSession     | `MatrixSessionDiagnostics` (frozen dataclass)     | Immutable, no SDK refs    |
| MeshtasticSession | `MeshtasticSessionDiagnostics` (frozen dataclass) | Immutable, no SDK refs    |
| MeshCoreSession   | `dict[str, Any]` (plain dict copy)                | Mutable copy, no SDK refs |
| LxmfSession       | `LxmfSessionDiagnostics` (frozen dataclass)       | Immutable, no SDK refs    |

### 8.3 Runtime Snapshot

```yaml
capture_runtime_snapshot(
    adapter_healths: Sequence[_AdapterHealthInput] | None = None,
    renderer_pipeline: Any | None = None,
    event_bus: Any | None = None,
    storage_status: dict[str, Any] | None = None,
    replay_status: dict[str, Any] | None = None,
) -> RuntimeSnapshot
```

Pure function. Does not start polls, trigger health checks, or modify supplied objects. Tested in `tests/test_runtime_diagnostics.py` (553+ lines).

### 8.4 Health Normalization

```yaml
normalize_adapter_health(
    info: AdapterInfo,
    lifecycle_state: AdapterState | None = None,
    adapter: Any | None = None,
    details: dict[str, object] | None = None,
) -> dict[str, Any]
```

Returns a JSON-safe dict with `adapter_id`, `platform`, `health`, `mode`, and optionally `capabilities` and `details`. Tested in `tests/test_runtime_diagnostics.py` and `tests/test_capabilities.py`.

## 9. Contractual Guarantees for Beta

1. **No secret leakage through any diagnostic path.** Frozen dataclasses and explicit docstring guarantees enforce this.
2. **No SDK object leakage.** Verified across all four adapters.
3. **Deterministic serialization when consumed through `RuntimeSnapshot.to_dict()`.**
4. **Observational-only semantics.** Diagnostics never modify state.
5. **Stable common keys.** The eight common keys listed in section 3 are contractual and will not be removed without a contract version bump.
6. **Adapter-specific keys may grow.** New transport-specific diagnostic keys may be added. Existing keys will not be removed or have their types changed without a contract version bump.

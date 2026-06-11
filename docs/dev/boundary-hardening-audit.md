# Adapter Boundary Hardening Audit

> **Status**: Post-implementation audit (adapter-lifecycle-parity wave, synced with source/tests/docs).
> **Scope**: Runtime correctness at the adapter–SDK boundary. Not lifecycle authority, not capability declarations, not SDK parity.
> **Authority**: `docs/spec/adapter-runtime.md` (normative), `src/medre/core/contracts/adapter.py` (code contracts).
> **Concurrency**: Other workers own lifecycle, evidence, capability, and SDK parity docs. This document does not touch those files.

---

## Methodology

Audited all four adapter boundaries (Matrix, Meshtastic, MeshCore, LXMF) by reading:

- `src/medre/core/contracts/adapter.py` — contracts and value types.
- `src/medre/adapters/{matrix,meshtastic,meshcore,lxmf}/adapter.py` — adapter entry points.
- `src/medre/adapters/{matrix,meshtastic,meshcore,lxmf}/session.py` — session boundaries.
- `tests/test_adapter_boundary.py`, `tests/test_adapter_conformance.py`, `tests/test_operational_boundaries.py`.
- Transport-specific boundary tests (`test_*_boundaries.py`, `test_*_operational_boundaries.py`).
- `docs/spec/adapter-runtime.md` §9.2 (field semantics), §14 (session boundaries).
- `docs/dev/lifecycle-authority-audit.md` (status vocabulary).

Each boundary vector is classified as:

| Classification                | Meaning                                                             |
| ----------------------------- | ------------------------------------------------------------------- |
| **Implemented**               | Protection exists and tests prove it.                               |
| **Partial**                   | Protection exists but has known gaps or insufficient test coverage. |
| **Missing**                   | No protection; observable correctness risk.                         |
| **Intentionally unsupported** | Not applicable to this transport by design.                         |

---

## Boundary Vectors

### 1. Malformed SDK Data (Inbound)

What happens when the SDK delivers structurally invalid data to the adapter callback?

| Adapter        | Status          | Evidence                                                                                                                                                                                                                                                                          |
| -------------- | --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Matrix**     | **Implemented** | `_normalize_event` uses `getattr` with defaults for all nio fields. `_on_room_message` coerces `room_id`, `sender` to `str(event.get(…, ""))`. Missing `room_id` / `sender` becomes `""` and hits allowlist/self-message filters. Codec handles missing `source` dict gracefully. |
| **Meshtastic** | **Implemented** | `_on_packet` → classifier rejects malformed packets (action `drop`, reason `malformed`). Codec `decode` extracts fields with `packet.get` defaults. Session `_on_receive` forwards raw SDK dicts unchanged; classifier is the safety net.                                         |
| **MeshCore**   | **Implemented** | `_on_sdk_event` normalizes SDK Event objects: `isinstance(event, dict)` / `hasattr(event, "payload")` branches handle both dict and object forms, falling back to `{}`. Classifier drops unknown categories.                                                                      |
| **LXMF**       | **Implemented** | `_normalise_inbound_message` handles missing/invalid `source_hash`, `content` (bytes→utf-8 with `errors="replace"`), `title`, `fields`. Returns empty strings for missing fields.                                                                                                 |

### 2. Missing Identifiers (Outbound)

What happens when `deliver()` receives a `RenderingResult` with missing target identifiers?

| Adapter        | Status          | Evidence                                                                                                                                                                                         |
| -------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Matrix**     | **Implemented** | `deliver()` checks `room_id`: if both `result.target_channel` and `payload["room_id"]` are absent/empty, raises `AdapterPermanentError("no room_id")`. Test: `test_matrix_no_room_id_permanent`. |
| **Meshtastic** | **Implemented** | `channel_index` falls back to `self._config.default_channel` when `payload.get("channel_index")` is not `int`. No hard error — uses configured default.                                          |
| **MeshCore**   | **Implemented** | `deliver()` in fake mode returns `None`. In real mode, `contact_id` and `text` are extracted with `str()` coercion; `channel_index` is validated as `int` (excluding `bool`).                    |
| **LXMF**       | **Implemented** | `deliver()` checks `not content and not title` → returns `None` (silent no-op). `destination_hash` is extracted from payload and passed to session.                                              |

### 3. Duplicate Inbound Events

What prevents the same inbound event from entering the pipeline twice?

| Adapter        | Status          | Evidence                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| -------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Matrix**     | **Implemented** | Self-message suppression (`sender == config.user_id`). MEDRE-origin envelope loop suppression (envelope `source_adapter == self.adapter_id`). Startup history suppression (`session.is_live` guard). Stale-event filter in base `publish_inbound` (timestamp < start time).                                                                                                                                                                                                                                                                                                                                                                             |
| **Meshtastic** | **Implemented** | Self-echo suppression via classifier (`REASON_SELF_ECHO`). Startup backlog suppression via `should_suppress_startup_backlog` with `rxTime` check. Stale-event filter in base `publish_inbound`.                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| **MeshCore**   | **Implemented** | Classifier filters by category (`text` only) and drops ACKs. Stale-event filter in base `publish_inbound`. **Inbound dedup**: `OrderedDict` keyed by `(sender_id, packet_id, channel_index, text)`, where `sender_id` is derived from the packet's `pubkey_prefix` — native identity plus text content. Exact replays suppressed; same identity with different content allowed. Bounded to `_DEDUP_MAX_SIZE` (1024) with true LRU eviction (`move_to_end` on hit, `popitem(last=False)` when full). Dedup skipped when `packet_id` is `None` (no reliable native identity). Cleared on stop/start boundaries. Tests: `test_boundary_hardening.py` (G1). |
| **LXMF**       | **Implemented** | Classifier filters `is_ack` and non-`text` categories. Stale-event filter in base `publish_inbound`. **Inbound dedup**: `OrderedDict` keyed by `(message_id, content)` — message hash plus content. Exact replays suppressed; same ID with different content allowed. Bounded to `_DEDUP_MAX_SIZE` (1024) with true LRU eviction (`move_to_end` on hit, `popitem(last=False)` when full). Dedup skipped when `message_id` is `None`. Cleared on stop/start boundaries. Tests: `test_boundary_hardening.py` (G2).                                                                                                                                        |

### 4. Stale Callbacks

What prevents callbacks from a previous start/stop cycle from affecting a new session?

| Adapter        | Status          | Evidence                                                                                                                                                                                                                                                                                                                                                                                     |
| -------------- | --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Matrix**     | **Implemented** | Session `_closed` flag; double-start guard (`_client is not None and not _closed`). Adapter clears `_session = None` on stop. New start creates fresh `MatrixSession`. `_live_sync_started` resets on session start.                                                                                                                                                                         |
| **Meshtastic** | **Implemented** | `_started` flag gates `_on_packet` (sync, SDK thread) and `_on_packet_async` (async). Both check `self._started` before processing. Session `_stop_requested` prevents reconnect loops after stop.                                                                                                                                                                                           |
| **MeshCore**   | **Implemented** | `_started` flag checked in `_on_message` (explicit `if not self._started: return` guard plus `self.ctx is None` guard). Session `_stop_requested` flag prevents reconnect after stop. Subscriptions unsubscribed in `stop()`. Dedup cleared on start via `_reset_inbound_counters()` (which also resets counters) and on stop via `self._inbound_dedup.clear()` in `MeshCoreAdapter.stop()`. |
| **LXMF**       | **Implemented** | `_on_lxmf_delivery` checks `self._stop_requested or not self._started`. `_loop` reference cleared in `stop()`, so late SDK callbacks on Reticulum threads are dropped (`loop is None`). `_message_callback` set to `None` in `stop()`.                                                                                                                                                       |

### 5. Reconnect Races

What happens if the SDK reconnects while the adapter is processing an inbound event?

| Adapter        | Status          | Evidence                                                                                                                                                                                                                                          |
| -------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Matrix**     | **Implemented** | Sync loop runs in a single `_sync_task`; reconnect is sequential (inner `while` loop with backoff). No concurrent sync tasks. `_stop_requested` breaks the loop. `_reconnect_attempts` reset on success.                                          |
| **Meshtastic** | **Implemented** | `notify_connection_lost` creates a single `_reconnect_task`. Guard: `if self._stop_requested or self._reconnecting: return`. Old client closed before new one created. `_subscribed` flag prevents duplicate subscriptions.                       |
| **MeshCore**   | **Implemented** | `_on_disconnect_event` creates a single `_reconnect_task` (guard: `self._reconnect_task is None or self._reconnect_task.done()`). `_connect_real` unsubscribes old subscriptions before subscribing new. `_stop_requested` breaks reconnect loop. |
| **LXMF**       | **Implemented** | Reconnect loop guarded by `_stop_requested`. Reticulum singleton reuse (`RNS.Reticulum.get_instance()`) prevents double-init. `_teardown_sdk` clears router/identity references before reconnect attempt.                                         |

### 6. Shutdown Races (stop() vs. inbound processing)

What happens when `stop()` is called while inbound events are being processed?

| Adapter        | Status          | Evidence                                                                                                                                                                                                                                                                                                                                                                                                                      |
| -------------- | --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Matrix**     | **Implemented** | `stop()` calls `session.stop()` which cancels `_sync_task`. `_message_callback` remains set but `_on_room_message` checks `self.ctx is None` (not cleared until after session stop). Session `_closed = True` stops nio event processing.                                                                                                                                                                                     |
| **Meshtastic** | **Implemented** | `_started` cleared **before** draining. `_on_packet` (called from SDK thread) checks `_started` early, rejecting late packets. `_drain_background_tasks` cancels tracked inbound futures and awaits background tasks with bounded timeout. Detached tasks get observer callbacks.                                                                                                                                             |
| **MeshCore**   | **Implemented** | `_started` cleared **before** draining (`self._started = False` at top of `stop()`). `_on_message` checks `if not self._started: return` before task creation — closes the race window between drain completing and session unsubscribing. `_drain_background_tasks` cancels and awaits with `return_exceptions=True`. Session `stop()` calls `_unsubscribe_all` then `disconnect`. Tests: `test_boundary_hardening.py` (G3). |
| **LXMF**       | **Implemented** | `_on_lxmf_delivery` checks `_stop_requested` and `_started` on the Reticulum thread before scheduling. `loop.call_soon_threadsafe` bridges to event loop; `stop()` sets `_loop = None` and `_message_callback = None` so late bridges are dropped. `_drain_background_tasks` cancels tracked tasks.                                                                                                                           |

### 7. Callback-After-Stop

What prevents SDK callbacks from firing after `stop()` returns?

| Adapter        | Status          | Evidence                                                                                                                                                                                                |
| -------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Matrix**     | **Implemented** | Session `stop()` cancels sync task, clears `_closed = True`. Nio client is closed (`_client.close()`). Callback is registered on the client object which is discarded.                                  |
| **Meshtastic** | **Implemented** | `_unsubscribe_callbacks` unsubscribes from pubsub. Session `_client = None` after close. `_started = False` gates `_on_packet`. Remaining inbound futures are cancelled in `_drain_background_tasks`.   |
| **MeshCore**   | **Implemented** | `_unsubscribe_all` unsubscribes all SDK event subscriptions. `_meshcore = None` after disconnect. `_started = False` set before drain (at top of `stop()`).                                             |
| **LXMF**       | **Implemented** | `stop()` clears `_router`, `_identity`, `_message_callback`, and `_loop`. Late Reticulum callbacks see `_stop_requested` or `None` refs. `_on_delivery_state` returns early when `_started` is `False`. |

### 8. Metadata Namespace Rules

Do adapters follow `metadata[<transport>]` namespacing per §9.2?

| Adapter        | Status          | Evidence                                                                                                                                                                                                        |
| -------------- | --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Matrix**     | **Implemented** | `metadata=MappingProxyType({"matrix": MappingProxyType({"txn_id": txn_id})})`. Keys under `"matrix"` namespace. Test: `TestPipelineMetadataIgnoredForLifecycle` proves pipeline ignores metadata for lifecycle. |
| **Meshtastic** | **Implemented** | `metadata=MappingProxyType({"meshtastic": {"channel_index": channel_index}})`. Keys under `"meshtastic"` namespace. `delivery_status="enqueued"`.                                                               |
| **MeshCore**   | **Implemented** | `metadata=MappingProxyType({"meshcore": MappingProxyType({"local_acceptance": True})})`. Keys under `"meshcore"` namespace. `delivery_status="sent"` (synchronous local acceptance).                            |
| **LXMF**       | **Implemented** | `metadata=MappingProxyType({"lxmf": MappingProxyType({"delivery_state": …, "delivery_method": …})})`. Keys under `"lxmf"` namespace. `delivery_status="sent"` (honest local acceptance).                        |

### 9. `AdapterDeliveryResult` Contract Adherence

Do adapters return correct `delivery_status` values and comply with §9.1/§9.2 semantics?

| Adapter        | Status          | Evidence                                                                                                                                                                            |
| -------------- | --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Matrix**     | **Implemented** | `delivery_status="sent"` (default). Returns `native_message_id=event_id` from homeserver. `native_channel_id=room_id`. Test: `test_deliver_accepts_rendering_result`.               |
| **Meshtastic** | **Implemented** | `delivery_status="enqueued"`. `native_message_id=None` (queue-based). `delivery_note="locally enqueued"`. Queue drain later produces `OutboundNativeRefRecord` with real native ID. |
| **MeshCore**   | **Implemented** | `delivery_status="sent"` (default). Returns `native_message_id` (expected_ack hex or message_id hex) for DMs, `None` for channel sends. Honest `delivery_note`.                     |
| **LXMF**       | **Implemented** | `delivery_status="sent"` (default, honest local acceptance). Returns `native_message_id` (LXMF message hash hex). `delivery_note="accepted by LXMRouter — async delivery pending"`. |

### 10. Exception Normalization

Do adapters normalize transport-specific errors to `AdapterSendError`/`AdapterPermanentError`?

| Adapter        | Status          | Evidence                                                                                                                                                                                                                                                                                                              |
| -------------- | --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Matrix**     | **Implemented** | `MatrixSendError` → `AdapterSendError(transient=True)` or `AdapterPermanentError`. `_NioRateLimitError` → `AdapterSendError(transient=True)`. `_is_transient_error` classifies network errors. `CancelledError` re-raised. Tests: `test_matrix_send_error_converted_to_transient`, `TestErrorClassificationPipeline`. |
| **Meshtastic** | **Implemented** | `MeshtasticSendError` → `AdapterSendError` / `AdapterPermanentError`. `TimeoutError`, `ConnectionError`, `OSError` → `AdapterSendError(transient=True)`. `CancelledError` re-raised. Tests: `test_meshtastic_timeout_transient`, `test_meshtastic_send_error_converted_to_transient`.                                 |
| **MeshCore**   | **Implemented** | `MeshCoreSendError` → `AdapterSendError` / `AdapterPermanentError`. Same transient catch pattern. Tests: `test_meshcore_timeout_transient`, `test_meshcore_send_error_converted_to_transient`.                                                                                                                        |
| **LXMF**       | **Implemented** | `LxmfSendError` → `AdapterSendError` / `AdapterPermanentError`. Same transient catch pattern. Tests: `test_lxmf_timeout_transient`, `test_lxmf_send_error_converted_to_transient`.                                                                                                                                    |

Transport-specific `*SendError` classes do **not** inherit from `AdapterSendError` (verified by `test_transport_send_errors_not_in_classify`). This is correct per spec §9.4.

### 11. Diagnostics / Health Check Plain-Data Boundaries

Do `diagnostics()` and `health_check()` return only JSON-safe primitives?

| Adapter        | Status          | Evidence                                                                                                                                                                                         |
| -------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Matrix**     | **Implemented** | `diagnostics()` returns `dict[str, Any]` with bool/int/str/None values. `health_check()` returns `AdapterInfo` (frozen dataclass with JSON-safe fields). Test: `test_adapter_info_is_json_safe`. |
| **Meshtastic** | **Implemented** | `diagnostics()` constructs plain dict from scalar values. Session `diagnostics()` returns `MeshtasticSessionDiagnostics` (frozen dataclass).                                                     |
| **MeshCore**   | **Implemented** | `diagnostics()` returns `dict[str, Any]` with scalar values. Session uses `_SessionDiagnostics` dataclass. Uses `sanitize_diagnostic_mapping` for session diagnostics.                           |
| **LXMF**       | **Implemented** | `diagnostics()` returns `LxmfSessionDiagnostics` (frozen dataclass). All fields are str/int/bool/None. `delivery_state_counts()` returns `dict[str, int]`.                                       |

### 12. Resource Release Guarantees

Do adapters release all resources (SDK clients, tasks, futures) on `stop()`?

| Adapter        | Status                  | Evidence                                                                                                                                                                                                                                                                         |
| -------------- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Matrix**     | **Bounded best-effort** | `stop()` calls `session.stop()` which cancels sync task and closes nio client. Adapter sets `_session = None`.                                                                                                                                                                   |
| **Meshtastic** | **Bounded best-effort** | `stop()` clears `_started`, cancels drain task, drains background tasks (bounded timeout with detach observer), calls `session.stop()` which closes client and unsubscribes pubsub. `_session = None`.                                                                           |
| **MeshCore**   | **Bounded best-effort** | `stop()` sets `_started = False` before drain, drains background tasks with `return_exceptions=True`, calls `session.stop()` which unsubscribes, stops auto-fetching, disconnects SDK client. `_session = None`.                                                                 |
| **LXMF**       | **Bounded best-effort** | `stop()` sets `_started = False` before drain, cancels announce/reconnect tasks, unsubscribes callbacks, tears down SDK (router/identity/reticulum references), clears outbound tracking, sets `_message_callback = None`, `_loop = None`. `_inbound_dedup` cleared in `stop()`. |

**Resource release note:** MeshCore and LXMF implement bounded best-effort drain for normal background publish tasks via `_drain_background_tasks()` with a timeout and `return_exceptions=True`. Python `asyncio.Task.cancel()` is a cooperative request, not a hard kill; tasks can suppress `CancelledError`. The drain is therefore best-effort, not a strict guaranteed release for cancellation-resistant tasks. Hardening against long-running or cancellation-resistant observation/drain tasks remains future resilience work.

### 13. Reconnect Suppression During Shutdown

Do adapters suppress reconnect attempts after `stop()` is called?

| Adapter        | Status          | Evidence                                                                                                                            |
| -------------- | --------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| **Matrix**     | **Implemented** | `_stop_requested` flag checked in `_sync_with_reconnect` loop. On `True`, sets `_sync_failure` and returns.                         |
| **Meshtastic** | **Implemented** | `_stop_requested` checked in `_reconnect_loop`. `notify_connection_lost` returns early if `_stop_requested`.                        |
| **MeshCore**   | **Implemented** | `_stop_requested` checked in `_reconnect_loop`. `_on_disconnect_event` returns early if `_stop_requested`.                          |
| **LXMF**       | **Implemented** | `_stop_requested` checked in reconnect loop. `_on_lxmf_delivery` and `_on_delivery_state_update` return early if `_stop_requested`. |

---

## Summary Matrix

| Boundary Vector             | Matrix                 | Meshtastic             | MeshCore               | LXMF                   |
| --------------------------- | ---------------------- | ---------------------- | ---------------------- | ---------------------- |
| 1. Malformed SDK data       | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         |
| 2. Missing identifiers      | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         |
| 3. Duplicate inbound events | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         |
| 4. Stale callbacks          | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         |
| 5. Reconnect races          | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         |
| 6. Shutdown races           | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         |
| 7. Callback-after-stop      | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         |
| 8. Metadata namespace       | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         |
| 9. Delivery result contract | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         |
| 10. Exception normalization | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         |
| 11. Diagnostics plain-data  | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         |
| 12. Resource release        | ✅ Bounded best-effort | ✅ Bounded best-effort | ✅ Bounded best-effort | ✅ Bounded best-effort |
| 13. Reconnect suppression   | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         | ✅ Implemented         |

**Overall**: Vectors 1 through 11 and 13 are fully implemented with test coverage (48 of 52 adapter-vector cells). Vector 12 (resource release) is scoped as bounded best-effort drain for normal background tasks; strict guaranteed release for cancellation-resistant tasks is future resilience work (see vector 12 note). All previously identified gaps (G1, G2, G3) have been resolved.

---

## Resolved Gaps

### G1. ~~MeshCore: No inbound dedup by message identity~~ — RESOLVED

**Status**: **Completed.** Implemented and tested.

**Implementation**: MeshCoreAdapter maintains `_inbound_dedup`, an `OrderedDict` keyed by `(sender_id, packet_id, channel_index, text)`, where `sender_id` is derived from the packet's `pubkey_prefix`. The dedup key includes native identity (sender ID derived from pubkey prefix + packet ID + channel index) plus text content, ensuring exact replays of the same packet are suppressed while distinct payloads sharing the same packet ID are both processed. The dict is bounded to `_DEDUP_MAX_SIZE` (1024 entries) with true LRU semantics: `move_to_end()` on hit promotes the entry, and `popitem(last=False)` evicts the least-recently-seen entry when the cap is exceeded. When `packet_id` is `None` (no reliable native identity), adapter-level dedup is skipped entirely. The dedup dict is cleared on start via `_reset_inbound_counters()` (which also zeroes counters) and on stop via `self._inbound_dedup.clear()` in `MeshCoreAdapter.stop()`.

**Tests**: `test_boundary_hardening.py` — `test_meshcore_simulate_inbound_deduplicates_identical_packets`, `test_meshcore_simulate_inbound_allows_different_packets`, `test_meshcore_dedup_resets_on_restart`, `test_meshcore_on_message_deduplicates_via_callback`, `test_meshcore_dedup_evicts_oldest_at_capacity`. `test_meshcore_lifecycle_boundaries.py` — `test_meshcore_stop_clears_dedup_without_restart`, `test_meshcore_simulate_inbound_raises_before_start`, `test_meshcore_simulate_inbound_publishes_while_started`, `test_meshcore_simulate_inbound_silent_after_stop`, `test_meshcore_restart_allows_same_packet_to_publish`.

### G2. ~~LXMF: No inbound dedup by message hash~~ — RESOLVED

**Status**: **Completed.** Implemented and tested.

**Implementation**: LxmfAdapter maintains `_inbound_dedup`, an `OrderedDict` keyed by `(message_id, content)`. The dedup key includes the LXMF message hash plus content, ensuring exact replays are suppressed while same-ID-different-content messages are allowed. The dict is bounded to `_DEDUP_MAX_SIZE` (1024 entries) with true LRU semantics: `move_to_end()` on hit, `popitem(last=False)` eviction when full. When `message_id` is `None`, adapter-level dedup is skipped. The dedup dict is cleared on stop (`stop()` calls `self._inbound_dedup.clear()`) and on start (`start()` calls `self._inbound_dedup.clear()`).

**Tests**: `test_boundary_hardening.py` — `test_lxmf_simulate_inbound_deduplicates_identical_messages`, `test_lxmf_simulate_inbound_allows_different_messages`, `test_lxmf_dedup_resets_on_restart`, `test_lxmf_on_packet_deduplicates_via_callback`, `test_lxmf_dedup_evicts_oldest_at_capacity`.

### G3. ~~MeshCore: Shutdown race between `_on_message` task creation and `_started` guard~~ — RESOLVED

**Status**: **Completed.** Implemented and tested.

**Implementation**: `MeshCoreAdapter.stop()` sets `self._started = False` at the top, before calling `_drain_background_tasks()`. `_on_message` (sync callback from session) checks `if not self._started: return` before creating any `asyncio.create_task`. This closes the race window where a message arriving between drain completion and session unsubscribe could create a task against a stopped adapter. `LxmfAdapter._on_packet` also checks `if not self._started: return` before task creation, matching the same pattern.

**Tests**: `test_boundary_hardening.py` — `test_meshcore_on_message_drops_after_started_false`, `test_meshcore_stop_gates_callbacks_before_drain`, `test_lxmf_on_packet_drops_after_stop`, `test_lxmf_stop_gates_callbacks_before_drain`.

---

## Prioritized Hardening Tests

Ranked by correctness risk (highest first). Gaps G1, G2, and G3 are resolved; their tests now pass.

### P1 — MeshCore shutdown race guard (§G3) — IMPLEMENTED & TESTED

**File**: `tests/test_boundary_hardening.py`

**Tests**: `test_meshcore_on_message_drops_after_started_false`, `test_meshcore_stop_gates_callbacks_before_drain`.

**Status**: Passing. `_on_message` checks `if not self._started: return` before task creation. Late callbacks after drain are silently dropped.

### P2 — MeshCore inbound dedup (§G1) — IMPLEMENTED & TESTED

**File**: `tests/test_boundary_hardening.py`

**Tests**: `test_meshcore_simulate_inbound_deduplicates_identical_packets`, `test_meshcore_simulate_inbound_allows_different_packets`, `test_meshcore_dedup_resets_on_restart`, `test_meshcore_on_message_deduplicates_via_callback`, `test_meshcore_dedup_evicts_oldest_at_capacity`.

**Status**: Passing. Duplicate exact replays suppressed; same identity with different content allowed; LRU cap-bounded eviction verified.

### P3 — LXMF inbound dedup (§G2) — IMPLEMENTED & TESTED

**File**: `tests/test_boundary_hardening.py`

**Tests**: `test_lxmf_simulate_inbound_deduplicates_identical_messages`, `test_lxmf_simulate_inbound_allows_different_messages`, `test_lxmf_dedup_resets_on_restart`, `test_lxmf_on_packet_deduplicates_via_callback`, `test_lxmf_dedup_evicts_oldest_at_capacity`, `test_lxmf_on_packet_drops_after_stop`.

**Status**: Passing. Duplicate exact replays suppressed; same ID with different content allowed; LRU cap-bounded eviction verified.

### P4 — ~~Cross-adapter callback-after-stop verification~~ — IMPLEMENTED & TESTED

**File**: `tests/test_adapter_post_stop_ingress.py`

**Tests**: `test_fake_adapters_drop_simulate_inbound_after_stop`, `test_meshtastic_adapter_drops_simulate_inbound_after_stop`, `test_matrix_adapter_drops_room_callback_after_stop`.

**Status**: Passing. `test_fake_adapters_drop_simulate_inbound_after_stop`,
`test_meshtastic_adapter_drops_simulate_inbound_after_stop`, and
`test_matrix_adapter_drops_room_callback_after_stop` confirm Fake Matrix,
Meshtastic, MeshCore, and LXMF retain `ctx` after `stop()` but drop post-stop
`simulate_inbound` calls and Matrix room callbacks. Real Meshtastic
`simulate_inbound` and Matrix room callback coverage verify the same boundary
on production adapters.

### P5 — Meshtastic callback-after-stop with in-flight futures (medium risk)

**File**: `tests/test_meshtastic_boundaries.py`

**Test**: Start MeshtasticAdapter in fake mode. Inject an inbound packet that schedules `_on_packet_async` via `run_coroutine_threadsafe`. Immediately call `stop()`. Assert the in-flight future is cancelled and no event is published.

**Current coverage**: `_drain_background_tasks` cancels futures, but this specific race is not independently tested.

### P6 — ~~Matrix rate-limit dedup across retries~~ — IMPLEMENTED & TESTED

**File**: `tests/test_matrix_boundaries.py`

**Test**: `TestMatrixDeliveryNioResponseHardening::test_rate_limit_retry_reuses_transaction_id`.

**Status**: Passing. A rate-limited `deliver()` call raises `AdapterSendError(transient=True)` for the pipeline retry worker. Retrying the same `RenderingResult` reuses the same Matrix `tx_id`, and the successful delivery metadata reports that same transaction ID.

### P7 — ~~LXMF delivery-state callback after adapter stop~~ — IMPLEMENTED & TESTED

**File**: `tests/test_adapter_post_stop_ingress.py`

**Test**: `test_lxmf_adapter_drops_delivery_state_callback_after_stop`.

**Status**: Passing. `LxmfAdapter._on_delivery_state` returns immediately when `_started` is `False`, so terminal delivery callbacks after adapter stop produce no log or other side effect even though `ctx` is retained.

---

## Testing Rules Relevant to This Audit

From `docs/dev/testing.md`:

1. **File size limits**: Target < 1,200 lines per test file; hard ceiling 1,500 lines. Before adding tests to any boundary file, check line count. Split by behavioral domain if approaching the cap.
2. **No fixed sleeps**: Use `wait_until()` from `tests/helpers/async_utils.py` or deterministic hooks (`asyncio.Event`, mock callbacks). Never `asyncio.sleep(fixed)`.
3. **Async mocking**: Match mock type to production call shape. `await client.close()` → `AsyncMock`. `client.add_event_callback(fn)` → `MagicMock`. Wrong mock type causes `RuntimeWarning: coroutine was never awaited`.
4. **CancelledError handling**: Async fakes that simulate cancellation must raise `asyncio.CancelledError`, not return a value. Test cancellation paths catch `CancelledError` explicitly.
5. **Patch target policy**: Patch at the lookup site, not the definition site. E.g., `@patch("medre.adapters.matrix.adapter.HAS_NIO")`, not `@patch("medre.adapters.matrix.HAS_NIO")`.
6. **Test evidence honesty**: Label tests by tier (`fake_pipeline`, `fake_adapter_callback`, etc.). Never overclaim evidence level.
7. **Coroutine leak prevention**: When faking scheduler helpers, close passed coroutines before returning to prevent "coroutine was never awaited" warnings.

---

## Intentionally Unsupported

These protections are **not required** and are documented as intentionally absent:

| Protection                                                  | Why Not Applicable                                                                                                                                                          |
| ----------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix: queue-based `delivery_status="enqueued"`            | Matrix is synchronous — `room_send` returns `event_id` immediately. No queue path.                                                                                          |
| Meshtastic: reply threading via Matrix-style `m.relates_to` | Meshtastic uses protobuf `reply_id` field, not relation-based threading.                                                                                                    |
| LXMF: synchronous delivery confirmation                     | LXMF is inherently async multi-hop. `deliver()` returns local acceptance state; delivery progression is tracked via callbacks.                                              |
| MeshCore: ACK-based delivery confirmation                   | MeshCore has no ACK protocol for channel messages. DM `expected_ack` is captured as native ID but not confirmed.                                                            |
| All adapters: pipeline-level retry scheduling               | Per spec §3.4, adapters do not implement their own durable retry loops. Pipeline owns retry. Bounded transport-level retries (3 attempts) are permitted within `deliver()`. |

---

## Dedup Rollback Semantics (MeshCore and LXMF)

The inbound dedup mechanism (vector 3) for MeshCore and LXMF suppresses only
successful exact replays. It does not poison the dedup table with entries from
failed decode or publish attempts.

### Dedup insertion and rollback semantics

The two inbound code paths handle dedup key insertion differently:

**`simulate_inbound` path** (all-async, used in tests and fake adapters):
Dedup keys are inserted only after both decode and `publish_inbound` succeed.
If either step fails (codec error, publish exception), the dedup key is never
inserted. A retry of the same message will not be suppressed by a previous
failed attempt, because the failed attempt left no dedup entry.

**SDK callback path** (`_on_message` / `_on_packet`, sync-to-async bridge):
Dedup keys are inserted after successful decode but _before_ the async
publish. This guards the async publish window against concurrent duplicates:
if the same packet arrives again while the first is still in flight, the
dedup check in `_on_message` suppresses it. If the async publish fails
(`_on_message_async` / `_on_packet_async` raises), the dedup key is rolled
back via `pop(dedup_key, None)` so that redelivery is not suppressed. If
decode fails, the dedup key is never inserted (same as the simulate_inbound
path).

This means:

- **Successful exact replays are suppressed.** The same `(sender_id,
packet_id, channel_index, text)` tuple (MeshCore; `sender_id` derived
  from the packet's `pubkey_prefix`) or `(message_id, content)`
  tuple (LXMF) that was already published will not be published again.
- **Decode failures never insert a dedup key.** Both paths agree: if decode
  raises, no dedup key is recorded, so a retry gets another chance.
- **Publish failures do not poison dedup state.** In `simulate_inbound`, the
  key is never inserted on publish failure. In the SDK callback path, the key
  is temporarily inserted after decode for in-flight duplicate suppression,
  then rolled back on async publish failure. Neither path retains the key
  after a failed publish.
- **CancelledError bypasses the rollback handler but does not leak dedup
  entries.** The SDK callback rollback uses `except Exception` to catch
  publish failures. `CancelledError` is `BaseException`, not `Exception`, so
  it is not caught by the rollback handler. During graceful stop,
  `_drain_background_tasks` cancels in-flight tasks, which raises
  `CancelledError` and skips rollback. This is safe because lifecycle
  teardown (the `stop()` / `start()` boundary) clears the entire dedup dict
  via `_inbound_dedup.clear()`, so any key left by a cancelled task is
  removed. This does not imply durable authority; the clear is
  adapter-local and in-memory only.
- **Dedup is adapter-local replay suppression, not durable/core delivery
  authority.** The `OrderedDict` lives in the adapter instance. It is cleared
  on stop/start boundaries. It is never persisted to storage. Its purpose is
  preventing the same SDK callback from producing duplicate canonical events
  within a single adapter session, not providing cross-restart delivery
  guarantees.

### MeshCore: native replay identity limitation

MeshCore packets do not always carry a reliable `packet_id`. When `packet_id`
is `None`, the adapter skips dedup entirely for that packet. This is correct:
without a native identity, there is no basis for distinguishing a replay from
a genuinely new message with identical content from the same sender. Packets
with `packet_id = None` are always passed through, accepting the risk of
occasional duplicates in exchange for not suppressing legitimate messages.

### LXMF: message hash as identity

LXMF messages carry a `message_id` (hash) provided by the LXMRouter. When
`message_id` is `None`, dedup is skipped, matching the same principle as
MeshCore. In practice, LXMRouter always provides a hash, so this path is
defensive.

### LRU eviction semantics

Both adapters use true LRU eviction via Python's `OrderedDict`:

- `move_to_end(key)` on dedup hit, promoting the accessed entry to most-recent.
- `popitem(last=False)` when the dict exceeds `_DEDUP_MAX_SIZE` (1024 entries),
  evicting the least-recently-seen entry.

This is true LRU, not FIFO or approximate. Entries that are accessed
repeatedly stay in the dict; stale entries age out from the front.

### Resource release is bounded best-effort

The dedup `OrderedDict` is an in-memory data structure bounded to 1024 entries.
Its memory footprint is deterministic and small (a few tens of KB at most). It
is cleared on stop and on start, so it does not leak across adapter lifecycles.
No overclaim is made about dedup providing resource release guarantees beyond
this bounded best-effort scope.

---

## Related Documents

- `docs/spec/adapter-runtime.md` — normative adapter runtime specification.
- `docs/spec/state-machines.md` — receipt/outbox transition graphs.
- `docs/spec/delivery-lifecycle.md` — delivery lifecycle vocabulary.
- `docs/dev/lifecycle-authority-audit.md` — status vocabulary audit guide.
- `docs/dev/testing.md` — test conventions and tier definitions.
- `docs/dev/adapter-authoring.md` — adapter authoring guide.

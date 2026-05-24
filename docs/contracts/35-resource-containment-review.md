# Resource Containment Review

> Contract version: 1
> Last updated: 2026-05-10
> Track: 9 (Transport Capability Contracts)
> Supplements: Contracts 31 (session boundary), 33 (failure taxonomy).
> Status: Review. Documents resource containment risks and mitigations per session.

This document reviews the resource containment posture of each transport
session: task cleanup, retry budgets, callback registration/deregistration,
store/session retention, and potential resource leaks. It identifies risks
and records mitigations already in place.

This is a review document. No runtime redesign is proposed.

## 1. Scope

- Per-session resource ownership and cleanup.
- Task lifecycle management (sync tasks, reconnect tasks, announce tasks).
- Retry budgets and their exhaustion behavior.
- Callback registration and deregistration.
- Store/crypto persistence and cleanup.
- Session retention patterns (memory growth risks).

## 2. Non-goals

- Proposing connection pooling or session reuse patterns.
- Redesigning retry or reconnect policies.
- Adding new resource management features.

## 3. MatrixSession

### 3.1 Task Management

| Resource          | Type           | Owner   | Cleanup                                                                                                 |
| ----------------- | -------------- | ------- | ------------------------------------------------------------------------------------------------------- |
| `_sync_task`      | `asyncio.Task` | Session | Cancelled and awaited in `stop()` with configurable timeout (default 5s). Cleared to `None` after stop. |
| `nio.AsyncClient` | SDK client     | Session | `stop_sync_forever()` + `close()` called in `stop()`. Set to `None`.                                    |

**Risk assessment:**

- **Sync task leak on stop timeout.** If `stop()` times out waiting for the sync task, the task reference is cleared (`self._sync_task = None`), but the underlying coroutine may still be running. The `_stop_requested` flag prevents reconnect loops. The task will eventually complete when `sync_forever` returns or raises.
  - **Mitigation:** `_stop_requested` guard in `_sync_with_reconnect`. Even if the task lingers, it will not create new reconnect cycles.
  - **Residual risk:** The nio client may hold open HTTP connections even after `close()` if the sync task is still running. This is a nio-level concern, not MEDRE's.

- **Double-start protection.** `start()` checks `self._client is not None and not self._closed`. If a second `start()` is called, it logs a warning and returns. No new task is created.
  - **Mitigation:** Explicit guard. No resource leak.

- **Partial startup cleanup.** If startup fails mid-way (e.g., login fails after client creation), the client is closed and set to `None`.
  - **Mitigation:** `_finalize_start()` and `_start_e2ee_required()` both clean up on failure.

### 3.2 Retry Budget

- **Reconnect attempts:** Max 10 (`_MAX_RECONNECT_ATTEMPTS`). After exhaustion, `_sync_failure` is set and the sync loop exits.
- **Backoff:** Exponential, base 1s, cap 60s, ±25% jitter. No retry after budget exhaustion.
- **Bounded send retry.** `deliver()` retries transient send failures up to 3 times with a stable per-delivery `tx_id`. The homeserver uses `tx_id` to deduplicate retried attempts within its transaction-ID window, reducing duplicate visible messages. This is not exactly-once delivery; duplicates are still possible across restarts or outside the dedup window.

**Risk assessment:** Low. The retry budget is finite and bounded. The stable `tx_id` reduces but does not eliminate duplicate risk. The backoff prevents thundering-herd reconnection attempts.

### 3.3 Callback Management

| Callback                    | Registration                                    | Deregistration                                           |
| --------------------------- | ----------------------------------------------- | -------------------------------------------------------- |
| `_message_callback`         | In `_finalize_start()` via `add_event_callback` | Client is closed on stop; nio handles cleanup internally |
| `_on_megolm_event`          | In `_register_megolm_callback()`                | Same — client close handles it                           |
| `_on_room_encryption_event` | In `_register_megolm_callback()`                | Same — client close handles it                           |

**Risk assessment:** Low. Callbacks are registered on the nio client, which is closed and set to `None` on stop. No dangling callback risk after `stop()`.

### 3.4 Store/Crypto Retention

| Resource                     | Type                             | Retention                                                                                    |
| ---------------------------- | -------------------------------- | -------------------------------------------------------------------------------------------- |
| `_room_states`               | `dict[str, RoomEncryptionState]` | In-memory. Cleared on `start()` (`self._room_states = {}`). Grows with number of rooms seen. |
| Crypto store                 | SQLite database on disk          | Managed by nio. Path set via `store_path` config. MEDRE does not manage lifecycle.           |
| `_undecryptable_event_count` | `int`                            | Monotonically increasing counter. Never reset.                                               |

**Risk assessment:**

- **`_room_states` memory growth.** Grows linearly with the number of rooms the bot is in. For typical bot usage (tens of rooms), this is negligible. For large-scale deployments (thousands of rooms), this could be a concern.
  - **Mitigation:** Cleared on `start()`. For long-running sessions, this is a slow leak at worst.
  - **Residual risk:** No eviction policy for rooms no longer joined. Acceptable for alpha/beta.

- **`_undecryptable_event_count` unbounded.** Monotonically increasing, never reset. For normal operation this stays small. For extended operation with persistent crypto failures, it grows without bound.
  - **Mitigation:** Diagnostics snapshot only. No functional impact.
  - **Residual risk:** Cosmetic. Counter could overflow in pathological cases, but `int` is unbounded in Python.

### 3.5 Test Coverage

Existing tests in `tests/test_matrix_session.py` and its split files cover:

- Start/stop lifecycle (`test_matrix_session.py`)
- Double-start protection (`test_matrix_session.py`)
- E2EE dependency detection (monkeypatchable) (`test_matrix_session.py`)
- Encryption mode behavior (`test_matrix_session.py`, `test_matrix_session_config.py`)
- Diagnostics keys (`test_matrix_session.py`)
- Encrypted room safety check (`test_matrix_session_e2ee.py`)
- Sync failure logging (`test_matrix_session_recovery.py`)
- MegolmEvent handling (`test_matrix_session_e2ee.py`)

## 4. MeshtasticSession

### 4.1 Task Management

| Resource          | Type           | Owner   | Cleanup                                                                         |
| ----------------- | -------------- | ------- | ------------------------------------------------------------------------------- |
| `_reconnect_task` | `asyncio.Task` | Session | Cancelled and awaited in `stop()` with configurable timeout. Cleared to `None`. |
| `_client`         | SDK interface  | Session | `close()` called in `stop()`. Set to `None`.                                    |

**Risk assessment:**

- **Reconnect task leak on stop timeout.** Same pattern as Matrix. `_stop_requested` flag prevents further reconnects. Task reference cleared.
- **Pubsub callback threading.** `mtjk` fires callbacks on a background thread. MEDRE must bridge to the event loop. If the bridge fails silently, callbacks are lost but no resource leak occurs.
- **Double-start protection.** `start()` checks `self._started`. Returns early with warning if already started.

### 4.2 Retry Budget

- **Reconnect attempts:** Max 10 (`_MAX_RECONNECT_ATTEMPTS`). Same pattern as Matrix.
- **Send retry:** Max 3 (`_MAX_SEND_RETRIES`). On exhaustion, adapter normalizes internal `MeshtasticSendError` and raises `AdapterSendError(transient=True)`/`AdapterPermanentError`.
- **Backoff:** Exponential, base 1s, cap 30s, ±25% jitter.

**Risk assessment:** Low. Both reconnect and send budgets are bounded.

### 4.3 Callback Management

| Callback            | Registration             | Deregistration                         |
| ------------------- | ------------------------ | -------------------------------------- |
| `_message_callback` | Set in `start()`         | `_unsubscribe_callbacks()` in `stop()` |
| Pubsub subscription | `_subscribe_callbacks()` | `_unsubscribe_callbacks()` in `stop()` |

**Risk assessment:** Low. Explicit unsubscribe on stop.

### 4.4 Store/Session Retention

| Resource                       | Type  | Retention                              |
| ------------------------------ | ----- | -------------------------------------- |
| `_transient_delivery_failures` | `int` | Monotonically increasing. Never reset. |
| `_permanent_delivery_failures` | `int` | Monotonically increasing. Never reset. |

**Risk assessment:** Low. Counters only. No functional impact.

### 4.5 Outbound Queue (MeshtasticOutboundQueue)

The outbound queue is owned by the adapter, not the session. Key resource
properties:

- **Queue:** `collections.deque` — capacity enforced explicitly at enqueue time via `max_queue_size`; the internal deque is unbounded but growth is prevented by enqueue rejection. In-memory.
  - **Risk:** Memory grows linearly if messages are enqueued faster than processed.
  - **Mitigation:** `process_one()` drains one item at a time with pacing delay. Caller controls dequeue rate. Queue overflow is rejected (not silently evicted).
  - **Residual risk:** No backpressure. If the adapter enqueues faster than the radio can send, the queue grows up to `max_queue_size`, then rejects new enqueues.

- **Transient send failures are retried** up to `queue_send_max_attempts` from the
  adapter-local in-memory queue. `total_requeued` increments on each retry.
  Exhausted retries and permanent failures are dropped. Retry is best-effort,
  non-durable, and not exactly-once.

### 4.6 Test Coverage

Existing tests in `tests/test_meshtastic_adapter.py` cover adapter lifecycle.
No dedicated session-only test file exists; session is tested through adapter tests.

## 5. MeshCoreSession

### 5.1 Task Management

| Resource          | Type           | Owner   | Cleanup                                               |
| ----------------- | -------------- | ------- | ----------------------------------------------------- |
| `_reconnect_task` | `asyncio.Task` | Session | Cancelled and awaited in `stop()`. Cleared to `None`. |
| `_subscriptions`  | `list[Any]`    | Session | `_unsubscribe_all()` called in `stop()`. Cleared.     |
| `_meshcore`       | SDK client     | Session | `disconnect()` called in `stop()`. Set to `None`.     |

**Risk assessment:** Low. Same pattern as other sessions. Clean teardown path.

### 5.2 Retry Budget

- **Reconnect attempts:** Max 10 (`_RECONNECT_MAX_ATTEMPTS`).
- **Send retry:** Max 3 (`_SEND_MAX_RETRIES`).
- **Backoff:** Same pattern as other sessions.

### 5.3 Callback Management

| Callback                | Registration                       | Deregistration                                             |
| ----------------------- | ---------------------------------- | ---------------------------------------------------------- |
| `_message_callback`     | Set in `start()`                   | No explicit deregistration (SDK subscriptions are cleared) |
| SDK event subscriptions | `subscribe()` in `_connect_real()` | `_unsubscribe_all()` in `stop()`                           |

**Risk assessment:** Low.

### 5.4 Store/Session Retention

Same pattern as other sessions: monotonically increasing counters, no memory
growth risk under normal operation.

### 5.5 Test Coverage

Existing tests in `tests/test_meshcore_session.py` cover lifecycle, reconnect,
send, and diagnostics in fake mode.

## 6. LxmfSession

### 6.1 Task Management

| Resource          | Type           | Owner   | Cleanup                                                            |
| ----------------- | -------------- | ------- | ------------------------------------------------------------------ |
| `_reconnect_task` | `asyncio.Task` | Session | Cancelled and awaited in `stop()` with timeout. Cleared to `None`. |
| `_announce_task`  | `asyncio.Task` | Session | Cancelled and awaited in `stop()`. Cleared to `None`.              |
| `_router`         | LXMRouter      | Session | Torn down in `_teardown_sdk()`.                                    |
| `_identity`       | RNS Identity   | Session | Torn down in `_teardown_sdk()`.                                    |
| `_reticulum`      | RNS.Reticulum  | Session | Torn down in `_teardown_sdk()`.                                    |

**Risk assessment:**

- **Multiple tasks to cancel.** LxmfSession has both `_reconnect_task` and `_announce_task`. Both must be cancelled on stop.
  - **Mitigation:** Both are cancelled and awaited in `stop()`. Announce task is cancelled first.

- **SDK teardown order.** Router must be torn down before identity and reticulum.
  - **Mitigation:** `_teardown_sdk()` follows correct order.

- **Outbound delivery tracking.** `_outbound_deliveries` dict tracks pending deliveries. Cleared in `stop()`.
  - **Risk:** In-flight deliveries may have callbacks that fire after stop. The `_stop_requested` guard prevents new operations.
  - **Residual risk:** A delivery callback could fire between `_stop_requested = True` and `_unsubscribe_callbacks()`. The callback would be a no-op (no handler registered) or would update tracking data that is immediately cleared.

### 6.2 Retry Budget

- **Reconnect attempts:** Max 10 (`_RECONNECT_MAX_ATTEMPTS`).
- **Send retry:** Max 3 (`_SEND_MAX_RETRIES`).
- **Backoff:** Same pattern as other sessions.

### 6.3 Callback Management

| Callback            | Registration                                        | Deregistration                         |
| ------------------- | --------------------------------------------------- | -------------------------------------- |
| `_message_callback` | Set in `start()`                                    | `_unsubscribe_callbacks()` in `stop()` |
| Delivery callback   | `register_delivery_callback()` in `_connect_real()` | `_unsubscribe_callbacks()` in `stop()` |

**Risk assessment:** Low. Explicit unsubscribe.

### 6.4 Store/Session Retention

| Resource               | Type                           | Retention                                                                         |
| ---------------------- | ------------------------------ | --------------------------------------------------------------------------------- |
| `_outbound_deliveries` | `dict[str, _OutboundDelivery]` | Grows with pending outbound messages. Cleared in `stop()`.                        |
| Identity file          | 64-byte private key on disk    | Managed by operator. MEDRE loads it but does not create/modify it (in real mode). |

**Risk assessment:**

- **`_outbound_deliveries` memory growth.** Grows with each outbound send. Entries are never evicted until `stop()`. For high-throughput scenarios, this could accumulate.
  - **Mitigation:** Cleared on `stop()`. For long-running sessions with many sends, entries accumulate.
  - **Residual risk:** No eviction for completed deliveries. Entries persist even after `DELIVERED`/`FAILED`/`REJECTED`/`CANCELLED` state.
  - **Recommendation (future):** Consider periodic eviction of terminal-state entries.

### 6.5 Test Coverage

Existing tests in `tests/test_lxmf_session.py` cover lifecycle, start/stop,
inbound normalisation, outbound pending semantics, diagnostics, delivery state
model, and no raw-object leakage.

## 7. Cross-Session Resource Summary

| Resource Type              | Matrix                | Meshtastic                | MeshCore                  | LXMF                        |
| -------------------------- | --------------------- | ------------------------- | ------------------------- | --------------------------- |
| **Background tasks**       | 1 (sync)              | 0–1 (reconnect)           | 0–1 (reconnect)           | 0–2 (reconnect + announce)  |
| **SDK client**             | nio AsyncClient       | mtjk interface            | meshcore.MeshCore         | RNS + Identity + LXMRouter  |
| **Max reconnect attempts** | 10                    | 10                        | 10                        | 10                          |
| **Max send retries**       | 3 (bounded retry)     | `queue_send_max_attempts` | 3                         | 3                           |
| **Outbound queue**         | None                  | Bounded retry (in-memory) | None                      | None (router-managed)       |
| **Monotonic counters**     | 1 (undecryptable)     | 2 (transient + permanent) | 2 (transient + permanent) | 2 (transient + permanent)   |
| **Memory growth risk**     | `_room_states` dict   | Outbound queue            | None                      | `_outbound_deliveries` dict |
| **Disk persistence**       | Crypto store (SQLite) | None                      | None                      | Identity file (raw key)     |

## 8. Key Findings

1. **All sessions have bounded retry budgets.** Max 10 reconnects. Meshtastic adapter-local queue retries transient send failures up to `queue_send_max_attempts`. Matrix bounded send retry (3) with stable `tx_id`. MeshCore and LXMF cap at 3 send retries. No unbounded retry loops.

2. **All sessions have idempotent stop()** with timeout-based task cancellation. The `_stop_requested` flag prevents zombie reconnect loops.

3. **No session leaks SDK objects across stop/start boundaries.** All sessions set SDK references to `None` on stop and re-create on start.

4. **Three memory growth risks identified:**
   - Matrix `_room_states` dict (mitigated by clear on start).
   - Meshtastic outbound queue is bounded by explicit `max_queue_size` (default 1024); overflow rejects new enqueues instead of evicting accepted items. Pacing provides additional backpressure.
   - LXMF `_outbound_deliveries` dict (no eviction for completed deliveries).

5. **No secret leakage through diagnostics.** All diagnostics snapshots use read-only dataclasses that exclude secrets, tokens, keys, and raw SDK objects.

6. **Meshtastic has the most complex threading model** (pubsub callbacks on background threads, `asyncio.to_thread` for sends). The bridging is handled correctly but adds complexity.

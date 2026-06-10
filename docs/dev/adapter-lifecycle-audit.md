# Adapter Lifecycle Parity Audit

> **Classification:** Audit documentation (pre-release)
> **Branch:** `main` (post `adapter-sdk-parity` merge)
> **Scope:** Meshtastic, MeshCore, LXMF, Matrix runtime lifecycle behavior
> **Authority:** `src/medre/core/lifecycle/states.py` owns the `AdapterState` enum and `VALID_TRANSITIONS` graph; `src/medre/runtime/app.py` owns startup/shutdown ordering and per-adapter state management; adapters report facts via `health_check()` and the runtime sets lifecycle state.

## Purpose

This document audits the runtime lifecycle behavior of all four MEDRE adapters
across three phases — Startup, Runtime, and Shutdown — and identifies concrete
follow-up tests or fixes. It does **not** implement those fixes. Other
concurrent workers own evidence quality, capability reporting, SDK parity, and
boundary hardening.

## Testing Rules for Later Code/Test Workers

From `docs/dev/testing.md`, rules that materially affect later work on
lifecycle tests:

1. **File size limit:** 1,500-line hard cap per test file (target < 1,200).
   Split by behavioral domain before approaching the cap.
2. **pytest function style:** New tests use module-level `async def`, not
   `unittest.TestCase`.
3. **No fixed sleeps:** Use `wait_until()` from `tests/helpers/async_utils.py`
   or deterministic hooks (`asyncio.Event`).
4. **Async mock matching:** `await`-ed callables get `AsyncMock`; synchronous
   registration gets `MagicMock`. Never swap them.
5. **Coroutine leak prevention:** Close passed coroutines in scheduler fakes.
6. **Test tiers:** Label honestly. Fake adapters = tier 1–2, never "docker" or
   "live".
7. **Patch target policy:** Patch at the lookup site, not the definition site.
8. **No compatibility shims:** No env-branching or version detection in tests.
9. **Schema version frozen at 1:** Do not add migration or version-bump tests
   during pre-release.

---

## Lifecycle State Authority

The runtime tracks per-adapter state via `MedreApp._adapter_states` (a
`dict[str, AdapterState]`). The `AdapterState` enum has eight members:

| State         | Terminal | Role                                         |
| ------------- | -------- | -------------------------------------------- |
| INITIALIZING  | No       | Adapter is being set up                      |
| READY         | No       | Fully operational                            |
| DEGRADED      | No       | Partially functional                         |
| BACKPRESSURED | No       | Outbound queue full, throttle inbound        |
| DISCONNECTED  | No       | Lost transport connection                    |
| STOPPING      | No       | Shutting down gracefully                     |
| FAILED        | Yes      | Unrecoverable error; no outgoing transitions |
| STOPPED       | Yes      | Clean shutdown; no outgoing transitions      |

The runtime sets `INITIALIZING` before calling `adapter.start(ctx)`, then
`READY` on success or `FAILED` on exception. During `app.stop()`, adapters
transition through `STOPPING` → `STOPPED` (clean) or `STOPPING` → `FAILED`
(error). The runtime validates every transition against `VALID_TRANSITIONS`
and raises `InvalidStateTransition` on illegal moves.

Adapters **do not** set their own `AdapterState` — the runtime owns it.
Adapters report health facts via `health_check()` → `AdapterInfo.health`
(string: `"healthy"`, `"degraded"`, `"failed"`, `"unknown"`). The runtime
maps these to `AdapterState` values via `health_to_adapter_state()`.

**Test coverage:**

- `tests/test_lifecycle_states.py`: Terminal semantics, transition validation,
  `STOPPED` health mapping, supervision classification.
- `tests/test_adapter_lifecycle_registry.py`: `READY` after start, `FAILED`
  after start failure, `STOPPED` after clean stop, `FAILED` after stop
  failure, total-failure cleanup, build-failure registration, snapshot
  determinism.

---

## Phase 1: Startup

### 1.1 SDK Initialization

| Adapter    | Status      | Evidence                                                                                                                                                                                     |
| ---------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Implemented | `MatrixSession._start_plaintext()` / `_start_e2ee_required()` creates `nio.AsyncClient`, calls `restore_login`. Guards `HAS_NIO` and `HAS_E2EE`. File: `adapters/matrix/session.py:399–627`. |
| Meshtastic | Implemented | `MeshtasticSession._create_client()` constructs TCP/Serial/BLE interface. `HAS_MESHTASTIC` guard for non-fake modes. File: `adapters/meshtastic/session.py:640–698`.                         |
| MeshCore   | Implemented | `MeshCoreSession._connect_real()` calls `MeshCore.create_tcp()` / `create_serial()` / `create_ble()`. `HAS_MESHCORE` guard. File: `adapters/meshcore/session.py:471–615`.                    |
| LXMF       | Implemented | `LxmfSession._connect_real()` initialises `RNS.Reticulum`, loads/creates `RNS.Identity`, creates `LXMF.LXMRouter`. `HAS_LXMF` guard. File: `adapters/lxmf/session.py:848–919`.               |

**Parity assessment:** All four adapters implement SDK initialization with
lazy import guards and config-driven connection type selection. No gaps.

### 1.2 Connection Establishment

| Adapter    | Status      | Evidence                                                                                                                                                                                                      |
| ---------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Implemented | `restore_login()` with access token. Device ID discovered via `whoami()`. E2EE crypto store loaded in `_start_e2ee_required()`. File: `adapters/matrix/session.py:458–594`.                                   |
| Meshtastic | Implemented | `_create_client()` constructs interface; `_subscribe_callbacks()` registers pubsub. `_refresh_node_id()` populates own node ID. File: `adapters/meshtastic/session.py:300–314`.                               |
| MeshCore   | Implemented | `_connect_real()` creates SDK client, subscribes events, sends `APP_START`, starts auto message fetching. File: `adapters/meshcore/session.py:544–615`.                                                       |
| LXMF       | Implemented | `_connect_real()` creates `RNS.Reticulum` (reuses singleton if available), loads identity, creates `LXMRouter`, registers delivery callback, configures stamp cost. File: `adapters/lxmf/session.py:848–919`. |

**Parity assessment:** All four establish connections. Matrix additionally
performs device ID discovery and crypto store validation. No functional gaps.

### 1.3 Readiness Determination

| Adapter    | Status      | Evidence                                                                                                                                                                                                                                                  |
| ---------- | ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Implemented | `MatrixAdapter.start()` calls `_mark_started(ctx)` then `session.start()`. Session validates login in `_finalize_start()`. Adapter does not return until session is logged in and sync task is created. File: `adapters/matrix/adapter.py:294–346`.       |
| Meshtastic | Implemented | `MeshtasticAdapter.start()` calls `_mark_started(ctx)` then `session.start()`. Session sets `_started = True` after connection. Adapter sets `_started = True` after session + queue drain task creation. File: `adapters/meshtastic/adapter.py:210–260`. |
| MeshCore   | Implemented | `MeshCoreAdapter.start()` calls `_mark_started(ctx)` then `session.start()`. Session sets `_diag.connected = True` after `_connect_real()` + `APP_START` success. File: `adapters/meshcore/adapter.py:246–284`.                                           |
| LXMF       | Implemented | `LxmfAdapter.start()` calls `_mark_started(ctx)` then `session.start()`. Session sets `_diag.connected = True` and `_diag.router_running = True` after `_connect_real()`. File: `adapters/lxmf/adapter.py:131–177`.                                       |

**Parity assessment:** All four call `_mark_started(ctx)` (base class method
recording start time for stale-event filtering). All four block until
connection is established. No gaps.

### 1.4 Subscription Registration

| Adapter    | Status      | Evidence                                                                                                                                                                                                                            |
| ---------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Implemented | `_finalize_start()` registers callbacks for `RoomMessageText`, `RoomMessageNotice`, `RoomMessageEmote`, `ReactionEvent`, `MegolmEvent`, `RoomEncryptionEvent`, and `InviteMemberEvent`. File: `adapters/matrix/session.py:767–826`. |
| Meshtastic | Implemented | `_subscribe_callbacks()` subscribes to `meshtastic.receive` via `pubsub.pub`. File: `adapters/meshtastic/session.py:702–718`.                                                                                                       |
| MeshCore   | Implemented | `_subscribe_events()` subscribes to `CONTACT_MSG_RECV`, `CHANNEL_MSG_RECV`, and `DISCONNECTED` event types. File: `adapters/meshcore/session.py:617–641`.                                                                           |
| LXMF       | Implemented | `_connect_real()` registers `_on_lxmf_delivery` via `router.register_delivery_callback()`. File: `adapters/lxmf/session.py:900–906`.                                                                                                |

**Parity assessment:** All four register inbound event/callback subscriptions.
Matrix has the richest set (message types, reactions, encryption events,
invites). No gaps.

### 1.5 Ingress Activation

| Adapter    | Status      | Evidence                                                                                                                                                                                                                                      |
| ---------- | ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Implemented | Inbound events flow through `_on_nio_event` → `_normalize_event` → `_message_callback` (`_on_room_message`). Sync task (`_run_sync`) drives the long-poll loop. File: `adapters/matrix/session.py:1152–1314`.                                 |
| Meshtastic | Implemented | SDK reader thread calls `_on_receive` (pubsub) → `_message_callback` (`_on_packet`) which bridges to asyncio via `run_coroutine_threadsafe`. File: `adapters/meshtastic/session.py:748–762`.                                                  |
| MeshCore   | Implemented | SDK fires `CONTACT_MSG_RECV` / `CHANNEL_MSG_RECV` → `_on_sdk_event` → `_message_callback` (`_on_message`). Callback may be sync or async; async results are scheduled as fire-and-forget tasks. File: `adapters/meshcore/session.py:701–735`. |
| LXMF       | Implemented | LXMRouter fires delivery callback on Reticulum thread → `_on_lxmf_delivery` → `_normalise_inbound_message` → `loop.call_soon_threadsafe(_invoke_inbound_callback)`. File: `adapters/lxmf/session.py:945–996`.                                 |

**Parity assessment:** All four activate ingress during startup. Thread
bridging differs: Meshtastic uses `run_coroutine_threadsafe`, LXMF uses
`call_soon_threadsafe`, MeshCore uses `asyncio.ensure_future`, Matrix uses
nio's async sync loop. No gaps.

### 1.6 Backlog Handling

| Adapter    | Status                    | Evidence                                                                                                                                                                                                                                         |
| ---------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Matrix     | Implemented               | `MatrixSession._live_sync_started` flag set after first successful sync with `next_batch`. Before this, `_on_room_message` suppresses all inbound events as startup backlog. File: `adapters/matrix/adapter.py:703–711`.                         |
| Meshtastic | Implemented               | `MeshtasticAdapter._check_startup_backlog_suppress()` uses `rxTime` from packets and `adapter_start_epoch` with configurable `startup_backlog_suppress_seconds` window. File: `adapters/meshtastic/adapter.py:555–607`.                          |
| MeshCore   | Intentionally unsupported | MeshCore SDK's `start_auto_message_fetching` drains buffered messages during startup. No explicit backlog suppression needed because the SDK drains the device buffer before live events arrive. File: `adapters/meshcore/session.py:594–612`.   |
| LXMF       | Intentionally unsupported | LXMF is a store-and-forward protocol. Messages arriving at the router have no reliable "sent time" vs. "received time" distinction for backlog filtering. The adapter trusts the router's delivery callback ordering. No code-level suppression. |

**Parity assessment:** Matrix and Meshtastic have explicit backlog suppression.
MeshCore relies on SDK buffering. LXMF relies on protocol semantics. The
difference is intentional — each transport's SDK/protocol handles history
differently. No action needed.

---

## Phase 2: Runtime

### 2.1 Reconnect Behavior

| Adapter    | Status                | Evidence                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ---------- | --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Implemented           | `_sync_with_reconnect()` wraps the sync loop in a bounded reconnect loop: exponential backoff (1s–60s, ±25% jitter), max 10 attempts. `_stop_requested` guard prevents reconnect during shutdown. `reconnect_attempts` and `reconnecting` diagnostics exposed. File: `adapters/matrix/session.py:1159–1314`.                                                                                                                                                                                                                                        |
| Meshtastic | Implemented           | `notify_connection_lost()` starts `_reconnect_loop()`: exponential backoff (1s–30s, ±25% jitter), max 10 attempts. Tears down old client, creates new one, re-subscribes callbacks, refreshes node ID. `_stop_requested` guard. File: `adapters/meshtastic/session.py:766–879`.                                                                                                                                                                                                                                                                     |
| MeshCore   | Implemented           | `_on_disconnect_event()` starts `_reconnect_loop()`: exponential backoff (1s–30s, ±25% jitter), max 10 attempts. Calls `_connect_real()` on each attempt. `_stop_requested` guard. File: `adapters/meshcore/session.py:751–832`.                                                                                                                                                                                                                                                                                                                    |
| LXMF       | Partially implemented | `LxmfSession` defines `_reconnect_loop` with the same pattern (exponential backoff, max 10 attempts), but the trigger mechanism is incomplete. The session registers a delivery callback but does not subscribe to an explicit RNS disconnect event — RNS does not expose a comparable `DISCONNECTED` event type. Reconnect is structurally present but may not fire automatically on transport loss without an external health-check trigger. File: `adapters/lxmf/session.py` (reconnect_loop code exists but no automatic disconnect detection). |

**Follow-up:** Verify LXMF reconnect triggering. If RNS provides a transport-down callback, wire it. If not, document that LXMF reconnect relies on send-time failure detection rather than proactive disconnect notification.

### 2.2 Health Detection

| Adapter    | Status                | Evidence                                                                                                                                                                                                                                                                                                                                       |
| ---------- | --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Implemented           | `health_check()` derives health from session state: `"healthy"` if logged in, `"failed"` if sync error, `"unknown"` if no session or not connected. File: `adapters/matrix/adapter.py:390–429`.                                                                                                                                                |
| Meshtastic | Implemented           | `health_check()` derives health from session + config: `"healthy"` if connected or fake mode, `"degraded"` if reconnecting, `"unknown"` if not started, `"failed"` if session exists but start didn't complete. File: `adapters/meshtastic/adapter.py:307–336`.                                                                                |
| MeshCore   | Implemented           | `health_check()` returns `"healthy"` if started and session connected, `"degraded"` if started but disconnected or reconnecting, `"unknown"` if not started. File: `adapters/meshcore/adapter.py:312–341`.                                                                                                                                     |
| LXMF       | Partially implemented | `health_check()` returns `"healthy"` if `_started` is True, `"failed"` if session connected but adapter not started (partial-start state), `"unknown"` otherwise. Does not differentiate between "connected and operational" vs. "connected but degraded" — always reports `"healthy"` when started. File: `adapters/lxmf/adapter.py:203–225`. |

**Follow-up:** LXMF `health_check()` could be more granular — distinguishing
"router running and path known" from "router running but no path to peer."
Consider checking `_session.router_running` and outbound delivery states for
degraded detection. Low priority; the current implementation is honest (does
not overclaim).

### 2.3 Ingress Processing

| Adapter    | Status      | Evidence                                                                                                                                                                                                                                                                                             |
| ---------- | ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Implemented | `_on_room_message()` applies room allowlist filter, startup-backlog suppression (`is_live`), self-message suppression, MEDRE-envelope loop suppression, then decodes via `MatrixCodec` and enriches with display name metadata. File: `adapters/matrix/adapter.py:662–791`.                          |
| Meshtastic | Implemented | `_on_packet()` classifies via `MeshtasticPacketClassifier`, increments counters, checks startup-backlog suppression, enriches with node info (longname/shortname), decodes via `MeshtasticCodec`, bridges to asyncio via `run_coroutine_threadsafe`. File: `adapters/meshtastic/adapter.py:609–674`. |
| MeshCore   | Implemented | `_on_message()` classifies via `MeshCorePacketClassifier`, increments counters, decodes via `MeshCoreCodec`, creates tracked background task for `publish_inbound`. File: `adapters/meshcore/adapter.py:454–492`.                                                                                    |
| LXMF       | Implemented | `_on_packet()` classifies via `LxmfPacketClassifier`, filters non-text and ACK categories, decodes via `LxmfCodec`, creates tracked background task for `publish_inbound`. File: `adapters/lxmf/adapter.py:391–423`.                                                                                 |

**Parity assessment:** All four implement classification → decode → publish
ingress pipelines. Meshtastic and Matrix add transport-specific enrichment
(node info, display name). No gaps.

### 2.4 Outbound Processing

| Adapter    | Status      | Evidence                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ---------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Implemented | `deliver()` sends via `session.room_send()` with deterministic txn_id for idempotency. Implements bounded retry (3 attempts, exponential backoff + jitter) for transient errors. Rate-limit responses (`M_LIMIT_EXCEEDED`) classified as transient. Permanent errors (M_FORBIDDEN, M_NOT_FOUND) raise immediately. Returns `AdapterDeliveryResult` with `event_id` as `native_message_id`. File: `adapters/matrix/adapter.py:464–658`. |
| Meshtastic | Implemented | `deliver()` enqueues to `MeshtasticOutboundQueue`. Returns `AdapterDeliveryResult` with `delivery_status="enqueued"` (no native ID yet). Background `_process_queue()` task drains the queue, calls `session.send()`, records delayed native refs and terminal outcomes. Listen-only mode suppresses outbound. File: `adapters/meshtastic/adapter.py:340–439`.                                                                         |
| MeshCore   | Implemented | `deliver()` delegates to `session.send_text()` for real modes, returns `None` for fake mode. Session implements bounded retry (3 attempts) with pacing via `_send_lock`. Returns `AdapterDeliveryResult` with `expected_ack` hex as `native_message_id`. File: `adapters/meshcore/adapter.py:345–450`.                                                                                                                                 |
| LXMF       | Implemented | `deliver()` delegates to `session.send_text()` which constructs an `LXMessage`, registers a delivery state callback, and hands it to `router.handle_outbound()`. Returns `(native_message_id, initial_delivery_state)`. Honest pending semantics: reports `OUTBOUND` state, not `DELIVERED`. File: `adapters/lxmf/adapter.py:285–387`.                                                                                                 |

**Parity assessment:** All four implement `deliver()` with honest delivery
semantics. Meshtastic uses an internal queue with background drain; the
others send synchronously (within the `deliver()` call). Matrix adds
idempotency via `txn_id`. No gaps.

### 2.5 Callback Handling

| Adapter    | Status      | Evidence                                                                                                                                                                                                                                                                                                                  |
| ---------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Implemented | nio callbacks are async-native — `_on_nio_event` receives `RoomMessageText` etc., normalizes to plain dict, forwards to `_message_callback`. No thread bridging needed. File: `adapters/matrix/session.py:751–765`.                                                                                                       |
| Meshtastic | Implemented | SDK calls `_on_receive` on a reader thread. Synchronous callback uses `run_coroutine_threadsafe` to bridge to the event loop. Futures tracked in `_inbound_futures` and drained on stop. `_started` guard rejects late packets. File: `adapters/meshtastic/session.py:748–762`, `adapters/meshtastic/adapter.py:609–674`. |
| MeshCore   | Implemented | SDK fires async events via subscriptions. `_on_sdk_event` extracts payload, calls `_message_callback` (sync or async). Async results are scheduled as fire-and-forget tasks with exception-logging done callbacks. File: `adapters/meshcore/session.py:701–735`.                                                          |
| LXMF       | Implemented | LXMRouter fires `_on_lxmf_delivery` on a Reticulum thread. Normalises message to plain dict, bridges via `loop.call_soon_threadsafe(_invoke_inbound_callback)`. Late callbacks dropped if `_stop_requested` or loop is closed. File: `adapters/lxmf/session.py:945–996`.                                                  |

**Parity assessment:** All four handle SDK callbacks with appropriate thread
safety. Meshtastic and LXMF use thread-to-async bridging; Matrix and MeshCore
are async-native or SDK-managed. No gaps.

### 2.6 Queue Interaction

| Adapter    | Status                    | Evidence                                                                                                                                                                                                                                                                                   |
| ---------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Matrix     | N/A (immediate-send)      | No internal outbound queue. `deliver()` sends directly via `room_send()`. Bounded retry happens within the single `deliver()` call. File: `adapters/matrix/adapter.py:464–658`.                                                                                                            |
| Meshtastic | Implemented               | `MeshtasticOutboundQueue` provides enqueue-only `deliver()`, background `_process_queue()` drain task, send retry, native-ref callback, terminal-outcome reporting. Pacing via `delay_between_messages`. File: `adapters/meshtastic/queue.py`, `adapters/meshtastic/adapter.py:1030–1134`. |
| MeshCore   | Intentionally unsupported | No internal outbound queue. `deliver()` sends directly via `session.send_text()` with pacing via `_send_lock`. The SDK itself manages radio-level queueing. File: `adapters/meshcore/session.py:838–950`.                                                                                  |
| LXMF       | Intentionally unsupported | No internal outbound queue. `deliver()` sends directly via `session.send_text()`. The LXMRouter manages propagation queueing internally. Pacing via `_send_lock` and `message_delay_seconds`. File: `adapters/lxmf/session.py:702–769`.                                                    |

**Parity assessment:** Queue interaction is intentionally not uniform. Only
Meshtastic has a MEDRE-level outbound queue because its SDK requires paced
radio transmission. Matrix, MeshCore, and LXMF delegate queueing to their
SDKs. No action needed.

---

## Phase 3: Shutdown

### 3.1 Stop Ordering

| Adapter    | Status      | Evidence                                                                                                                                                                                                                                                                            |
| ---------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Implemented | `stop()`: stores sync failure for diagnostics, calls `session.stop(timeout)` which cancels sync task, unsubscribes, closes nio client, yields for aiohttp cleanup, sets `self._session = None`. File: `adapters/matrix/adapter.py:362–374`, `adapters/matrix/session.py:1315–1371`. |
| Meshtastic | Implemented | `stop()`: clears `_started` (inbound gate), cancels drain task, drains background tasks + inbound futures, calls `session.stop(timeout)`, sets `_session = None`. File: `adapters/meshtastic/adapter.py:262–305`.                                                                   |
| MeshCore   | Implemented | `stop()`: drains background tasks, calls `session.stop()` (no timeout param), sets `_session = None`. File: `adapters/meshcore/adapter.py:286–310`.                                                                                                                                 |
| LXMF       | Implemented | `stop()`: drains background tasks, calls `session.stop(timeout)`, sets `_started = False`. File: `adapters/lxmf/adapter.py:179–201`.                                                                                                                                                |

**Parity assessment:** All four follow the pattern: drain internal work →
stop session → null references. Meshtastic additionally drains
`concurrent.futures.Future` objects from its thread-bridged inbound path.
No gaps.

### 3.2 Task Cancellation

| Adapter    | Status      | Evidence                                                                                                                                                                                                                                                                                                                                                       |
| ---------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Implemented | `session.stop()` cancels `_sync_task`, awaits with timeout, handles `CancelledError` and `TimeoutError` gracefully. Cancels join tasks. File: `adapters/matrix/session.py:1315–1371`.                                                                                                                                                                          |
| Meshtastic | Implemented | `stop()` cancels `_drain_task` with timeout, then `_drain_background_tasks()` cancels all tracked `asyncio.Task` instances with a two-phase drain (initial wait, cancel pending, second wait, detach stubborn tasks). Also cancels all `concurrent.futures.Future` instances in `_inbound_futures`. File: `adapters/meshtastic/adapter.py:262–305`, `895–986`. |
| MeshCore   | Implemented | `_drain_background_tasks()` cancels all tracked tasks, awaits with `asyncio.wait_for` + `asyncio.gather(return_exceptions=True)`. File: `adapters/meshcore/adapter.py:588–606`.                                                                                                                                                                                |
| LXMF       | Implemented | `_drain_background_tasks()` same pattern as MeshCore: cancel all, gather with timeout. Session cancels `_announce_task` and `_reconnect_task`. File: `adapters/lxmf/adapter.py:263–281`, `adapters/lxmf/session.py:640–696`.                                                                                                                                   |

**Parity assessment:** All four cancel background tasks. Meshtastic has the
most sophisticated drain (two-phase with detach for cancellation-resistant
tasks). MeshCore and LXMF share the simpler gather-based pattern. No gaps.

### 3.3 Callback Draining

| Adapter    | Status      | Evidence                                                                                                                                                                                                                                                                                                                      |
| ---------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Implemented | Session sets `_stop_requested = True` before cancelling sync task, preventing reconnect loops. Sync task catches `CancelledError` and returns. File: `adapters/matrix/session.py:1317–1318`.                                                                                                                                  |
| Meshtastic | Implemented | Adapter clears `_started` before draining, causing `_on_packet` to reject late packets. Session sets `_stop_requested = True`, unsubscribes pubsub callbacks via `_unsubscribe_callbacks()`. Inbound futures cancelled and cleared. File: `adapters/meshtastic/adapter.py:281–285`, `adapters/meshtastic/session.py:334–363`. |
| MeshCore   | Implemented | Session calls `_unsubscribe_all()` which unsubscribes all SDK event subscriptions. `_stop_requested = True` prevents reconnect. File: `adapters/meshcore/session.py:325–371`, `687–695`.                                                                                                                                      |
| LXMF       | Implemented | Session calls `_unsubscribe_callbacks()`, then `_teardown_sdk()`. `_stop_requested = True` guards late SDK callbacks. Callback and loop references cleared. File: `adapters/lxmf/session.py:654–696`.                                                                                                                         |

**Parity assessment:** All four drain callbacks before teardown. Meshtastic
has the additional inbound-future cancellation step (unique due to its
thread-based SDK). No gaps.

### 3.4 SDK Cleanup

| Adapter    | Status      | Evidence                                                                                                                                                                                                                                    |
| ---------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Implemented | `session.stop()` calls `client.close()`, then `await asyncio.sleep(0)` to allow aiohttp connector cleanup, then `self._client = None`. File: `adapters/matrix/session.py:1351–1365`.                                                        |
| Meshtastic | Implemented | `session.stop()` calls `client.close()` (synchronous), sets `_client = None`, `_node_id = None`. File: `adapters/meshtastic/session.py:352–363`.                                                                                            |
| MeshCore   | Implemented | `session.stop()` calls `meshcore.stop_auto_message_fetching()` (with timeout), then `meshcore.disconnect()`, sets `_meshcore = None`. File: `adapters/meshcore/session.py:340–371`.                                                         |
| LXMF       | Implemented | `session.stop()` calls `_teardown_sdk()` which nulls `_router`, `_identity`, `_reticulum` in reverse order. Notes that RNS.Reticulum singleton has no `stop()` — only drops reference. File: `adapters/lxmf/session.py:677–678`, `921–939`. |

**Parity assessment:** All four clean up SDK objects. LXMF notes the RNS
singleton constraint (cannot tear down shared transport). This is
intentional and documented. No gaps.

### 3.5 Reconnect Suppression

| Adapter    | Status      | Evidence                                                                                                                                                                                                          |
| ---------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Implemented | `_stop_requested` flag checked in `_sync_with_reconnect()` loop. On `True`, loop exits without reconnecting. File: `adapters/matrix/session.py:1317–1318`, `1174`.                                                |
| Meshtastic | Implemented | `_stop_requested` flag checked in `_reconnect_loop()`, `notify_connection_lost()`, and `session.stop()`. Prevents new reconnect tasks during shutdown. File: `adapters/meshtastic/session.py:334–336`, `772`.     |
| MeshCore   | Implemented | `_stop_requested` flag checked in `_reconnect_loop()` and `_on_disconnect_event()`. Prevents reconnect during shutdown. File: `adapters/meshcore/session.py:325–326`, `756`.                                      |
| LXMF       | Implemented | `_stop_requested` flag checked in `_on_lxmf_delivery()` and `_on_delivery_state_update()`. Prevents late SDK callbacks from scheduling work during shutdown. File: `adapters/lxmf/session.py:654`, `960`, `1067`. |

**Parity assessment:** All four suppress reconnect during shutdown via a
`_stop_requested` flag. No gaps.

### 3.6 Resource Release

| Adapter    | Status      | Evidence                                                                                                                                                                                                                                                                                                          |
| ---------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Implemented | `_session = None` in adapter, `_client = None` + `_sync_task = None` in session, `_closed = True`. Diagnostic counters reset (`reconnect_attempts = 0`). File: `adapters/matrix/adapter.py:367–374`, `adapters/matrix/session.py:1365–1371`.                                                                      |
| Meshtastic | Implemented | `_session = None`, `_drain_task = None`, `_loop = None` in adapter. Session sets `_client = None`, `_node_id = None`, `_started = False`. Diagnostic counters reset. File: `adapters/meshtastic/adapter.py:296–305`, `adapters/meshtastic/session.py:352–363`.                                                    |
| MeshCore   | Implemented | `_session = None`, `_started = False` in adapter. Session sets `_meshcore = None`, `_diag.connected = False`, `_started = False`. File: `adapters/meshcore/adapter.py:304–310`, `adapters/meshcore/session.py:367–372`.                                                                                           |
| LXMF       | Implemented | `_started = False` in adapter. Session nulls `_router`, `_identity`, `_reticulum`, `_message_callback`, `_delivery_state_callback`, `_loop`. Clears `_outbound_deliveries` and `_delivery_insert_order`. Diagnostic counters reset. File: `adapters/lxmf/adapter.py:199–201`, `adapters/lxmf/session.py:677–696`. |

**Parity assessment:** All four release references, clear state, and reset
diagnostics. No gaps.

---

## Summary Matrix

### Startup

| Cell                      | Matrix      | Meshtastic  | MeshCore                  | LXMF                      |
| ------------------------- | ----------- | ----------- | ------------------------- | ------------------------- |
| SDK Initialization        | Implemented | Implemented | Implemented               | Implemented               |
| Connection Establishment  | Implemented | Implemented | Implemented               | Implemented               |
| Readiness Determination   | Implemented | Implemented | Implemented               | Implemented               |
| Subscription Registration | Implemented | Implemented | Implemented               | Implemented               |
| Ingress Activation        | Implemented | Implemented | Implemented               | Implemented               |
| Backlog Handling          | Implemented | Implemented | Intentionally unsupported | Intentionally unsupported |

### Runtime

| Cell                | Matrix      | Meshtastic  | MeshCore                  | LXMF                      |
| ------------------- | ----------- | ----------- | ------------------------- | ------------------------- |
| Reconnect Behavior  | Implemented | Implemented | Implemented               | Partially implemented     |
| Health Detection    | Implemented | Implemented | Implemented               | Partially implemented     |
| Ingress Processing  | Implemented | Implemented | Implemented               | Implemented               |
| Outbound Processing | Implemented | Implemented | Implemented               | Implemented               |
| Callback Handling   | Implemented | Implemented | Implemented               | Implemented               |
| Queue Interaction   | N/A         | Implemented | Intentionally unsupported | Intentionally unsupported |

### Shutdown

| Cell                  | Matrix      | Meshtastic  | MeshCore    | LXMF        |
| --------------------- | ----------- | ----------- | ----------- | ----------- |
| Stop Ordering         | Implemented | Implemented | Implemented | Implemented |
| Task Cancellation     | Implemented | Implemented | Implemented | Implemented |
| Callback Draining     | Implemented | Implemented | Implemented | Implemented |
| SDK Cleanup           | Implemented | Implemented | Implemented | Implemented |
| Reconnect Suppression | Implemented | Implemented | Implemented | Implemented |
| Resource Release      | Implemented | Implemented | Implemented | Implemented |

---

## Identified Follow-Up Items

These are concrete follow-up tests or fixes identified by this audit. They are
**not** implemented in this wave.

### LXMF-1: Verify Reconnect Triggering

**Cell:** Runtime → Reconnect Behavior
**Finding:** `LxmfSession._reconnect_loop` exists with correct backoff logic, but
the trigger mechanism is unclear. Unlike MeshCore (which subscribes to a
`DISCONNECTED` event type) and Meshtastic (which has `notify_connection_lost`),
LXMF relies on RNS transport-layer events that may not surface as a discrete
disconnected callback.
**Action:** Investigate whether RNS provides a transport-down or link-established
callback. If yes, wire it to trigger reconnect. If no, document that LXMF
reconnect is send-failure-triggered and add a test proving the reconnect loop
fires when `send_text()` fails with a connection error.
**Files:** `src/medre/adapters/lxmf/session.py`, new test file.

### LXMF-2: Granular Health Detection

**Cell:** Runtime → Health Detection
**Finding:** `LxmfAdapter.health_check()` returns `"healthy"` whenever
`_started` is True, regardless of router operational state or path
availability. Other adapters differentiate `"healthy"` / `"degraded"` /
`"unknown"` based on session connection state.
**Action:** Consider checking `_session.router_running`, `_session.connected`,
and outbound delivery state counts to produce more granular health. At minimum,
add a test that verifies the current honest behavior (always `"healthy"` when
started) and document that LXMF does not detect degraded state.
**Priority:** Low. Current implementation is honest — it does not overclaim.
**Files:** `src/medre/adapters/lxmf/adapter.py:203–225`.

### MESHTASTIC-1: Verify Inbound-Future Drain Completeness

**Cell:** Shutdown → Callback Draining
**Finding:** `MeshtasticAdapter.stop()` cancels `concurrent.futures.Future`
objects in `_inbound_futures` but does not await their completion (cancellation
is fire-and-forget for `concurrent.futures.Future`). This is correct —
`Future.cancel()` is synchronous — but a test should verify that no
`ResourceWarning` or "coroutine was never awaited" warning fires during a
stop-during-inbound scenario.
**Action:** Add a test that simulates an inbound packet arriving during
`stop()` and verifies clean shutdown with no warnings.
**Files:** New or existing meshtastic test file.

### CROSS-1: Stale-Event Filter Parity Test

**Cell:** Startup → Backlog Handling
**Finding:** Matrix and Meshtastic implement stale-event/backlog suppression
with different mechanisms (`is_live` flag vs. `rxTime` window). MeshCore and
LXMF intentionally do not suppress. A cross-adapter test should verify that
each adapter's backlog policy is documented and tested.
**Action:** Add a cross-adapter test that verifies: (a) Matrix suppresses
events before `is_live`, (b) Meshtastic suppresses events before
`adapter_start_epoch - window`, (c) MeshCore and LXMF do not suppress.
**Files:** New test file or extend existing adapter lifecycle tests.

### CROSS-2: Reconnect Parity Integration Test

**Cell:** Runtime → Reconnect Behavior
**Finding:** Matrix, Meshtastic, and MeshCore all implement bounded
exponential-backoff reconnect with max 10 attempts. The parameters are nearly
identical (1s base, 30–60s cap, ±25% jitter). A parameterized test could
verify that all three share the same backoff contract.
**Action:** Create a parameterized test that verifies backoff parameters and
max-attempt behavior for Matrix, Meshtastic, and MeshCore sessions. LXMF
should be included once LXMF-1 is resolved.
**Files:** New test file.

---

## Normative References

| Document                                | Authority                                          |
| --------------------------------------- | -------------------------------------------------- |
| `docs/spec/adapter-runtime.md`          | Adapter protocol, capabilities, session boundaries |
| `docs/spec/state-machines.md`           | Receipt/outbox state machines, transition graphs   |
| `docs/spec/delivery-lifecycle.md`       | Delivery lifecycle vocabulary and authority        |
| `docs/dev/lifecycle-authority-audit.md` | Status vocabulary audit guide                      |
| `docs/dev/testing.md`                   | Test style, file limits, async mocking rules       |
| `src/medre/core/lifecycle/states.py`    | `AdapterState` enum, `VALID_TRANSITIONS`           |
| `src/medre/runtime/app.py`              | `MedreApp` lifecycle coordination                  |

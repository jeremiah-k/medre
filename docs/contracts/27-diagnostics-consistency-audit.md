# Diagnostics Consistency Audit

> Contract version: 1
> Last updated: 2026-05-09
> Track: 9 (Transport Capability Contracts)
> Supersedes: Nothing. Supplements contracts 21, 22, 26.
> Status: Audit observations. No code changes proposed.

This document audits the consistency of diagnostics, session patterns, metadata envelopes, delivery semantics, and live test harnesses across MEDRE's four adapter families (Matrix, Meshtastic, MeshCore, LXMF). It records what is consistent, what diverges by design, and where asymmetries that cannot be abstracted away exist.

This is an audit document, not a design document. No runtime redesign, adapter abstraction, or cross-transport normalization changes are proposed.

## 1. Scope

- Diagnostics field inventory per adapter: common fields, transport-specific fields, safety checks.
- Session pattern audit: lifecycle shape, ownership, reconnect, divergent behavior, size.
- Metadata envelope consistency: namespacing, outbound support, lossiness.
- Delivery semantics: native_message_id, delivery_state, async vs immediate, failure behavior, duplicate risk.
- Live test harness inventory: files, marks, env vars, secrets, coverage, limitations.

## 2. Non-goals

- Proposing new runtime abstractions or adapter interfaces.
- Normalizing transport semantics that are inherently different.
- Changing any adapter behavior, diagnostics shape, or delivery semantics.
- Expanding live test coverage or adding new test harnesses.

## 3. Diagnostics Field Inventory

### 3.1 Common Diagnostic Fields

All four adapters expose a `health_check()` method returning `AdapterInfo` (from `base.py`) and a `diagnostics()` method returning a plain dict. The following fields appear across all or most adapters:

| Field                         | Matrix               | Meshtastic                | MeshCore | LXMF | Notes                                  |
| ----------------------------- | -------------------- | ------------------------- | -------- | ---- | -------------------------------------- |
| `connected`                   | Yes                  | Yes (in session sub-dict) | Yes      | Yes  | Boolean. Present on all.               |
| `reconnecting`                | Yes                  | Yes (in session sub-dict) | Yes      | Yes  | Boolean. All track reconnect state.    |
| `reconnect_attempts`          | Yes                  | Yes (in session sub-dict) | Yes      | Yes  | Integer. All bounded to max 10.        |
| `transient_delivery_failures` | Yes (adapter-level)  | Yes (in session sub-dict) | Yes      | Yes  | Integer counter.                       |
| `permanent_delivery_failures` | Yes (adapter-level)  | Yes (in session sub-dict) | Yes      | Yes  | Integer counter.                       |
| `last_error`                  | As `last_sync_error` | Yes (in session sub-dict) | Yes      | Yes  | Stringified exception or None.         |
| `started`                     | —                    | Yes                       | Yes      | —    | Meshtastic/MeshCore expose explicitly. |

### 3.2 Transport-Specific Diagnostic Fields

**Matrix** (source: `matrix/adapter.py` `diagnostics()`, `matrix/session.py` `MatrixSessionDiagnostics`):

- `logged_in`: bool — nio login restoration state.
- `sync_task_running`: bool — background sync loop alive.
- `store_path_configured`: bool — E2EE crypto store path present.
- `device_id_configured`: bool — E2EE device ID present.
- `encryption_mode`: str — `"plaintext"`, `"e2ee_optional"`, or `"e2ee_required"`.
- `crypto_enabled`: bool — vodozemac loaded and crypto active.
- `last_crypto_error`: str | None — last E2EE failure reason.
- `encrypted_room_seen`: bool — at least one encrypted room encountered.
- `undecryptable_event_count`: int — messages that failed decryption.
- `sync_running`: bool — sync loop state (Track 1).
- `last_successful_sync`: float | None — epoch timestamp.
- `crypto_store_loaded`: bool — crypto database loaded (Track 2).
- `encrypted_room_count`: int — count of encrypted rooms (Track 4, no room IDs exposed).
- `plaintext_room_count`: int — count of plaintext rooms.

**Meshtastic** (source: `meshtastic/adapter.py` `diagnostics()`, `meshtastic/session.py` `MeshtasticSessionDiagnostics`):

- `adapter_id`: str — adapter identifier.
- `platform`: str — always `"meshtastic"`.
- `connection_type`: str — `"fake"`, `"tcp"`, `"serial"`, `"ble"`.
- `queue_pending`: int — outbound queue depth.
- `queue_total_sent`: int — lifetime sends via queue.
- `queue_total_failed`: int — lifetime failures via queue.
- `background_tasks`: int — tracked asyncio tasks.
- `session.node_id`: str | None — local node number.
- `session.channel_count`: int — configured channels.
- `session.last_packet_time`: float | None — epoch of last received packet.

**MeshCore** (source: `meshcore/adapter.py` `diagnostics()`, `meshcore/session.py` `_SessionDiagnostics`):

- `adapter_id`: str — adapter identifier.
- `platform`: str — always `"meshcore"`.
- `mode`: str — connection type (`"fake"`, `"tcp"`, `"serial"`, `"ble"`).
- `last_message_time`: str | None — ISO 8601 timestamp.
- `peer_count`: int | None — known mesh peers.

**LXMF** (source: `lxmf/session.py` `LxmfSessionDiagnostics`, adapter does not expose its own diagnostics dict):

- `router_running`: bool — LXMRouter is active.
- `last_message_time`: str | None — ISO 8601 timestamp.
- `known_path_count`: int | None — Reticulum path table entries.
- `propagation_enabled`: bool | None — LXMF propagation node state.
- `pending_delivery_count`: int | None — outbound deliveries not yet terminal.
- `mode`: str — connection type (`"fake"` or `"reticulum"`).

### 3.3 Safety Checks: No Secrets, No Raw SDK Objects, No Protobuf

Every diagnostics dataclass and method includes explicit docstring guarantees:

| Adapter    | Docstring Guarantee                                                                                  | Mechanism                                                                                   |
| ---------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Matrix     | "No secrets, access tokens, keys, or private device material"                                        | Frozen dataclass; token/key fields never included; room names/IDs excluded from room counts |
| Meshtastic | "No secrets, private keys, raw protobuf dumps, or sensitive radio identifiers beyond what is public" | Frozen dataclass; node_id is public; no packet payloads                                     |
| MeshCore   | "No secrets, private keys, or raw SDK internals"                                                     | Mutable internal `_SessionDiagnostics`; returned as plain dict copy; no pubkey material     |
| LXMF       | "No secrets, private keys, identity material, raw RNS/LXMF objects, or unsafe peer dumps"            | Frozen dataclass; identity hashes not included; mode is string                              |

**Verification:** All four adapters convert exceptions to `str()` before inclusion. No adapter exposes the underlying SDK client object, connection handle, or crypto material through diagnostics. No protobuf objects, no `LXMessage` instances, no nio client references leak.

### 3.4 Observational-Only Caveat

Diagnostics are **snapshot observations**, not authoritative state. This is implied by the "read-only snapshot" language in all four session diagnostics docstrings but is not explicitly called out in all adapter-level diagnostics methods. The key implications:

1. A `connected: true` diagnostic does not guarantee the next operation will succeed. The transport may have disconnected between the snapshot and the operation.
2. `reconnect_attempts: 0` does not mean the connection is stable. It means no reconnect loop is currently running.
3. Delivery failure counters are cumulative since adapter start, not per-message receipts. Use the delivery receipt system (contract 21) for authoritative delivery state.
4. Diagnostics are not a substitute for the delivery receipt pipeline. They are for operational monitoring and debugging only.

## 4. Session Pattern Audit

### 4.1 Common Lifecycle Shape

All four sessions follow the same conceptual lifecycle:

| Phase            | Matrix                  | Meshtastic                             | MeshCore                               | LXMF                    |
| ---------------- | ----------------------- | -------------------------------------- | -------------------------------------- | ----------------------- |
| Construction     | `__init__(config, ...)` | `__init__(config, ...)`                | `__init__(config, ...)`                | `__init__(config, ...)` |
| Start            | `start()`               | `start()`                              | `start()`                              | `start()`               |
| Stop             | `stop()`                | `stop()`                               | `stop()`                               | `stop()`                |
| Diagnostics      | `diagnostics()`         | `diagnostics()`                        | `diagnostics()`                        | `diagnostics()`         |
| Connection modes | `"fake"`, real via nio  | `"fake"`, `"tcp"`, `"serial"`, `"ble"` | `"fake"`, `"tcp"`, `"serial"`, `"ble"` | `"fake"`, `"reticulum"` |

### 4.2 Callback/Subscription Ownership

| Session           | Callback Registration                                                 | Callback Owner                       | Cleanup                                                     |
| ----------------- | --------------------------------------------------------------------- | ------------------------------------ | ----------------------------------------------------------- |
| MatrixSession     | Registers `_on_room_message` on nio client in `start()`               | Session (via closure)                | `stop()` sets `_closed=True`, cancels sync task             |
| MeshtasticSession | Registers `_on_receive` on Meshtastic interface in `start()`          | Session (stores `_message_callback`) | `stop()` sets `_stop_requested=True`, disconnects interface |
| MeshCoreSession   | Registers callback via `subscribe()` in `_start_real()`               | Session (stores `_message_callback`) | `stop()` calls `disconnect()`, sets guard flags             |
| LxmfSession       | Registers `_on_delivery_state_update` on LXMRouter in `_start_real()` | Session (stores `_message_callback`) | `stop()` tears down router/identity, sets `_stop_requested` |

**Finding:** All four sessions own their callbacks. The adapter provides the `message_callback` to the session constructor. The session registers transport-level callbacks internally. No adapter directly registers callbacks with SDK objects.

### 4.3 Reconnect/Retry Ownership

| Session           | Reconnect Owner              | Max Attempts | Backoff              | Jitter |
| ----------------- | ---------------------------- | ------------ | -------------------- | ------ |
| MatrixSession     | Session (sync recovery loop) | 10           | Exponential, cap 60s | +-25%  |
| MeshtasticSession | Session                      | 10           | Exponential, cap 30s | +-25%  |
| MeshCoreSession   | Session                      | 10           | Exponential, cap 30s | +-25%  |
| LxmfSession       | Session                      | 10           | Exponential, cap 30s | +-25%  |

**Finding:** All four sessions own their own reconnect logic with bounded exponential backoff. Parameters are nearly identical (Meshtastic/MeshCore/LXMF share identical 30s cap and 25% jitter; Matrix uses 60s cap). No runtime-level reconnect orchestration exists.

### 4.4 Outbound Send Retry

| Session                 | Retry Owner           | Max Retries | Scope                         |
| ----------------------- | --------------------- | ----------- | ----------------------------- |
| MatrixSession (adapter) | Adapter `deliver()`   | 3           | Transient network errors only |
| MeshtasticSession       | Session `send_text()` | 3           | Transient send failures       |
| MeshCoreSession         | Session `send_text()` | 3           | Transient send failures       |
| LxmfSession             | Session `send_text()` | 3           | Transient send failures       |

### 4.5 Divergent Transport-Specific Behavior

| Behavior              | Matrix                                   | Meshtastic                            | MeshCore                                         | LXMF                                 |
| --------------------- | ---------------------------------------- | ------------------------------------- | ------------------------------------------------ | ------------------------------------ |
| Sync model            | Long-poll/WebSocket `/sync` loop         | Event-driven pubsub callback          | Event-driven subscribe callback                  | Event-driven delivery callback       |
| Authentication        | Access token + optional E2EE device keys | None (radio identity)                 | None (radio identity)                            | Reticulum identity file              |
| E2EE                  | Yes (via vodozemac/olm/megolm)           | No (AES-256 CTR at channel level)     | Yes (built into protocol)                        | Yes (Reticulum link-layer)           |
| Inbound message model | Persistent ordered event stream          | Fire-and-forget broadcast/directed    | Fire-and-forget broadcast/directed               | Async multi-hop store-and-forward    |
| Outbound queue        | None (direct `room_send`)                | `MeshtasticOutboundQueue` with pacing | Direct `send_text()`                             | Direct `send_text()`                 |
| Delivery confirmation | Server `event_id` response               | Optional ACK (not de-duplicated)      | Optional ACK (not de-duplicated)                 | Async state progression via callback |
| Room/channel concept  | Room ID (string)                         | Channel index (integer)               | Channel index (integer) or contact pubkey prefix | Destination hash (16-byte hex)       |

### 4.6 Size/Complexity

| Session           | LOC  | Key Complexity Drivers                                                                                                       |
| ----------------- | ---- | ---------------------------------------------------------------------------------------------------------------------------- |
| MatrixSession     | 682  | E2EE lifecycle, sync recovery, room encryption state tracking, crypto store continuity                                       |
| MeshtasticSession | 608  | Outbound send with retry, pubsub callback wiring, connection type dispatch                                                   |
| MeshCoreSession   | 654  | Connection type dispatch, event subscription, outbound retry                                                                 |
| LxmfSession       | 1260 | Delivery state model (8 states), delivery state tracking, outbound delivery map, state update callbacks, Reticulum lifecycle |

**LXMF is roughly 2x the size** of the other three. The primary complexity driver is the honest delivery state model: LXMF has 8 discrete delivery states (generating, outbound, sending, sent, delivered, failed, rejected, cancelled) and tracks each outbound delivery individually with state change callbacks. This is an inherent transport asymmetry, not a design flaw.

### 4.7 Cleanup/Refactor Recommendations

1. **LXMF delivery tracking could be extracted** into a standalone `_DeliveryTracker` class to reduce session LOC. This is not urgent.
2. **Matrix's 60s backoff cap differs** from the 30s cap on the other three. Consider aligning to 30s for consistency, or document why Matrix needs longer. Low priority.
3. **Meshtastic is the only adapter with an outbound queue** (`MeshtasticOutboundQueue`). The other three send directly. This is by design (Meshtastic's 228-byte packet limit and radio duty cycle constraints), but it's an architectural divergence worth noting.
4. **No broad refactor is recommended now.** The session pattern is consistent enough. Divergences reflect genuine transport differences.

## 5. Metadata Envelope Consistency

### 5.1 Namespaced Metadata Only

All transport metadata flows through the `EventMetadata` model (defined in `core/events/metadata.py`) with six namespaces: `transport`, `routing`, `radio`, `telemetry`, `native`, `custom`. Per contract 26, transport-specific data goes into `metadata.native.data` under a per-transport namespace key.

| Adapter    | Native Metadata Namespace            | Location              |
| ---------- | ------------------------------------ | --------------------- |
| Matrix     | `metadata.native.data["matrix"]`     | `matrix/codec.py`     |
| Meshtastic | `metadata.native.data["meshtastic"]` | `meshtastic/codec.py` |
| MeshCore   | `metadata.native.data["meshcore"]`   | `meshcore/codec.py`   |
| LXMF       | `metadata.native.data["lxmf"]`       | `lxmf/codec.py`       |

### 5.2 No Loose Ad-Hoc Transport Fields

**Verified:** No adapter injects loose transport-specific fields directly onto `CanonicalEvent` or top-level `EventMetadata`. All transport data is namespaced under `metadata.native.data[<transport>]`. The codec layer enforces this boundary.

### 5.3 No Protobuf/SDK Object Leakage

| Adapter    | Inbound Normalization                                                                                                                                     | Outbound                                        |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| Matrix     | Codec converts nio-shaped event dicts (already plain dicts from nio). No nio objects cross the codec boundary.                                            | Renderer produces plain `m.room.message` dicts. |
| Meshtastic | Codec receives packet dicts (Meshtastic library converts protobuf to dict before callback). No protobuf objects.                                          | Renderer produces plain text dicts for queue.   |
| MeshCore   | Session normalizes SDK events to plain dicts before callback. No `Event` objects leak.                                                                    | Renderer produces plain text dicts.             |
| LXMF       | Session normalizes `LXMessage` to plain dict with hex strings for hashes. No `LXMessage`, `RNS.Destination`, or `RNS.Identity` objects leave the session. | Renderer produces plain content dicts.          |

**Verified:** No protobuf objects, no SDK objects, no binary wire-format types cross any adapter boundary in either direction.

### 5.4 Outbound Envelope Support Per Transport

| Adapter    | Outbound Metadata Envelope | Mechanism                                                                                                                                                      |
| ---------- | -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Yes                        | `MatrixRenderer` builds `m.room.message` with metadata envelope subtree and `m.relates_to` for threading. `room_id` is stripped before send.                   |
| Meshtastic | Yes (limited)              | `MeshtasticRenderer` builds text payloads. Channel index and destination embedded in payload dict. No metadata envelope subtree — payload is the message text. |
| MeshCore   | Yes (limited)              | `MeshCoreRenderer` builds text payloads with optional `channel_index` and `contact_id`. No metadata envelope subtree — constrained payload.                    |
| LXMF       | Yes                        | `LxmfRenderer` builds content dict with `content`, `title`, `destination_hash`, `delivery_method`, `fields`. Structured but not a Matrix-style envelope.       |

**Finding:** Matrix has the richest outbound metadata envelope (JSON structure within `m.room.message`). Meshtastic and MeshCore are constrained by payload size (228 bytes, ~200 bytes) and cannot carry rich metadata envelopes. LXMF carries structured fields via its `fields` dict. This asymmetry is inherent to transport constraints.

### 5.5 Honest Lossiness

| Transport  | Lossiness                                                                                                                                                        | Documented                                                  |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| Matrix     | No inherent message loss. Server stores persistently. Sync echoes self-messages.                                                                                 | Yes (contract 22)                                           |
| Meshtastic | Fire-and-forget with optional ACK. Messages may be lost due to radio conditions, hop limits, or duty cycle. No de-duplication of ACKs.                           | Yes (contract 22, session docstring duplicate-send warning) |
| MeshCore   | Fire-and-forget with optional ACK. Same loss characteristics as Meshtastic. No de-duplication of ACKs.                                                           | Yes (session docstring duplicate-send warning)              |
| LXMF       | Async store-and-forward. Messages may be delayed hours/days on propagation nodes. Delivery state is tracked but eventual. `pending` is the honest initial state. | Yes (session docstring, runbook)                            |

**Verified:** All lossy transports document their lossiness honestly. No adapter claims delivery confirmation that the transport does not provide.

## 6. Delivery Semantics Findings

### 6.1 native_message_id Semantics

| Transport  | Source of ID                   | Type                                | When Available                          | Guarantees                                |
| ---------- | ------------------------------ | ----------------------------------- | --------------------------------------- | ----------------------------------------- |
| Matrix     | Homeserver-assigned `event_id` | String (e.g. `$xxx`)                | Immediately on `RoomSendResponse`       | Globally unique, persistent, queryable    |
| Meshtastic | Firmware-assigned packet ID    | Integer (32-bit) → stored as string | On send acknowledgment (if ACK enabled) | Unique per sender, may wrap at 2^32       |
| MeshCore   | SDK-assigned message ID        | String                              | On send return                          | Unique within session context             |
| LXMF       | `LXMessage.hash` (hex)         | String (hex of message hash)        | On message creation                     | Cryptographically unique, tied to content |

**Matrix is the only transport where `native_message_id` implies confirmed delivery.** On Meshtastic, MeshCore, and LXMF, a `native_message_id` indicates the message was submitted to the transport layer, not that it was received by the destination.

### 6.2 delivery_state Metadata Shape

| Transport  | delivery_state in AdapterDeliveryResult.metadata | Shape                                                                                                                          |
| ---------- | ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------ |
| Matrix     | Not included                                     | N/A — `event_id` from homeserver response implies synchronous delivery                                                         |
| Meshtastic | Not included                                     | N/A — fire-and-forget; no state progression model                                                                              |
| MeshCore   | Not included                                     | N/A — fire-and-forget; no state progression model                                                                              |
| LXMF       | `metadata["lxmf"]["delivery_state"]`             | String enum value: `"generating"`, `"outbound"`, `"sending"`, `"sent"`, `"delivered"`, `"failed"`, `"rejected"`, `"cancelled"` |

**LXMF is the only adapter that includes `delivery_state` in the delivery result metadata.** This is because LXMF is the only transport with an asynchronous delivery state progression model. The other three either confirm synchronously (Matrix) or have no meaningful state model (Meshtastic/MeshCore).

### 6.3 Async vs Immediate Delivery Differences

| Transport  | Synchronous?                              | Return Behavior                                                                                                                                                 |
| ---------- | ----------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Yes (with retry)                          | `deliver()` returns `AdapterDeliveryResult` with `event_id` after server response                                                                               |
| Meshtastic | No (queued)                               | `deliver()` enqueues to outbound queue, returns `None`. Actual send is async via queue worker.                                                                  |
| MeshCore   | Yes (direct send with retry)              | `deliver()` returns `AdapterDeliveryResult` with native_id after send completes                                                                                 |
| LXMF       | Yes (send initiated, state tracked async) | `deliver()` returns `AdapterDeliveryResult` with native_id and initial `delivery_state` (typically `"outbound"`). State progresses asynchronously via callback. |

**Meshtastic is unique** in that `deliver()` returns `None` synchronously and the actual send happens asynchronously through the queue. All others return a result synchronously (though LXMF's delivery completes asynchronously).

### 6.4 Failed Delivery Behavior

| Transport  | On Transient Failure                                                                                                                                                                           | On Permanent Failure                                                                                           |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Matrix     | Retry up to 3x with exponential backoff (500ms, 1s, 2s +-25% jitter). On exhaustion: adapter normalizes internal error and raises `AdapterSendError(transient=True)`.                          | Adapter normalizes internal error and raises `AdapterPermanentError` immediately.                              |
| Meshtastic | Session retries send up to 3x. On exhaustion: increment `transient_delivery_failures`, adapter normalizes internal error and raises `AdapterSendError(transient=True)`. Queue marks as failed. | Increment `permanent_delivery_failures`, adapter normalizes internal error and raises `AdapterPermanentError`. |
| MeshCore   | Session retries send up to 3x. On exhaustion: increment counters, adapter normalizes internal error and raises `AdapterSendError(transient=True)`.                                             | Increment counters, adapter normalizes internal error and raises `AdapterPermanentError`.                      |
| LXMF       | Session retries send up to 3x. On exhaustion: increment counters, adapter normalizes internal error and raises `AdapterSendError(transient=True)`.                                             | Increment counters, adapter normalizes internal error and raises `AdapterPermanentError`.                      |

All four adapters normalize session/internal transport errors into `AdapterSendError`/`AdapterPermanentError` at the runtime boundary before letting exceptions propagate to the pipeline, which records delivery receipts via the retry/dead-letter system (contract 21, `phase-1-limitations.md` Track 3). The pipeline's `classify_failure` relies only on `AdapterSendError.transient`, not on the transport-specific `*SendError` hierarchy.

### 6.5 Duplicate-Send Risk Where Retries Exist

All four adapters acknowledge duplicate-send risk from their bounded retry:

| Adapter    | Documented                                                                                                                                       | Mechanism of Duplication                                                                                |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| Matrix     | Yes (adapter `deliver()` docstring: "Retry may cause duplicate messages if the first attempt succeeded on the server but the response was lost") | Send succeeded on homeserver, but HTTP response lost. Retry sends again.                                |
| Meshtastic | Yes (session docstring: duplicate-send risk from ACK loss)                                                                                       | Send received by remote node, but ACK lost on radio link. Retry sends again.                            |
| MeshCore   | Yes (session docstring: explicit duplicate-send risk warning)                                                                                    | Same as Meshtastic.                                                                                     |
| LXMF       | Implicit (LXMF `send()` is idempotent per message hash, but retries with new messages would be distinct)                                         | Retries are on new `send_text()` calls. If first send entered the network, retry creates a new message. |

**Consumers must be tolerant of duplicate deliveries.** This is a fundamental property of at-least-once delivery with bounded retry.

## 7. Live Test Harness Inventory

### 7.1 Default Exclusion Mechanism

All live tests are excluded from the default `pytest` run via `pyproject.toml`:

```toml
[tool.pytest.ini_options]
addopts = "-m 'not live'"
markers = ["live: tests requiring real network/hardware"]
```

Each live test file also applies module-level `pytestmark` with both `pytest.mark.live` and a `skipif` guard that checks for required environment variables.

### 7.2 Per-Transport Harness Details

#### Matrix (`tests/test_matrix_live.py`, 834 LOC)

| Attribute              | Value                                                                                                                                                                                                     |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Test class             | `TestMatrixLiveSmoke`                                                                                                                                                                                     |
| Test count             | 12 async test functions                                                                                                                                                                                   |
| Required env vars      | `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, `MATRIX_ACCESS_TOKEN`, `MATRIX_ROOM_ID`                                                                                                                            |
| Secret handling        | `MATRIX_ACCESS_TOKEN` read from env var only. Never logged, never committed.                                                                                                                              |
| What it proves         | `start()` connects to real homeserver; `deliver()` returns real `event_id`; `health_check()` transitions correctly; `stop()` cleans up; self-message suppression; allowlist enforcement; redelivery smoke |
| What it does NOT prove | E2EE, reactions, edits, deletes, attachments, admin APIs, webhook server, non-Matrix connectivity, inbound reception from third party                                                                     |
| Runbook                | `docs/runbooks/matrix-live-smoke.md`, `docs/runbooks/matrix-alpha-operation.md`                                                                                                                           |

#### Matrix E2EE (`tests/test_matrix_e2ee_live.py`, 306 LOC)

| Attribute              | Value                                                                                                                   |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Test classes           | `TestLiveE2EEStart`, `TestLiveE2EESend`                                                                                 |
| Test count             | ~4 async test functions                                                                                                 |
| Required env vars      | `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, `MATRIX_ACCESS_TOKEN`, `MATRIX_ROOM_ID`, `MATRIX_DEVICE_ID`, `MATRIX_STORE_PATH` |
| Secret handling        | Same as Matrix live. Device ID and store path are operational, not secrets.                                             |
| What it proves         | E2EE-required mode starts with valid config; encrypted text sends and decrypts; crypto store persists across restarts   |
| What it does NOT prove | Multi-device key verification, key rotation, cross-signed device trust, megolm session corruption recovery              |
| Runbook                | `docs/runbooks/matrix-live-smoke.md` (E2EE section)                                                                     |

#### Meshtastic (`tests/test_meshtastic_live.py`, 616 LOC)

| Attribute              | Value                                                                                                                                                                    |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Test class             | `TestMeshtasticLiveSmoke` (Category B: MEDRE adapter), plus Category A raw `mtjk` API tests                                                                              |
| Test count             | 10 async test functions                                                                                                                                                  |
| Required env vars      | `MESHTASTIC_CONNECTION_TYPE`, `MESHTASTIC_HOST` (for TCP), `MESHTASTIC_CHANNEL_INDEX`                                                                                    |
| Secret handling        | No secrets required. Radio connection parameters only.                                                                                                                   |
| What it proves         | TCP interface connects; adapter starts and reports healthy; diagnostics expose session state; raw send via interface; pubsub callback receives packets; start/stop cycle |
| What it does NOT prove | Reliable delivery, multi-hop routing, channel encryption, DM delivery, backlog handling                                                                                  |
| Runbook                | `docs/runbooks/meshtastic-live-smoke.md`, `docs/runbooks/meshtastic-alpha-operation.md`                                                                                  |

#### MeshCore (`tests/test_meshcore_live.py`, 401 LOC)

| Attribute              | Value                                                                                                                                                                                                                           |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Test class             | `TestMeshCoreLiveSmoke`                                                                                                                                                                                                         |
| Test count             | 8 async test functions                                                                                                                                                                                                          |
| Required env vars      | `MESHCORE_CONNECTION_TYPE`, `MESHCORE_HOST` (for TCP), `MESHCORE_CHANNEL_INDEX`                                                                                                                                                 |
| Secret handling        | No secrets required. Radio connection parameters only.                                                                                                                                                                          |
| What it proves         | Adapter starts and reports healthy; session connected after start; session disconnected after stop; diagnostics available and contain no secrets; channel message send; inbound callback receives messages; repeated start/stop |
| What it does NOT prove | Reliable delivery, E2EE, multi-hop routing, telemetry, position data                                                                                                                                                            |
| Runbook                | `docs/runbooks/meshcore-live-smoke.md`, `docs/runbooks/meshcore-alpha-operation.md`                                                                                                                                             |

#### LXMF (`tests/test_lxmf_live.py`, 829 LOC)

| Attribute              | Value                                                                                                                                                                                                                                                                                                                                               |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Test class             | `TestLxmfLiveSmoke`                                                                                                                                                                                                                                                                                                                                 |
| Test count             | 19 async test functions                                                                                                                                                                                                                                                                                                                             |
| Required env vars      | `LXMF_CONNECTION_TYPE`, `LXMF_IDENTITY_PATH`                                                                                                                                                                                                                                                                                                        |
| Optional env vars      | `LXMF_DISPLAY_NAME`, `LXMF_DESTINATION_HASH`                                                                                                                                                                                                                                                                                                        |
| Secret handling        | `LXMF_IDENTITY_PATH` points to a file containing the Reticulum private key (64 bytes). The identity file must be protected. Tests never log the file contents.                                                                                                                                                                                      |
| What it proves         | Config validation; adapter starts and reports healthy; health transitions; outbound send returns delivery result with unique IDs; type validation; inbound session callback wired; simulate_inbound publishes; restart lifecycle (start/stop/start/stop); rapid start/stop cycles; full lifecycle; diagnostics after start; E2EE not supported note |
| What it does NOT prove | Multi-hop delivery, propagation node operation, delivery state progression to "delivered", message signing verification, real inbound from third party                                                                                                                                                                                              |
| Runbook                | `docs/runbooks/lxmf-live-smoke.md`, `docs/runbooks/lxmf-alpha-operation.md`                                                                                                                                                                                                                                                                         |

### 7.3 Summary: What Live Harnesses Collectively Prove

| Capability                            | Proven by Live Harness?                                        |
| ------------------------------------- | -------------------------------------------------------------- |
| Adapter lifecycle (start/stop/health) | Yes — all four                                                 |
| Diagnostics snapshot (no secrets)     | Yes — all four                                                 |
| Outbound send returns native ID       | Yes — Matrix, MeshCore, LXMF (Meshtastic returns None, queued) |
| Inbound message callback fires        | Yes — Matrix, MeshCore, Meshtastic (LXMF simulated)            |
| Delivery state progression            | Partially — LXMF tracks state; others are fire-and-forget      |
| E2EE                                  | Yes — Matrix E2EE live harness                                 |
| Reconnect resilience                  | Not tested — no live test exercises reconnect                  |
| Multi-hop delivery                    | Not tested                                                     |
| Production reliability claims         | No — these are smoke tests, not reliability tests              |

### 7.4 Summary: What Live Harnesses Do NOT Prove

- No harness tests reconnect under real network failure.
- No harness tests sustained high-throughput message flow.
- No harness tests multi-hop delivery (Meshtastic, MeshCore, LXMF).
- No harness tests delivery receipt pipeline against real network.
- No harness tests concurrent delivery to multiple targets.
- No Meshtastic outbound delivery result (returns None due to queue architecture).

## 8. Cross-Cutting Findings

### 8.1 Consistency Verdict

The four adapters are **structurally consistent** in their session lifecycle, diagnostics safety guarantees, metadata namespacing, and reconnect parameters. The divergences that exist reflect genuine transport differences:

- Matrix's richer diagnostics (E2EE state, room encryption counts) reflect its richer feature set.
- Meshtastic's outbound queue reflects radio duty cycle constraints.
- LXMF's larger session and delivery state model reflect its async store-and-forward architecture.
- MeshCore is the simplest session, closest to Meshtastic in shape.

### 8.2 Recommendations (No Code Changes)

1. **Document the "diagnostics are not authoritative state" caveat** in adapter-level diagnostics methods. Currently implied by "read-only snapshot" language but could be more explicit.
2. **Consider extracting LXMF delivery tracking** into a helper class in a future refactor tranche. Not blocking.
3. **Consider adding a `delivery_state` field to Meshtastic and MeshCore delivery results** for consistency, even if the value is always `"sent"` or `"unknown"`. Not blocking — the absence is honest.
4. **Consider aligning Matrix's backoff cap** to 30s to match the other three. Not blocking.

# Adapter Reality Audit

**Work Package**: Adapter Reality Audit & Boundary Tightening
**Branch**: `adapter-reality-audit`
**Date**: 2026-06-05
**Scope**: All 4 real adapters + transport profile docs + `AdapterDeliveryResult` contract

## 1. Goal

Validate that MEDRE's adapter assumptions match the real reference
ecosystem (mtjk, mindroom-nio, meshcore_py, LXMF, plus upstream
matrix-spec and Meshtastic firmware protobufs) and tighten the
adapter fact vocabulary where it was ambiguous.

**Core owns lifecycle; adapters report facts; adapters must not become hidden
lifecycle authorities.** See `lifecycle-authority-audit.md` for the vocabulary
audit that established this boundary.

## 2. References Consulted

| Reference                 | URL                                                                                    | Version             | What was checked                                                                                                         |
| ------------------------- | -------------------------------------------------------------------------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| mmrelay                   | <https://github.com/jeremiah-k/meshtastic-matrix-relay>                                | commit `7b9efca`    | Meshtastic packet handling, relay logic, self-echo filtering                                                             |
| meshtastic-python (mtjk)  | <https://github.com/jeremiah-k/meshtastic-python>                                      | v2.5.10             | SDK API shapes, `sendText()` return, `myInfo.myNodeNum`, classifier portnum map                                          |
| Meshtastic protobufs      | <https://github.com/meshtastic/protobufs>                                              | snapshot 2026-05-27 | `PortNum` enum values, `Data.reply_id`, `Data.emoji`, packet field presence                                              |
| mindroom-nio              | <https://github.com/mindroom-ai/mindroom-nio>                                          | snapshot 2026-05-04 | Matrix send responses, `event_id` presence, error code strings (`M_FORBIDDEN`, `M_NOT_FOUND`, `M_DUPLICATE_ANNOTATION`)  |
| Matrix client-server spec | <https://spec.matrix.org/v1.14/client-server-api/> + Context7 `matrix-org/matrix-spec` | v1.14               | `txn_id` semantics, annotation endpoint errors, `event_id` vs `M_UNKNOWN` edge cases                                     |
| meshcore_py               | <https://github.com/meshcore-dev/meshcore_py>                                          | v2.2.5              | `send_msg()` return shape (`expected_ack`, `suggested_timeout`), `send_appstart()` requirement, `MSG_SENT` event payload |
| MeshCore firmware         | <https://github.com/meshcore-dev/MeshCore>                                             | snapshot 2026-04-28 | APP_START command requirement after connect, ACK protocol docs                                                           |
| LXMF                      | <https://github.com/markqvist/LXMF> + Context7 `markqvist/lxmf`                        | v0.9.6              | `LXMessage` state model, delivery state progression, `RNS.Identity.recall()` vs self-identity destination                |
| Reticulum docs            | <https://reticulum.network/manual/reference.html>                                      | accessed 2026-06-05 | `RNS.Destination` constructor semantics, identity recall API                                                             |

## 3. Confirmed Assumptions (no fix needed)

### Meshtastic Assumptions

- `sendText()` returns a `MeshPacket` with populated `id` field; usable as `native_message_id`
- `decoded.replyId` (protobuf `Data.reply_id`) is optional int; confirmed in firmware protobufs
- `decoded.emoji` (protobuf `Data.emoji`) is optional int; confirmed in firmware protobufs
- `_NUMERIC_PORTNUM_FALLBACK` in `packet_classifier.py` matches protobuf `PortNum` enum exactly
- `fromId`/`toId` enrichment is SDK-added (not protobuf); confirmed from mtjk source
- MMRelay uses 3-action model (`RELAY`, `PLUGIN_ONLY`, `DROP`); MEDRE's 4-action model is a superset
- `rxTime` used for startup backlog suppression is protobuf `MeshPacket.rx_time`

### MeshCore Assumptions

- `CONTACT_MSG_RECV` carries `pubkey_prefix` (6-byte hex); `CHANNEL_MSG_RECV` carries `channel_idx`
- No native reply mechanism, no native reaction mechanism, no protobuf at any layer
- E2EE is always-on (AES-128 + HMAC, no toggle)
- Contact list is dict keyed by pubkey hex; no numeric node ID
- `send_msg()` returns `Event` with `expected_ack` (4-byte hex) and `suggested_timeout`

### Matrix Assumptions

- `txn_id` is client-generated and used for idempotent PUT `/send/{txn_id}`; confirmed in spec
- Successful send returns `event_id`; failed send lacks it
- `M_FORBIDDEN`, `M_NOT_FOUND` are permanent; rate-limit responses are transient
- nio `RoomSendResponse` carries `event_id` on success; error responses lack it

### LXMF Assumptions

- 9-state delivery model maps correctly to `LXMF.LXMessage` states
- Identity hash is 16-byte truncated SHA-256; destination hash is one-way derived
- `LXMessage` wire format: `dest_hash` + `src_hash` + signature + msgpack payload
- Thread-to-asyncio bridge uses `call_soon_threadsafe` (not `create_task`)
- Outbound tracking bounded at `_MAX_OUTBOUND_DELIVERIES = 1000` with FIFO eviction

## 4. Corrected Assumptions (fixes applied)

### R1: Matrix permanent error codes incomplete

`M_DUPLICATE_ANNOTATION` (HTTP 400 on duplicate reaction) was missing from
`_PERMANENT_ERRCODES`. Without it, a duplicate reaction would be retried
indefinitely instead of being classified as permanent.

- `src/medre/adapters/matrix/adapter.py:79`
- Updated docstring at line 179

### R2: Meshtastic classifier lacks self-echo detection

MEDRE would relay packets sent by its own node. MMRelay filters these
at the relay layer; MEDRE's classifier had no equivalent.

- Added `own_node_id` keyword parameter to `classify()`
- Added `is_self_echo` field to `ClassificationResult`
- Added `REASON_SELF_ECHO` constant
- `src/medre/adapters/meshtastic/packet_classifier.py:44,273,329,378-381,476-478,555`

### R3: Meshtastic adapter does not pass own node ID to classifier

The adapter owns the session (which holds `node_id`); the classifier is
stateless. The adapter must pass its own node ID on every `classify()` call.

- Real path and simulate path both pass `own_node_id`
- Added `classifier_packets_self_echo_ignored` diagnostic counter
- `src/medre/adapters/meshtastic/adapter.py:191,457,612-613,704,771`

### R4: MeshCore session skips `send_appstart()` after connect

meshcore_py requires `send_appstart()` after every successful connect or
reconnect. Without it, the firmware may ignore subsequent commands.

- Added `send_appstart()` call after event subscription
- On failure: disconnects, clears subscriptions, raises `MeshCoreConnectionError`
- `src/medre/adapters/meshcore/session.py:495-513`

### R5: MeshCore native_id extraction guesses wrong field

The adapter tried `message_id` from the SDK result, but meshcore_py's
`MSG_SENT` returns `expected_ack` (4 bytes) as the canonical message
identifier. Channel sends return OK with no ID.

- Now extracts `expected_ack` first, falls back to `message_id`
- Converts `bytes` to hex string for consistency
- `src/medre/adapters/meshcore/session.py:736-758`

### R8: LXMF destination construction uses self-identity

The session constructed `RNS.Destination(self._identity, ...)` then
overwrote `dest.hash`. Correct pattern is `RNS.Identity.recall(dest_bytes)`
to look up the remote identity, then construct `Destination` from that.

- Uses `RNS.Identity.recall()` to get remote identity
- Raises `LxmfSendError(transient=False)` if identity not found
- `src/medre/adapters/lxmf/session.py:1224-1232`

### R9: LXMF session registers unused announce callback

`_on_lxmf_announce` was registered but only logged at DEBUG. No canonical
event was produced, no tests covered it, and not all LXMF versions support
the callback. Removed the handler and registration.

- `src/medre/adapters/lxmf/session.py` (removed `_on_lxmf_announce` method
  and `register_announce_callback` call)

### R10: LXMF retry loop does not handle `LxmfSendError` distinctly

The retry loop caught generic `Exception` but not `LxmfSendError`. A
permanent `LxmfSendError` (e.g., identity recall failure) would be retried
instead of failing fast.

- Added explicit `LxmfSendError` handler before generic `Exception`
- Permanent errors increment `permanent_delivery_failures` and re-raise
- Transient errors use same backoff as generic path
- `src/medre/adapters/lxmf/session.py:1288-1304`

### Spec and profile updates

- `docs/spec/adapter-runtime.md` section 9.2: removed `adapter_status` from
  permitted top-level metadata keys; all adapter-specific state now lives
  under `metadata[<transport>]`
- `docs/spec/transport-profiles/meshcore.md`: updated all references from
  `metadata.adapter_status="local_accepted"` to `metadata["meshcore"]["local_acceptance"]=True`

## 5. Renames (lifecycle boundary clarity)

### `metadata['adapter_status']` to `metadata['meshcore']['local_acceptance']`

`adapter_status` was a top-level metadata key that looked like a lifecycle
status field. Renamed to live inside the `meshcore` namespace as a boolean,
making it clearly transport-local evidence rather than a pipeline status.

- `src/medre/adapters/meshcore/adapter.py:430`
- `src/medre/adapters/fakes/meshcore.py:366`
- Updated tests: `tests/test_adapter_parity.py:333,689`, `tests/test_docs_lifecycle_authority.py:442-497`

### `LxmfDeliveryState.UNKNOWN` to `UNMAPPED`

`UNKNOWN` implied uncertainty about the state value. `UNMAPPED` is clearer:
the value was received but MEDRE has no mapping for it. This is not
"unknown," it is explicitly unmapped.

- `src/medre/adapters/lxmf/session.py:185`
- Updated all references: lines 237, 241, 248, 249

## 6. Tests Added

### Meshtastic Test Coverage

| File                                           | Lines | Status                                                          |
| ---------------------------------------------- | ----- | --------------------------------------------------------------- |
| `tests/test_meshtastic_packet_classifier.py`   | 559   | Refactored (1498 to 559; metadata tests moved out)              |
| `tests/test_meshtastic_classifier_metadata.py` | 1049  | New (split from packet classifier)                              |
| `tests/test_meshtastic_adapter.py`             | 1002  | Modified (+59 lines: self-echo tests, classifier counter tests) |

### MeshCore Test Coverage

| File                                      | Lines | Status                                                           |
| ----------------------------------------- | ----- | ---------------------------------------------------------------- |
| `tests/test_meshcore_session.py`          | 1008  | Refactored (startup/recovery moved out)                          |
| `tests/test_meshcore_session_startup.py`  | 725   | New (startup lifecycle)                                          |
| `tests/test_meshcore_session_recovery.py` | 756   | New (reconnection recovery)                                      |
| `tests/helpers/meshcore_session.py`       | 81    | New (shared test fixtures)                                       |
| `tests/test_meshcore_adapter.py`          | 1004  | Modified (+60 lines: appstart, metadata namespace, expected_ack) |

### LXMF Test Coverage

| File                                 | Lines | Status                                                                                |
| ------------------------------------ | ----- | ------------------------------------------------------------------------------------- |
| `tests/test_lxmf_session.py`         | 1482  | Modified (+102 lines: UNMAPPED rename, recall-based destination, LxmfSendError retry) |
| `tests/test_lxmf_session_startup.py` | 620   | Modified (+12 lines: announce callback removal)                                       |

### Matrix Test Coverage

| File                              | Lines | Status                                                            |
| --------------------------------- | ----- | ----------------------------------------------------------------- |
| `tests/test_matrix_boundaries.py` | 790   | Modified (+41 lines: M_DUPLICATE_ANNOTATION permanent error test) |

### Cross-adapter

| File                                     | Lines   | Status                                         |
| ---------------------------------------- | ------- | ---------------------------------------------- |
| `tests/test_adapter_parity.py`           | updated | Modified (meshcore namespace assertion)        |
| `tests/test_docs_lifecycle_authority.py` | updated | Modified (meshcore namespace regression tests) |

**Total test lines: 8,295 across 11 files.**

## 7. Remaining Risks (deferred to future work packages)

| Item                                    | Risk     | Notes                                                                                                                                                                                                                                                                                                      |
| --------------------------------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R11 `delivery_receipts` capability      | Low      | Matrix/LXMF semantics debatable; not obviously wrong                                                                                                                                                                                                                                                       |
| R12 `text_message_ack` portnum          | Cosmetic | Artifact in portnum handling; no functional impact                                                                                                                                                                                                                                                         |
| Meshtastic byte budget (B2)             | Medium   | 227 bytes doesn't account for protobuf overhead when `reply_id` is set; could break valid sends. Needs user decision on whether to reduce budget or measure dynamically                                                                                                                                    |
| MeshCore hardcoded port 4000            | Low      | Fallback is intentional config behavior; unclear if should require explicit user opt-in                                                                                                                                                                                                                    |
| MeshCore reconnect parameters           | Low      | 1s to 30s backoff, 10 attempts is more aggressive than SDK default. Intentional for MEDRE's use case                                                                                                                                                                                                       |
| `M_UNKNOWN` HTTP status check           | Cosmetic | Current behavior treats M_UNKNOWN as permanent (in `_PERMANENT_ERRCODES`); may be overly conservative — unknown server errors are usually transient. Deferred.                                                                                                                                             |
| LXMF "in production" destination nuance | Low      | Comment removed during fix; destination lookup complexity deferred to future work                                                                                                                                                                                                                          |
| ~~Meshtastic `_node_id` population~~    | ~~High~~ | **Resolved in this work package.** `_refresh_node_id()` added to session (lines 730-742); called after connect/reconnect. Additionally, `_on_receive()` performs a lazy refresh when `_node_id is None`, covering late-arriving `myInfo`. Self-echo detection now activates without requiring a reconnect. |

## 8. Next Work Package

Two candidates from the backlog:

1. **RetryWorker / DeliveryLifecycleService convergence** (from `lifecycle-authority-audit.md` deferred refactors). Retry logic is split between scheduling (RetryWorker) and state transitions (DeliveryLifecycleService). Consolidating the boundary would close the gap identified in the prior work package.

2. **Continue adapter hardening.** The remaining risks in section 7 include actionable items: ~~Meshtastic `node_id` population (unblocks self-echo detection at runtime)~~ _(Done — see §7 for resolution note)_, Meshtastic byte budget for structured messages, and MeshCore hardcoded port handling.

Either work package builds on this audit's evidence without reopening the reference checks already completed.

# Transport Failure Taxonomy Contract

> Contract version: 1
> Last updated: 2026-05-10
> Track: 9 (Transport Capability Contracts)
> Supersedes: Nothing. Formalizes failure-mode observations from contracts 22, 31, 18.
> Status: Contract. Documents observed and inferred failure modes per transport.

This document classifies the failure modes of each MEDRE transport along axes
that matter for operational reasoning: transient vs. permanent, reconnectable
vs. unrecoverable, duplicate-send risk, queue-drain semantics, delivery
uncertainty windows, encrypted-room failure classes, and transport-specific
caveats.

It does **not** normalize failure modes across transports. If two transports
handle the same class of failure differently, this document records both
behaviors honestly. It does **not** propose new features, retry engines, or
cross-transport orchestration.

## 1. Scope

- Per-transport failure classification along defined axes.
- Duplicate-send risk assessment per transport.
- Queue-drain and message-loss semantics.
- Delivery uncertainty windows and their duration.
- Encrypted-room / E2EE failure classes.
- Transport-specific caveats and edge cases.

## 2. Non-goals

- Proposing cross-transport failure normalization.
- Implementing retry schedulers or reliability engines.
- Claiming production failure coverage (no live failure injection data).
- Comparing transports on dimensions unrelated to failure behavior.

## 3. Failure Classification Axes

### 3.1 Transient vs. Permanent

| Axis          | Definition                                                                                   |
| ------------- | -------------------------------------------------------------------------------------------- |
| **Transient** | Failure may resolve with time, retry, or reconnection. The message may still be deliverable. |
| **Permanent** | Failure is definitive. The message will not be delivered regardless of retries.              |

### 3.2 Reconnectable vs. Unrecoverable

| Axis              | Definition                                                                                  |
| ----------------- | ------------------------------------------------------------------------------------------- |
| **Reconnectable** | The transport session can re-establish connectivity and resume operation after the failure. |
| **Unrecoverable** | The transport session cannot recover; a new session must be created.                        |

### 3.3 Duplicate-Send Risk

| Level      | Definition                                                                |
| ---------- | ------------------------------------------------------------------------- |
| **None**   | The transport guarantees exactly-once delivery or idempotent sends.       |
| **Low**    | Duplicates are possible only under specific, documented conditions.       |
| **Medium** | Duplicates are possible under normal failure/retry scenarios.             |
| **High**   | Duplicates are likely during normal operation; consumer must deduplicate. |

### 3.4 Queue-Drain Semantics

| Category           | Definition                                                                                                                                   |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------- |
| **FIFO drain**     | Messages are drained in order; no reordering under normal conditions.                                                                        |
| **Lossy drain**    | Some messages may be silently dropped during drain (e.g., queue overflow).                                                                   |
| **No queue**       | No outbound queue; sends are immediate and fire-and-forget.                                                                                  |
| **Scaffold queue** | Outbound queue with bounded retry: transient SDK failures retried up to `queue_send_max_attempts`; exhausted and permanent failures dropped. |

## 4. Matrix Failure Taxonomy

### 4.1 Connection Failures

| Failure                | Transient/Permanent               | Reconnectable   | Notes                                                                          |
| ---------------------- | --------------------------------- | --------------- | ------------------------------------------------------------------------------ |
| Network unreachable    | Transient                         | Yes             | TCP/TLS failure to homeserver. Resolves when network returns.                  |
| DNS resolution failure | Transient                         | Yes             | Resolves when DNS is restored.                                                 |
| TLS handshake failure  | Transient (misconfig → permanent) | Yes (transient) | Bad cert = permanent until fixed. Transient = cert rotation in progress.       |
| HTTP 429 (rate limit)  | Transient                         | Yes             | Homeserver enforces rate limit. nio/MEDRE should back off.                     |
| HTTP 401/403 (auth)    | Permanent                         | No              | Access token revoked or invalid. Requires new token. Session is unrecoverable. |
| Homeserver shutdown    | Transient                         | Yes             | If homeserver restarts, session can reconnect.                                 |
| Federation timeout     | Transient                         | Yes             | Remote server unreachable; local server remains functional.                    |

### 4.2 Sync Loop Failures

| Failure                          | Transient/Permanent | Reconnectable | Notes                                                                                                  |
| -------------------------------- | ------------------- | ------------- | ------------------------------------------------------------------------------------------------------ |
| Sync timeout                     | Transient           | Yes           | `sync_forever` times out; retry with backoff. MatrixSession uses exponential backoff, max 10 attempts. |
| Sync exception (unclassified)    | Transient           | Yes           | `_MAX_RECONNECT_ATTEMPTS = 10`, exponential backoff with 1–60 s, ±25% jitter.                          |
| `sync_forever` task cancellation | Permanent           | No            | Session is stopping; no reconnect.                                                                     |
| Long-poll gap                    | Transient           | Yes           | Client may miss events during gap; Matrix protocol allows gap fill via `/sync` `since` token.          |

### 4.3 Send Failures

| Failure                | Transient/Permanent         | Duplicate-Send Risk                                                                            |
| ---------------------- | --------------------------- | ---------------------------------------------------------------------------------------------- |
| `room_send` HTTP error | Transient (4xx → permanent) | Low: event_id is server-assigned. Retries use stable tx_id for dedup within homeserver window. |
| `room_send` timeout    | Transient                   | Medium: message may have been accepted but ACK lost. Server may have created the event.        |
| Room not joined        | Permanent                   | None: send fails immediately.                                                                  |
| Message too large      | Permanent                   | None: deterministic rejection.                                                                 |

### 4.4 Duplicate-Send Risk Assessment: Matrix

**Risk level: Low to Medium.**

- Matrix assigns event IDs server-side. Two sends of the same content produce
  two different events with two different event IDs.
- MEDRE now implements a deterministic transaction ID (tx_id) for Matrix room_send calls. This reduces duplicate events within the homeserver's transaction-ID dedup window. Duplicate risk remains nonzero across restarts, replay, changed delivery identity, or outside the homeserver window.
- Under timeout/retry, a message may be duplicated (same content, different event_id).
- The sync loop will echo back the sent message, but MEDRE's self-message
  suppression removes own messages by sender match.

### 4.5 Queue-Drain Semantics: Matrix

Matrix has **no outbound queue** in MEDRE. `deliver()` calls `room_send`
directly. There is no buffering, ordering, or drain behavior. Failed sends
raise `AdapterSendError` to the caller (normalizing the internal `MatrixSendError`).

### 4.6 Delivery Uncertainty Window: Matrix

- After `room_send` returns successfully, the event is confirmed persisted on
  the homeserver. The uncertainty window is effectively **zero** for the
  sending client.
- For **remote recipients** (federated), uncertainty exists until the remote
  homeserver receives the event via federation. This is outside MEDRE's
  visibility.
- For **inbound confirmation**, MEDRE relies on the sync loop echo. The delay
  is one sync cycle (typically 1–30 s depending on server-side `timeout`).

### 4.7 Encrypted-Room Failure Classes

| Failure                                            | Class                                            | Recovery                                                                                                        |
| -------------------------------------------------- | ------------------------------------------------ | --------------------------------------------------------------------------------------------------------------- |
| Missing crypto dependency (vodozemac)              | Permanent, startup-fatal in `e2ee_required` mode | Install `mindroom-nio[e2e]` and restart.                                                                        |
| Device not verified                                | Permanent per message                            | Verify device via interactive verification.                                                                     |
| Megolm session not received                        | Transient                                        | Wait for session key from other device. Undecryptable events are counted and logged, not forwarded.             |
| Crypto store corruption                            | Permanent                                        | Delete store, re-verify device, accept key loss.                                                                |
| `encryption_mode="e2ee_required"` + plaintext room | Permanent                                        | Adapter raises `AdapterPermanentError` on deliver to unencrypted room (normalizing internal `MatrixSendError`). |
| `encryption_mode="e2ee_optional"` + no deps        | Graceful degradation                             | Falls back to plaintext operation.                                                                              |
| Cross-signing not set up                           | Warning, not fatal                               | Messages decrypt if session keys are available.                                                                 |

### 4.8 Matrix-Specific Caveats

- `mindroom-nio` is a **fork** of `matrix-nio`. Its maintenance cadence and
  API stability relative to upstream are unverified.
- Sync loop is a single `asyncio.Task`. If the event loop blocks, the sync
  task stalls.
- `restore_login()` does not validate the token against the server at startup.
  An invalid token is only discovered on the first sync response (HTTP 401).
- Rate limiting is enforced server-side; MEDRE does not implement client-side
  rate limiting for sends.

## 5. Meshtastic Failure Taxonomy

### 5.1 Connection Failures

| Failure                 | Transient/Permanent                         | Reconnectable         | Notes                                                     |
| ----------------------- | ------------------------------------------- | --------------------- | --------------------------------------------------------- |
| TCP connection refused  | Transient                                   | Yes                   | Node offline or wrong port. Resolves when node returns.   |
| Serial port unavailable | Transient (permission) → Permanent (absent) | Yes (transient)       | `dialout` group issue = transient. No device = permanent. |
| BLE pairing failure     | Transient                                   | Yes                   | Retry pairing; device-specific.                           |
| Radio firmware crash    | Transient                                   | Yes (if node reboots) | Node may reboot automatically.                            |

### 5.2 Send Failures

| Failure                          | Transient/Permanent | Duplicate-Send Risk                                                               |
| -------------------------------- | ------------------- | --------------------------------------------------------------------------------- |
| `sendText` exception (transient) | Transient           | **High**: session retries up to 3 times. ACK may have been sent but lost on link. |
| `sendText` exception (permanent) | Permanent           | None: definitive failure.                                                         |
| Channel busy                     | Transient           | Medium: radio CSMA may retry at firmware level independently of MEDRE retry.      |
| Packet too large                 | Permanent           | None: deterministic rejection.                                                    |

### 5.3 Duplicate-Send Risk Assessment: Meshtastic

**Risk level: High.**

- The session retries transient failures up to 3 times (`_MAX_SEND_RETRIES = 3`).
- Radio ACKs are unreliable; a packet may have been received by the remote
  node but the ACK was lost on the LoRa link.
- Firmware-level CSMA may independently retransmit.
- Packet IDs are 32-bit integers assigned by firmware. Two sends produce
  different packet IDs, but the same text content may arrive multiple times.
- **Consumers must be tolerant of duplicate deliveries.**

### 5.4 Queue-Drain Semantics: Meshtastic

MEDRE uses a **bounded-retry outbound queue** (`MeshtasticOutboundQueue`):

- Messages are dequeued one at a time via `process_one`.
- Pacing delay enforced between sends (`delay_between_messages`, default 0.5 s).
- **Transient send failures are retried** up to `queue_send_max_attempts` times from
  the adapter-local in-memory queue. `total_requeued` is incremented on each retry.
- **Exhausted retries and permanent failures are dropped.** `total_exhausted` (for
  exhausted retries) and `total_permanent_failed` (for permanent failures) are
  incremented. `total_failed` is a superset of both (terminal send failures
  = exhausted retries + permanent failures). The item
  is permanently discarded; the exception is not re-raised.
- No persistence; queue contents and retry counters are lost on adapter shutdown.
- Retry is best-effort, adapter-local, in-memory, non-durable, and not exactly-once.

### 5.5 Delivery Uncertainty Window: Meshtastic

- After `sendText` returns with a `MeshPacket.id`, the packet was transmitted
  to the local radio. **This does not confirm delivery to any recipient.**
- ACK is at the LoRa link level (hop-by-hop), not end-to-end.
- Multi-hop delivery adds additional uncertainty: the message may be in transit
  for seconds to minutes.
- **No end-to-end delivery confirmation exists in the Meshtastic protocol**
  for text messages.
- The uncertainty window is effectively **unbounded** from MEDRE's perspective.

### 5.6 Meshtastic-Specific Caveats

- `mtjk` is a **fork** of the upstream Meshtastic Python library. Version
  2.7.8.post2+ is imported as `meshtastic`. Distribution name is `mtjk`.
- `sendText` and `sendData` are **synchronous** in mtjk; MEDRE wraps them in
  `asyncio.to_thread()`.
- Pubsub callbacks fire on a background thread, not the asyncio event loop.
  MEDRE must bridge thread → event loop safely.
- BLE connectivity is documented but not exercised in any live harness.
- Radio channels are shared medium; test messages may interfere with other
  traffic on the same channel.

## 6. MeshCore Failure Taxonomy

### 6.1 Connection Failures

| Failure                 | Transient/Permanent   | Reconnectable   | Notes                                                  |
| ----------------------- | --------------------- | --------------- | ------------------------------------------------------ |
| TCP connection refused  | Transient             | Yes             | Node offline. Resolves when node returns.              |
| Serial port unavailable | Transient → Permanent | Yes (transient) | Same as Meshtastic serial.                             |
| BLE pairing failure     | Transient             | Yes             | Device-specific.                                       |
| SDK connect timeout     | Transient             | Yes             | Bounded backoff: 1–30 s, max 10 attempts, ±25% jitter. |

### 6.2 Send Failures

| Failure                           | Transient/Permanent | Duplicate-Send Risk                                                                          |
| --------------------------------- | ------------------- | -------------------------------------------------------------------------------------------- |
| `send_text` exception (transient) | Transient           | **Medium**: session retries up to 3 times (`_SEND_MAX_RETRIES = 3`). ACK may have been lost. |
| `send_text` exception (permanent) | Permanent           | None.                                                                                        |
| Channel index invalid             | Permanent           | None: deterministic.                                                                         |

### 6.3 Duplicate-Send Risk Assessment: MeshCore

**Risk level: Medium.**

- The session retries transient failures up to 3 times.
- The MeshCore protocol has E2EE at the radio level, but ACKs are link-level.
- A message may have been received by the remote node but the ACK was lost.
- **Consumers must be tolerant of duplicate deliveries.** (Documented in session
  docstring.)

### 6.4 Queue-Drain Semantics: MeshCore

MeshCore has **no outbound queue** in MEDRE. `send_text()` is called directly
on the session. There is no buffering or drain behavior.

### 6.5 Delivery Uncertainty Window: MeshCore

- Similar to Meshtastic: send confirmation is at the local radio level.
- No end-to-end delivery confirmation.
- E2EE is at the radio level; MEDRE does not manage keys.
- Uncertainty window is **unbounded**.

### 6.6 MeshCore-Specific Caveats

- SDK is fully async (all methods are coroutines). No `to_thread` wrapping needed.
- SDK dependency chain: `bleak`, `pyserial-asyncio-fast`, `pycayennelpp`.
- `BLEConnection` is documented but may not be fully implemented in the SDK.
- Sender identity is a 6-byte pubkey prefix — not globally unique.
- No built-in reply/threading support.

## 7. LXMF/Reticulum Failure Taxonomy

### 7.1 Connection Failures

| Failure                       | Transient/Permanent | Reconnectable      | Notes                                                            |
| ----------------------------- | ------------------- | ------------------ | ---------------------------------------------------------------- |
| RNS.Reticulum init failure    | Permanent           | No (session-level) | Config or interface error. New session required.                 |
| Identity file missing/corrupt | Permanent           | No                 | `LxmfConnectionError` raised on start.                           |
| LXMRouter init failure        | Permanent           | No                 | Depends on Reticulum instance being healthy.                     |
| Transport interface down      | Transient           | Yes                | Reticulum supports multiple interfaces; failover is SDK-managed. |

### 7.2 Send Failures

| Failure                       | Transient/Permanent                        | Duplicate-Send Risk                                                                          |
| ----------------------------- | ------------------------------------------ | -------------------------------------------------------------------------------------------- |
| `handle_outbound` exception   | Transient (network) → Permanent (identity) | **Low**: LXMF assigns a unique message hash. Retrying creates a new message with a new hash. |
| Destination unreachable       | Transient (long-lived)                     | None: message enters `OUTBOUND`/`SENDING` state and may timeout to `FAILED`.                 |
| Message rejected by recipient | Permanent                                  | None: `REJECTED` state is definitive.                                                        |
| Propagation node unavailable  | Transient                                  | Low: propagated messages queue at the node; delivery is opportunistic.                       |

### 7.3 Duplicate-Send Risk Assessment: LXMF

**Risk level: Low.**

- Each LXMF message has a unique hash assigned at creation.
- The session does **not** retry outbound sends automatically. The LXMRouter
  manages delivery state progression internally.
- Duplicate risk is primarily from application-level retry (re-sending the
  same content), which produces a different message hash.

### 7.4 Queue-Drain Semantics: LXMF

LXMF has **no outbound queue** in MEDRE. `send()` calls `handle_outbound`
directly on the LXMRouter. The router manages its own internal delivery queue.

- Propagated messages are stored at propagation nodes for later pickup.
- Direct messages are sent immediately and progress through delivery states:
  `generating → outbound → sending → sent → delivered` (or `failed/rejected/cancelled`).

### 7.5 Delivery Uncertainty Window: LXMF

- Outbound `send()` returns in `OUTBOUND` state — **not** `DELIVERED`.
- Actual delivery is asynchronous. The LXMRouter fires delivery callbacks
  when the state changes.
- Multi-hop Reticulum transport can introduce **seconds to hours** of delivery
  latency depending on network topology.
- Propagated messages have **no delivery time guarantee** — they wait at a
  propagation node until the recipient connects.
- The uncertainty window is **effectively unbounded** for propagated delivery.

### 7.6 LXMF-Specific Caveats

- Identity is a 16-byte hash (hex-encoded), not human-readable.
- Delivery confirmation (`DELIVERED` state) is asynchronous and may never arrive.
- `lxmf` and `RNS` packages are optional; import failures are graceful.
- Reticulum supports many transport interface types (TCP, serial, LoRa, AX.25,
  etc.) — each with its own failure characteristics.
- No built-in message deduplication at the MEDRE level; consumers must handle
  `message_id` (hash) for dedup if needed.

## 8. Cross-Transport Failure Summary

| Dimension                           | Matrix                                   | Meshtastic                               | MeshCore                                  | LXMF                                     |
| ----------------------------------- | ---------------------------------------- | ---------------------------------------- | ----------------------------------------- | ---------------------------------------- |
| **Transient failure primary cause** | Network/auth/rate-limit                  | Radio/link/serial                        | Radio/link/serial                         | Network/RNS transport                    |
| **Permanent failure primary cause** | Auth revocation, config error            | Config error, port error                 | Config error                              | Identity/RNS init error                  |
| **Reconnect model**                 | Exponential backoff, 10 attempts, 1–60 s | Exponential backoff, 10 attempts, 1–30 s | Exponential backoff, 10 attempts, 1–30 s  | Exponential backoff, 10 attempts, 1–30 s |
| **Duplicate-send risk**             | Low–Medium                               | High                                     | Medium                                    | Low                                      |
| **Outbound queue**                  | None (direct send)                       | Scaffold (lossy drain)                   | None (direct send)                        | None (router-managed)                    |
| **Delivery confirmation**           | Server event_id (sync)                   | None (fire-and-forget)                   | None (fire-and-forget)                    | Async state callback                     |
| **Uncertainty window**              | ~0 (server-side) to one sync cycle       | Unbounded                                | Unbounded                                 | Unbounded                                |
| **E2EE failure class**              | Megolm session loss, device verification | N/A (no E2EE)                            | N/A (radio-level E2EE, not MEDRE-managed) | N/A (identity-based signing)             |
| **ACK model**                       | HTTP response                            | LoRa hop-by-hop (unreliable)             | Link-level (unreliable)                   | Reticulum transport-dependent            |

## 8.1 Route Policy Suppression

Route policy suppression is a **cross-transport** failure classification that is not specific to any single transport adapter. It occurs when the route-policy evaluator denies a delivery after route matching but before delivery side effects.

**Classification:**

| Property        | Value                                                                                 |
| --------------- | ------------------------------------------------------------------------------------- |
| Failure kind    | `policy_suppressed`                                                                   |
| Retryable       | No — permanent classification                                                         |
| Pipeline stage  | Route policy (after route match, before delivery)                                     |
| Receipt status  | `suppressed`                                                                          |
| Receipt context | Includes `route_id`, `target_adapter`, `target_channel`, and the policy denial reason |

**Denial reason codes** (stable, machine-readable):

| Reason code                  | Policy field              | Description                          |
| ---------------------------- | ------------------------- | ------------------------------------ |
| `source_adapter_not_allowed` | `allowed_source_adapters` | Source adapter not in allowlist      |
| `dest_adapter_not_allowed`   | `allowed_dest_adapters`   | Destination adapter not in allowlist |
| `sender_not_allowed`         | `sender_allowlist`        | Sender identity not in allowlist     |
| `room_not_allowed`           | `room_allowlist`          | Room identifier not in allowlist     |
| `channel_not_allowed`        | `channel_allowlist`       | Channel identifier not in allowlist  |

Policy suppression is visible in `RouteStats.policy_suppressed` counters and in `RuntimeAccounting.policy_suppressed`. Each suppressed delivery produces a persisted receipt with the denial reason, enabling post-hoc investigation via `medre inspect receipts`.

## 9. Operational Implications

1. **Consumers must handle duplicates** for Meshtastic and MeshCore. This is
   not optional — it is a protocol-level reality.

2. **Delivery confirmation is transport-dependent.** Matrix provides the
   strongest confirmation (server-persisted event_id). Meshtastic and MeshCore
   provide none. LXMF provides asynchronous state callbacks but no guaranteed
   delivery time.

3. **Queue-drain retry is bounded** in Meshtastic. Transient SDK send failures are
   retried up to `queue_send_max_attempts` from the adapter-local in-memory queue.
   Exhausted retries and permanent failures are dropped. Retry is best-effort,
   non-durable, and not exactly-once.

4. **E2EE failures in Matrix are recoverable** (re-verify device, re-send keys)
   but require operator intervention. MEDRE does not automate crypto recovery.

5. **Reconnect budgets are finite.** All four transports cap at 10 consecutive
   attempts. After exhaustion, the session is effectively dead and must be
   restarted by the runtime or operator.

6. **No transport provides end-to-end delivery confirmation** that MEDRE can
   observe, except Matrix (server-side event_id) and LXMF (async DELIVERED
   state callback). Even these are not instantaneous.

7. **Route policy suppression is permanent and not retryable.** When a delivery
   is denied by route-policy evaluation, it is classified as `policy_suppressed`
   and produces a `status="suppressed"` receipt. This is an intentional
   access-control outcome, not a transient failure. Operators should review
   route policy configuration and adjust allowlists if the denial was
   unintended.

# Delivery Semantics Matrix

> Contract version: 1
> Last updated: 2026-05-09
> Track: 9 (Transport Capability Contracts)
> Supersedes: Partially supersedes 65-constrained-transport-comparison.md (which covered structural comparison). This document focuses on delivery behavior.
> Supplements: Contracts 16, 18, 19, 20.

This document compares the delivery semantics of MEDRE's four adapter families across the dimensions that matter for reliable message transport. It is explicit about asymmetry. It avoids false equivalence. If two transports look similar on paper but behave differently in practice, this document says so.

Nothing here claims production connectivity. These are protocol-level capabilities observed from SDK source, specification, and existing adapter scaffolding, verified where possible by deterministic testing.


## 1. Scope

- Per-transport delivery guarantees (or lack thereof).
- Message identification, acknowledgment, and confirmation semantics.
- Retry, retransmission, and reliability mechanisms.
- Queueing, ordering, and store-and-forward behavior.
- Relations, replies, and threading support.
- Identity and addressing as it affects delivery.
- Encryption and attachment/media capability.
- What MEDRE can and cannot normalize across these transports.

## 2. Non-goals

- Claiming that any adapter has been tested against real hardware or services in default CI.
- Proposing new MEDRE features to bridge semantic gaps.
- Implementing retry schedulers, queue workers, or transport-level reliability engines.
- Comparing transports on dimensions unrelated to delivery (e.g., installation, licensing, community).


## 3. Transport Profiles

### 3.1 Matrix

| Property | Value |
|----------|-------|
| Role | PRESENTATION |
| Wire transport | HTTP/JSON over TLS (long polling or WebSocket `/sync`) |
| Message ID | Server-assigned event ID string (e.g., `$event_id`) |
| Message model | Persistent, ordered event stream per room |
| Payload limit | Approximately 100 KB per message |
| Directionality | Bidirectional, symmetric send/receive |

### 3.2 Meshtastic

| Property | Value |
|----------|-------|
| Role | TRANSPORT |
| Wire transport | LoRa radio, protobuf encoding |
| Message ID | Packet ID (32-bit integer, assigned by firmware) |
| Message model | Broadcast or directed, fire-and-forget with optional ACK |
| Payload limit | Approximately 228 bytes per packet |
| Directionality | Bidirectional, but asymmetric (broadcast dominates) |

### 3.3 MeshCore

| Property | Value |
|----------|-------|
| Role | TRANSPORT |
| Wire transport | LoRa radio, custom binary encoding with E2EE |
| Message ID | Sender-assigned timestamp (integer) |
| Message model | Directed or flood, ACK-driven with CRC verification |
| Payload limit | 184 bytes per packet |
| Directionality | Bidirectional, directed or flood |

### 3.4 LXMF

| Property | Value |
|----------|-------|
| Role | TRANSPORT |
| Wire transport | Reticulum network (multiple physical layers), msgpack encoding |
| Message ID | LXMF hash (bytes, derived from message content) |
| Message model | Store-and-forward with propagation nodes, delivery receipts |
| Payload limit | Large (multiple Reticulum "resources", multi-KB typical) |
| Directionality | Bidirectional, directed to destination hash |


## 4. Delivery Semantics Comparison

### 4.1 Message Identification

| Dimension | Matrix | Meshtastic | MeshCore | LXMF |
|-----------|--------|------------|----------|------|
| Native ID type | Server-assigned string (`$event_id`) | 32-bit integer (packet_id) | Sender timestamp (int) | Content-derived hash (bytes) |
| Uniqueness scope | Global (within federation) | Local to sending node session | Local to sender | Global (cryptographic) |
| ID available immediately | Yes (from `room_send` response) | Yes (from sent packet object) | Yes (sender-assigned) | Yes (computed locally) |
| ID stability | Immutable after creation | Mutable within session (wraps at 32-bit boundary) | May collide for simultaneous sends | Immutable (content-addressed) |

**What MEDRE normalizes:** All native IDs are stored as strings in `NativeMessageRef.native_message_id`. MEDRE does not enforce uniqueness across transports. Native refs are keyed by `(adapter_id, native_channel_id, native_message_id)` tuple.

**What MEDRE cannot normalize:** Matrix event IDs are globally unique and persistent. Meshtastic packet IDs wrap and are only session-scoped. MeshCore timestamps can collide. LXMF hashes are content-addressed and deterministic. Treating these as equivalent would be a false equivalence. Code that compares native IDs across adapters is incorrect by contract.

### 4.2 Acknowledgment and Confirmation

| Dimension | Matrix | Meshtastic | MeshCore | LXMF |
|-----------|--------|------------|----------|------|
| ACK mechanism | HTTP response from `room_send` (sync) | `ROUTING_APP` ACK packet (async) | ACK event with CRC (async) | LXMF delivery receipt (async) |
| ACK guarantee | Server received; not necessarily delivered to clients | Packet was received by at least one node | Packet was received and CRC verified | Message reached destination or propagation node |
| ACK timing | Synchronous (milliseconds) | Asynchronous (seconds, variable) | Asynchronous (seconds, timeout-based) | Asynchronous (seconds to hours depending on network) |
| NAK available | HTTP error codes | Implicit (no ACK = possible failure) | Yes (explicit NAK possible) | Yes (delivery failure indication) |

**What MEDRE normalizes:** `AdapterDeliveryResult` carries `native_message_id` and `native_channel_id`. Both must be platform-provided values — never fabricated or backfilled from route configuration. Success or failure is reported through the `deliver()` return/exception contract (Contract 21, Section 3.4). Native refs are persisted only when `native_message_id` is not `None`.

**What MEDRE cannot normalize:** Matrix's synchronous confirmation is fundamentally different from Meshtastic's asynchronous implicit-ACK model. An adapter that returns successfully from `deliver()` on Matrix has server confirmation. An adapter that returns successfully from `deliver()` on Meshtastic with a paced queue has confirmed only that the message was queued, not that any radio transmitted it. These are not the same kind of "success".

### 4.3 Delivery Guarantees

| Guarantee | Matrix | Meshtastic | MeshCore | LXMF |
|-----------|--------|------------|----------|------|
| At-most-once | No (server stores) | Yes (default for broadcast) | No (ACK-driven) | No (store-and-forward) |
| At-least-once | Yes (server stores, client syncs) | Possible with ACK retry | Yes (ACK with retry) | Yes (propagation nodes) |
| Exactly-once | No (distributed system) | No | No | No |
| Ordered per-channel | Yes (server-assigned sequence) | No (radio contention) | No (radio contention) | No (mesh routing) |
| Ordered per-sender | Yes (within room) | Partially (firmware queues) | Partially (sender queues) | No (multi-path routing) |
| Persistent storage | Yes (homeserver) | No (ephemeral radio) | No (ephemeral radio) | Yes (propagation nodes, local storage) |

**Key asymmetry:** Matrix has durable, ordered, server-confirmed delivery. The three constrained transports have none of these guarantees natively. Meshtastic broadcast is genuinely fire-and-forget. MeshCore adds ACK-driven confirmation but over a lossy radio link. LXMF adds store-and-forward but over a multi-hop mesh with unpredictable latency.

Any MEDRE feature that assumes ordered, persistent, or confirmed delivery will work on Matrix and fail silently or spectacularly on constrained transports. This is the central design tension in the adapter layer.

### 4.4 Retry and Retransmission

| Dimension | Matrix | Meshtastic | MeshCore | LXMF |
|-----------|--------|------------|----------|------|
| Transport-level retry | Client SDK reconnects on disconnect | Firmware retransmits with hop limit | SDK retries with configurable timeout | Reticulum retries at link layer |
| Application-level retry | Client re-sends on error | Application must implement | Application must implement | LXMF propagation node handles |
| Retry scope | Per-message | Per-packet | Per-packet | Per-message (multi-resource) |
| Backoff strategy | Exponential (client SDK) | Fixed interval (firmware) | Configurable timeout | Reticulum adaptive |
| Dedup on retry | Yes (idempotent `txn_id`) | No | No | Content-hash naturally deduplicates |

**What MEDRE provides:** `RetryExecutor` computes backoff timing and records `next_retry_at` on failed delivery receipts. `RetryWorker` (a single-process background worker) automatically picks up due receipts for `ADAPTER_TRANSIENT` failures and re-invokes delivery. Retry is bounded by `RetryPolicy`, survives process restart through persistent receipts, and uses the same delivery planning path.

**What MEDRE does not provide:** Per-adapter retry rate limiting, retry budgets, deduplication of retried deliveries, or automatic retry for non-transient failure kinds (`ADAPTER_PERMANENT`, `RENDERER_FAILURE`, `PLANNER_FAILURE`, `DEADLINE_EXCEEDED`). See Contract 04, Section 16.3.

### 4.5 Queueing and Offline Behavior

| Dimension | Matrix | Meshtastic | MeshCore | LXMF |
|-----------|--------|------------|----------|------|
| Inbound queue | Server-side event buffer (`/sync`) | No queue (broadcast) | No queue (directed or flood) | Propagation node stores until collected |
| Outbound queue | Client SDK manages send queue | Adapter owns queue (paced) | Adapter may queue (ACK-driven) | SDK queues for delivery |
| Offline support | Yes (server stores, sync on reconnect) | No (radio: if nobody hears it, it's gone) | No (radio: same constraint) | Yes (propagation nodes, local mailbox) |
| Store-and-forward | Yes (homeserver) | No | No | Yes (LXMF propagation, Reticulum transport) |
| Backlog depth | Server-configured (typically thousands) | Zero (no storage) | Zero (no storage) | Propagation-node-configured (hours to days) |

**What this means for MEDRE:** Meshtastic and MeshCore have no persistence. If the radio link is down, messages sent during the outage are lost unless the adapter queues them internally. LXMF and Matrix have persistence, but at very different timescales and reliability levels.

The adapter's queueing mode (Contract 21, Section 4.1) determines how this is handled. An immediate-send adapter on Meshtastic drops messages when the link is down. An enqueue-only adapter buffers them. The choice is the adapter's, not the pipeline's.

### 4.6 Relations, Replies, and Threading

| Dimension | Matrix | Meshtastic | MeshCore | LXMF |
|-----------|--------|------------|----------|------|
| Native reply | Yes (`m.in_reply_to`) | Yes (`replyId` at packet level) | No native mechanism | No native mechanism |
| Threading | Yes (thread roots, bundled relations) | No | No | No |
| Reactions | Yes (aggregation via `m.reaction`) | No | No | No |
| Edits | Yes (replace with relations) | No | No | No |
| Forwards | Yes (via redaction/copy) | No | No | No |

**What MEDRE normalizes:** `EventRelation` with `relation_type="reply"` and `target_native_ref` carries reply semantics across all four transports. The codec extracts native reply data when available. The renderer injects native reply data when the target transport supports it.

**What MEDRE cannot normalize:** MeshCore and LXMF have no native reply mechanism. MEDRE can carry reply metadata in the canonical event model, but the target transport will not render it as a threaded reply. It will appear as plain text or a quoted prefix. This is not a deficiency in MEDRE. It is a transport limitation. Code that assumes replies work identically across all transports is incorrect.

### 4.7 Per-Adapter Delivery Summary

Consolidated delivery behavior per adapter. This table summarizes what each adapter's `deliver()` completion means, how native IDs are sourced, and the error taxonomy for transient vs permanent failures.

**Normalization boundary:** Transport-specific `*SendError` classes (`MatrixSendError`, `MeshtasticSendError`, `MeshCoreSendError`, `LxmfSendError`) are session/internal-layer errors that do not subclass `AdapterSendError` or `AdapterPermanentError`. Adapters normalize session/internal transport errors into `AdapterSendError(transient=True)` or `AdapterPermanentError` at the runtime boundary before the pipeline's `classify_failure` inspects them. The pipeline relies only on `AdapterSendError.transient` to classify failures.

| Adapter | `deliver()` completion meaning | `native_message_id` source | `native_channel_id` source | `native_thread_id` source | Transient errors (types/patterns) | Permanent errors (types/patterns) | ACK / final-delivery limitation |
|---------|-------------------------------|---------------------------|---------------------------|--------------------------|----------------------------------|----------------------------------|--------------------------------|
| **Matrix** | SDK `room_send` returns `event_id`; homeserver accepted and stored | Matrix event ID (e.g. `$xxx`) from `RoomSendResponse` (platform-provided only) | Room ID string (e.g. `!roomid:server.tld`) (platform-provided only) | Thread root event ID via `m.relates_to` | Connection errors, rate-limit (HTTP 429), sync timeout, network unreachable | Auth failure (HTTP 401/403), room not joined, message too large, not-connected, SDK not initialized | Synchronous server ACK. Server received ≠ delivered to clients. No end-to-end delivery receipt. `CancelledError` propagates (not swallowed). Broad catch is `Exception`, not `BaseException`. |
| **MeshCore** | SDK `send_text()` / `send_data()` returns; message locally accepted | MeshCore message reference (timestamp-based) from SDK send return (platform-provided only) | Channel slot index as string (platform-provided, may be `None`) | `None` (no native threading) | Transport timeout, connection reset, serial link failure, queue-full | Invalid address, payload encoding failure, config error, not-connected, SDK not initialized | No end-to-end ACK. `delivery_note` documents local-acceptance only. |
| **Meshtastic** | Message locally enqueued to outbound queue | `None` — no native send confirmation at enqueue time | Channel index as string (platform-provided only) | `None` (no native threading) | Serial/connection failures, timeout, queue capacity exhaustion | Payload encoding failure, config error, not-connected, SDK not initialized | Local-acceptance only. Actual radio send is async via queue worker. No platform ACK returned to caller. |
| **LXMF** | LXMF message dispatched to `LXMRouter` | LXMF message hash (hex of `LXMessage.hash`) (platform-provided only) | `None` (LXMF uses destination-hash addressing, no channel concept) | `None` (no native threading) | Propagation delay, transport timeout, Reticulum link failure | Invalid destination hash, config error, not-connected, SDK not initialized | Store-and-forward eventual delivery. Async state progression (`outbound` → `delivered`/`failed`). Delivery receipts available but asynchronous. |

**Key asymmetries preserved:**

- Matrix is the only adapter where `deliver()` completion implies confirmed server-side storage.
- Meshtastic is the only adapter where `native_message_id` is `None` at `deliver()` return time (queue-based, no synchronous send confirmation).
- MeshCore returns a local reference but explicitly notes local-acceptance via `delivery_note` since there is no end-to-end ACK.
- LXMF returns a content-addressed hash immediately, but actual delivery is asynchronous through the mesh.
- All four adapters treat not-connected and SDK-not-initialized as **permanent** errors (not retryable). An adapter that cannot reach its transport should not be retried without intervention.
- `native_message_id` and `native_channel_id` are always platform-provided. Adapters must never fabricate these values. The pipeline must not backfill `native_channel_id` or any other native ref field from route configuration.

**Exception handling in Matrix `deliver()`:** The Matrix adapter catches `Exception` (not `BaseException`) in its `deliver()` error path. `CancelledError` is explicitly re-raised before the broad catch, ensuring asyncio task cancellation propagates correctly to the caller. This is a contract-level requirement: no adapter may swallow `CancelledError`.

### 4.8 Ordering

| Dimension | Matrix | Meshtastic | MeshCore | LXMF |
|-----------|--------|------------|----------|------|
| Per-room ordering | Server-assigned, total order | None | None | None |
| Per-sender ordering | Preserved within room | Firmware FIFO within channel | Sender FIFO | No guarantee (multi-path) |
| Cross-adapter ordering | N/A | N/A | N/A | N/A |
| Causal ordering | Possible via relations | No | No | No |

**What this means:** MEDRE cannot provide ordering guarantees across transports. Events from different adapters are ordered by timestamp at best, and timestamps are not reliable clocks on constrained devices. The `lineage` tuple provides causal ordering within a single derivation chain, but it does not order events from different sources.


## 5. Identity and Addressing (Delivery Context)

This section covers identity only as it affects delivery. For the full identity comparison, see Contract 23.

| Dimension | Matrix | Meshtastic | MeshCore | LXMF |
|-----------|--------|------------|----------|------|
| Sender identity | MXID (`@user:server.org`) | Node number (int) | Ed25519 pubkey (32B hex) | Reticulum Identity hash (16B hex) |
| Destination addressing | Room ID or user MXID | Node number or broadcast | Pubkey or flood | Destination hash (16B hex) |
| Address resolution | Server-side | Local node database | Local contact list | Reticulum announce system |
| Address stability | Stable (MXIDs don't change) | Session-scoped (node numbers can change) | Stable (pubkey is identity) | Stable (hash is derived from pubkey) |

**Delivery asymmetry:** Matrix addresses are human-readable strings resolved by a server. Meshtastic addresses are integers that may change between sessions. MeshCore and LXMF addresses are cryptographic hashes that are stable but opaque. A delivery plan that works with Matrix's named rooms has no direct equivalent on Meshtastic's numbered channels.

## 6. Encryption

| Dimension | Matrix | Meshtastic | MeshCore | LXMF |
|-----------|--------|------------|----------|------|
| Transport encryption | TLS to homeserver | Optional per-packet AES-256 | Always-on E2EE (AES-256-CTR) | Always-on (Reticulum link encryption) |
| E2EE available | Yes (Megolm/Olm) | Yes (channel PSK) | Default (built into protocol) | Default (Reticulum Identity-based) |
| E2EE implemented in MEDRE | No | No | No | No |
| Key management | Homeserver-assisted | Pre-shared channel key | Per-contact key exchange | Reticulum Identity-based |

**Phase 1 status:** MEDRE does not implement E2EE for any transport. Transport-level TLS on Matrix is handled by the HTTP client (nio). The constrained transports handle their own encryption at the protocol level. MEDRE sends and receives plaintext payloads; the transport encrypts them.

This is acceptable for Phase 1 because MEDRE is not an encryption boundary. It trusts the transport to handle confidentiality. When E2EE is added in a future phase, it will be an adapter-internal concern, not a pipeline concern.

## 7. Attachments and Media

| Dimension | Matrix | Meshtastic | MeshCore | LXMF |
|-----------|--------|------------|----------|------|
| File/image upload | Yes (mxc:// URLs) | No | No | Yes (Reticulum resources) |
| Rich content | Yes (HTML, formatted body) | No (plain text only) | No (plain text only) | No (plain text with structured fields) |
| Embeds/widgets | Yes | No | No | No |

**Phase 1 status:** MEDRE handles text and replies only. No attachments, no media, no rich formatting beyond what the renderer produces as plain text for constrained transports. Matrix's rich content capability is available in the renderer but not exercised beyond `m.room.message` with `m.relates_to`.

## 8. What MEDRE Can Normalize

| Aspect | Normalizable? | How |
|--------|--------------|-----|
| Message existence (was it sent?) | Yes | `AdapterDeliveryResult` records `native_message_id` on success (platform-provided only) |
| Delivery failure (did it fail?) | Yes | Exception taxonomy → `DeliveryFailureKind` (CAPACITY_REJECTION for full semaphore, SHUTDOWN_REJECTION for stopped controller) |
| Reply relationships | Partially | `EventRelation` with `target_native_ref`; degraded on transports without native reply |
| Sender identity (as string) | Yes | `source_transport_id` carries whatever the transport provides |
| Timestamp | Yes (with caveats) | `CanonicalEvent.timestamp` is UTC; constrained devices may have poor clocks |
| Retry intent | Yes | `next_retry_at` on receipts |
| Event lineage | Yes | `lineage` tuple + `parent_event_id` |

## 9. What MEDRE Cannot Normalize

| Aspect | Why Not |
|--------|---------|
| Delivery confirmation | ACK semantics differ fundamentally (sync HTTP vs async radio ACK vs implicit) |
| Message ordering | Only Matrix has server-assigned ordering |
| Persistence guarantees | Meshtastic and MeshCore have zero persistence |
| Reply rendering | MeshCore and LXMF have no native reply mechanism |
| Address resolution | Each transport resolves addresses differently (server vs local DB vs announce) |
| Native message ID semantics | Globally unique vs session-scoped vs content-addressed vs collision-prone |
| Bandwidth/payload constraints | 100 KB vs 228 B vs 184 B vs multi-KB; no single sending strategy fits all |
| Encryption model | Per-transport, not pipeline-level |

Code that treats these as normalized is incorrect. The adapter boundary exists precisely because these asymmetries cannot be abstracted away without losing essential transport characteristics.


## 10. Queue/Reliability Audit Summary

This section consolidates the queue and reliability model across all four transports and the MEDRE pipeline.

### 10.1 Send Patterns

| Pattern | Matrix | Meshtastic | MeshCore | LXMF | Pipeline |
|---------|--------|------------|----------|------|---------|
| Immediate-send | Default | Possible | Possible | Possible | Always (deliver and return) |
| Enqueue-only | Not needed | Recommended | Possible | Possible | Never (pipeline does not queue) |
| Paced | Not needed | Required (duty cycle) | Possible | Not needed | Never |
| ACK-driven | Not needed | Optional | Default | Possible | Never (pipeline does not wait for ACKs) |
| Best-effort | N/A (server confirms) | Default for broadcast | Possible | N/A (store-and-forward) | Default |

### 10.2 Runtime Ownership

The MEDRE runtime owns:

1. **Retry scheduling.** `RetryWorker` loads due receipts (where `next_retry_at <= now` and `failure_kind = 'adapter_transient'`) and re-invokes delivery. Retry is single-process, bounded by `RetryPolicy`, and survives process restart through persistent receipts.

The MEDRE runtime does **not** own:

1. **Outbound queues.** These are adapter internals.
2. **Deduplication.** No duplicate detection at the delivery level.
3. **Transport health monitoring.** The runtime reads health state. The adapter sets it.
4. **Connection management.** Connect, disconnect, and reconnect are adapter-owned.

### 10.3 Retry Ownership by Failure Kind

| Failure kind | Retry owner | Auto-retried? |
|---|---|---|
| `ADAPTER_TRANSIENT` | `RetryWorker` | Yes — same delivery lineage, bounded by `RetryPolicy` |
| `ADAPTER_PERMANENT` | None | No |
| `RENDERER_FAILURE` | None | No |
| `PLANNER_FAILURE` | None | No |
| `DEADLINE_EXCEEDED` | None | No |
| `ADAPTER_MISSING` | None | No |
| `CAPACITY_REJECTION` | None | No |
| `SHUTDOWN_REJECTION` | None | No |

> Retry is single-process, in-process, bounded by `RetryPolicy`, and survives process restart through persistent receipts. Retry is not replay: retry continues the same delivery lineage (linked via `parent_receipt_id`), while replay creates a new bridge execution with duplicate-send risk.


## 11. Implications

### 11.1 For Adapter Authors

- Your transport's ACK model determines what "success" means for `deliver()`. Be honest about what you confirm.
- If your transport has no persistence, your adapter must decide whether to buffer or drop during outages. The pipeline will not make this decision for you.
- Native message IDs have different scopes and stability. Store them accurately. Do not assume they are globally unique.

### 11.2 For Pipeline Authors

- Do not assume ordered delivery. Do not assume confirmed delivery. Do not assume persistent storage.
- `AdapterDeliveryResult.native_message_id` is a transport-local identifier. It is not a correlation key across adapters.
- The receipt system records what happened. It does not make things happen. No code reads `next_retry_at` and acts on it in Phase 1.

### 11.3 For Operators

- Matrix is the only transport with reliable, confirmed, persistent delivery. Everything else is best-effort or ACK-driven over lossy links.
- Meshtastic and MeshCore messages are ephemeral. If nobody is listening on the radio channel, the message is gone.
- LXMF messages persist at propagation nodes but delivery latency is unpredictable and can range from seconds to hours.
- Monitor adapter health and delivery receipts. These are your primary signals.

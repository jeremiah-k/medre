# Delivery Result Contract

> **Status:** Active
> **Classification:** Normative
> **Authority:** Locked-in delivery result contract for beta; governs AdapterDeliveryResult semantics
> **Last reviewed:** 2026-05-24
>
> Contract version: 1
> Last updated: 2026-05-09
> Track: 9 (Transport Capability Contracts)
> Supersedes: Nothing. Formalizes findings from contracts 21, 22, 27, 28.
> Status: Contract. Documents the locked-in delivery result semantics for beta.

This document defines the contractual semantics of `AdapterDeliveryResult` and per-transport delivery behavior. It records what each field means, when it is populated, what "delivered" actually means per transport, and the risks consumers must handle.

This is a contract document. No runtime redesign, adapter abstraction, or new delivery features are proposed.

## 1. Scope

- `AdapterDeliveryResult` field semantics: `native_message_id`, `native_channel_id`, `delivery_note`, `metadata`, and delivery state.
- Per-transport delivery models: pending vs sent vs delivered.
- The meaning of immediate return vs final delivery.
- Failed send behavior.
- Duplicate-send risk under retry.
- Per-transport delivery metadata and namespacing.

## 2. Non-goals

- Adding confirmed delivery to transports that cannot provide it.
- Normalizing delivery semantics across transports with fundamentally different models.
- Building delivery tracking, receipt pipelines, or webhook notification systems.
- Proposing new transports or delivery methods.

## 3. AdapterDeliveryResult Definition

Location: `src/medre/core/contracts/adapter.py`

```python
@dataclass(frozen=True)
class AdapterDeliveryResult:
    native_message_id: str | None = None
    native_channel_id: str | None = None
    native_thread_id: str | None = None
    native_relation_id: str | None = None
    delivery_note: str | None = None
    metadata: MappingProxyType[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )
```

This is an immutable, frozen dataclass. The pipeline uses it to store `NativeMessageRef` mappings. The pipeline owns receipts and storage. Adapters only report what the platform returned.

## 4. Field Semantics

### 4.1 native_message_id

Platform-native message ID assigned by the external system. `None` when the platform did not return one.

**Fabrication prohibition:** `native_message_id` must be a platform-provided identifier. Adapters must never fabricate, synthesize, or locally-generate a `native_message_id`. If the platform did not return an ID, the field must be `None`. The fake adapters in test mode are the sole exception and are clearly bounded by their test-only scope.

| Transport  | Source                         | Type                               | When Available                          | Uniqueness                                |
| ---------- | ------------------------------ | ---------------------------------- | --------------------------------------- | ----------------------------------------- |
| Matrix     | Homeserver-assigned `event_id` | String (e.g. `$xxx`)               | Immediately on `RoomSendResponse`       | Globally unique, persistent, queryable    |
| Meshtastic | Firmware-assigned packet ID    | Integer (32-bit), stored as string | On send acknowledgment (if ACK enabled) | Unique per sender, may wrap at 2^32       |
| MeshCore   | SDK-assigned message ID        | String                             | On send return                          | Unique within session context             |
| LXMF       | `LXMessage.hash` (hex)         | String (hex of message hash)       | On message creation                     | Cryptographically unique, tied to content |

**Matrix is the only transport where `native_message_id` implies confirmed delivery.** On Meshtastic, MeshCore, and LXMF, a `native_message_id` indicates the message was submitted to the transport layer, not that it was received by the destination. In all cases, `native_message_id` is a platform-provided value — never fabricated or locally generated.

### 4.2 native_channel_id

Platform-native channel, room, or conversation identifier. Always platform-provided — never fabricated or backfilled from route configuration.

**Pipeline backfill prohibition:** The pipeline must not backfill `native_channel_id` (or any other native ref field) from route configuration (e.g., `target.channel`). Native refs reflect what the platform returned, not what the route expected. If the platform did not return a channel ID, the field must be `None`.

| Transport  | Value                                                | Example                |
| ---------- | ---------------------------------------------------- | ---------------------- |
| Matrix     | Room ID string                                       | `"!roomid:server.tld"` |
| Meshtastic | Channel index as string                              | `"0"`, `"1"`           |
| MeshCore   | Channel index as string, or `None` for DMs           | `"0"`, `None`          |
| LXMF       | `None` (destination is per-message, not per-channel) | `None`                 |

### 4.3 metadata

Immutable, namespaced delivery metadata. Empty `MappingProxyType` by default. Only LXMF populates this with transport-specific delivery state information. See section 7 for details.

### 4.4 native_thread_id and native_relation_id

Populated when the platform supports reply threading or message relations. Currently used for Matrix reply threading via `m.relates_to`. Other transports do not have native threading support, so these remain `None`.

### 4.5 delivery_note

Optional human-readable context string (`str | None`, default `None`). Explains the delivery outcome when native IDs alone are insufficient. Use cases:

- Queue-based adapters noting local-acceptance without platform ACK (e.g., Meshtastic: message enqueued but no radio confirmation).
- MeshCore noting the absence of end-to-end ACK: `"no end-to-end ACK; status reflects local acceptance only"`.
- Any adapter providing diagnostic context about why `native_message_id` is `None`.
- Explaining that "sent" means adapter handoff/acceptance succeeded, not final native-platform delivery.

`delivery_note` is informational only. Consumers must not parse it for control-flow decisions. It is not structured metadata — use `metadata` for machine-readable fields. Its primary purpose is to document the limited ACK semantics and local-acceptance nature of delivery for constrained transports.

### 4.6 Native Ref Persistence Rule

Native refs (`native_message_id`, `native_channel_id`, `native_thread_id`, `native_relation_id`) are persisted in delivery receipts only when `native_message_id` is not `None`. When `native_message_id` is `None` (e.g., Meshtastic queue-based delivery returns synchronously before the platform assigns an ID), no native ref record is created.

The pipeline must not fabricate native refs or backfill them from route configuration. This applies to all native ref fields without exception.

## 5. Delivery State Semantics

### 5.1 Immediate Return Does Not Imply Final Delivery

**The delivery state "sent" means the adapter handoff/acceptance succeeded.** When `deliver()` returns an `AdapterDeliveryResult`, it means the adapter accepted the delivery — the handoff from pipeline to adapter succeeded at the local level. It does **not** mean the message reached its final destination on the native platform, except for Matrix where the homeserver confirms storage. "Sent" is a local-acceptance guarantee, not a final-delivery guarantee. When `native_message_id` is `None`, the `delivery_note` field documents the local-acceptance status.

| Transport  | What `deliver()` return means                                                                                     |
| ---------- | ----------------------------------------------------------------------------------------------------------------- |
| Matrix     | Homeserver accepted and stored the message. `event_id` is proof of server-side persistence.                       |
| Meshtastic | Message was enqueued to the outbound queue. Actual radio send is asynchronous via queue worker.                   |
| MeshCore   | Message was submitted to the SDK `send_text()`. Radio transmission may still fail.                                |
| LXMF       | Message was created and submitted to LXMRouter. Delivery state progresses asynchronously through multiple states. |

### 5.2 Async Transports Report Pending/Outbound

Transports with asynchronous delivery models report initial states that are not "delivered":

| Transport  | Initial Reported State                                                                                                | Final State                                                               |
| ---------- | --------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| Matrix     | Sent (synchronous confirmation)                                                                                       | Sent (server-stored, effectively final)                                   |
| Meshtastic | Queued (returns `None` from `deliver()`) or `AdapterDeliveryResult` with `delivery_note` documenting local-acceptance | Unknown (fire-and-forget)                                                 |
| MeshCore   | Sent (SDK accepted)                                                                                                   | Unknown (fire-and-forget)                                                 |
| LXMF       | Typically `"outbound"`                                                                                                | May progress to `"delivered"`, `"failed"`, `"rejected"`, or `"cancelled"` |

### 5.3 LXMF Delivery State Model

LXMF is the only transport with a formal asynchronous delivery state progression. The eight states, tracked by `LxmfSession`:

```text
generating -> outbound -> sending -> sent -> delivered
                                           -> failed
                                           -> rejected
                                           -> cancelled
```

The initial state reported in `AdapterDeliveryResult.metadata` is typically `"outbound"`. State progression happens asynchronously via the `_on_delivery_state_update` callback registered on the LXMRouter.

The delivery state is exposed in the result metadata under the `lxmf` namespace:

```python
metadata=MappingProxyType({
    "lxmf": {
        "delivery_state": "outbound",        # String enum value
        "delivery_method": "direct",         # Delivery method used
    },
})
```

## 6. Failed Send Behavior

### 6.1 Failed Sends Avoid Native Refs

When a send fails (transient exhaustion or permanent error), the adapter raises `AdapterSendError` (transient, `transient=True`) or `AdapterPermanentError` (permanent, `transient=False`). Transport-specific `*SendError` classes (`MatrixSendError`, `MeshtasticSendError`, `MeshCoreSendError`, `LxmfSendError`) are session/internal-layer errors — they do **not** subclass `AdapterSendError` or `AdapterPermanentError`. Adapters normalize session/internal transport errors into the runtime-facing `AdapterSendError`/`AdapterPermanentError` at the boundary. No `AdapterDeliveryResult` is returned. This means:

- No `native_message_id` is generated for failed sends.
- The pipeline records the failure via the delivery receipt system (contract 21).
- Consumers rely on the receipt pipeline, not on diagnostics, for authoritative failure state.

### 6.2 Per-Transport Failure Behavior

| Transport  | Transient Failure                                                                                                                                                                                                                                 | Permanent Failure                                                                                                                                                                                    |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Retry up to 3x with exponential backoff (500ms, 1s, 2s +-25% jitter). On exhaustion: adapter normalizes internal error and raises `AdapterSendError(transient=True)`.                                                                             | Adapter normalizes internal error and raises `AdapterPermanentError` immediately. Includes: auth failure (HTTP 401/403), room not joined, message too large, not-connected, SDK not initialized.     |
| Meshtastic | Session retries send up to 3x. On exhaustion: increment `transient_delivery_failures`, adapter normalizes internal error and raises `AdapterSendError(transient=True)`. Includes: serial/connection failures, timeout, queue capacity exhaustion. | Increment `permanent_delivery_failures`, adapter normalizes internal error and raises `AdapterPermanentError`. Includes: payload encoding failure, config error, not-connected, SDK not initialized. |
| MeshCore   | Session retries send up to 3x. On exhaustion: increment counters, adapter normalizes internal error and raises `AdapterSendError(transient=True)`. Includes: transport timeout, connection reset, serial link failure.                            | Increment counters, adapter normalizes internal error and raises `AdapterPermanentError`. Includes: invalid address, payload encoding failure, config error, not-connected, SDK not initialized.     |
| LXMF       | Session retries send up to 3x. On exhaustion: increment counters, adapter normalizes internal error and raises `AdapterSendError(transient=True)`. Includes: propagation delay, transport timeout, Reticulum link failure.                        | Increment counters, adapter normalizes internal error and raises `AdapterPermanentError`. Includes: invalid destination hash, config error, not-connected, SDK not initialized.                      |

All four adapters normalize session/internal errors into `AdapterSendError`/`AdapterPermanentError` and let those exceptions propagate to the pipeline, which records delivery receipts. The pipeline's `classify_failure` relies only on `AdapterSendError.transient` to map to `DeliveryFailureKind.ADAPTER_TRANSIENT` (retryable) or `DeliveryFailureKind.ADAPTER_PERMANENT` (dead-letter). It does not inspect the transport-specific `*SendError` hierarchy. See Contract 33 for the full failure taxonomy.

**Matrix `deliver()` exception handling:** The Matrix adapter catches `Exception` (not `BaseException`) in its broad error path. `CancelledError` is explicitly re-raised before this catch, ensuring asyncio task cancellation propagates correctly. This is a contract-level requirement: no adapter may swallow `CancelledError`.

### 6.3 Meshtastic: `deliver()` Returns `None` When Queued

Meshtastic is unique in that `deliver()` enqueues to `MeshtasticOutboundQueue` and returns `None` synchronously. The actual send happens asynchronously via the queue worker. The queue worker does produce an `AdapterDeliveryResult` with a native packet ID when the send completes, but this result is not returned to the caller of `deliver()`.

This means the pipeline cannot correlate a Meshtastic delivery receipt with the original `deliver()` call at present. The queue result flows through the queue's internal tracking only.

## 7. Per-Transport Delivery Metadata and Namespacing

### 7.1 Metadata Namespacing Convention

Transport-specific delivery metadata is namespaced under the transport name within the `metadata` `MappingProxyType`. Only LXMF uses this at present:

```python
metadata["lxmf"]["delivery_state"]   # LXMF only
metadata["lxmf"]["delivery_method"]  # LXMF only
```

Matrix, Meshtastic, and MeshCore return `AdapterDeliveryResult` with empty metadata. This is honest: those transports do not have meaningful delivery state metadata beyond what the presence or absence of a `native_message_id` already conveys.

### 7.2 What Could Appear in Metadata (Per Transport)

| Transport  | Current Metadata                                            | Honest Possibility                                                                                       |
| ---------- | ----------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| Matrix     | Empty `{}`                                                  | None planned. `event_id` is sufficient.                                                                  |
| Meshtastic | Empty `{}`                                                  | Could add `delivery_state: "sent"` for consistency, but this would be decorative. The absence is honest. |
| MeshCore   | Empty `{}`                                                  | Same as Meshtastic.                                                                                      |
| LXMF       | `{"lxmf": {"delivery_state": ..., "delivery_method": ...}}` | The only transport with meaningful delivery metadata.                                                    |

### 7.3 No Loose Ad-Hoc Fields

No adapter injects loose transport-specific fields directly onto `AdapterDeliveryResult`. All transport-specific data is namespaced under `metadata[<transport>]`. This matches the metadata envelope convention documented in contract 26 and audited in contract 27 section 5.

## 8. Duplicate-Send Risk Under Retry

All four adapters implement bounded retry with acknowledged duplicate-send risk. This is a fundamental property of at-least-once delivery, not a bug.

### 8.1 Per-Transport Duplication Mechanism

| Adapter    | Documented                                | How Duplication Happens                                                                                                            |
| ---------- | ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | Yes (adapter `deliver()` docstring)       | Send succeeded on homeserver, but HTTP response lost. Retry sends again, producing a second event.                                 |
| Meshtastic | Yes (session docstring)                   | Send received by remote node, but ACK lost on radio link. Retry sends again.                                                       |
| MeshCore   | Yes (session docstring, explicit warning) | Same pattern as Meshtastic.                                                                                                        |
| LXMF       | Implicit                                  | Retries on new `send_text()` calls. If first send entered the network, retry creates a distinct new message with a different hash. |

### 8.2 Consumer Contract

**Consumers must be tolerant of duplicate deliveries.** This is non-negotiable. The bounded retry model guarantees at-least-once delivery, not exactly-once. Deduplication, if needed, is the consumer's responsibility using `native_message_id` as a dedup key where available.

### 8.3 Meshtastic Queue-Side Duplicate Risk

The Meshtastic outbound queue (`MeshtasticOutboundQueue`) adds an additional duplication vector: if the queue worker sends a message successfully but crashes or loses track before recording the send, a restart could re-process the queue item. The queue does not implement transactional send-and-record.

## 9. Contractual Guarantees for Beta

1. **`AdapterDeliveryResult` is frozen and immutable.** No fields can be mutated after construction.
2. **`native_message_id` is `str | None` and always platform-provided.** Adapters must never fabricate or locally-generate a native message ID. Callers must handle `None`, especially for Meshtastic where `deliver()` returns `None` synchronously.
3. **`native_channel_id` is platform-provided and may be `None`.** The pipeline must not backfill `native_channel_id` or any other native ref field from route configuration.
4. **Metadata is namespaced.** Transport-specific metadata lives under `metadata[<transport>]`. No loose ad-hoc fields.
5. **Failed sends raise exceptions, not delivery results.** No `AdapterDeliveryResult` is returned on failure.
6. **"Sent" means adapter handoff/acceptance succeeded, not final delivery.** Immediate return is not final delivery except for Matrix. All other transports may lose, delay, or fail to deliver the message after `deliver()` returns.
7. **Native refs are persisted only when `native_message_id` exists.** No native ref record is created when `native_message_id` is `None`.
8. **Duplicate-send risk is universal.** All adapters implement at-least-once delivery with bounded retry. Consumers must be duplicate-tolerant.
9. **LXMF delivery state progression is the only async delivery tracking model.** No other transport provides post-return delivery state updates.
10. **No delivery metadata will be removed or retyped** without a contract version bump. New metadata keys may be added under existing namespaces.
11. **`CancelledError` must propagate.** No adapter may swallow `CancelledError`. Adapters catch `Exception`, not `BaseException`.

# Delivery Result Contract

> Contract version: 1
> Last updated: 2026-05-09
> Track: 9 (Transport Capability Contracts)
> Supersedes: Nothing. Formalizes findings from contracts 21, 22, 27, 28.
> Status: Contract. Documents the locked-in delivery result semantics for beta.

This document defines the contractual semantics of `AdapterDeliveryResult` and per-transport delivery behavior. It records what each field means, when it is populated, what "delivered" actually means per transport, and the risks consumers must handle.

This is a contract document. No runtime redesign, adapter abstraction, or new delivery features are proposed.


## 1. Scope

- `AdapterDeliveryResult` field semantics: `native_message_id`, `native_channel_id`, `metadata`, and delivery state.
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

Location: `src/medre/adapters/base.py`

```python
@dataclass(frozen=True)
class AdapterDeliveryResult:
    native_message_id: str | None = None
    native_channel_id: str | None = None
    native_thread_id: str | None = None
    native_relation_id: str | None = None
    metadata: MappingProxyType[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )
```

This is an immutable, frozen dataclass. The pipeline uses it to store `NativeMessageRef` mappings. The pipeline owns receipts and storage. Adapters only report what the platform returned.


## 4. Field Semantics

### 4.1 native_message_id

Platform-native message ID assigned by the external system. `None` when the platform did not return one.

| Transport | Source | Type | When Available | Uniqueness |
|-----------|--------|------|----------------|------------|
| Matrix | Homeserver-assigned `event_id` | String (e.g. `$xxx`) | Immediately on `RoomSendResponse` | Globally unique, persistent, queryable |
| Meshtastic | Firmware-assigned packet ID | Integer (32-bit), stored as string | On send acknowledgment (if ACK enabled) | Unique per sender, may wrap at 2^32 |
| MeshCore | SDK-assigned message ID | String | On send return | Unique within session context |
| LXMF | `LXMessage.hash` (hex) | String (hex of message hash) | On message creation | Cryptographically unique, tied to content |

**Matrix is the only transport where `native_message_id` implies confirmed delivery.** On Meshtastic, MeshCore, and LXMF, a `native_message_id` indicates the message was submitted to the transport layer, not that it was received by the destination.

### 4.2 native_channel_id

Platform-native channel, room, or conversation identifier.

| Transport | Value | Example |
|-----------|-------|---------|
| Matrix | Room ID string | `"!roomid:server.tld"` |
| Meshtastic | Channel index as string | `"0"`, `"1"` |
| MeshCore | Channel index as string, or `None` for DMs | `"0"`, `None` |
| LXMF | `None` (destination is per-message, not per-channel) | `None` |

### 4.3 metadata

Immutable, namespaced delivery metadata. Empty `MappingProxyType` by default. Only LXMF populates this with transport-specific delivery state information. See section 7 for details.

### 4.4 native_thread_id and native_relation_id

Populated when the platform supports reply threading or message relations. Currently used for Matrix reply threading via `m.relates_to`. Other transports do not have native threading support, so these remain `None`.


## 5. Delivery State Semantics

### 5.1 Immediate Return Does Not Imply Final Delivery

`deliver()` returning `AdapterDeliveryResult` means the transport accepted the message. It does **not** mean the message reached its destination, except for Matrix where the homeserver confirms storage.

| Transport | What `deliver()` return means |
|-----------|------------------------------|
| Matrix | Homeserver accepted and stored the message. `event_id` is proof of server-side persistence. |
| Meshtastic | Message was enqueued to the outbound queue. Actual radio send is asynchronous via queue worker. |
| MeshCore | Message was submitted to the SDK `send_text()`. Radio transmission may still fail. |
| LXMF | Message was created and submitted to LXMRouter. Delivery state progresses asynchronously through multiple states. |

### 5.2 Async Transports Report Pending/Outbound

Transports with asynchronous delivery models report initial states that are not "delivered":

| Transport | Initial Reported State | Final State |
|-----------|----------------------|-------------|
| Matrix | Sent (synchronous confirmation) | Sent (server-stored, effectively final) |
| Meshtastic | Queued (returns `None` from `deliver()`) | Unknown (fire-and-forget) |
| MeshCore | Sent (SDK accepted) | Unknown (fire-and-forget) |
| LXMF | Typically `"outbound"` | May progress to `"delivered"`, `"failed"`, `"rejected"`, or `"cancelled"` |

### 5.3 LXMF Delivery State Model

LXMF is the only transport with a formal asynchronous delivery state progression. The eight states, tracked by `LxmfSession`:

```
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

When a send fails (transient exhaustion or permanent error), the adapter raises a transport-specific exception. No `AdapterDeliveryResult` is returned. This means:

- No `native_message_id` is generated for failed sends.
- The pipeline records the failure via the delivery receipt system (contract 21).
- Consumers rely on the receipt pipeline, not on diagnostics, for authoritative failure state.

### 6.2 Per-Transport Failure Behavior

| Transport | Transient Failure | Permanent Failure |
|-----------|-------------------|-------------------|
| Matrix | Retry up to 3x with exponential backoff (500ms, 1s, 2s +-25% jitter). On exhaustion: raise `MatrixSendError`. | Raise `MatrixSendError` immediately. |
| Meshtastic | Session retries send up to 3x. On exhaustion: increment `transient_delivery_failures`, raise `MeshtasticSendError`. | Increment `permanent_delivery_failures`, raise `MeshtasticSendError`. |
| MeshCore | Session retries send up to 3x. On exhaustion: increment counters, raise `MeshCoreSendError`. | Increment counters, raise `MeshCoreSendError`. |
| LXMF | Session retries send up to 3x. On exhaustion: increment counters, raise `LxmfSendError`. | Increment counters, raise `LxmfSendError`. |

All four adapters let exceptions propagate to the pipeline, which records delivery receipts. See `phase-1-limitations.md` Track 3 for the retry/dead-letter system.

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

| Transport | Current Metadata | Honest Possibility |
|-----------|-----------------|-------------------|
| Matrix | Empty `{}` | None planned. `event_id` is sufficient. |
| Meshtastic | Empty `{}` | Could add `delivery_state: "sent"` for consistency, but this would be decorative. The absence is honest. |
| MeshCore | Empty `{}` | Same as Meshtastic. |
| LXMF | `{"lxmf": {"delivery_state": ..., "delivery_method": ...}}` | The only transport with meaningful delivery metadata. |

### 7.3 No Loose Ad-Hoc Fields

No adapter injects loose transport-specific fields directly onto `AdapterDeliveryResult`. All transport-specific data is namespaced under `metadata[<transport>]`. This matches the metadata envelope convention documented in contract 26 and audited in contract 27 section 5.


## 8. Duplicate-Send Risk Under Retry

All four adapters implement bounded retry with acknowledged duplicate-send risk. This is a fundamental property of at-least-once delivery, not a bug.

### 8.1 Per-Transport Duplication Mechanism

| Adapter | Documented | How Duplication Happens |
|---------|-----------|------------------------|
| Matrix | Yes (adapter `deliver()` docstring) | Send succeeded on homeserver, but HTTP response lost. Retry sends again, producing a second event. |
| Meshtastic | Yes (session docstring) | Send received by remote node, but ACK lost on radio link. Retry sends again. |
| MeshCore | Yes (session docstring, explicit warning) | Same pattern as Meshtastic. |
| LXMF | Implicit | Retries on new `send_text()` calls. If first send entered the network, retry creates a distinct new message with a different hash. |

### 8.2 Consumer Contract

**Consumers must be tolerant of duplicate deliveries.** This is non-negotiable. The bounded retry model guarantees at-least-once delivery, not exactly-once. Deduplication, if needed, is the consumer's responsibility using `native_message_id` as a dedup key where available.

### 8.3 Meshtastic Queue-Side Duplicate Risk

The Meshtastic outbound queue (`MeshtasticOutboundQueue`) adds an additional duplication vector: if the queue worker sends a message successfully but crashes or loses track before recording the send, a restart could re-process the queue item. The queue does not implement transactional send-and-record.


## 9. Contractual Guarantees for Beta

1. **`AdapterDeliveryResult` is frozen and immutable.** No fields can be mutated after construction.
2. **`native_message_id` is `str | None`.** Callers must handle `None`, especially for Meshtastic where `deliver()` returns `None` synchronously.
3. **Metadata is namespaced.** Transport-specific metadata lives under `metadata[<transport>]`. No loose ad-hoc fields.
4. **Failed sends raise exceptions, not delivery results.** No `AdapterDeliveryResult` is returned on failure.
5. **Immediate return is not final delivery**, except for Matrix. All other transports may lose, delay, or fail to deliver the message after `deliver()` returns.
6. **Duplicate-send risk is universal.** All adapters implement at-least-once delivery with bounded retry. Consumers must be duplicate-tolerant.
7. **LXMF delivery state progression is the only async delivery tracking model.** No other transport provides post-return delivery state updates.
8. **No delivery metadata will be removed or retyped** without a contract version bump. New metadata keys may be added under existing namespaces.

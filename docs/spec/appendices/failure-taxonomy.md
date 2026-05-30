# Failure Taxonomy

Per-transport failure classification, retry semantics, and operational
implications.

---

## 1. Failure Classification Axes

### 1.1 Transient vs. Permanent

| Axis          | Definition                                                                                   |
| ------------- | -------------------------------------------------------------------------------------------- |
| **Transient** | Failure may resolve with time, retry, or reconnection. The message may still be deliverable. |
| **Permanent** | Failure is definitive. The message will not be delivered regardless of retries.              |

### 1.2 Reconnectable vs. Unrecoverable

| Axis              | Definition                                                                                  |
| ----------------- | ------------------------------------------------------------------------------------------- |
| **Reconnectable** | The transport session can re-establish connectivity and resume operation after the failure. |
| **Unrecoverable** | The transport session cannot recover; a new session must be created.                        |

### 1.3 Duplicate-Send Risk

| Level      | Definition                                                                |
| ---------- | ------------------------------------------------------------------------- |
| **None**   | The transport guarantees exactly-once delivery or idempotent sends.       |
| **Low**    | Duplicates are possible only under specific, documented conditions.       |
| **Medium** | Duplicates are possible under normal failure/retry scenarios.             |
| **High**   | Duplicates are likely during normal operation; consumer must deduplicate. |

### 1.4 Queue-Drain Semantics

| Category           | Definition                                                                   |
| ------------------ | ---------------------------------------------------------------------------- |
| **FIFO drain**     | Messages drained in order; no reordering under normal conditions.            |
| **Lossy drain**    | Some messages may be silently dropped during drain (e.g., queue overflow).   |
| **No queue**       | No outbound queue; sends are immediate and fire-and-forget.                  |
| **Scaffold queue** | Outbound queue with bounded retry; exhausted and permanent failures dropped. |

## 2. Cross-Transport Failure Summary

| Dimension                 | Matrix               | Meshtastic        | MeshCore          | LXMF                  |
| ------------------------- | -------------------- | ----------------- | ----------------- | --------------------- |
| **Transient cause**       | Network/auth/rate    | Radio/link/serial | Radio/link/serial | Network/RNS           |
| **Permanent cause**       | Auth revocation      | Config/port error | Config error      | Identity/RNS init     |
| **Reconnect model**       | Exp backoff, 10 att  | Exp backoff, 10   | Exp backoff, 10   | Exp backoff, 10       |
| **Duplicate-send risk**   | Low-Medium           | High              | Medium            | Low                   |
| **Outbound queue**        | None (direct send)   | Scaffold (lossy)  | None (direct)     | None (router-managed) |
| **Delivery confirmation** | Server event_id      | None              | None              | Async state callback  |
| **Uncertainty window**    | ~0 to one sync cycle | Unbounded         | Unbounded         | Unbounded             |

## 3. Matrix Failure Detail

### Connection Failures

| Failure                | Transient/Permanent | Reconnectable   |
| ---------------------- | ------------------- | --------------- |
| Network unreachable    | Transient           | Yes             |
| DNS resolution failure | Transient           | Yes             |
| TLS handshake failure  | Transient (or perm) | Yes (transient) |
| HTTP 429 (rate limit)  | Transient           | Yes             |
| HTTP 401/403 (auth)    | Permanent           | No              |

### Send Failures

| Failure                | Duplicate-Send Risk |
| ---------------------- | ------------------- |
| `room_send` HTTP error | Low (tx_id dedup)   |
| `room_send` timeout    | Medium              |

### E2EE Failure Classes

| Failure                          | Class             | Recovery                 |
| -------------------------------- | ----------------- | ------------------------ |
| Missing crypto dependency        | Permanent, fatal  | Install deps and restart |
| Device not verified              | Permanent per msg | Interactive verification |
| Megolm session not received      | Transient         | Wait for key delivery    |
| `e2ee_required` + plaintext room | Permanent         | Use encrypted room       |

## 4. Meshtastic Failure Detail

- Session retries transient failures up to 3 times.
- Duplicate-send risk: **High**. Consumers must be tolerant of duplicates.
- Queue: bounded-retry outbound queue. Exhausted retries and permanent
  failures are dropped. Retry is best-effort, adapter-local, in-memory,
  non-durable across process restart.
- No end-to-end delivery confirmation exists for text messages.
- ACKs are at the LoRa link level (hop-by-hop), not end-to-end.

## 5. MeshCore Failure Detail

- Session retries transient failures up to 3 times.
- Duplicate-send risk: **Medium**.
- No outbound queue. `send_text()` called directly on the session.
- No end-to-end delivery confirmation.
- E2EE at radio level; MEDRE does not manage keys.

## 6. LXMF Failure Detail

- Session retries transient outbound failures up to 3 attempts with linear backoff (0.1 s × attempt).
- Duplicate-send risk: **Low**. Each LXMF message has a unique hash.
- No outbound queue. The LXMRouter manages its own internal delivery queue.
- Delivery confirmation is asynchronous via state callbacks.
- Propagated messages have no delivery time guarantee.
- Uncertainty window is effectively unbounded for propagated delivery.

## 7. Route Policy Suppression

Route policy suppression is a cross-transport failure classification. It
occurs when the route-policy evaluator denies a delivery after route matching
but before delivery side effects.

| Property       | Value               |
| -------------- | ------------------- |
| Failure kind   | `policy_suppressed` |
| Retryable      | No (permanent)      |
| Receipt status | `suppressed`        |

Denial reason codes: `source_adapter_not_allowed`, `dest_adapter_not_allowed`,
`sender_not_allowed`, `room_not_allowed`, `channel_not_allowed`.

## 8. Loop Suppression

Loop suppression is a cross-transport failure classification. It occurs when a
loop-prevention guard fires during delivery: either the self-loop guard
(`target_adapter == source_adapter`) or the route-trace guard (a route ID
appears more than once in the event's routing metadata). The adapter's
`send()` method is NOT called.

| Property         | Value                                                                                           |
| ---------------- | ----------------------------------------------------------------------------------------------- |
| Failure kind     | `loop_suppressed`                                                                               |
| Outcome status   | `skipped`                                                                                       |
| Receipt status   | `suppressed`                                                                                    |
| Receipt evidence | `event_id`, `route_id`, `target_adapter`, `failure_kind="loop_suppressed"`, and a reason string |
| Retryable        | No — `next_retry_at` is `None`, receipt does not enter retry queue                              |
| Adapter called   | No                                                                                              |

Self-loop and route-trace suppression produce the same `failure_kind` but are
distinguishable by the reason string in the receipt `error` field. Both
increment the `loop_prevented` counter in runtime accounting and route stats.

## 9. Capability Suppression

Capability suppression is a cross-transport failure classification. It occurs
when the target adapter's declared capabilities do not support the event's
kind or relation type. The `CapabilityDecisionResolver` produces a decision
with `capability_level="unsupported"` and `delivery_strategy="skip"`, which
causes the pipeline to suppress delivery before rendering and adapter
invocation.

| Property         | Value                                                                                                                    |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Failure kind     | `capability_suppressed`                                                                                                  |
| Outcome status   | `skipped`                                                                                                                |
| Receipt status   | `suppressed`                                                                                                             |
| Receipt evidence | `event_id`, `route_id`, `target_adapter`, `failure_kind="capability_suppressed"`, `capability_field`, `capability_level` |
| Retryable        | No — `next_retry_at` is `None`, receipt does not enter retry queue                                                       |
| Adapter called   | No                                                                                                                       |

The receipt `error` field carries the capability reason (e.g. `"reactions
unsupported by adapter"`). The `capability_field` identifies which
`AdapterCapabilities` field caused the suppression (e.g. `reactions`,
`replies`, `text`). See the Routing and Delivery Specification § 6.3.3 for the
complete event-kind to capability-field mapping.

## 10. Operational Implications

1. Consumers must handle duplicates for Meshtastic and MeshCore.
2. Delivery confirmation is transport-dependent. Only Matrix provides strong
   confirmation (server-persisted event_id).
3. Queue-drain retry is bounded in Meshtastic. Exhausted retries are dropped.
4. E2EE failures in Matrix are recoverable but require operator intervention.
5. Reconnect budgets are finite (10 consecutive attempts) across all transports.
6. No transport provides end-to-end delivery confirmation that MEDRE can
   observe, except Matrix (server-side event_id) and LXMF (async DELIVERED
   state callback).
7. Suppressed deliveries (loop, policy, capability) never invoke the adapter.
   The adapter is not aware of suppressed events — there is no adapter-side
   counter or state change.
8. Suppressed receipts have `status="suppressed"`, not `"failed"`. They do not
   enter the retry queue. Operators checking for "failed deliveries" must
   query for `status IN ('failed', 'dead_lettered')` to exclude suppressed
   entries, or query `status IN ('suppressed', 'failed', 'dead_lettered')` to
   include all non-successful outcomes.

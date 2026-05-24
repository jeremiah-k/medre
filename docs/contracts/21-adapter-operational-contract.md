# Adapter Operational Contract

> **Status:** Active
> **Classification:** Normative
> **Authority:** Current contract for adapter operational boundaries, pacing, queueing, and health
> **Last reviewed:** 2026-05-24
>
> Contract version: 1
> Last updated: 2026-05-09
> Track: 9 (Transport Capability Contracts)
> Supersedes: Nothing. Extends contracts 02, 15, 16, 18.

This document defines the operational ownership boundaries for MEDRE adapters. It specifies who owns what during the transport lifecycle, where pacing and queueing responsibilities sit, what adapters may and may not do to canonical events, and how health, failure, retry, and callback semantics work at the adapter boundary.

This is a contract, not an implementation plan. It codifies what already holds and what future adapter work must respect. It does not introduce a production scheduler, a retry engine, or background queue workers.

## 1. Scope

- Adapter lifecycle ownership: start, stop, health, and failure reporting.
- Pacing and queueing ownership for constrained transports.
- Boundaries between adapter, renderer, codec, classifier, runtime, and storage.
- Ingress immutability: what adapters must not mutate.
- Fake adapter expectations for testing.
- Optional dependency and callback semantics.

## 2. Non-goals

- Production retry scheduler implementation.
- Background queue worker implementation.
- Rate limiting or retry budget enforcement.
- Dead-letter queue management beyond receipt recording.
- E2EE, reactions, media, or attachment handling.
- Real network connectivity claims.
- Admin APIs, webhooks, or plugin extensions.

## 3. Adapter Owns the Transport Lifecycle

The adapter is the sole owner of its transport connection. No other component in the system opens, closes, restarts, or inspects the adapter's transport.

### 3.1 Start

`start(context)` is called once during runtime initialization. The adapter must:

1. Establish whatever connection or session its transport requires.
2. Register internal listeners or callbacks that feed into `context.publish_inbound()`.
3. Transition its internal health state from `"unknown"` to `"healthy"` or `"degraded"` as appropriate.
4. Return only after the adapter is ready to accept delivery work or after the connection attempt has progressed far enough to report a definitive health state.

The runtime does not time out `start()`. The adapter is expected to handle its own connection timeouts internally and report `"unhealthy"` if the transport cannot be reached.

### 3.2 Stop

`stop()` is called once during graceful shutdown. The adapter must:

1. Reject new delivery work. In-flight deliveries may complete if the transport permits.
2. Close connections, cancel background tasks, and release resources.
3. Transition health state to `"unknown"` or `"stopped"`.

Phase 1 fake adapters have no background queues or connections to drain. Real adapters must ensure no orphaned asyncio tasks remain after `stop()` returns.

### 3.3 Health Check

`health_check()` is called periodically by the runtime's lifecycle manager. It must be cheap, non-blocking, and return one of:

| State         | Meaning                                                                |
| ------------- | ---------------------------------------------------------------------- |
| `"unknown"`   | Adapter not started, stopped, or health indeterminate                  |
| `"healthy"`   | Transport connected and operational                                    |
| `"degraded"`  | Transport partially functional (intermittent connection, high latency) |
| `"unhealthy"` | Transport disconnected or non-functional                               |

The adapter sets its own health state. The runtime reads it. The runtime never sets adapter health.

### 3.4 Failure Reporting

When a transport-level failure occurs during `deliver()`, the adapter raises an exception. The pipeline classifies the exception into a `DeliveryFailureKind`. The adapter does not classify its own failures. It reports them honestly and lets the pipeline decide.

Adapters may log transport-specific diagnostics at whatever verbosity their configuration permits. They do not write receipts, update delivery state, or trigger retries. The pipeline owns all of that.

## 4. Adapter Owns Pacing and Queueing for Constrained Transports

Constrained transports (Meshtastic, MeshCore, and to a lesser extent LXMF) have limited bandwidth, small payload sizes, and duty cycle restrictions. The adapter is responsible for managing the pace at which it sends messages over its transport.

### 4.1 Queueing Modes

| Mode               | Behavior                                                                                         | Adapter Responsibility                                                                                                                                                    |
| ------------------ | ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Immediate-send** | No queuing. `deliver()` sends immediately and returns.                                           | Adapter calls transport send directly. No internal queue. Suitable for Matrix and unconstrained transports.                                                               |
| **Enqueue-only**   | `deliver()` places the rendered payload into an internal outbound queue and returns immediately. | Adapter maintains its own queue. A background task or timer drains the queue at a transport-appropriate rate. The pipeline sees a fast `deliver()` return.                |
| **Paced**          | `deliver()` sends with an inter-message delay to respect transport duty cycles.                  | Adapter enforces a minimum interval between sends. Meshtastic's `MeshtasticOutboundQueue` is the canonical example: configurable delay, FIFO ordering within the adapter. |
| **ACK-driven**     | `deliver()` sends and waits for a transport-level acknowledgment before returning.               | Adapter blocks until ACK received or timeout. MeshCore's `send_msg()` with `expected_ack` follows this pattern.                                                           |
| **Best-effort**    | `deliver()` attempts to send, ignores failures, returns immediately.                             | No retry, no ACK wait, no queue. Fire and forget. Appropriate for telemetry or low-priority status messages on unreliable links.                                          |

An adapter may support multiple modes and select based on message kind or configuration. The pipeline does not dictate the mode. The adapter declares its behavior through its configuration and implementation.

### 4.2 What the Runtime Does NOT Own

The runtime does not own:

- Per-adapter outbound queues. Those live inside the adapter.
- Pacing timers or duty cycle calculations. Those are adapter internals.
- Retry scheduling. The pipeline records retry-eligible failures on receipts and computes `next_retry_at`, but no background scheduler exists to re-attempt delivery. This is explicitly a Phase 1 limitation (see `phase-1-limitations.md`, Section 0.5).
- Retry budgets or rate limits beyond what the adapter self-imposes.

### 4.3 Future Scheduler Boundaries

When a retry scheduler is eventually implemented, its boundary with the adapter must respect this contract: the scheduler re-submits a `RenderingResult` to `deliver()`. The adapter does not know or care whether this is a first attempt or a retry. The adapter does not track attempt counts. The pipeline tracks attempt counts on receipts.

This boundary is fixed now so future scheduler work does not require adapter changes.

## 5. Ownership Boundaries

The following table defines who owns what. Every row is a hard boundary. Violations indicate a design error.

| Concern                                                                  | Owner                   | Others May                               |
| ------------------------------------------------------------------------ | ----------------------- | ---------------------------------------- |
| Transport lifecycle (connect, disconnect, reconnect)                     | Adapter                 | Read health state                        |
| Pacing, queueing, duty cycle management                                  | Adapter                 | Set rate limit config                    |
| Payload formatting (text, rich content, transport-specific layout)       | Renderer                | Provide RenderingResult                  |
| Payload encoding/decoding (native format to CanonicalEvent)              | Codec                   | Read codec output                        |
| Packet classification (portnum detection, ACK detection, type inference) | Classifier              | Read classification result               |
| Pipeline orchestration (routing, delivery planning, receipt tracking)    | Runtime                 | None; adapters never bypass the pipeline |
| Event authority, correlation, and lineage storage                        | Storage                 | Read via storage API                     |
| Retry/backoff computation (stateless)                                    | Runtime (RetryExecutor) | Record on receipts                       |
| Retry scheduling (timed re-attempt)                                      | Not implemented         | Reserved for future work                 |
| Native message reference persistence                                     | Storage                 | Read via storage API                     |

### 5.1 Renderer Owns Payload Formatting Only

The renderer converts a `CanonicalEvent` into a `RenderingResult` suitable for the target adapter. It formats text, applies transport-specific length constraints, and builds whatever native content structure the adapter expects. The renderer does not send, queue, or deliver anything. It produces a rendering result and hands it off.

### 5.2 Codec and Classifier Own Translation Only

The codec decodes inbound native data into a `CanonicalEvent`. The classifier inspects native packet structure to determine type, sender, channel, and message ID. Neither makes routing decisions. Neither stores events. Neither calls `publish_inbound()`; the adapter does that after codec/classifier processing.

### 5.3 Runtime Owns Orchestration Only

The runtime coordinates the pipeline: ingestion, routing, delivery planning, rendering, delivery execution, receipt recording, and retry computation. It does not transport, encode, decode, format, or store anything itself. It delegates each step to the component that owns it.

### 5.4 Storage Owns Authority and Correlation

Storage is the single source of truth for events, lineage, relations, native refs, and receipts (Contracts 03, 07, 17). Any feature that needs reliable event history reads from storage. Metadata carried in external platform envelopes is secondary and diagnostic.

## 6. Envelopes Are Secondary Hints

A "MEDRE envelope" is metadata embedded in the native payload of an outbound message. Matrix puts it in `medre.envelope` within `m.room.message` content. LXMF puts it in `FIELD_CUSTOM_META` (0xFD) in the `fields` dict. Meshtastic and MeshCore payloads are too small for rich envelopes.

Envelopes are hints. They assist diagnostic correlation when present. They are not authoritative. They may be stripped, truncated, or corrupted by:

- Transport payload limits (Meshtastic, MeshCore).
- Client redaction (Matrix clients may prune custom content).
- Protocol version changes.
- Third-party client behavior that ignores unknown fields.

Storage is always authoritative. Any code that relies on envelope metadata for correctness rather than diagnostics is incorrect by contract.

## 7. Ingress Immutability

After an adapter codec produces a `CanonicalEvent` and the adapter publishes it via `context.publish_inbound()`, the event is frozen. No component may mutate the canonical event after ingress.

Specifically:

1. **Adapters must not mutate canonical events after calling `publish_inbound()`.** The event reference held by the adapter is now shared with the pipeline. Modifying it would corrupt pipeline state.
2. **CanonicalEvent is `frozen=True` in its struct definition.** Msgspec enforces this at attribute assignment time. This is a runtime guard, not just a convention.
3. **Derived events are new events.** Pipeline stages that transform, enrich, or derive from a source event must create a new `CanonicalEvent` with a new `event_id`. They never modify the source event in place.
4. **Metadata enrichment is additive.** If a pipeline stage needs to add metadata, it creates a derived event with the additional metadata. The source event's metadata remains unchanged.

## 8. Health, Start, Stop, and Failure Callbacks

### 8.1 Health Transitions

The adapter owns its health state machine. The runtime observes it through `health_check()`. Valid transitions:

```text
unknown -> healthy
unknown -> degraded
unknown -> unhealthy
healthy -> degraded
healthy -> unhealthy
healthy -> unknown  (on stop)
degraded -> healthy
degraded -> unhealthy
degraded -> unknown  (on stop)
unhealthy -> healthy  (on reconnect)
unhealthy -> degraded
unhealthy -> unknown  (on stop)
```

Any other transition is a bug.

### 8.2 Failure Callbacks

When `deliver()` raises an exception, the pipeline catches it and:

1. Classifies the failure via `RetryExecutor.classify_failure()`.
2. Records a `DeliveryReceipt` with the appropriate `failure_kind` and `next_retry_at`.
3. If retries remain, the receipt records `attempt_number` and `parent_receipt_id` for lineage.
4. If retries exhausted, a dead-letter receipt is appended.

The adapter is not notified of this process. It does not receive a callback. It does not know whether its failure triggered a retry. This isolation is intentional: the adapter's job is to attempt delivery and report the outcome (success or exception). The pipeline's job is to decide what happens next.

### 8.3 Task Callbacks

Adapters may spawn background asyncio tasks for listener loops, ACK waiters, or queue drainers. These tasks are owned by the adapter. The runtime does not track or manage them.

The adapter must ensure all spawned tasks are cancelled and awaited during `stop()`. Leaked tasks after `stop()` returns are a bug. The optional Matrix live smoke harness verifies this explicitly.

## 9. Optional Dependencies

### 9.1 Principle

No adapter's SDK is a required MEDRE dependency. The core runtime and its tests must pass without any transport SDK installed. This policy exists because:

- MEDRE deployments may only use a subset of adapters.
- Transport SDKs bring hardware-specific dependencies (BLE, serial, etc.) that not all environments support.
- CI must be deterministic and independent of hardware or network availability.

### 9.2 Pattern

Each adapter follows the same pattern:

| Component           | Requires real SDK | Fallback                                  |
| ------------------- | ----------------- | ----------------------------------------- |
| Fake adapter        | No                | Uses deterministic fixtures               |
| Codec unit tests    | No                | Uses fixture dicts matching native format |
| Renderer unit tests | No                | Uses fixture RenderingResults             |
| Live smoke harness  | Yes               | Skipped by default, enabled by env vars   |

When the real SDK is not installed, importing the live adapter class must fail gracefully. The fake adapter must never import the real SDK.

### 9.3 Current Status

| Adapter    | SDK package                       | Installed by default | Live harness                             |
| ---------- | --------------------------------- | -------------------- | ---------------------------------------- |
| Matrix     | `matrix-nio` (via `mindroom-nio`) | No                   | Optional, at `tests/test_matrix_live.py` |
| Meshtastic | `meshtastic`                      | No                   | Not yet                                  |
| MeshCore   | `meshcore`                        | No                   | Not yet                                  |
| LXMF       | `lxmf`, `rns`                     | No                   | Not yet                                  |

## 10. Fake Adapter Expectations

Fake adapters are first-class contract participants. They are not stubs or placeholders. They enforce the same boundaries as real adapters and must satisfy the same `Adapter` protocol.

### 10.1 Contractual Requirements

Every fake adapter must:

1. Satisfy the full `Adapter` protocol: `start()`, `stop()`, `deliver()`, `health_check()`.
2. Enforce the rendering boundary: `deliver()` accepts `RenderingResult` only, not `CanonicalEvent`.
3. Report deterministic health transitions: `"unknown"` on construction, `"healthy"` after `start()`, `"unknown"` after `stop()`.
4. Return deterministic `AdapterDeliveryResult` instances with synthetic native IDs.
5. Exercise the codec/classifier pipeline with fixture data matching the real native format.
6. Never import the real SDK.
7. Support the same `supported_event_kinds` as the real adapter.

### 10.2 What Fake Adapters Must NOT Do

1. Open network connections.
2. Import the real transport SDK.
3. Bypass the rendering boundary by accepting `CanonicalEvent` directly.
4. Produce non-deterministic output (random IDs, timestamps that vary between runs).
5. Depend on external state (files, environment variables beyond test configuration).

## 11. Retry and Dedup: Runtime Non-Ownership

Phase 1 implements automatic retry scheduling via RetryWorker (opt-in). Deduplication is not implemented. This section records the boundary so it is not accidentally crossed.

### 11.1 Retry

The `RetryExecutor` computes backoff timing and exhaustion checks. It is stateless and has no side effects. It produces values that are recorded on `DeliveryReceipt` rows:

- `next_retry_at`: When a retry should theoretically occur.
- `attempt_number`: 1-indexed count of delivery attempts.
- `parent_receipt_id`: Links this receipt to the previous attempt's receipt.

No code existed to read `next_retry_at` and trigger a re-delivery prior to RetryWorker. RetryWorker now acts on `next_retry_at` by polling for due retry receipts. The receipt records the intent, and RetryWorker executes it when enabled.

Adapters must not implement their own retry loops. If `deliver()` fails, it raises. The pipeline records the failure. Period.

### 11.2 Deduplication

The pipeline does not deduplicate delivery attempts. If the same delivery plan is submitted twice, it is delivered twice. Deduplication is a storage-layer concern (idempotent `append` on events) and a routing-layer concern (duplicate plan detection), not an adapter concern.

Adapters must not deduplicate. If the transport provides native dedup (e.g., Matrix's idempotent `txn_id`), the adapter may use it as an optimization, but the pipeline must not depend on it.

## 12. Implications

### 12.1 For Adapter Authors

- Your adapter is an island. It owns its transport, its pacing, and its queueing. It does not own routing, retries, or receipts.
- `deliver()` must be honest. Return normally on success, raise on failure. Nothing else.
- Health state must be accurate. The runtime and any operator tooling relies on it.
- Background tasks must be cleaned up in `stop()`. No exceptions.

### 12.2 For Runtime Authors

- The runtime never opens a transport, never paces a queue, and never classifies a failure that the adapter should classify.
- Receipts are append-only. No receipt row is ever updated or deleted (Contract 03).
- The retry scheduler, when it arrives, re-submits through `deliver()`. It does not bypass the adapter boundary.

### 12.3 For Operators

- Adapter health is the primary operational signal. Monitor `health_check()` output.
- Failed deliveries are visible in receipt records. Use `list_receipts_for_plan()` to inspect.
- No automatic retry exists. Failed deliveries stay failed until replayed manually or until a scheduler is implemented.
- Fake adapters are for testing only. They do not validate transport behavior.

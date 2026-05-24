# Contract 52 — Routed Delivery Result Contract

**Status:** Active
**Scope:** Authoritative specification for how the route layer preserves adapter delivery semantics, how `AdapterDeliveryResult` flows through routing, self-loop guard behavior, per-destination result separation, failure attribution, and delivery finality guarantees.
**Audience:** Runtime builders, adapter authors, pipeline authors, operators, test harnesses.
**References:** Contract 49 (Routing and Bridge), Contract 50 (Runtime Topology), Contract 51 (Route Attribution), Contract 31 (Session Boundary).

Every agent or document that references routed delivery outcomes, per-destination results, self-loop behavior, delivery finality, or duplicate-send semantics must defer to this contract.

## 1. Route Layer Preserves Adapter Delivery Semantics

The routing layer does not alter the delivery semantics of any transport. Each adapter's `deliver()` method returns an `AdapterDeliveryResult`, and the pipeline records what the adapter reported — honestly and without upgrade.

| Transport  | Adapter reports              | Routing layer records                           | Does routing upgrade?                    |
| ---------- | ---------------------------- | ----------------------------------------------- | ---------------------------------------- |
| Matrix     | `event_id` from homeserver   | `sent` or `confirmed` with `adapter_message_id` | No. The adapter's report is the truth.   |
| Meshtastic | Local node acceptance only   | `sent` without confirmation                     | No. Radio best-effort stays best-effort. |
| MeshCore   | Local node acceptance only   | `sent` without confirmation                     | No. Radio best-effort stays best-effort. |
| LXMF       | Local `LXMRouter` acceptance | `sent` without confirmation                     | No. Store-and-forward stays eventual.    |

The routing layer adds attribution (`route_id`, `source_adapter`, `dest_adapter`). It does not add, fabricate, or imply delivery confirmation that the transport did not provide.

## 2. `AdapterDeliveryResult` Fields and Routing Flow

### 2.1 `AdapterDeliveryResult` Structure

`AdapterDeliveryResult` is a frozen dataclass returned by adapters after delivery:

| Field                | Type                            | Description                                                                                                                                                     |
| -------------------- | ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `native_message_id`  | `str` or `None`                 | Platform-native message ID (e.g., Matrix `event_id`). `None` when the platform did not return one.                                                              |
| `native_channel_id`  | `str` or `None`                 | Platform-native channel / room / conversation ID.                                                                                                               |
| `native_thread_id`   | `str` or `None`                 | Platform-native thread or parent message ID.                                                                                                                    |
| `native_relation_id` | `str` or `None`                 | Platform-native ID of the related entity (e.g., the message being replied to).                                                                                  |
| `delivery_note`      | `str` or `None`                 | Optional human-readable context string explaining the delivery outcome (e.g., local-acceptance without platform ACK). Informational only; not for control-flow. |
| `metadata`           | `MappingProxyType[str, object]` | Adapter-specific immutable metadata about the delivery.                                                                                                         |

### 2.2 Flow Through Routing

```text
1. PipelineRunner.route_event(event)
   → matches routes, populates route_trace on event metadata
   → produces list of (Route, DeliveryPlan) pairs

2. PipelineRunner.execute_route_deliveries(event, route_targets)
   → for each (Route, DeliveryPlan):
     a. Check self-loop guard (section 3)
     b. Call PipelineRunner.deliver_to_target(event, route, plan)
        → adapter.deliver(event, plan)
        → adapter returns AdapterDeliveryResult
     c. Pipeline creates DeliveryReceipt with:
        - route_id from the route
        - adapter_message_id from AdapterDeliveryResult.native_message_id
        - status based on adapter result
     d. Pipeline creates DeliveryOutcome with:
        - route_id from the route
        - status: success / transient_failure / permanent_failure / skipped
        - receipt: the DeliveryReceipt
        - failure_kind: classified from the error
```

The `AdapterDeliveryResult` flows into `DeliveryReceipt.adapter_message_id` and informs the receipt `status`. The routing layer wraps this result with attribution but does not modify the adapter's reported outcome.

## 3. Self-Loop Guard Behavior

### 3.1 Definition

A self-loop occurs when a route would deliver an event back to the adapter that originated it: `event.source_adapter == target_adapter`.

### 3.2 Behavior

When the pipeline detects a self-loop during `execute_route_deliveries`:

1. The delivery is **skipped** — no adapter call is made.
2. A `DeliveryOutcome` with `status="skipped"` and `error="loop_prevented"` is produced.
3. `RouteStats.record_loop_prevented(route_id)` is called.
4. A warning is logged with the event ID, source adapter, and route ID.
5. No `DeliveryReceipt` is created (the `receipt` field on the outcome is `None`).

### 3.3 Scope

The self-loop guard operates at the individual delivery level. In a fan-out route with multiple targets, the guard is evaluated independently per target. A self-loop on one target does not prevent delivery to other targets.

### 3.4 Loop Prevention vs. Loop Detection

Two distinct mechanisms exist:

| Mechanism              | Layer                           | When                   | Effect                 |
| ---------------------- | ------------------------------- | ---------------------- | ---------------------- |
| Self-loop guard        | `PipelineRunner` (per-delivery) | Runtime, per delivery  | Skip + record          |
| `check_route_loops`    | `route_engine` (config-time)    | Startup, configuration | Log warning            |
| `_filter_replay_loops` | `replay` module                 | Replay                 | Skip + `loop_warnings` |

These are independent. The self-loop guard catches runtime self-loops. `check_route_loops` catches configuration-level cycles. `_filter_replay_loops` catches replay-specific loops.

## 4. Failed Delivery Attribution

Every failed delivery is attributed to a specific route and destination:

```text
DeliveryOutcome(
    event_id="evt_001",
    target_adapter="longfast",
    route_id="matrix_to_radio",
    delivery_plan_id="plan_abc",
    status="permanent_failure",
    failure_kind=DeliveryFailureKind.ADAPTER_PERMANENT,
    error="Node connection lost",
    ...
)
```

The `route_id` and `target_adapter` fields are always populated on failure outcomes. This ensures that failed deliveries are traceable to a specific route and destination, enabling per-route failure counting via `RouteStats`.

`RouteStats.record_failed(route_id, error)` is called for every failed delivery, accumulating per-route error counts and storing the latest error message.

## 5. Per-Destination Results

### 5.1 One-to-Many Independence

When a single event matches a route with multiple destinations, each destination produces an independent `DeliveryOutcome`:

```text
Route "hub": source=[bot1], dest=[radio_a, radio_b]

Outcome 1: target_adapter=radio_a, status=success
Outcome 2: target_adapter=radio_b, status=transient_failure
```

These outcomes are independent:

- A success on `radio_a` does not imply success on `radio_b`.
- A failure on `radio_b` does not prevent delivery to `radio_a`.
- Each outcome has its own `DeliveryReceipt` (if a receipt was produced).

### 5.2 Multi-Route Independence

When an event matches multiple routes, each route produces independent outcomes:

```text
Route "to_alpha": source=[bot1], dest=[radio_alpha]
Route "to_beta":  source=[bot1], dest=[radio_beta]

Outcome 1: route_id=to_alpha, target_adapter=radio_alpha, status=success
Outcome 2: route_id=to_beta,  target_adapter=radio_beta,  status=failed
```

Routes are evaluated and executed independently. A failure in one route does not affect other routes.

### 5.3 Result Ordering

`execute_route_deliveries` returns outcomes in the same order as the `route_targets` input. This preserves determinism: for the same event and configuration, the outcome list is always in the same order.

## 6. No False Delivery Finality

### 6.1 Principle

The runtime never claims delivery finality that the transport cannot confirm. A `sent` status means exactly what the adapter reported — no more, no less.

### 6.2 Status Semantics Are Transport-Relative

| Status      | Matrix meaning              | Radio meaning            | LXMF meaning             |
| ----------- | --------------------------- | ------------------------ | ------------------------ |
| `sent`      | Homeserver accepted         | Local node queued        | Local router accepted    |
| `confirmed` | Server-verified persistence | **Never reached**        | **Never reached**        |
| `failed`    | Adapter-reported failure    | Adapter-reported failure | Adapter-reported failure |

The routing layer does not change these semantics. A `sent` receipt for Meshtastic is recorded as `sent`, not upgraded to `confirmed`.

### 6.3 Receipt Honesty

Receipts are the audit trail. They must be trustworthy. The runtime:

- Records the adapter's reported status honestly.
- Never upgrades a receipt status retroactively.
- Records `attempt_number` and `parent_receipt_id` to form retry lineage.
- Records `route_id` on every receipt for attribution.

### 6.4 Explicit Non-Guarantees

The routing layer explicitly does **not** provide:

- End-to-end delivery confirmation for any transport.
- Exactly-once delivery semantics.
- Distributed loop prevention across MEDRE instances.
- Delivery confirmation beyond what the adapter reports.
- Adapter-local outbound queue durability. Items remaining in an adapter's in-memory outbound queue at process termination (graceful or ungraceful) are lost. The Meshtastic adapter's outbound queue is non-durable; durable queue persistence and crash-recovery are deferred to a future implementation. This is a documented non-guarantee — operators requiring delivery assurance must ensure the queue is drained before shutdown or accept the loss of in-flight items.
- Outbound gate suppression retry. When `outbound_mode = "listen_only"` is configured on a Meshtastic adapter, suppressed outbound deliveries are non-retryable. The routing layer records the failure honestly; retry is not attempted because the suppression is an intentional operator decision.

## 7. Duplicate-Send Risk for Radio and Async Transports

### 7.1 Radio Transports (Meshtastic, MeshCore)

Radio transports are probabilistic. Duplicate sends are normal operational reality:

- The runtime delivers what routes specify. It does not deduplicate at the radio layer.
- Retries after transient failures may produce duplicates if the first send succeeded but the response was lost.
- The runtime does not suppress duplicate sends — radio operators expect them.

### 7.2 Async Transports (Matrix)

- Duplicates are rare but possible when a send succeeds but the response is lost, triggering a retry.
- Matrix event IDs will differ for each attempt.
- The runtime does not suppress retries based on assumed success.

### 7.3 Store-and-Forward (LXMF)

- Duplicates are low-probability due to Reticulum's protocol-level handling.
- Late duplicates from slow propagation paths are possible.
- The runtime does not deduplicate at the protocol layer.

### 7.4 Bridge Fan-Out

When a single event routes to multiple targets (fan-out), each target gets an independent delivery. A duplicate on one target is independent of duplicates on another. The runtime records each delivery attempt independently.

### 7.5 Replay Re-Delivery

`BEST_EFFORT` replay produces new outbound messages on all matched targets. This is intentional duplication for re-delivery. Always verify with `DRY_RUN` or `RE_ROUTE` before running `BEST_EFFORT`.

## 8. Delivery Outcome Semantics

### 8.1 Per-Route, Per-Adapter Results

Each `DeliveryOutcome` is scoped to exactly one route and one adapter target. When a single event matches multiple routes (fan-out), or a single route targets multiple adapters (one-to-many), each (route, adapter) pair produces an independent outcome:

```text
Route "hub": source=[bot1], dest=[radio_a, radio_b]
Route "aux": source=[bot1], dest=[radio_c]

Outcomes:
  (hub, radio_a) -> DeliveryOutcome(status="success", ...)
  (hub, radio_b) -> DeliveryOutcome(status="transient_failure", ...)
  (aux, radio_c) -> DeliveryOutcome(status="success", ...)
```

No outcome is shared, aggregated, or coalesced.

### 8.2 Success/Failure/Skip Semantics

| Status              | Meaning                                                                           | Receipt created?   | Retryable?             |
| ------------------- | --------------------------------------------------------------------------------- | ------------------ | ---------------------- |
| `success`           | Adapter reported successful handoff. The transport accepted the message.          | Yes                | N/A                    |
| `transient_failure` | Adapter reported a recoverable error (timeout, connection reset).                 | Yes                | Yes, per `RetryPolicy` |
| `permanent_failure` | Adapter reported an unrecoverable error, or delivery exhausted retries.           | Yes (last attempt) | No                     |
| `skipped`           | Delivery was skipped before adapter invocation. Reason recorded in `error` field. | No                 | No                     |

`skipped` outcomes are produced by: self-loop guard, route-trace loop prevention, or policy filtering. No adapter call is made for skipped deliveries.

### 8.3 Route Attribution in Delivery Results

Every `DeliveryOutcome` carries `route_id` and `target_adapter` fields, regardless of status. This ensures that even failed and skipped deliveries are attributable to a specific routing decision.

```text
DeliveryOutcome(
    route_id="matrix_to_radio",
    target_adapter="longfast",
    status="skipped",
    error="loop_prevented: route already in route_trace",
    ...
)
```

### 8.4 Radio Transports Remain Probabilistic

The routing layer does not alter the probabilistic nature of radio transports. A `success` status for Meshtastic or MeshCore means the local node accepted the packet. No remote-node confirmation is implied. This is a transport-layer constraint, not a routing-layer limitation.

### 8.5 No Distributed Loop Prevention

Loop prevention (self-loop guard, route-trace check, `_filter_replay_loops`) operates within a single MEDRE process only. If two MEDRE instances bridge the same transports in opposite directions, neither detects the cross-instance loop. There is no shared loop-prevention state between instances.

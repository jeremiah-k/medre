# Runtime Delivery Contract

This document describes how MEDRE routes, delivers, tracks, and recovers events. It exists so developers and operators can understand the delivery path, failure classification, retry behavior, duplicate suppression, loop prevention, and replay semantics without reading every source file.

## Inbound Event Lifecycle

1. **Adapter receives native event** — adapter's sync loop or callback produces a `CanonicalEvent` via the codec.
2. **`AdapterContract.publish_inbound(event)`** — checks stale-event guard (`event.timestamp >= adapter._start_time`), then delegates to `ctx.publish_inbound(event)` which is wired to `PipelineRunner.handle_ingress`.
3. **`PipelineRunner.handle_ingress(event)`** — the central orchestrator (src/medre/core/engine/pipeline.py). Stages:
   - Validate event_id, event_kind, source_adapter
   - Duplicate native-ref check: if `event.source_native_ref` resolves to an already-stored event via `storage.resolve_native_ref()`, the event is suppressed
   - Resolve relations: map native refs in relations to canonical event IDs
   - Store event: `storage.append(event)`
   - Persist inbound native ref: `storage.store_native_ref(NativeMessageRef(direction="inbound"))`
   - Reaction-to-reaction suppression: reject reactions targeting other reactions
   - Route matching: `Router.match(event)` — matches on `source.adapter`, `event_kinds`, `channel`
   - Route expansion: `_expand_route_config()` produces one `Route` per source adapter per directionality
   - Delivery planning: per-target `DeliveryPlan` with `RetryPolicy` from config
   - Fanout: each matching route's targets are delivered concurrently via `asyncio.gather`

4. **`PipelineRunner.deliver_to_targets(event, deliveries)`** — per target:
   - Acquire capacity slot from `CapacityController`
   - Route-trace loop prevention: skip if route ID appears >1 in `route_trace`
   - Self-loop guard: skip if `target.adapter == event.source_adapter`
   - Enrich relations with target-native refs
   - Render event via `RenderingPipeline.render()`
   - Call `adapter.deliver(rendering_result)` → `AdapterDeliveryResult`
   - Record `DeliveryReceipt` (status="sent" or "failed")
   - On success: store `NativeMessageRef(direction="outbound")`
   - On retryable failure with retry policy: set `next_retry_at` on receipt
   - On retry exhaustion: append `dead_lettered` receipt

## Delivery Outcome Model

`DeliveryOutcome` (src/medre/core/planning/delivery_plan.py):

| Field            | Type                                                                          | Description                     |
| ---------------- | ----------------------------------------------------------------------------- | ------------------------------- |
| event_id         | str                                                                           | Canonical event being delivered |
| target_adapter   | str                                                                           | Target adapter name             |
| target_channel   | str or None                                                                   | Target channel on adapter       |
| route_id         | str                                                                           | Route that triggered delivery   |
| delivery_plan_id | str                                                                           | Plan this belongs to            |
| status           | Literal["success","queued","transient_failure","permanent_failure","skipped"] | Delivery status                 |
| failure_kind     | DeliveryFailureKind or None                                                   | Classification of failure       |
| receipt          | DeliveryReceipt or None                                                       | Persisted receipt               |
| error            | str or None                                                                   | Sanitized error message         |
| duration_ms      | float                                                                         | Wall-clock duration             |

## Delivery Receipt Model

`DeliveryReceipt` (src/medre/core/events/canonical.py):

| Field              | Type                                                                     | Description                    |
| ------------------ | ------------------------------------------------------------------------ | ------------------------------ |
| sequence           | int                                                                      | Autoincrement PK               |
| receipt_id         | str                                                                      | UUID ("rcpt-...")              |
| event_id           | str                                                                      | Canonical event ID             |
| delivery_plan_id   | str                                                                      | Plan identifier                |
| target_adapter     | str                                                                      | Target adapter name            |
| target_channel     | str or None                                                              | Target channel                 |
| route_id           | str                                                                      | Route that triggered delivery  |
| status             | Literal["accepted","queued","sent","confirmed","failed","dead_lettered"] | Delivery status                |
| error              | str or None                                                              | Sanitized error message        |
| failure_kind       | str or None                                                              | Failure classification         |
| adapter_message_id | str or None                                                              | Native message ID from adapter |
| next_retry_at      | datetime or None                                                         | Scheduled retry time           |
| attempt_number     | int                                                                      | 1-indexed attempt              |
| parent_receipt_id  | str or None                                                              | Previous receipt in lineage    |
| source             | Literal["live","retry","replay"]                                         | How this attempt was triggered |
| replay_run_id      | str or None                                                              | Populated when source="replay" |
| retry_max_attempts | int or None                                                              | From RetryPolicy               |
| retry_backoff_base | float or None                                                            | From RetryPolicy               |
| retry_max_delay    | float or None                                                            | From RetryPolicy               |
| retry_jitter       | bool or None                                                             | From RetryPolicy               |
| created_at         | datetime                                                                 | Timestamp                      |

## Delivery Failure Classification

`DeliveryFailureKind` enum (src/medre/core/planning/delivery_plan.py):

| Kind                 | Retryable | When                                           |
| -------------------- | --------- | ---------------------------------------------- |
| ADAPTER_TRANSIENT    | Yes       | Timeout, connection error, network unreachable |
| ADAPTER_PERMANENT    | No        | Malformed payload, business rejection          |
| ADAPTER_MISSING      | No        | Target adapter not registered                  |
| PLANNER_FAILURE      | No        | Router/planner misconfiguration                |
| RENDERER_FAILURE     | No        | No renderer registered for event kind          |
| TARGET_NOT_FOUND     | No        | Reserved — channel/address not found           |
| DEADLINE_EXCEEDED    | No        | Delivery plan deadline passed                  |
| CAPACITY_REJECTION   | No        | All in-flight slots occupied                   |
| SHUTDOWN_REJECTION   | No        | Pipeline shutting down                         |
| DUPLICATE_SUPPRESSED | No | Reserved — defined in the enum but not currently emitted as a receipt/outcome. |
| LOOP_SUPPRESSED      | No        | Route-trace or self-loop prevented             |

`ADAPTER_TRANSIENT` is the **only** retryable kind.

> **Note:** ``DUPLICATE_SUPPRESSED`` is defined in the ``DeliveryFailureKind``
> enum but is not currently emitted as a receipt or ``DeliveryOutcome``.
> Duplicate native-ref suppression happens before routing in ``handle_ingress``
> and returns ``[]`` (no outcomes, no receipts).  The suppression is recorded
> in ``RuntimeAccounting.loop_prevented``, not in persisted receipts or
> ``RouteStats``.

## Retry Mechanism

- **Opt-in**: `[retry] enabled = true` in config (default: false)
- **RetryWorker** (src/medre/runtime/retry.py): background asyncio task polling `list_due_retry_receipts` at configurable interval (default 10s)
- **Batch size**: configurable (default 20)
- **Max attempts**: configurable (default 3)
- **Backoff**: exponential `delay = backoff_base * 2^(attempt-1)`, capped at `max_delay_seconds`
- **Jitter**: optional deterministic SHA-256 based jitter in `[delay * 0.5, delay]` range
- **Receipt lineage**: each retry creates a new `DeliveryReceipt` with `parent_receipt_id` linking to the previous attempt, `attempt_number` incrementing, `source="retry"`
- **Dead-letter**: when all retry attempts exhausted, a `dead_lettered` receipt is appended — no further retries

## Duplicate Suppression

- **Native ref dedup**: during ``handle_ingress``, if ``event.source_native_ref``
  resolves to an already-stored event via ``storage.resolve_native_ref()``, the
  pipeline returns ``[]`` (no outcomes, no receipts).  The duplicate event is
  not persisted.  The suppression is recorded in ``RuntimeAccounting.loop_prevented``,
  not in persisted receipts or ``RouteStats``.
- **No event_id dedup**: the pipeline does NOT deduplicate by ``event_id`` —
  each ``handle_ingress`` call proceeds independently.

## Loop Prevention

Three mechanisms:

1. **Self-loop guard** (pipeline.py): if `target.adapter == event.source_adapter`, delivery is skipped. Records `loop_prevented` in RouteStats and RuntimeAccounting.
2. **Route-trace loop prevention** (pipeline.py): `RoutingMetadata.route_trace` maintains a rolling window of the last 16 route IDs. If a route ID appears >1 time, delivery is skipped.
3. **Native-ref loop detection**: when an outgoing delivery echo comes back as an inbound event, the native-ref dedup (above) catches and suppresses it.

## Replay

- **ReplayEngine** (src/medre/core/storage/replay.py): deterministic re-processing of historical events
- **5 modes**: STRICT (verify only), RE_RENDER (verify+render), RE_ROUTE (verify+route), BEST_EFFORT (full re-delivery), DRY_RUN (verify+route, skip deliver)
- **BEST_EFFORT** creates new receipts tagged `source="replay"` with `replay_run_id` — no existing receipts are mutated
- **Capacity-guarded**: replay acquires a slot from `CapacityController`

## Operator Evidence

- **Evidence bundle** (`collect_evidence_bundle`): includes config_summary, route_validation, diagnostics, storage, timeline
- **Storage section**: event details, receipt summary, native refs, incident classification, timeline
- **Trace** (`assemble_event_timeline`): chronological timeline combining event, receipts, native refs, and relations
- **Retry evidence**: receipts show `source="retry"`, `attempt_number`, `parent_receipt_id`, `next_retry_at`
- **Replay evidence**: receipts show `source="replay"`, `replay_run_id`
- **Suppression evidence**: `RouteStats` counters for `loop_prevented`

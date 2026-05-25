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
   - Route-policy evaluation: after route match, the route-policy evaluator checks allowlist fields (source adapter, dest adapter, sender, room, channel). A denial produces `failure_kind="policy_suppressed"` and skips delivery. Policy fields are config-file-only (not settable via env).
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

| Field              | Type                                                                                  | Description                    |
| ------------------ | ------------------------------------------------------------------------------------- | ------------------------------ |
| sequence           | int                                                                                   | Autoincrement PK               |
| receipt_id         | str                                                                                   | UUID ("rcpt-...")              |
| event_id           | str                                                                                   | Canonical event ID             |
| delivery_plan_id   | str                                                                                   | Plan identifier                |
| target_adapter     | str                                                                                   | Target adapter name            |
| target_channel     | str or None                                                                           | Target channel                 |
| route_id           | str                                                                                   | Route that triggered delivery  |
| status             | Literal["accepted","queued","sent","confirmed","suppressed","failed","dead_lettered"] | Delivery status                |
| error              | str or None                                                                           | Sanitized error message        |
| failure_kind       | str or None                                                                           | Failure classification         |
| adapter_message_id | str or None                                                                           | Native message ID from adapter |
| next_retry_at      | datetime or None                                                                      | Scheduled retry time           |
| attempt_number     | int                                                                                   | 1-indexed attempt              |
| parent_receipt_id  | str or None                                                                           | Previous receipt in lineage    |
| source             | Literal["live","retry","replay"]                                                      | How this attempt was triggered |
| replay_run_id      | str or None                                                                           | Populated when source="replay" |
| retry_max_attempts | int or None                                                                           | From RetryPolicy               |
| retry_backoff_base | float or None                                                                         | From RetryPolicy               |
| retry_max_delay    | float or None                                                                         | From RetryPolicy               |
| retry_jitter       | bool or None                                                                          | From RetryPolicy               |
| created_at         | datetime                                                                              | Timestamp                      |

## Delivery Failure Classification

`DeliveryFailureKind` enum (src/medre/core/planning/delivery_plan.py):

| Kind               | Retryable | When                                               |
| ------------------ | --------- | -------------------------------------------------- |
| ADAPTER_TRANSIENT  | Yes       | Timeout, connection error, network unreachable     |
| ADAPTER_PERMANENT  | No        | Malformed payload, business rejection              |
| ADAPTER_MISSING    | No        | Target adapter not registered                      |
| PLANNER_FAILURE    | No        | Router/planner misconfiguration                    |
| RENDERER_FAILURE   | No        | No renderer registered for event kind              |
| DEADLINE_EXCEEDED  | No        | Delivery plan deadline passed                      |
| CAPACITY_REJECTION | No        | All in-flight slots occupied                       |
| SHUTDOWN_REJECTION | No        | Pipeline shutting down                             |
| LOOP_SUPPRESSED    | No        | Route-trace or self-loop prevented                 |
| POLICY_SUPPRESSED  | No        | Route-policy denial (not in allowlist)             |

`ADAPTER_TRANSIENT` is the **only** retryable kind.

> **Note:** `TARGET_NOT_FOUND` and `DUPLICATE_SUPPRESSED` were removed from
> the enum. Channel-not-found and target-address failures map to
> `ADAPTER_PERMANENT`. Duplicate native-ref suppression occurs before event
> storage in `handle_ingress` and returns `[]` (no outcomes, no receipts).
> The suppression is recorded in `RuntimeAccounting.loop_prevented`, not in
> persisted receipts or `RouteStats`. No `DeliveryReceipt` is produced for
> duplicate suppression.

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

- **Native ref dedup**: during `handle_ingress`, if `event.source_native_ref`
  resolves to an already-stored event via `storage.resolve_native_ref()`, the
  pipeline returns `[]` (no outcomes, no receipts). The duplicate event is
  not persisted. The suppression is recorded in `RuntimeAccounting.loop_prevented`,
  not in persisted receipts or `RouteStats`.
- **No event_id dedup**: the pipeline does NOT deduplicate by `event_id` —
  each `handle_ingress` call proceeds independently.

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

## Unified Delivery Evidence

This section describes the unified operator-facing evidence surface that answers the question: _"Why did this event deliver, retry, suppress, fail, defer, drop, or dead-letter?"_

All evidence described here is **best-effort** and **local-process scoped**. It reflects what the local MEDRE process observed. It does not represent distributed consensus, end-to-end delivery confirmation, or transport-level acknowledgement from remote nodes.

### Evidence Scope and Limitations

1. **Best-effort.** Evidence is recorded on a best-effort basis. Process crashes, ungraceful shutdowns, or storage failures may cause evidence gaps. Absence of evidence is not evidence of absence.

2. **Local-process scoped.** All evidence (receipts, native refs, classifier counters, diagnostics) reflects the state of a single MEDRE process. There is no cross-instance coordination or shared evidence store.

3. **No exactly-once delivery.** MEDRE does not provide exactly-once delivery semantics on any transport. Matrix is at-least-once. Meshtastic is probabilistic. LXMF is at-least-once with eventual delivery. Duplicate suppression on inbound native refs reduces duplicates but does not eliminate them under all conditions.

4. **Not production-ready.** The evidence surface is under active development. Field names, shapes, and availability may change without notice.

### Delivery Explanation Shape

The `inspect` and `evidence` commands expose a delivery explanation/summary shape for a given event. When available, the JSON output includes these fields:

| Field                | Type            | Description                                                                                                                                                                                                                                                                                                       |
| -------------------- | --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `event_id`           | string          | Canonical event ID                                                                                                                                                                                                                                                                                                |
| `event_kind`         | string          | Event kind (e.g., `message.created`)                                                                                                                                                                                                                                                                              |
| `source_adapter`     | string          | Adapter that produced the inbound event                                                                                                                                                                                                                                                                           |
| `route_id`           | string or null  | Route that triggered this delivery                                                                                                                                                                                                                                                                                |
| `target_adapter`     | string or null  | Target adapter for this delivery                                                                                                                                                                                                                                                                                  |
| `target_channel`     | string or null  | Target channel on the destination adapter                                                                                                                                                                                                                                                                         |
| `status`             | string          | Final delivery status: `sent`, `confirmed`, `suppressed`, `failed`, `dead_lettered`, `queued`, `accepted`. The `suppressed` status covers loop-suppressed and policy-suppressed rejection receipts persisted where event/target context exists. `CAPACITY_REJECTION` and `SHUTDOWN_REJECTION` do not produce persisted receipts (Contract 57 §Receipt non-persistence). Duplicate suppression produces no receipt. |
| `failure_kind`       | string or null  | Classification of failure (see Delivery Failure Classification above)                                                                                                                                                                                                                                             |
| `retryable`          | boolean         | Whether the failure kind is retryable (only `ADAPTER_TRANSIENT`)                                                                                                                                                                                                                                                  |
| `attempt_number`     | integer         | 1-indexed attempt count                                                                                                                                                                                                                                                                                           |
| `retry_max_attempts` | integer or null | Maximum retry attempts from RetryPolicy                                                                                                                                                                                                                                                                           |
| `retry_backoff_base` | float or null   | Backoff base from RetryPolicy                                                                                                                                                                                                                                                                                     |
| `retry_max_delay`    | float or null   | Max delay cap from RetryPolicy                                                                                                                                                                                                                                                                                    |
| `retry_jitter`       | boolean or null | Whether jitter is enabled                                                                                                                                                                                                                                                                                         |
| `next_retry_at`      | string or null  | ISO 8601 timestamp for next scheduled retry                                                                                                                                                                                                                                                                       |
| `adapter_message_id` | string or null  | Native message ID from the target adapter (e.g., Matrix event ID, Meshtastic packet ID)                                                                                                                                                                                                                           |
| `error`              | string or null  | Sanitized error message                                                                                                                                                                                                                                                                                           |
| `source`             | string          | How this attempt was triggered: `live`, `retry`, or `replay`                                                                                                                                                                                                                                                      |
| `replay_run_id`      | string or null  | Populated when `source="replay"`                                                                                                                                                                                                                                                                                  |
| `parent_receipt_id`  | string or null  | Previous receipt in retry lineage                                                                                                                                                                                                                                                                                 |
| `receipt_id`         | string          | Unique receipt identifier (`rcpt-...`)                                                                                                                                                                                                                                                                            |
| `created_at`         | string          | ISO 8601 timestamp of receipt creation                                                                                                                                                                                                                                                                            |

### Per-Adapter Delivery State

Each adapter contributes adapter-specific metadata to delivery evidence:

#### Matrix

- **`matrix_txn_id`**: Deterministic transaction ID computed from `event_id`, `target_adapter`, `target_channel`, and `room_id`. Passed as `tx_id` to the Matrix homeserver. The homeserver uses `tx_id` to deduplicate retried sends — if the same `tx_id` is sent twice, the homeserver returns the original event ID instead of creating a duplicate event. This **reduces duplicate retries** but does **not** provide exactly-once delivery: the homeserver may have already processed and lost the first attempt, or the `tx_id` window may have expired.
- **`undecryptable_event_count`**: Count of inbound MegolmEvents that could not be decrypted. Incremented when E2EE is enabled and crypto keys are unavailable. A non-zero count indicates Matrix E2EE is blocked for those events — the events were received but their content is inaccessible.
- **`delivery_attempts` / `delivery_successes` / `delivery_failures`**: Cumulative outbound delivery counters.

#### Meshtastic

- **Queue state**: `queue_total_enqueued`, `queue_total_sent`, `queue_total_failed`, `queue_total_rejected`, `queue_total_requeued`, `queue_total_exhausted`, `queue_total_permanent_failed`, `queue_send_max_attempts`, `queue_pending`, `queue_max_size`. Being **queued** or **enqueued** means accepted into the adapter-local outbound queue only — this is not RF transmission. Being **sent** means the SDK/client send returned a success result and native packet ID — this is local send confirmation only, not RF delivery or remote receipt. Transient SDK send failures are retried from the adapter-local queue up to `queue_send_max_attempts`; permanent failures and exhausted retries are dropped. Retry is best-effort, adapter-local, in-memory, non-durable across process restart, and not exactly-once.
- **Outbound gate suppression**: When `outbound_mode = "listen_only"` is configured, `deliver()` rejects outbound payloads as non-retryable adapter failures with detail `outbound suppressed: listen_only mode`. Suppressed deliveries are recorded as `failure_kind="adapter_permanent"` with `failure_kind_detail="meshtastic_outbound_suppressed"`. This is an intentional operator-configured gate, not a transport failure. Inbound reception and inbound diagnostics are unaffected. The suppression is non-retryable — the pipeline will not attempt re-delivery because the operator has explicitly disabled outbound transmission.
- **Shutdown queue abandonment**: Items remaining in the Meshtastic adapter-local outbound queue at process termination are lost. The queue is in-memory and non-durable. Durable queue persistence and crash-recovery are deferred to a future implementation. Delivery receipts already written to SQLite survive shutdown, but in-flight queue items do not.
- **Classifier aggregate counters**: `classifier_packets_seen`, `classifier_packets_relayed`, `classifier_packets_ignored`, `classifier_packets_dropped`, `classifier_packets_deferred`, plus reason-level sub-counters (`classifier_packets_malformed`, `classifier_packets_encrypted_dropped`, `classifier_packets_detection_sensor_deferred`, `classifier_packets_dm_ignored`, `classifier_packets_empty_text_ignored`, `classifier_packets_unknown_portnum_deferred`). These are **aggregate inbound classification counters** that explain how many packets the classifier saw and what it did with them. They do **not** mean live validation — they count decisions made by the pure-function classifier against each inbound packet. They do **not** persist a record of every individual ignored, dropped, or deferred packet; only the aggregate totals are maintained in memory and exposed via `diagnostics()`.

### Suppression Evidence

- **Native-ref dedup**: When `event.source_native_ref` resolves to an already-stored event, the pipeline suppresses the duplicate and returns `[]` (no outcomes, no receipts). The suppression is recorded in `RuntimeAccounting.loop_prevented`, not in persisted receipts or `RouteStats`.
- **`LOOP_SUPPRESSED`**: Recorded when route-trace or self-loop prevention fires. Visible in `RouteStats.loop_prevented` and in the delivery outcome. The pipeline persists a `status="suppressed"` receipt for loop suppression where event/target context exists. `CAPACITY_REJECTION` and `SHUTDOWN_REJECTION` do not produce persisted `DeliveryReceipt` — the rejection occurs at the capacity gate before any adapter interaction. Durable evidence is recorded via `RuntimeAccounting` counters and `RouteStats` (see Contract 57 §Receipt non-persistence).
- **`POLICY_SUPPRESSED`**: Recorded when the route-policy evaluator denies a delivery after route matching. Visible in `RouteStats.policy_suppressed` and `RuntimeAccounting.policy_suppressed`. The pipeline persists a `status="suppressed"` receipt with `failure_kind="policy_suppressed"` and the policy denial reason code. Policy denials are permanent and not retryable. Policy fields are config-file-only.

### Derived Enrichment Fields

The `failure_kind_detail` field is derived from error patterns and provides a more specific classification than `failure_kind` without changing the `DeliveryFailureKind` enum. Current values:

| `failure_kind_detail`            | Condition                                                                                            |
| -------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `e2ee_blocked`                   | Matrix encrypted/E2EE decryption or blocking errors                                                  |
| `meshtastic_queue_rejected`      | Meshtastic adapter queue-full errors (requires "queue" + "full" or "enqueue rejected" in error text) |
| `meshtastic_outbound_suppressed` | Meshtastic adapter outbound gate suppression when `outbound_mode = "listen_only"`                    |
| (original `failure_kind`)        | Default — no specialised pattern matched                                                             |

The `delivery_state_by_target` dict in the incident summary provides per-target delivery state. The dict is keyed by composite JSON strings, not adapter names. Shape:

```text
{
  "{\"delivery_plan_id\":\"dp-001\",\"route_id\":\"route-a\",\"target_adapter\":\"radio\",\"target_channel\":\"ch-0\"}": {
    "target_adapter": str,
    "target_channel": str | None,
    "route_id": str,
    "delivery_plan_id": str,
    "status": str | None,
    "attempt_number": int | None,
    "failure_kind": str | None,
    "failure_kind_detail": str | None,
    "retryable": bool,
    "next_retry_at": str | None,  (ISO 8601)
    "native_message_id": str | None,
    "adapter_message_id": str | None
  }
}
```

Each key is a JSON string containing `delivery_plan_id`, `route_id`, `target_adapter`, and `target_channel` (sorted keys via `json.dumps(..., sort_keys=True)`). Each value selects the receipt with the highest `attempt_number` for that composite key, then the latest receipt sequence within that attempt, and carries decomposed fields from that receipt. Same adapter on multiple channels or same channel via different routes produces separate entries, so no target is hidden by adapter-level collapse.

> **Warning:** This is a per-target summary, not a full receipt list. Only the highest-attempt receipt per composite key is represented. When multiple receipts share the same `attempt_number`, the one with the latest sequence (append order) wins.

### Incident Summary

The evidence bundle's storage section includes an `incident_summary` for scoped events with fields:

| Field                       | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `event_id`                  | The canonical event ID                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `event_kind`                | Event kind                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `source_adapter`            | Source adapter                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `first_failure_kind`        | Best-effort inferred failure kind from error patterns                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `classification`            | One of: `success`, `retryable`, `permanent`, `operational`, `unknown`                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `replay_receipts_present`   | Whether any replay-sourced receipts exist                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `native_refs_present`       | Whether native transport references exist                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `receipt_count`             | Total number of delivery receipts                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `failed_count`              | Count of `failed` or `dead_lettered` receipts                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `sent_count`                | Count of `sent` receipts                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `dead_lettered_count`       | Count of `dead_lettered` receipts                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `suppressed_count`          | Count of receipts with `status="suppressed"` (covers loop_suppressed, policy_suppressed, capacity_rejection, shutdown_rejection)                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `sent_unconfirmed_count`    | Count of `sent` receipts (not yet confirmed by transport)                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `delivery_state_by_target`  | Per-target delivery state dict. Keys are composite JSON strings containing `delivery_plan_id`, `route_id`, `target_adapter`, `target_channel` (sorted keys). Each value includes decomposed fields: `target_adapter`, `target_channel`, `route_id`, `delivery_plan_id`, `status`, `attempt_number`, `native_message_id`, `adapter_message_id`, `failure_kind`, `failure_kind_detail`, `retryable`, `next_retry_at`. Each entry selects the receipt with the highest `attempt_number` for that composite key. Same adapter on multiple channels or same channel via different routes produces separate entries. |
| `recommended_commands`      | Suggested CLI commands for investigation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `commands`                  | Structured command list (primary + specialized)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |

The `classification` field is derived from `infer_failure_kind()` which reconstructs a best-effort failure kind from error message patterns. It is not authoritative — it is a heuristic.

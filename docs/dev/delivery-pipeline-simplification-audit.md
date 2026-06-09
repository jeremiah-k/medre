# Delivery Pipeline Simplification Audit

> Audits the delivery pipeline's control flow, data structures, authority boundaries, and complexity hotspots to identify simplification opportunities.
> Source: code inspection of `src/medre/core/engine/pipeline/runner.py`, `src/medre/core/engine/pipeline/target_delivery.py`, `src/medre/core/engine/pipeline/delivery_lifecycle.py`, `src/medre/core/engine/pipeline/delivery_state.py`, `src/medre/core/planning/delivery_plan.py`, `src/medre/core/events/canonical.py`, `src/medre/core/rendering/evidence.py`, `src/medre/core/contracts/adapter.py`, `src/medre/runtime/retry.py`, and `src/medre/core/engine/replay/`.
> This audit describes the delivery-pipeline structure and should be updated when pipeline ownership, receipt construction, or retry reconstruction changes.

## 1. End-to-End Delivery Map

A new contributor should be able to explain the full path from an inbound event to persisted evidence after reading this section alone.

The pipeline has six logical stages: **event** (adapter publishes a `CanonicalEvent`), **planning** (route matching produces one `DeliveryPlan` per target), **execution** (`PipelineRunner` orchestrates checks, capacity, and outbox lifecycle), **adapter** (`TargetDeliveryService` renders content and calls the transport adapter), **receipt** (a `DeliveryReceipt` is constructed and persisted for every attempt), and **evidence** (rendering diagnostics, `native_message_refs` and `delivery_outbox` state are persisted for audit and retry recovery).

### The path in one page

```text
Adapter publishes CanonicalEvent
  │
  ▼
PipelineRunner.handle_ingress()          ← entry point
  │
  ├─ INGRESS: validate event_id, event_kind, source_adapter
  ├─ DEDUP: resolve native ref; suppress if already processed
  ├─ RESOLVE_RELATIONS: native refs → canonical event IDs
  │   └─ assign conversation_id / root_event_id
  ├─ STORE: persist event + inbound native ref
  │   └─ suppress reaction-to-reaction
  ├─ ROUTE: match routes → create DeliveryPlan per target
  │   └─ FallbackResolver + CapabilityDecisionResolver
  └─ DELIVER: per-target execution (see sub-phases below)
      │
      ├─ PipelineRunner._deliver_single_target()   ← orchestrates checks + outbox
      │   ├─ loop / policy / capability / skip checks
      │   ├─ capacity acquisition
      │   ├─ outbox creation + lease renewal
      │   └─ inflight tracking
      │
      ├─ PipelineRunner.deliver_to_target()  ← enriches relations
      │   └─ RelationEnricher adds target-adapter native refs
      │
      ├─ TargetDeliveryService.deliver_to_target()  ← executes one delivery
      │   ├─ RenderingPipeline.render() → RenderingResult
      │   │   └─ attaches RenderingEvidence
      │   ├─ AdapterContract.deliver(rendering_result)
      │   │   └─ returns AdapterDeliveryResult
      │   ├─ DeliveryReceipt construction + persist
      │   ├─ NativeMessageRef (outbound) persist on success
      │   └─ dead-letter receipt if retries exhausted
      │
      └─ PipelineRunner._finalize_outbox_outcome()
          └─ DeliveryLifecycleService updates outbox status
```

**Key data identities that flow through the pipeline:**

| Identity            | Created by                                                                                                                                   | Carried by                                                                         | Persisted in                           |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- | -------------------------------------- |
| `event_id`          | Adapter at ingress                                                                                                                           | `CanonicalEvent`, all downstream structures                                        | `canonical_events` table               |
| `delivery_plan_id`  | `stable_delivery_plan_id()` (defined in `src/medre/core/planning/delivery_plan.py`, called by `FallbackResolver`)                            | `DeliveryPlan`, `DeliveryReceipt`, `DeliveryOutboxItem`, `OutboundNativeRefRecord` | `delivery_receipts`, `delivery_outbox` |
| `receipt_id`        | Created at receipt construction site by `TargetDeliveryService`, `DeliveryLifecycleService`, or `RetryExecutor` via `f"rcpt-{uuid.uuid4()}"` | `DeliveryReceipt`, `DeliveryOutcome`                                               | `delivery_receipts`                    |
| `outbox_id`         | `_create_outbox_for_delivery` (UUID)                                                                                                         | `DeliveryOutboxItem`                                                               | `delivery_outbox`                      |
| `native_message_id` | External platform (returned by adapter)                                                                                                      | `AdapterDeliveryResult`, `DeliveryReceipt`, `NativeMessageRef`                     | `native_message_refs`                  |

**Three delivery sources:** `"live"` (normal ingress), `"retry"` (RetryWorker reclaiming due `delivery_outbox` items), `"replay"` (replay engine re-delivering historical events). Each source stamps its `delivery_receipts` and `delivery_outbox` rows for downstream correlation.

## 2. DeliveryPlan Data Flow

### Identity fields across data structures

The delivery pipeline propagates a set of identity and context fields through multiple data structures. The table below documents which fields appear where and why.

| Field                          |      DeliveryPlan      | RenderingContext | RenderingEvidence |       DeliveryReceipt       | AdapterDeliveryResult | DeliveryOutboxItem |
| ------------------------------ | :--------------------: | :--------------: | :---------------: | :-------------------------: | :-------------------: | :----------------: |
| `plan_id` / `delivery_plan_id` |        ✓ source        |        —         |         —         |              ✓              |           —           |         ✓          |
| `event_id`                     |           ✓            |        —         |         —         |              ✓              |           —           |         ✓          |
| `target_adapter`               |      via `target`      |        ✓         |         ✓         |              ✓              |           —           |         ✓          |
| `target_channel`               |      via `target`      |        ✓         |         ✓         |              ✓              |           —           |         ✓          |
| `target_platform`              |           —            |        ✓         |         ✓         |              —              |           —           |         —          |
| `route_id`                     |           ✓            |        —         |         —         |              ✓              |           —           |         ✓          |
| `capability_level`             |           ✓            |        ✓         |         ✓         |              —              |           —           |         —          |
| `capability_field`             |           ✓            |        —         |         —         |              —              |           —           |         —          |
| `capability_reason`            |           ✓            |        —         |         —         |              —              |           —           |         —          |
| `delivery_strategy`            | via `primary_strategy` |        ✓         |         ✓         |              —              |           —           |         —          |
| `native_message_id`            |           —            |        —         |         —         | ✓ (as `adapter_message_id`) |       ✓ source        |         —          |
| `receipt_id`                   |           —            |        —         |         —         |          ✓ source           |           —           |         ✓          |
| `attempt_number`               |           —            |        —         |         —         |              ✓              |           —           |         ✓          |
| `parent_receipt_id`            |           —            |        —         |         —         |              ✓              |           —           |         —          |
| `source`                       |           —            |        —         |         —         |              ✓              |           —           |         —          |
| `replay_run_id`                |           —            |        —         |         —         |              ✓              |           —           |         —          |
| `retry_max_attempts`           |   via `retry_policy`   |        —         |         —         |              ✓              |           —           |         —          |
| `retry_backoff_base`           |   via `retry_policy`   |        —         |         —         |              ✓              |           —           |         —          |
| `retry_max_delay`              |   via `retry_policy`   |        —         |         —         |              ✓              |           —           |         —          |
| `retry_jitter`                 |   via `retry_policy`   |        —         |         —         |              ✓              |           —           |         —          |
| `rendering_evidence`           |           —            |        —         |     ✓ source      |       ✓ (serialized)        |           —           |         —          |
| `failure_kind`                 |           —            |        —         |         —         |              ✓              |           —           |         —          |

### Denormalization rationale

Repeated identity fields across structures are **intentional evidence denormalization**, not accidental duplication:

1. **Retry policy fields on receipts** (`retry_max_attempts`, `retry_backoff_base`, `retry_max_delay`, `retry_jitter`): Retry plan reconstruction (`RetryWorker._retry_outbox_item`) reads these from the previous receipt because the original `DeliveryPlan` is not persisted. Without these fields on receipts, retry reconstruction could not recover the original backoff parameters after a process restart.

2. **Target identity across receipt + outbox + evidence**: Each structure serves a different query pattern — receipts for audit, outbox for operational work state, evidence for rendering diagnostics. Independent query surfaces require independent identity fields.

3. **Capability metadata on plan only**: `capability_level`, `capability_field`, `capability_reason` live on `DeliveryPlan` and are consumed at specific decision points (Phase 2.5 capability check, rendering context). They are not propagated to receipts. On successful deliveries, capability context is captured in `RenderingEvidence`; however, failed deliveries carry neither `RenderingEvidence` nor a `failure_kind` that encodes the original capability decision, so the capability metadata is lost entirely on the failure path. This creates the retry parity gap documented in Section 7.

## 3. PipelineRunner Phase Map

### Six PipelinePhase enum values

Defined in `src/medre/core/engine/phases.py`:

| Phase               | Value                 | Where set in `handle_ingress` | Purpose                                 |
| ------------------- | --------------------- | ----------------------------- | --------------------------------------- |
| `INGRESS`           | `"ingress"`           | Line ~537                     | Event received; validation running      |
| `DEDUP`             | `"dedup"`             | Line ~544                     | Duplicate native-ref detection          |
| `RESOLVE_RELATIONS` | `"resolve_relations"` | Line ~585                     | Cross-adapter relation resolution       |
| `STORE`             | `"store"`             | Line ~597                     | Persist event + inbound native ref      |
| `ROUTE`             | `"route"`             | Line ~615                     | Route matching + delivery plan creation |
| `DELIVER`           | `"deliver"`           | Line ~650                     | Per-target adapter delivery             |

These phases are diagnostic instrumentation only — they do not drive pipeline behavior. `phase_snapshot()` returns the current phase and per-phase invocation counts for observability.

### DELIVER sub-phases inside `_deliver_single_target`

The `DELIVER` phase contains the richest internal structure. The parent method `_deliver_to_targets_fan_out` (runner.py ~1145–1776) fans out to individual targets via the nested `_deliver_single_target` closure, which executes the following sub-phases sequentially per target:

| Sub-phase                                       | Lines      | State mutation? | Description                                                                                                                                                                                                                                                                                                          |
| ----------------------------------------------- | ---------- | :-------------: | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Phase 1: Loop checks**                        | ~1152–1239 |       No        | Route-trace loop detection (suppress when route ID appears 2+ times in `route_trace`); self-loop guard (adapter == source_adapter). Produces `LOOP_SUPPRESSED` outcome.                                                                                                                                              |
| **Phase 2: Route policy**                       | ~1241–1305 |       No        | `evaluate_route_policy()` checks allowlists on the route. Produces `POLICY_SUPPRESSED` outcome. Runs before capacity to avoid consuming slots.                                                                                                                                                                       |
| **Phase 2.5: Capability check**                 | ~1307–1372 |       No        | Reads `plan.capability_level == "unsupported"` set during planning. Produces `CAPABILITY_SUPPRESSED` outcome. Only applied for registered adapters (missing adapters fall through to `ADAPTER_MISSING`).                                                                                                             |
| **Phase 2.75: Plan-level skip**                 | ~1374–1441 |       No        | Checks `plan.primary_strategy.method == "skip"`. Canonical skip path; defense-in-depth skip also exists in `TargetDeliveryService`. Produces `CAPABILITY_SUPPRESSED` outcome.                                                                                                                                        |
| **Phase 3: Capacity acquisition**               | ~1443–1488 |       Yes       | Acquires slot from `CapacityController`. Classifies as `CAPACITY_REJECTION` or `SHUTDOWN_REJECTION` on failure.                                                                                                                                                                                                      |
| **Phase 3.5: Outbox creation**                  | ~1490–1525 |       Yes       | Creates `DeliveryOutboxItem` with status `"in_progress"`. Computes attempt number (replay: max(existing) + 1). Ownership check against terminal/active rows; failure produces `OUTBOX_NOT_OWNED` `DeliveryOutcome` with no receipt.                                                                                  |
| **Phase 3.75: Lease renewal**                   | ~1527–1535 |       Yes       | Starts background `asyncio.Task` that renews outbox lease every 30 seconds (TTL 60 seconds). Cancelled in finally block.                                                                                                                                                                                             |
| **Phase 4: Inflight + delivery + finalization** | ~1537–1772 |       Yes       | Registers `InflightDelivery` for shutdown evidence. Calls `deliver_to_target()` → `TargetDeliveryService`. Handles `_AdapterDeliveryError`, `_RendererDeliveryError`, `CancelledError`, and unexpected exceptions. Finally block: cancel lease renewal, finalize outbox outcome, release capacity, untrack inflight. |

**Key ordering constraint:** Loop checks → route policy → capability check → plan skip → capacity → outbox. This ensures policy-denied and capability-unsupported targets never consume capacity or create `delivery_outbox` rows.

**Sub-phase numbering convention:** The fractional labels (2.5, 2.75, 3.5, 3.75) are code-comment labels used for intra-phase reference. They are not `PipelinePhase` enum values — the `PipelinePhase` enum defines only the six top-level phases listed above.

## 4. TargetDeliveryService Audit

### Ownership boundary

`TargetDeliveryService` (target_delivery.py) owns single-target delivery execution:

| Responsibility                                                          | Owned by TargetDeliveryService |
| ----------------------------------------------------------------------- | :----------------------------: |
| Rendering invocation                                                    |               ✓                |
| Adapter lookup / invocation                                             |               ✓                |
| Adapter response normalization                                          |               ✓                |
| Rendering / adapter failure normalization                               |               ✓                |
| Primary single-attempt receipt construction                             |               ✓                |
| Rendering evidence attachment to receipt                                |               ✓                |
| `adapter_message_id` extraction                                         |               ✓                |
| Receipt status determination (`sent` / `queued` / `failed`)             |               ✓                |
| Native-ref fact handling (outbound `NativeMessageRef` on success)       |               ✓                |
| Error normalization (`_AdapterDeliveryError`, `_RendererDeliveryError`) |               ✓                |
| Delivery strategy validation                                            |               ✓                |
| Capability level validation                                             |               ✓                |

| Responsibility                                          |        NOT owned (owner in parentheses)         |
| ------------------------------------------------------- | :---------------------------------------------: |
| Outbox creation                                         | PipelineRunner (`_create_outbox_for_delivery`)  |
| Capacity acquisition / release                          |    PipelineRunner (`_deliver_single_target`)    |
| Lease renewal                                           | PipelineRunner (`_start_outbox_lease_renewal`)  |
| Retry scheduling                                        |            DeliveryLifecycleService             |
| Route planning                                          |        PipelineRunner + FallbackResolver        |
| Relation enrichment                                     | PipelineRunner (`_enrich_relations_for_target`) |
| Lifecycle authority (retry/dead-letter/attempt context) |            DeliveryLifecycleService             |
| Replay processing                                       |                  ReplayEngine                   |

### Collaboration pattern

```text
PipelineRunner._deliver_single_target()
  → PipelineRunner.deliver_to_target()          # enriches relations
    → TargetDeliveryService.deliver_to_target()  # renders + delivers
      → RenderingPipeline.render()               # produces RenderingResult
      → adapter.deliver(rendering_result)         # returns AdapterDeliveryResult
      → DeliveryLifecycleService.compute_attempt_context()
      → DeliveryLifecycleService.extract_retry_fields(plan)
      → DeliveryLifecycleService.classify_failure()
      → DeliveryLifecycleService.compute_next_retry_at()
      → DeliveryLifecycleService.should_dead_letter()
      → DeliveryLifecycleService.build_and_persist_dead_letter_receipt()
      → storage.append_receipt()
      → storage.store_native_ref()
```

The `TargetDeliveryService` receives lifecycle-computed values (attempt context, retry fields, next_retry_at) from `DeliveryLifecycleService` rather than computing them internally. This centralizes lifecycle logic in one authority.

## 5. Receipt Generation Audit

### Receipt input classification

Every `DeliveryReceipt` construction requires these inputs, classified by role:

| Input                | Classification | Source                                             |        Derivable from other inputs?        |
| -------------------- | -------------- | -------------------------------------------------- | :----------------------------------------: |
| `receipt_id`         | Required       | UUID generation                                    |                     No                     |
| `event_id`           | Required       | `CanonicalEvent.event_id`                          |                     No                     |
| `delivery_plan_id`   | Required       | `DeliveryPlan.plan_id`                             |                     No                     |
| `target_adapter`     | Required       | `DeliveryPlan.target.adapter`                      |                     No                     |
| `target_channel`     | Required       | `DeliveryPlan.target.channel`                      |                     No                     |
| `route_id`           | Required       | `Route.id`                                         |                     No                     |
| `status`             | Required       | Adapter result / error path                        |                     No                     |
| `attempt_number`     | Required       | `DeliveryLifecycleService.compute_attempt_context` | From `previous_receipt.attempt_number + 1` |
| `parent_receipt_id`  | Required       | `DeliveryLifecycleService.compute_attempt_context` |     From `previous_receipt.receipt_id`     |
| `source`             | Required       | Caller (`"live"` / `"retry"` / `"replay"`)         |                     No                     |
| `replay_run_id`      | Required       | Caller (null for non-replay)                       |                     No                     |
| `created_at`         | Required       | `datetime.now(timezone.utc)`                       |                     No                     |
| `sequence`           | Diagnostic     | Always `0`                                         |        Always zero; not meaningful         |
| `error`              | Optional       | Exception message                                  |                     No                     |
| `failure_kind`       | Optional       | `DeliveryLifecycleService.classify_failure`        |       From exception type + context        |
| `adapter_message_id` | Optional       | `AdapterDeliveryResult.native_message_id`          |                     No                     |
| `next_retry_at`      | Optional       | `DeliveryLifecycleService.compute_next_retry_at`   |     From retry policy + attempt number     |
| `retry_max_attempts` | Derivable      | `plan.retry_policy.max_attempts`                   |            From `DeliveryPlan`             |
| `retry_backoff_base` | Derivable      | `plan.retry_policy.backoff_base`                   |            From `DeliveryPlan`             |
| `retry_max_delay`    | Derivable      | `plan.retry_policy.max_delay_seconds`              |            From `DeliveryPlan`             |
| `retry_jitter`       | Derivable      | `plan.retry_policy.jitter`                         |            From `DeliveryPlan`             |
| `rendering_evidence` | Optional       | `RenderingResult.rendering_evidence`               |           No (rendering output)            |

### Repeated DeliveryReceipt construction

`DeliveryReceipt` is constructed in these locations:

| Location                            | File                  | Lines    | Context                                      | Uses `build_delivery_receipt` |
| ----------------------------------- | --------------------- | -------- | -------------------------------------------- | :---------------------------: |
| Adapter-missing receipt             | target_delivery.py    | ~370–391 | Adapter not in registry                      |               ✓               |
| Deadline-exceeded receipt           | target_delivery.py    | ~403–421 | Plan deadline passed                         |               ✓               |
| Invalid capability level receipt    | target_delivery.py    | ~469–487 | Unexpected capability_level                  |               ✓               |
| Strategy skip receipt               | target_delivery.py    | ~514–534 | Plan strategy is `"skip"` (defense-in-depth) |               ✓               |
| Invalid strategy receipt            | target_delivery.py    | ~552–577 | Unknown delivery strategy method             |               ✓               |
| Rendering failure receipt           | target_delivery.py    | ~596–621 | RenderingPipeline threw                      |               ✓               |
| No deliver() method receipt         | target_delivery.py    | ~639–658 | Adapter has no deliver method                |               ✓               |
| Primary delivery receipt            | target_delivery.py    | ~776–797 | Main success/failure receipt                 |               ✓               |
| Dead-letter receipt                 | delivery_lifecycle.py | ~382–394 | Retry exhausted (delegates to RetryExecutor) |               —               |
| Suppression receipt                 | delivery_lifecycle.py | ~447–466 | Loop/policy/capability/capacity suppressed   |               ✓               |
| Retry receipt                       | delivery_plan.py      | ~469–494 | RetryExecutor builds retry receipt           |               —               |
| Dead-letter receipt (RetryExecutor) | delivery_plan.py      | ~540–559 | RetryExecutor builds dead-letter receipt     |               —               |
| Supplemental sent receipt           | delivery_lifecycle.py | ~749–772 | Queue-based adapter callback                 |               ✓               |

**Observation:** Receipt construction previously followed a repeated pattern (identity fields + status + error + retry fields from lifecycle), with ~15 keyword arguments duplicated at each site. The implementation pass extracted a construction-only helper, `build_delivery_receipt` (in `receipt_factory.py`), which assembles a `DeliveryReceipt` from explicit caller-supplied fields without performing lifecycle decisions or persistence. All 8 construction sites in `TargetDeliveryService` and 2 in `DeliveryLifecycleService` (suppression + supplemental) route through this helper. `RetryExecutor` receipts (retry + dead-letter in `delivery_plan.py`) and the `DeliveryLifecycleService` dead-letter receipt (which delegates to `RetryExecutor`) remain separate because their construction is owned by the retry executor's internal logic.

## 6. Evidence Derivation Audit

### Persisted vs derived evidence

| Evidence type                    |     Persisted in storage      |      Derived at query time      | Notes                                                                        |
| -------------------------------- | :---------------------------: | :-----------------------------: | ---------------------------------------------------------------------------- |
| `DeliveryReceipt` rows           |               ✓               |                —                | Append-only audit log; never mutated                                         |
| `NativeMessageRef` rows          |               ✓               |                —                | Bidirectional mapping (inbound + outbound)                                   |
| `DeliveryOutboxItem` rows        |               ✓               |                —                | Mutable work state; transitions through status vocabulary                    |
| `RenderingEvidence` (on receipt) | ✓ (serialized as JSON string) |                —                | Attached to successful delivery receipts only                                |
| `InflightDelivery`               |               —               |         In-memory only          | Cleared on shutdown via `drain_abandoned_deliveries`                         |
| Delivery outcome statistics      |               —               |    From receipt aggregation     | `accepted` / `skipped` / `failed` counts computed in `handle_ingress`        |
| Retry exhaustion                 |               —               |       From receipt chain        | `should_dead_letter()` checks `RetryExecutor.is_exhausted(attempt_number)`   |
| Outbox status transitions        |               —               | From `delivery_state.py` tables | `OUTBOX_TRANSITIONS` and `RECEIPT_TRANSITIONS` are declarative documentation |

### RenderingEvidence rationale

`RenderingEvidence` (evidence.py) is a frozen snapshot of rendering decision inputs and observable outcomes. It is:

1. **Produced once** by `RenderingPipeline.render()` after a renderer returns.
2. **Attached to `RenderingResult`** via `dataclasses.replace()`.
3. **Serialized to JSON** by `_serialize_rendering_evidence_for_receipt()` in target_delivery.py.
4. **Persisted on the receipt** as a `rendering_evidence` string field.
5. **Only attached on success** (`status in ("sent", "queued")`) — failed/suppressed receipts naturally have `rendering_evidence=None`.

**Evidence fields classification:**

| Category                                | Fields                                                                                                                                                              | Purpose                                                                   |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| Decision inputs (from RenderingContext) | `renderer`, `delivery_strategy`, `target_adapter`, `target_platform`, `target_channel`, `max_text_chars`, `max_text_bytes`, `capability_level`, `capability_policy` | Reproduce _why_ a rendering decision was made                             |
| Outputs / diagnostics                   | `fallback_applied`, `truncated`                                                                                                                                     | What happened during rendering                                            |
| Derived metrics                         | `rendered_text_chars`, `rendered_text_bytes`, `original_text_chars`, `original_text_bytes`                                                                          | Size diagnostics (no content duplication)                                 |
| Conversation context                    | `conversation_id`, `root_event_id`                                                                                                                                  | Event graph correlation                                                   |
| Per-relation evidence                   | `relation_evidence` (tuple of `RelationTargetEvidence`)                                                                                                             | Per-relation native vs fallback rendering decision with provenance reason |

**Recovery and convergence:** Persisted `delivery_receipts` + `native_message_refs` + `delivery_outbox` form the durable evidence base. All can be reconstructed from storage after a process restart. The retry worker derives its operational state from these persisted structures. `RenderingEvidence` is not independently recoverable — it is a snapshot attached to receipts and cannot be re-derived without re-rendering.

## 7. Replay Parity Audit

### Entry points by source

| Source     | Entry point                                                     | How plan is obtained                                                                   | Attempt context                                       | Receipt provenance                          |
| ---------- | --------------------------------------------------------------- | -------------------------------------------------------------------------------------- | ----------------------------------------------------- | ------------------------------------------- |
| `"live"`   | `PipelineRunner.handle_ingress` → `deliver_to_targets`          | `route_event()` → `FallbackResolver.resolve_fallback()` — full capability decision     | `previous_receipt=None` → attempt 1                   | `source="live"`, `replay_run_id=None`       |
| `"retry"`  | `RetryWorker._retry_outbox_item` → `pipeline.deliver_to_target` | Reconstructed from `delivery_outbox` metadata + `delivery_receipts` — **minimal plan** | `previous_receipt` from storage → attempt N           | `source="retry"`, `replay_run_id=None`      |
| `"replay"` | `ReplayEngine._stage_deliver` → `pipeline.deliver_to_targets`   | `pipeline.route_event()` — full capability decision (re-planned)                       | `previous_receipt` looked up from storage → attempt N | `source="replay"`, `replay_run_id=<run_id>` |

### Documented parity gap: retry plan reconstruction

`RetryWorker._retry_outbox_item` (retry.py ~728–741) reconstructs a `DeliveryPlan` from `delivery_outbox` and `delivery_receipts` metadata via the `reconstruct_retry_delivery_plan` helper in `src/medre/core/engine/pipeline/retry_plan.py`. This helper centralises the reconstruction logic and returns a frozen `ReconstructedRetryPlan` bundle containing the minimal `Route`, `DeliveryPlan`, and resolved `RetryPolicy`.

```python
# retry_plan.py — reconstruct_retry_delivery_plan()
ReconstructedRetryPlan(
    route=Route(...),          # minimal route from item.route_id
    plan=DeliveryPlan(
        plan_id=item.delivery_plan_id or "",
        event_id=item.event_id,
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
        retry_policy=retry_policy,  # restored from previous receipt
        route_id=item.route_id or None,
        target_identity=delivery_target_identity(target),
    ),
    retry_policy=retry_policy,
)
```

**Fields not preserved from the original plan:**

| Original field            | Retry reconstruction | Impact                                                                                                                                                                                                                                                                                                                                                         |
| ------------------------- | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `capability_level`        | `None` (default)     | `DeliveryPlan.capability_level` defaults to `None`; `TargetDeliveryService` and rendering currently normalize `None` to native execution semantics. Retry therefore does not preserve the original live capability/fallback decision. This is intentional current behavior for minimal retry reconstruction, but remains a known live/retry parity limitation. |
| `capability_field`        | `None` (default)     | Lost provenance of which capability field determined suppression.                                                                                                                                                                                                                                                                                              |
| `capability_reason`       | `None` (default)     | Lost human-readable reason.                                                                                                                                                                                                                                                                                                                                    |
| `fallback_chain`          | `[]` (default)       | No fallback chain — retry always uses `"direct"` strategy.                                                                                                                                                                                                                                                                                                     |
| `deadline`                | `None` (default)     | Original deadline is not preserved.                                                                                                                                                                                                                                                                                                                            |
| `primary_strategy.method` | `"direct"` always    | Original strategy (e.g. `"fallback_text"`) is not recoverable from `delivery_outbox` metadata.                                                                                                                                                                                                                                                                 |

Retry reconstruction is intentionally minimal: it rebuilds a direct execution plan from `delivery_outbox` and `delivery_receipts` and does not re-run capability or fallback planning. Because `capability_level`/`capability_field`/`capability_reason` are not persisted, retry cannot preserve the original live planning decision. `TargetDeliveryService` therefore treats missing `capability_level` as native execution semantics. This is intentional current behavior for minimal retry reconstruction, not an accidental hidden re-planning path, but it remains a known live/retry parity limitation. Exact parity would require either persisted planning metadata or deliberate retry replanning.

The `reconstruct_retry_delivery_plan` helper in `retry_plan.py` preserves reconstruction semantics exactly, centralises the logic for testability, and documents all omitted fields in its docstring. If exact parity is required in the future, the retry path would need to persist `capability_level`, `capability_field`, and `capability_reason` either on the `delivery_outbox` row or on the `delivery_receipts` row.

**Deferred improvement warning:** Persisting capability metadata on `delivery_outbox` rows or `delivery_receipts` (Recommendation 10) is a deferred future improvement. Any implementation must account for stale-decision risk — the capability context may have changed between the original delivery and a retry after adapter restart — and must resolve the direct-vs-fallback tradeoff: preserving the original `capability_level` preserves intent but may conflict with a re-evaluated capability decision at rendering time.

## 8. Complexity Hotspot Ranking

| #   | File                                                                 | Approx. lines          | Owner                    | Why complex                                                                                                                                     | Candidate simplification                                                                                    | Risk                                                                      |
| --- | -------------------------------------------------------------------- | ---------------------- | ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| 1   | `runner.py` `_deliver_to_targets_fan_out` / `_deliver_single_target` | ~1145–1776 (630 lines) | PipelineRunner           | 8 sequential sub-phases with early returns, nested try/except/finally, capacity acquire/release in finally block, asyncio.gather fan-out        | Extract each sub-phase check into named methods; extract outbox lifecycle into `OutboxManager` helper class | High: deep nesting and finally-block invariants require careful testing   |
| 2   | `retry.py` `_retry_outbox_item`                                      | ~583–961 (380 lines)   | RetryWorker              | Long method with capacity handling, plan reconstruction, success/failure/exhaustion branching, dead-letter detection, backoff computation       | Extract capacity handling and dead-letter detection into helper methods                                     | Medium: well-tested but branching density makes bugs hard to spot         |
| 3   | `target_delivery.py` `deliver_to_target`                             | ~299–859 (560 lines)   | TargetDeliveryService    | ~8 distinct error paths each constructing a receipt with 15+ keyword arguments; rendering + adapter + receipt + native-ref in one method        | Extract receipt construction into a `ReceiptBuilder`; separate error-path methods from success path         | Medium: each error path is individually simple but the aggregate is dense |
| 4   | `delivery_lifecycle.py` `append_queued_to_sent_receipt`              | ~554–807 (250 lines)   | DeliveryLifecycleService | Complex correlation strategy (outbox_id + attempt_number exact match, delivery_plan_id validation); handles ambiguous multi-candidate scenarios | Simplify further by removing earlier fallback paths now that outbox_id is required                          | Low: correlation logic is defensive but well-structured                   |
| 5   | `delivery_lifecycle.py` `finalize_outbox_outcome`                    | ~811–938 (130 lines)   | DeliveryLifecycleService | 6-way branching on receipt status + failure kind + retry policy + retry exhaustion                                                              | Extract retry-exhaustion check and backoff computation into RetryExecutor methods                           | Low: mechanical extraction                                                |
| 6   | `runner.py` `handle_ingress`                                         | ~440–676 (240 lines)   | PipelineRunner           | Per-ingress cache setup (4 dicts), sequential phases with early returns, reaction-to-reaction check                                             | Extract cache setup into a context object; move reaction-to-reaction check into a guard method              | Low-Medium: cache invariants are subtle                                   |
| 7   | `outbox_manager.py` `OutboxManager.create_for_delivery`              | ~82–245 (160 lines)    | OutboxManager            | Outbox creation, lease renewal, ownership/attempt validation, skip-reason taxonomy                                                              | Keep ownership checks isolated and document skip-reason taxonomy                                            | Low: isolated and already extracted                                       |
| 8   | `replay/delivery.py` `_filter_plans_by_capability`                   | ~159–253 (95 lines)    | ReplayDeliveryMixin      | Dual-cache capability resolution + decision caching; tuple unwrapping                                                                           | Pre-resolve capabilities outside the loop; use typed plan list instead of `Any`                             | Low: type safety improvement                                              |
| 9   | `delivery_lifecycle.py` `_select_source_preferred_candidate`         | ~470–550 (80 lines)    | DeliveryLifecycleService | Source-aware candidate selection with replay-safety guard                                                                                       | Already well-isolated; document replay-safety rationale                                                     | Low: documentation only                                                   |
| 10  | `runner.py` `route_event`                                            | ~952–1041 (90 lines)   | PipelineRunner           | Route matching + target expansion + plan creation + route_trace update + retry policy attachment                                                | Extract route_trace update into `RoutingMetadata` method                                                    | Low: mechanical extraction                                                |

## 9. Simplification Recommendations

### Classification scheme

| Classification                     | Definition                                                                            |
| ---------------------------------- | ------------------------------------------------------------------------------------- |
| **Documentation-only**             | No code change; clarify naming, ownership, or intent in docs                          |
| **Naming cleanup**                 | Rename variables/methods for clarity; behavior-identical                              |
| **Behavior-preserving extraction** | Extract methods/classes; no behavior change                                           |
| **Defer / future**                 | Plausible improvement but deferred due to risk, test coverage, or diminishing returns |

### Recommendations

| #   | Recommendation                                                                                                        | Classification                             | Rationale                                                                                                                                                                                                                                                                                                                     |
| --- | --------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Document retry plan reconstruction parity gap (Section 7 above)                                                       | Documentation-only (completed)             | Documented in this audit and centralised in `retry_plan.py` with full docstring coverage of omitted fields.                                                                                                                                                                                                                   |
| 2   | Document `sequence` field on `DeliveryReceipt` as unused diagnostic (always 0)                                        | Documentation-only                         | Field exists but is never incremented. Document to prevent confusion.                                                                                                                                                                                                                                                         |
| 3   | Document why `_deliver_single_target` check ordering matters (loop → policy → capability → skip → capacity → outbox)  | Documentation-only                         | Ordering is a critical invariant. Already documented in code comments; surface in audit.                                                                                                                                                                                                                                      |
| 4   | Rename `_deliver_to_targets_inner` to `_deliver_to_targets_fan_out` or similar to distinguish from the public wrapper | Naming cleanup (completed)                 | **Completed.** Renamed in implementation pass. Renamed to `_deliver_to_targets_fan_out` in `runner.py`.                                                                                                                                                                                                                       |
| 5   | Rename `_deliver_one` to `_deliver_single_target` for self-documenting name                                           | Naming cleanup (completed)                 | **Completed.** Renamed in implementation pass. Renamed to `_deliver_single_target` in `runner.py`.                                                                                                                                                                                                                            |
| 6   | Extract outbox lifecycle into `OutboxManager` helper class (create + ownership + lease + finalize)                    | Behavior-preserving extraction (deferred)  | ~350 lines of outbox logic in runner.py could be isolated. Reduces `_deliver_single_target` scope. **Risk:** the extracted helper must preserve four finally-block invariants — lease renewal task cancellation, outbox finalization, capacity release, and inflight untracking — in the correct order across all exit paths. |
| 7   | Extract receipt construction into `build_delivery_receipt` helper                                                     | Behavior-preserving extraction (completed) | **Completed.** `build_delivery_receipt` in `receipt_factory.py` is a pure construction helper. All 8 sites in `TargetDeliveryService` and 2 in `DeliveryLifecycleService` migrated. `RetryExecutor` receipts remain separate.                                                                                                 |
| 8   | Extract per-ingress cache setup into `IngressCache` context object                                                    | Behavior-preserving extraction             | 4 cache dicts + 2 inflight dicts created per `handle_ingress` call. A context object would clarify ownership and lifecycle.                                                                                                                                                                                                   |
| 9   | Extract `_retry_outbox_item` capacity handling into separate method                                                   | Behavior-preserving extraction             | ~100 lines of capacity acquire/release/backoff could be isolated from the main retry flow.                                                                                                                                                                                                                                    |
| 10  | Consider persisting `capability_level`/`field`/`reason` on `delivery_outbox` rows for exact retry parity              | Defer / future                             | Plausible improvement but requires schema change and near-cap test coverage. Low priority.                                                                                                                                                                                                                                    |
| 11  | Consider requiring `delivery_plan_id` on all `OutboundNativeRefRecord` instances                                      | Obsoleted by outbox_id correlation         | Correlation now uses `outbox_id` + `attempt_number` as the primary selector. `delivery_plan_id` is a validation field only. The earlier fallback paths referenced here have been replaced by strict outbox_id correlation.                                                                                                    |
| 12  | Consider typed plan list for replay instead of `list[Any]`                                                            | Defer / future                             | `replay/delivery.py` uses `Any` for plan compatibility; typed plans would improve safety but require protocol changes.                                                                                                                                                                                                        |

## 10. Implementation Pass

This implementation pass made the branch more than an audit by completing these low-risk simplifications:

- Added `build_delivery_receipt()` construction helper in `receipt_factory.py`.
- Migrated `TargetDeliveryService` receipt construction sites (8 of 8) to use the helper.
- Migrated `DeliveryLifecycleService` suppression and queued→sent supplemental receipts to use the helper.
- Added `reconstruct_retry_delivery_plan()` helper in `retry_plan.py`.
- Updated `RetryWorker` to use the retry reconstruction helper.
- Clarified private `PipelineRunner` method names (`_deliver_to_targets_fan_out`, `_deliver_single_target`).
- Updated this audit to distinguish completed work from deferred larger refactors.

The following items were completed in earlier passes:- Receipt construction helper (`build_delivery_receipt`) added to `receipt_factory.py`.

- `TargetDeliveryService` and `DeliveryLifecycleService` receipt construction sites migrated to use the helper (10 of 13 sites).
- Retry plan reconstruction helper (`reconstruct_retry_delivery_plan`) added to `retry_plan.py`.
- `RetryWorker` updated to use the reconstruction helper.
- Private runner method names clarified: `_deliver_to_targets_fan_out` and `_deliver_single_target`.
- This audit document updated with implementation-pass details.

### Receipt construction helper (`receipt_factory.py`)

`src/medre/core/engine/pipeline/receipt_factory.py` was added as a pure construction-only helper. It provides a single function, `build_delivery_receipt`, that assembles a `DeliveryReceipt` from explicit caller-supplied fields with defaults for `receipt_id` and `created_at`. It performs no lifecycle decisions, exception classification, retry scheduling, or persistence.

**Migrated construction sites (10 of 13):**

| Service                    | Sites migrated                                                                                                                                         |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `TargetDeliveryService`    | All 8: adapter-missing, deadline-exceeded, invalid-capability, strategy-skip, invalid-strategy, rendering-failure, no-deliver-method, primary delivery |
| `DeliveryLifecycleService` | 2: suppression receipt, supplemental sent receipt                                                                                                      |

**Intentionally not migrated (3 of 13):**

| Service                    | Sites kept separate                                      | Reason                                                                   |
| -------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------ |
| `RetryExecutor`            | 2: retry receipt, dead-letter receipt (delivery_plan.py) | Construction is owned by the retry executor's internal logic             |
| `DeliveryLifecycleService` | 1: dead-letter receipt (delegates to RetryExecutor)      | Delegates to `RetryExecutor.build_dead_letter_receipt`, not direct build |

### Retry plan reconstruction helper (`retry_plan.py`)

`src/medre/core/engine/pipeline/retry_plan.py` was added to reconstruct a minimal retry execution plan from persisted `delivery_outbox` and `delivery_receipts` data. It provides `reconstruct_retry_delivery_plan`, which returns a frozen `ReconstructedRetryPlan` containing a minimal `Route`, `DeliveryPlan`, and resolved `RetryPolicy`. The helper preserves reconstruction semantics exactly and documents all omitted fields (fallback chain, deadline, capability metadata) in its docstring.

### Private naming cleanup

Two method renames were applied in `runner.py`:

| Old name                    | New name                      | Rationale                                                    |
| --------------------------- | ----------------------------- | ------------------------------------------------------------ |
| `_deliver_to_targets_inner` | `_deliver_to_targets_fan_out` | The method performs the actual fan-out, not an inner helper  |
| `_deliver_one`              | `_deliver_single_target`      | Self-documenting name for the codebase's most complex method |

All internal call sites and doc references were updated to match.

### Deferred items

The following recommendations from Section 9 remain deferred:

- **6** (OutboxManager extraction): high-risk extraction from `_deliver_single_target` with four finally-block invariants.
- **8** (IngressCache context object): behavior-preserving extraction, not yet prioritized.
- **9** (Retry capacity extraction): behavior-preserving extraction, not yet prioritized.
- **10–12** (Schema changes, typed plans): deferred due to schema impact and test coverage requirements.

# Planning Authority Audit

> Audits actual planning decisions, authority boundaries, suppression paths, and evidence flow in the MEDRE runtime.
> Source: code inspection of `src/medre/core/planning/`, `src/medre/core/engine/pipeline/`, `src/medre/core/rendering/`, `src/medre/core/routing/`, `src/medre/runtime/route_engine.py`, and `src/medre/runtime/reporting.py`.

## 1. Planning Pipeline Flow

The pipeline is orchestrated by `PipelineRunner.handle_ingress()`. Stages execute in this order:

| # | Stage | Authority | Input | Output | Mutates event? |
|---|-------|-----------|-------|--------|----------------|
| 1 | Validate | `PipelineRunner._validate_event` | `CanonicalEvent` | raise `ValueError` on missing fields | No |
| 1.5 | Dedup | `StorageBackend.resolve_native_ref` | `source_native_ref` triple | Drop or continue | No |
| 2 | Resolve relations | `RelationResolver` | Event with `target_native_ref`-only relations | Event with `target_event_id` populated where resolvable | Yes (new event) |
| 2.5 | Conversation identity | `ConversationGraphAuthority` | Resolved event | `root_event_id`, `conversation_id` assigned | Yes (new event) |
| 3 | Store | `StorageBackend.append` | Event | Persisted event | No |
| 4 | Persist inbound native ref | `PipelineRunner._persist_inbound_native_ref` | `source_native_ref` | `NativeMessageRef(direction="inbound")` | No |
| 4.5 | Reaction-to-reaction suppression | `PipelineRunner._is_reaction_to_reaction` | `MESSAGE_REACTED` event | Drop or continue | No |
| 5 | Route + plan | `Router` → `FallbackResolver` | Event | `list[tuple[Route, DeliveryPlan]]` | Yes (route_trace) |
| 6 | Per-target deliver | `TargetDeliveryService` | Event + plan | `DeliveryOutcome` + receipt | No |

## 2. Planning Authorities

### 2.1 CapabilityDecisionResolver

- **Module:** `core/planning/capability_decision.py`
- **Authority:** Single source of truth for all capability decisions (live, replay, diagnostics, rendering evidence).
- **Input:** `(CanonicalEvent, AdapterCapabilities, *, target_adapter)`
- **Output:** `CapabilityDecision` (frozen dataclass)
- **Consumers:** `FallbackResolver`, `PipelineRunner` Phase 2.5, `TargetDeliveryService`, replay BEST_EFFORT filtering, `RenderingEvidence`, `_derive_capability_evidence` in `reporting.py`
- **State:** Stateless, no caching, no framework dependency. Module-level singleton `resolver`.
- **Core rule:** Capabilities describe transport reality, not lifecycle. Adapters report facts; the resolver interprets them.

**Decision model (three-level):**

| Capability level | Delivery strategy | `supported` | Semantics |
|-----------------|-------------------|-------------|-----------|
| `"native"` | `"direct"` | `True` | First-class support |
| `"fallback"` | `"fallback_text"` | `True` | Degrade to inline text within native format |
| `"unsupported"` | `"skip"` | `False` | Suppress delivery before rendering |

**Mapping tables** (in `CapabilityDecisionResolver`):

| Event kind | Capability field | Type |
|-----------|-----------------|------|
| `message.reacted` | `reactions` | String (3-level) |
| `message.edited` | `edits` | String (3-level) |
| `message.deleted` | `deletes` | String (3-level) |
| `message.file` | `attachments` | Boolean |
| `message.created` | `text` | Boolean |
| `message.text` | `text` | Boolean |
| `presence.changed` | `presence` | Boolean |
| `telemetry.received` | `metadata_fields` | Boolean |
| `telemetry.position` | `metadata_fields` | Boolean |

| Relation type | Capability field | Notes |
|--------------|-----------------|-------|
| `reply` | `replies` | String (3-level) |
| `reaction` | `reactions` | String (3-level) |
| `edit` | `edits` | String (3-level) |
| `delete` | `deletes` | String (3-level) |
| `thread` | — | **Deferred**: no candidate produced |

**Precedence:** Multiple candidates → pick most severe (`unsupported` > `fallback` > `native`). Ties broken by evaluation order (event-kind first, then relations in order).

**Thread deferral:** Thread relations produce no capability candidate. Thread-carrying events receive native/direct passthrough with `capability_field=None` unless another candidate overrides.

### 2.2 RelationResolver

- **Module:** `core/planning/relation_resolution.py`
- **Authority:** Resolves `target_native_ref` → `target_event_id` via storage.
- **Input:** `CanonicalEvent` with relations carrying `target_native_ref`
- **Output:** New event with `target_event_id` populated where resolvable; unresolved native refs preserved
- **Consumers:** `PipelineRunner._resolve_relations` (Stage 2)
- **Core rule:** Core relation authority. Looks up `storage.resolve_native_ref(adapter, native_channel_id, native_message_id)`. No adapter imports.

### 2.3 RelationEnricher

- **Module:** `core/planning/relation_enricher.py`
- **Authority:** Enriches relations with target-adapter native refs, fallback text, and sender metadata for rendering.
- **Input:** `(CanonicalEvent, target_adapter, target_channel)`
- **Output:** New event with enriched `EventRelation.target_native_ref`, `fallback_text`, and `metadata` (original_text, original_sender, original_sender_displayname)
- **Consumers:** `PipelineRunner._enrich_relations_for_target` (per-target, before rendering)
- **Core rule:** Never mutates stored event. Returns original event unchanged when no enrichment needed. Uses per-ingress cached `storage.get` and `storage.list_native_refs_for_event`.

**Two phases per relation:**
1. **Native-ref enrichment:** Look up stored native refs for target adapter. Prefer exact channel match; fall back to adapter-only match.
2. **Text enrichment:** Extract original text, sender display name, and sender identity from the target event to populate `fallback_text` and `metadata`.

### 2.4 ConversationGraphAuthority

- **Module:** `core/planning/conversation_graph.py`
- **Authority:** Assigns `root_event_id` and `conversation_id` based on resolved relation ancestry.
- **Input:** `CanonicalEvent` (after relation resolution)
- **Output:** Event with `root_event_id` and `conversation_id` populated
- **Consumers:** `PipelineRunner._assign_conversation_identity` (Stage 2.5)
- **Core rule:** `conversation_id` always equals `root_event_id`. Walks relation ancestors via storage, bounded to depth 64. If event already has `root_event_id`, preserves it (never overwritten by relation walking). Self-roots when no resolved target found.

### 2.5 FallbackResolver (Delivery Planner)

- **Module:** `core/planning/fallback_resolution.py`
- **Authority:** Produces `DeliveryPlan` per `(event, target)` pair using `CapabilityDecisionResolver`.
- **Input:** `(CanonicalEvent, RouteTarget, AdapterCapabilities, *, route_id, target_index)`
- **Output:** `DeliveryPlan` with `primary_strategy` derived from capability decision
- **Consumers:** `PipelineRunner.route_event` (Stage 5)
- **Core rule:** Delegates all capability strategy decisions to `CapabilityDecisionResolver`. Plan carries `capability_level`, `capability_field`, `capability_reason` from the decision. Plan ID is deterministic via `stable_delivery_plan_id`.

### 2.6 Router (Route Resolver / RouteResolver)

- **Module:** `core/routing/router.py`
- **Authority:** Matches events against registered routes. Resolves targets.
- **Input:** `CanonicalEvent`
- **Output:** `list[Route]` (matched), `list[RouteTarget]` (resolved per route)
- **Consumers:** `PipelineRunner.route_event`
- **Core rule:** Pure in-memory matching, no I/O. All enabled routes evaluated (no first-match-wins). Target list returned as-is (Phase 1).

### 2.7 TargetDeliveryService (Target Planner / Target Delivery)

- **Module:** `core/engine/pipeline/target_delivery.py`
- **Authority:** Owns one-target execution: rendering invocation, adapter delivery, receipt construction, rendering evidence attachment.
- **Input:** `(CanonicalEvent, DeliveryPlan, RenderingPipeline, adapters, storage, ...)`
- **Output:** `DeliveryOutcome` + persisted `DeliveryReceipt`
- **Consumers:** `PipelineRunner._deliver_one`
- **Core rule:** Does NOT own outbox creation, capacity, retry scheduling, relation enrichment, or delivery lifecycle. Receives pre-enriched event.

### 2.8 RenderingPipeline (Rendering Planner)

- **Module:** `core/rendering/renderer.py`
- **Authority:** Selects renderer by priority, builds frozen `RenderingContext`, attaches `RenderingEvidence`.
- **Input:** `(CanonicalEvent, target_adapter, delivery_strategy, capability_level, ...)`
- **Output:** `RenderingResult` with attached `RenderingEvidence`
- **Consumers:** `TargetDeliveryService`
- **Core rule:** `delivery_strategy="skip"` raises `ValueError` — must be handled before rendering. `delivery_strategy` is a context hint, not a renderer selector. Target-native renderer always produces its native format.

## 3. Suppression Paths

All paths that prevent delivery from reaching an adapter:

| # | Reason | Authority | Stage | Receipt persisted? | Adapter sees it? | Operator visibility |
|---|--------|-----------|-------|--------------------|-------------------|---------------------|
| 1 | Native-ref dedup | `StorageBackend.resolve_native_ref` | 1.5 | No | No | Log + `RuntimeAccounting` counter |
| 2 | Reaction-to-reaction | `PipelineRunner._is_reaction_to_reaction` | 4.5 | No | No | Log only |
| 3 | Route-trace loop | `PipelineRunner._deliver_one` (Phase 1) | 6a | Yes (`LOOP_SUPPRESSED`) | No | Receipt + log + route stats |
| 4 | Self-loop | `PipelineRunner._deliver_one` (Phase 1) | 6a | Yes (`LOOP_SUPPRESSED`) | No | Receipt + log + route stats |
| 5 | Route-policy denial | `evaluate_route_policy` | 6b | Yes (`POLICY_SUPPRESSED`) | No | Receipt + log + route stats |
| 6 | Capability unsupported | `CapabilityDecisionResolver` | 6c | Yes (`CAPABILITY_SUPPRESSED`) | No | Receipt + log + route stats |
| 7 | Capacity exhaustion | `CapacityController` | 6d | Yes (`CAPACITY_REJECTION`) | No | Receipt |
| 8 | Shutdown rejection | `CapacityController` | 6d | Yes (`SHUTDOWN_REJECTION`) | No | Receipt |
| 9 | Adapter missing | `TargetDeliveryService` | 6g | Yes (`ADAPTER_MISSING`) | No | Receipt |
| 10 | Plan skip (strategy=`"skip"`) | `FallbackResolver` + Phase 2.5 | 6c | Yes (via `CAPABILITY_SUPPRESSED`) | No | Receipt + log |

### 3.1 Early-return paths (no receipt)

- **Native-ref dedup** (stage 1.5): Before storage. No `event_id` persisted yet → no receipt possible. Evidence via `RuntimeAccounting` counter only.
- **Reaction-to-reaction** (stage 4.5): After storage but before routing. No receipt because the event never enters delivery planning.
- **No routes matched** (stage 5): No delivery plan created. Event is stored and available for later replay.

### 3.2 Pre-outbox suppression paths (receipt created)

Stages 6a–6c run **before** capacity acquisition and outbox claim. These produce `DeliveryReceipt(status="suppressed")` with `failure_kind` and `error` fields. No renderer invoked. No adapter call made.

### 3.3 Post-outbox failure paths (receipt created)

- Rendering failure → `RENDERER_FAILURE`
- Adapter transient → `ADAPTER_TRANSIENT` (retryable)
- Adapter permanent → `ADAPTER_PERMANENT`
- Deadline exceeded → `DEADLINE_EXCEEDED`
- Outbox not owned → `OUTBOX_NOT_OWNED`

## 4. Fallback Paths

| Trigger | Authority | Strategy | Rendering behavior | Evidence |
|---------|-----------|----------|--------------------|----------|
| Capability level `"fallback"` | `CapabilityDecisionResolver` | `"fallback_text"` | Target-native renderer embeds relation context as inline text within native format | `RenderingEvidence.capability_level="fallback"`, `fallback_applied` on `RenderingResult` |
| Adapter `SIZE_LIMITS` capability | `MaxLengthPolicy` / renderer | Truncation | Renderer shortens content to fit byte/char budget | `RenderingResult.truncated=True`, `max_text_bytes` in `RenderingContext` |
| Relation target not resolved | `RelationEnricher` | Native ref missing | Renderer falls back to `EventRelation.fallback_text` if available | `RelationTargetEvidence.render_mode="fallback"` when `target_native_message_id` absent |

**Known gap:** No production transport profile currently declares a three-level field at `"fallback"`. All use `"native"` or `"unsupported"`. The fallback path is exercised by tests with synthetic capabilities but has no live-transport R-tier evidence.

## 5. Evidence Flow

### 5.1 Capability evidence chain

```
AdapterCapabilities (adapter-declared)
    → CapabilityDecisionResolver.decide()
    → CapabilityDecision (level, field, reason, strategy)
    → DeliveryPlan (capability_level, capability_field, capability_reason)
    → RenderingContext (capability_level, delivery_strategy)
    → RenderingEvidence (capability_level, delivery_strategy, relation_evidence)
    → DeliveryReceipt (rendering_evidence JSON column)
    → _derive_capability_evidence() in reporting.py (parses receipt for diagnostics)
```

### 5.2 Relation evidence chain

```
EventRelation (target_event_id, target_native_ref, fallback_text)
    → RelationEnricher (populates native refs for target adapter)
    → RenderingContext (delivery_strategy, capability_level)
    → RelationTargetEvidence (render_mode, target_available, fallback_text_source)
    → RenderingEvidence.relation_evidence tuple
    → DeliveryReceipt.rendering_evidence
```

### 5.3 Rendering evidence on receipts

| Receipt status | `rendering_evidence` | Why |
|---------------|---------------------|-----|
| `sent` | Populated | Renderer ran, adapter accepted |
| `queued` | Populated | Renderer ran, adapter enqueued |
| `suppressed` | `None` | No renderer invoked |
| `failed` (rendering) | `None` | Renderer raised |
| `failed` (adapter) | `None` | Renderer succeeded but adapter raised |

## 6. Core Rules

1. **Capabilities describe transport reality, not lifecycle.** Adapters report what they can do. The resolver interprets. No capability check queries the adapter at delivery time — it reads cached `AdapterCapabilities`.

2. **Adapters report facts.** The pipeline classifies failures. Adapters raise `AdapterSendError` / `AdapterPermanentError`. `RetryExecutor.classify_failure` maps to `DeliveryFailureKind`.

3. **Core relation/conversation authority.** `RelationResolver`, `RelationEnricher`, and `ConversationGraphAuthority` are core-layer (no adapter imports, no SDK imports). They accept storage protocol objects only.

4. **Evidence/diagnostics are derived-only.** `RenderingEvidence` is built by the pipeline after rendering (not by renderers). `_derive_capability_evidence` in `reporting.py` derives capability fields from receipt data without schema changes. `RuntimeSnapshot` is read-only observation, not authoritative state.

5. **Deterministic plan IDs.** `stable_delivery_plan_id` produces `plan:{event_id}:{route_part}:{index_part}:{target_hash}`. No `id()` dependency. Repeated replay produces same plan IDs.

6. **Append-only receipts.** Every delivery attempt produces a new receipt row. Existing rows never updated or deleted.

## 7. File Map

| Authority | Path |
|-----------|------|
| CapabilityDecisionResolver | `src/medre/core/planning/capability_decision.py` |
| Capability helpers | `src/medre/core/planning/capabilities.py` |
| RelationResolver | `src/medre/core/planning/relation_resolution.py` |
| RelationEnricher | `src/medre/core/planning/relation_enricher.py` |
| ConversationGraphAuthority | `src/medre/core/planning/conversation_graph.py` |
| FallbackResolver | `src/medre/core/planning/fallback_resolution.py` |
| DeliveryPlan, DeliveryStrategy, DeliveryFailureKind | `src/medre/core/planning/delivery_plan.py` |
| Router | `src/medre/core/routing/router.py` |
| Route models | `src/medre/core/routing/models.py` |
| RenderingPipeline, RenderingContext | `src/medre/core/rendering/renderer.py` |
| RenderingEvidence, RelationTargetEvidence | `src/medre/core/rendering/evidence.py` |
| PipelineRunner | `src/medre/core/engine/pipeline/runner.py` |
| TargetDeliveryService | `src/medre/core/engine/pipeline/target_delivery.py` |
| Route engine (config → routes) | `src/medre/runtime/route_engine.py` |
| Reporting (receipt dicts) | `src/medre/runtime/reporting.py` |

## 8. Spec Cross-References

The following spec documents carry normative language about planning decision authority:

| Spec document | Section | Planning authority content |
|---------------|---------|---------------------------|
| `docs/spec/routing-delivery.md` | § 6.1 | Planning decision authority: FallbackResolver/DeliveryPlan are authoritative; downstream consumes plan fields |
| `docs/spec/adapter-runtime.md` | § 6.3 | Planning authority boundary: adapters report facts, not planning truth |
| `docs/spec/event-model.md` | § 1.2 | Planning-derived fields: `root_event_id`/`conversation_id` are pipeline-assigned by ConversationGraphAuthority |
| `docs/spec/diagnostics-evidence.md` | § 12 | Planning authority boundary for diagnostics: derived-only consumers; replay re-runs planning as explicit exception |

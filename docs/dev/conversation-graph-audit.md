# Conversation Graph & Relation Resolution Audit

**Branch**: `conversation-graph`
**Date**: 2026-06-06
**Scope**: Event-centric conversation model, relation resolution authority, native target selection, renderer decisions, adapter delivery, native ref persistence
**Companion**: `docs/dev/native-relations-audit.md` (native ref ownership and transport metadata)

---

## 1. Purpose

This document lets a contributor trace the full data path:

> **incoming event** → conversation graph → relation resolution → native target selection → renderer decision → adapter delivery → native ref persistence

It records what exists today — types, ownership, lookup paths, and boundaries — so that future conversation-graph work (threading) builds on verified ground rather than assumed behaviour.

---

## 2. Current Conversation Model

`conversation_id` and `root_event_id` are populated at pipeline Stage 2.5 by `ConversationGraphAuthority` after relation resolution. Conversation structure is reconstructed at query time from two orthogonal mechanisms:

| Mechanism              | Field                                        | What it represents                                                   | Source file                          |
| ---------------------- | -------------------------------------------- | -------------------------------------------------------------------- | ------------------------------------ |
| **Derivation lineage** | `CanonicalEvent.parent_event_id` + `lineage` | Transform/enrichment ancestry — parent event produced this child     | `src/medre/core/events/canonical.py` |
| **Typed relations**    | `CanonicalEvent.relations` → `EventRelation` | Semantic links between events: reply, reaction, edit, delete, thread | `src/medre/core/events/canonical.py` |

**Derivation lineage** is _not_ a conversation thread. An enrichment pass creates a new event whose `parent_event_id` points to the pre-enrichment event, and `lineage` is the full derivation chain. Two events in the same Matrix thread have no derivation link unless one was produced by transforming the other.

**Relations** carry the conversational intent. A reply event has `EventRelation(relation_type="reply", target_event_id=<original>)`. This is the only mechanism that connects two independently-authored messages.

### 2.1 CanonicalEvent fields relevant to conversation graph

| Field               | Type                        | Conversation role                              | Current state                                                                                                                                                                                                                                           |
| ------------------- | --------------------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `event_id`          | `str`                       | THE identity of this node in the graph         | UUIDv7, immutable                                                                                                                                                                                                                                       |
| `parent_event_id`   | `str \| None`               | Derivation parent, **not** conversation parent | Set by enrichment/transform                                                                                                                                                                                                                             |
| `lineage`           | `tuple[str, ...]`           | Ordered derivation ancestry                    | Ordered chain of ancestor event IDs                                                                                                                                                                                                                     |
| `relations`         | `tuple[EventRelation, ...]` | Semantic edges to other events                 | Populated by adapter codec                                                                                                                                                                                                                              |
| `source_channel_id` | `str \| None`               | Native channel where event originated          | Set by codec (room ID, channel index, etc.)                                                                                                                                                                                                             |
| `source_native_ref` | `NativeRef \| None`         | Inbound native message reference               | Set by codec                                                                                                                                                                                                                                            |
| `root_event_id`     | `str \| None`               | Root event in relation chain                   | Populated by `ConversationGraphAuthority` at pipeline Stage 2.5; equals `event_id` when self-rooting (no resolved relations); otherwise inherited from the resolved ancestor chain (single- or multi-hop) via `ConversationGraphAuthority` at Stage 2.5 |
| `conversation_id`   | `str \| None`               | Conversation identifier                        | Populated by `ConversationGraphAuthority` at pipeline Stage 2.5; currently equals `root_event_id`                                                                                                                                                       |

**Root selection rule (first-resolved-relation-wins)**: When an event carries multiple relations, `ConversationGraphAuthority` walks them in storage order and selects the root from the _first_ relation whose `target_event_id` is present in storage. Subsequent relations are ignored for root selection. This keeps root assignment deterministic and avoids ambiguity when different relation targets could lead to different conversation roots. If no resolved target is found in storage, the event roots to itself.

### 2.2 EventRelation fields

| Field               | Type                                                   | Purpose                                                 |
| ------------------- | ------------------------------------------------------ | ------------------------------------------------------- |
| `relation_type`     | `Literal["reply","reaction","edit","delete","thread"]` | Semantic edge type                                      |
| `target_event_id`   | `str \| None`                                          | Canonical ID of target event (resolved)                 |
| `target_native_ref` | `NativeRef \| None`                                    | Native-space target (pre-resolution)                    |
| `key`               | `str \| None`                                          | Discriminator (e.g. emoji for reaction)                 |
| `fallback_text`     | `str \| None`                                          | Degraded text when target cannot render natively        |
| `metadata`          | `dict` (frozen)                                        | Arbitrary key-value (MMRelay keys, original text, etc.) |

Valid relation types: `{"reply", "reaction", "edit", "delete", "thread"}` (defined in `src/medre/core/events/schema.py:VALID_RELATION_TYPES`).

**Thread deferral**: `"thread"` is accepted by the constructor but no adapter renders thread relations. Thread capability requires future `AdapterCapabilities.threads` and planner-level routing support.

---

## 3. Storage Layer

### 3.1 Tables

| Table                 | Purpose                                                                                                                                                        | Key file                                  |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| `canonical_events`    | Append-only event log. Carries `source_native_*` columns for inbound native ref.                                                                               | `src/medre/core/storage/sqlite/schema.py` |
| `event_relations`     | One row per `EventRelation`. Split native columns: `target_native_adapter`, `target_native_channel_id`, `target_native_message_id`, `target_native_thread_id`. | `src/medre/core/storage/sqlite/schema.py` |
| `native_message_refs` | Idempotent mapping: `(adapter, native_channel_id, native_message_id) → event_id`. Carries `direction` (inbound/outbound).                                      | `src/medre/core/storage/sqlite/schema.py` |

### 3.2 Lookup Paths

| Lookup                      | Storage method                                                      | SQL key                                                 | File                                           |
| --------------------------- | ------------------------------------------------------------------- | ------------------------------------------------------- | ---------------------------------------------- |
| Native → canonical          | `resolve_native_ref(adapter, native_channel_id, native_message_id)` | `UNIQUE(adapter, native_channel_id, native_message_id)` | `src/medre/core/storage/sqlite/_native_ref.py` |
| Canonical → all native refs | `list_native_refs_for_event(event_id)`                              | `idx_nrefs_event_created` on `(event_id, created_at)`   | `src/medre/core/storage/sqlite/_native_ref.py` |
| Event → relations           | `list_relations(event_id)`                                          | `idx_relations_event_id` on `(event_id, id)`            | `src/medre/core/storage/sqlite/_relation.py`   |

**No reverse relation traversal exists.** Forward traversal (event → outgoing relations) IS supported via `list_relations(event_id)` using `idx_relations_event_id`. Reverse traversal — finding events whose `target_event_id == X.event_id` — is what's missing. The `event_relations.target_event_id` column is populated but unindexed for reverse lookup. This means: given event X, there is no efficient way to find events that relate _to_ event X.

### 3.3 Schema-level reserved fields

| Field                | Present on                                                 | Current value | Note                                |
| -------------------- | ---------------------------------------------------------- | ------------- | ----------------------------------- |
| `native_thread_id`   | `NativeRef`, `NativeMessageRef`, `OutboundNativeRefRecord` | Always `None` | **RESERVED** — no adapter populates |
| `native_relation_id` | `NativeMessageRef`, `OutboundNativeRefRecord`              | Always `None` | **RESERVED** — no adapter populates |

---

## 4. End-to-End Flow Trace

### 4.1 Inbound: Adapter → Canonical Event → Storage

```text
1. Adapter codec decodes native wire format
2. Codec creates CanonicalEvent with:
     - source_native_ref = NativeRef(adapter, native_channel_id, native_message_id)
     - relations = (EventRelation(relation_type, target_native_ref=NativeRef(...)),)
     - target_event_id = None  (unresolved at decode time)
 3. Pipeline calls RelationResolver.resolve_event_relations(event)
      - For each relation with target_native_ref but no target_event_id:
          storage.resolve_native_ref(adapter, channel_id, message_id) → event_id | None
      - If found: new EventRelation with target_event_id set, target_native_ref preserved
      - If not found: relation kept with native ref for future retry
 3.5. Pipeline calls ConversationGraphAuthority.resolve_conversation_identity(event)
      - Walks resolved relation targets to find the root ancestor
      - Sets root_event_id = root ancestor's event_id (or event.event_id if no relations)
      - Sets conversation_id = root_event_id
 4. Pipeline stores event (append) + relations (store_relation) + inbound NativeMessageRef
5. Event is now in canonical_events + event_relations + native_message_refs
```

**Key ownership**: Core resolves relations. Adapter codecs only set `target_native_ref`. The adapter never calls `resolve_native_ref` directly.

### 4.2 Outbound: Canonical Event → Renderer → Adapter → Native Ref

```text
1. Router matches event to delivery plan(s), each targeting (adapter, channel)
2. For each target:
   a. RelationEnricher.enrich_for_target(event, target_adapter, target_channel)
        Phase 1: For each resolved relation (target_event_id set):
            list_native_refs_for_event(target_event_id) → find ref for target_adapter
            Attach best-matching NativeRef (exact channel match preferred)
            Strip incompatible ref (wrong channel → None)
        Phase 2: For each relation:
            get(target_event_id) → extract original_text, original_sender_displayname,
            original_sender from target event → populate fallback_text + metadata
   b. RenderingPipeline.render(event, target_adapter, target_channel, ...)
        - Builds frozen RenderingContext(delivery_strategy, target_platform, ...)
        - Walks registered renderers in priority order
        - First renderer whose can_render() returns True calls render()
   c. Renderer decides native vs fallback:
        - Checks target_native_ref.adapter == target_adapter (ownership)
        - Uses delivery_strategy from context (direct vs fallback_text)
        - Produces RenderingResult with payload + metadata + fallback_applied
3. TargetDeliveryService delivers RenderingResult.payload to adapter
4. Adapter returns AdapterDeliveryResult:
     - Synchronous (Matrix, MeshCore, LXMF): native_message_id available
     - Queue-based (Meshtastic): native_message_id=None, delivery_status="enqueued"
5. Pipeline persists outbound NativeMessageRef (sync) or queues receipt (async)
6. Queue-based: later OutboundNativeRefRecord → NativeMessageRef + supplemental receipt
```

**Key ownership**: Renderers choose native/fallback. Adapters report native facts after delivery only. Adapters do not resolve relations.

---

## 5. Component Ownership

| Component                    | File                                             | Responsibility                                                                      |
| ---------------------------- | ------------------------------------------------ | ----------------------------------------------------------------------------------- |
| `CanonicalEvent`             | `src/medre/core/events/canonical.py`             | Universal immutable event envelope                                                  |
| `EventRelation`              | `src/medre/core/events/canonical.py`             | Typed link from one event to another                                                |
| `NativeRef`                  | `src/medre/core/events/canonical.py`             | Lightweight inline native-space reference                                           |
| `NativeMessageRef`           | `src/medre/core/events/canonical.py`             | Persisted canonical↔native mapping                                                  |
| `OutboundNativeRefRecord`    | `src/medre/core/contracts/adapter.py`            | Delayed queue-based native ID report                                                |
| `RelationResolver`           | `src/medre/core/planning/relation_resolution.py` | Resolves `target_native_ref` → `target_event_id` via `resolve_native_ref()`         |
| `ConversationGraphAuthority` | `src/medre/core/planning/conversation_graph.py`  | Assigns `root_event_id` and `conversation_id` at pipeline Stage 2.5                 |
| `RelationEnricher`           | `src/medre/core/planning/relation_enricher.py`   | Resolves target canonical event → target-adapter `NativeRef` + text/sender metadata |
| `RenderingPipeline`          | `src/medre/core/rendering/renderer.py`           | Ordered renderer dispatch with frozen context                                       |
| `RenderingContext`           | `src/medre/core/rendering/renderer.py`           | Frozen dispatch context (strategy, platform, capabilities, budgets)                 |
| `RenderingResult`            | `src/medre/core/rendering/renderer.py`           | Renderer output ready for adapter delivery                                          |
| `AdapterDeliveryResult`      | `src/medre/core/contracts/adapter.py`            | Adapter-reported delivery facts                                                     |
| `_RelationMixin`             | `src/medre/core/storage/sqlite/_relation.py`     | SQLite relation persistence                                                         |
| `_NativeRefMixin`            | `src/medre/core/storage/sqlite/_native_ref.py`   | SQLite native ref persistence and lookup                                            |

---

## 6. Per-Transport Native Target Selection

### 6.1 Matrix

**Renderer**: `src/medre/adapters/matrix/renderer.py` — `MatrixRenderer`

| Relation | Native target                                         | Selection rule                                                                                    | Fallback                             |
| -------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ------------------------------------ |
| Reply    | `m.relates_to.m.in_reply_to.event_id`                 | `target_native_ref.adapter == target_adapter` and `native_message_id` non-empty → Matrix event ID | Plain text body (no `m.relates_to`)  |
| Reaction | `m.reaction` (`m.relates_to.rel_type="m.annotation"`) | Same ownership check + `mmrelay_compat=False`                                                     | `m.emote` with MMRelay metadata keys |

**Target ID source**: `_matrix_target_event_id(rel, target_adapter)` — reads `rel.target_native_ref.native_message_id` only when `ref.adapter == target_adapter`. **Never** uses `rel.target_event_id` as a Matrix event ID (it is a canonical ID, not a Matrix event ID).

**MMRelay compatibility**: When enabled, reactions render as `m.emote` with wire-format keys (`meshtastic_id`, `meshtastic_emoji`, `meshtastic_text`, `meshtastic_replyId`). These keys are consumed by MMRelay-bridged Matrix rooms.

### 6.2 Meshtastic

**Renderer**: `src/medre/adapters/meshtastic/renderer.py` — `MeshtasticRenderer`

| Relation                       | Native target          | Selection rule                                                            | Fallback                                              |
| ------------------------------ | ---------------------- | ------------------------------------------------------------------------- | ----------------------------------------------------- |
| Reply                          | `reply_id` (uint32)    | `target_native_ref` owned by target adapter + numeric `native_message_id` | Plain text without `reply_id`                         |
| Native reaction (same adapter) | `reply_id` + `emoji=1` | Source adapter == target adapter + numeric `native_message_id`            | `"[reacted: {emoji}]"` text                           |
| Cross-platform reaction        | Descriptive text       | Source adapter != target adapter                                          | `{compact_prefix} reacted {emoji} to "{abbreviated}"` |

**Reply ID precedence**: (1) `target_native_ref.native_message_id` from target adapter, (2) `relation.metadata["meshtastic_reply_id"]` from Matrix codec, (3) `None`.

### 6.3 MeshCore

**Renderer**: `src/medre/adapters/meshcore/renderer.py` — `MeshCoreRenderer`

| Relation      | Native target                         | Selection rule                                 | Fallback                                              |
| ------------- | ------------------------------------- | ---------------------------------------------- | ----------------------------------------------------- |
| All relations | **None** — no native relation support | MeshCore has no reply/reaction protocol fields | Inline degraded text via `degrade_relations_inline()` |

MeshCore has no native message ID, no reply mechanism, no reaction mechanism. All relations are fallback-only. The `expected_ack` is a best-effort outbound correlation ID, not a durable protocol identity.

### 6.4 LXMF

**Renderer**: `src/medre/adapters/lxmf/renderer.py` — `LxmfRenderer`

| Relation      | Native target                                          | Selection rule           | Fallback                                                                 |
| ------------- | ------------------------------------------------------ | ------------------------ | ------------------------------------------------------------------------ |
| All relations | **None** — MEDRE does not decode LXMF native relations | No native rendering path | Inline degraded text + `0xFD` fields envelope for round-trip correlation |

LXMF `FIELD_THREAD` (`0x08`) exists in field constants but has zero documentation and zero implementation in the LXMF reference. MEDRE does not read or write it.

---

## 7. Transport-Specific Metadata Boundaries

The following boundary is consistent with prior audit findings:

| Concern                       | Authority                                            | Boundary rule                                                                                                          |
| ----------------------------- | ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| **Relation resolution**       | Core (`RelationResolver`)                            | Core resolves `target_native_ref` → `target_event_id`. Adapters never call `resolve_native_ref`.                       |
| **Native target enrichment**  | Core (`RelationEnricher`)                            | Core resolves target canonical event → target-adapter `NativeRef` before rendering.                                    |
| **Native vs fallback choice** | Renderer                                             | Renderer inspects `target_native_ref` ownership, `delivery_strategy`, and `capability_level` to decide rendering mode. |
| **Delivery facts**            | Adapter                                              | Adapter reports `native_message_id`, `delivery_status`, and transport-namespaced metadata after delivery.              |
| **Native ref persistence**    | Pipeline (`TargetDeliveryService`, `PipelineRunner`) | Pipeline stores `NativeMessageRef` from adapter-reported facts. Adapters never write to storage.                       |

**Invariant**: Adapters do not resolve relations. Adapters do not choose rendering mode. Adapters report native facts.

---

## 8. Evidence Gaps

### 8.1 `conversation_id` currently equals `root_event_id`

`conversation_id` is populated at pipeline Stage 2.5 by `ConversationGraphAuthority`, but currently always equals `root_event_id`. The field exists independently to allow future divergence (e.g. merging threads, cross-transport conversation grouping), but no such logic exists yet. Cross-transport conversations (a Matrix room bridged to a Meshtastic channel) share a `conversation_id` only when linked by relation chains.

> **Design note (intentional equality):** The current implementation intentionally sets `conversation_id = root_event_id` in every code path (`_assign_identity`). Ancestor `conversation_id` values are **not** independently propagated — when the authority walks an ancestor chain, it reads only `root_event_id`, never the ancestor's `conversation_id`. Future divergence (e.g. merged threads, cross-transport grouping) will require a new authority rule and a separate iteration over the conversation-graph module. This is a deliberate simplification, not an oversight.

**Impact**: `conversation_id` cannot yet diverge from `root_event_id`. Future divergence (merged threads, cross-transport grouping) will require extending `ConversationGraphAuthority`.

### 8.2 `root_event_id` ancestor walk bounded by storage availability

`root_event_id` is populated at pipeline Stage 2.5 by `ConversationGraphAuthority`. When the target event (or any ancestor) is not yet in storage — for example, an out-of-order reply arriving before the original — the authority degrades safely and sets `root_event_id = event.event_id`. The ancestor walk is bounded to 64 hops.

**Impact**: Events that arrive out of order may initially self-root. Once the true root is stored, later events in the chain will correctly inherit it. There is no retroactive repair of previously self-rooted events.

### 8.3 No reverse relation index

`event_relations.target_event_id` is populated but has no index. Finding all events that reply to event X requires a full table scan or a new index.

**Impact**: Conversation reconstruction queries would be O(n) without a `target_event_id` index.

### 8.4 `native_thread_id` always `None`

Reserved on `NativeRef`, `NativeMessageRef`, and `OutboundNativeRefRecord`. No adapter populates it. Matrix has `m.thread` relations; Meshtastic has no threading model; MeshCore and LXMF have no native threads.

**Impact**: Thread-aware rendering cannot use native thread IDs until adapters decode and report them.

### 8.5 Thread relation type accepted but unused

`"thread"` is valid in `VALID_RELATION_TYPES` but no adapter codec creates thread relations and no renderer handles them. The event model is ready; the pipeline is not.

### 8.6 MMRelay cross-transport metadata is fragile

Keys like `meshtastic_reply_id` in `EventRelation.metadata` are set by the Matrix codec only when detecting MMRelay-formatted emotes. This path is specific to MMRelay-bridged Matrix rooms and does not work for native Matrix reactions.

---

## 9. Non-Goals

This document does **not** include:

- Diverging `conversation_id` from `root_event_id` (they are currently equal)
- Retroactively repairing `root_event_id` on events that self-rooted due to out-of-order arrival
- Creating reverse relation indexes on `event_relations.target_event_id`
- Implementing thread relation rendering in any adapter
- Changing the relation type vocabulary
- Modifying adapter source code or test files
- Introducing conversation-level query APIs on the storage backend
- Populating `native_thread_id` on any type
- Adding `AdapterCapabilities.threads` or planner-level thread routing

---

## 10. File Index

| File                                             | What it provides                                                                         |
| ------------------------------------------------ | ---------------------------------------------------------------------------------------- |
| `src/medre/core/events/canonical.py`             | `CanonicalEvent`, `EventRelation`, `NativeRef`, `NativeMessageRef`, `DeliveryReceipt`    |
| `src/medre/core/events/schema.py`                | `VALID_RELATION_TYPES`, `CURRENT_SCHEMA_VERSION`, `SchemaRegistry`                       |
| `src/medre/core/events/metadata.py`              | `EventMetadata`, `TransportMetadata`, `RoutingMetadata`, `NativeMetadata`                |
| `src/medre/core/events/kinds.py`                 | `EventKind` constants, `KNOWN_KINDS`                                                     |
| `src/medre/core/events/bus.py`                   | `EventBus` (prefix-matched pub/sub)                                                      |
| `src/medre/core/storage/sqlite/schema.py`        | DDL for `canonical_events`, `event_relations`, `native_message_refs`                     |
| `src/medre/core/storage/sqlite/_relation.py`     | `store_relation()`, `list_relations()`                                                   |
| `src/medre/core/storage/sqlite/_native_ref.py`   | `store_native_ref()`, `resolve_native_ref()`, `list_native_refs_for_event()`             |
| `src/medre/core/storage/backend.py`              | `StorageBackend` protocol, `EventFilter`                                                 |
| `src/medre/core/planning/relation_resolution.py` | `RelationResolver` — native ref → canonical ID                                           |
| `src/medre/core/planning/conversation_graph.py`  | `ConversationGraphAuthority` — root_event_id and conversation_id assignment at Stage 2.5 |
| `src/medre/core/planning/relation_enricher.py`   | `RelationEnricher` — canonical event → target-adapter native ref + text                  |
| `src/medre/core/rendering/renderer.py`           | `RenderingContext`, `RenderingResult`, `Renderer` protocol, `RenderingPipeline`          |
| `src/medre/core/contracts/adapter.py`            | `AdapterDeliveryResult`, `OutboundNativeRefRecord`, `AdapterCapabilities`                |
| `src/medre/adapters/matrix/renderer.py`          | `MatrixRenderer` — native/fallback reply and reaction rendering                          |
| `src/medre/adapters/matrix/relations.py`         | Matrix relation extraction helpers (`extract_reply_target`, `extract_reaction`)          |
| `src/medre/adapters/meshtastic/renderer.py`      | `MeshtasticRenderer` — reply_id, emoji, cross-platform reactions                         |
| `src/medre/adapters/meshcore/renderer.py`        | `MeshCoreRenderer` — fallback-only relation rendering                                    |
| `src/medre/adapters/lxmf/renderer.py`            | `LxmfRenderer` — fallback + `0xFD` envelope                                              |
| `docs/spec/event-model.md`                       | Normative event model specification                                                      |
| `docs/dev/native-relations-audit.md`             | Native ref ownership, transport metadata, per-transport sections                         |

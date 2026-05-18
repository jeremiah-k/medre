# Event Lineage and Debugging Contract

> Contract version: 1
> Last updated: 2026-05-13
> Track: 5 (Operational Runtime Hardening)

This document is an operational gap audit, not a design proposal. It records what the MEDRE runtime actually provides for tracing event ancestry, walking derivation chains, and diagnosing pipeline failures. Every section distinguishes implemented behavior from known gaps. No new runtime features are introduced here.

## 1. Core Invariant: Storage Is Authoritative

Storage is the single source of truth for event history, derivation ancestry, and cross-adapter correlation. Metadata embedded in external platforms (Matrix custom content fields, Discord embeds, LXMF field envelopes) is secondary and diagnostic. It may be lost due to redaction, pruning, or API changes. Any feature that needs reliable lineage must read from the `canonical_events` and `event_relations` tables.

This invariant is stated in Contract 07 (Section 1) and Contract 03 (Section 1) and is not negotiable.

## 2. Lineage Mechanisms in the Canonical Event Model

### 2.1 `parent_event_id`

Every `CanonicalEvent` carries an optional `parent_event_id: str | None`. When a pipeline stage (enrichment, transform, policy) produces a derived event, the new event's `parent_event_id` points back to the event it was derived from. Source events (those created directly by adapter codecs) have `parent_event_id = None`.

**Implemented behavior:**

- The field is persisted in `canonical_events.parent_event_id`.
- An index (`idx_events_parent`) exists on this column.
- `RelationResolver.create_relation_event()` sets `parent_event_id = source_event.event_id` when creating relation events.

**Gap:**

- No referential integrity constraint enforces that `parent_event_id` references an existing row. The field is not validated at construction time (Contract 01, Section 5: "No referential integrity check").
- No API exists to walk the parent chain. A consumer must manually query `get(parent_event_id)` in a loop.

### 2.2 `lineage`

The `lineage: tuple[str, ...]` field carries the full chain of event IDs from the origin event to the current event. It is an immutable tuple of non-empty strings.

**Implemented behavior:**

- Stored as a JSON array in `canonical_events.lineage`.
- Validated at construction: every element must be a non-empty string (Contract 01, Section 1).
- `RelationResolver.create_relation_event()` appends the source event's ID: `lineage = (*source_event.lineage, source_event.event_id)`.
- Replay preserves lineage: every `ReplayResult` carries the source event's lineage tuple (Contract 07, Section 3.3).

**Gap:**

- `lineage` ordering is not validated. Items are checked for validity but not for chronological ordering (phase-1-limitations.md, Section 5).
- `parent_event_id` and `lineage` consistency is not enforced. `parent_event_id` may or may not appear as the last element of `lineage` (phase-1-limitations.md, Section 5).
- No index exists on `lineage` for ancestry queries. The column is stored as a JSON array, which SQLite cannot index element-by-element without a virtual table or generated column.

### 2.3 `depth`

The `depth: int` field tracks derivation depth. Source events have `depth = 0`. Each derivation increments by 1.

**Implemented behavior:**

- Stored in `canonical_events.depth` with `DEFAULT 0`.
- Validated at construction: `depth >= 0`.
- `RelationResolver.create_relation_event()` sets `depth = source_event.depth + 1`.

**Gap:**

- No validation that depth equals `len(lineage)`. The two fields are set independently and could diverge.

### 2.4 `trace_id`

The `trace_id: str | None` field is an optional distributed tracing correlation ID.

**Implemented behavior:**

- Stored in `canonical_events.trace_id`.
- Survives JSON and msgpack round-trips (phase-1-limitations.md, Section 2.4).
- `RelationResolver.create_relation_event()` propagates `trace_id` from the source event.

**Gap:**

- `trace_id` is never populated by the pipeline itself. No adapter or pipeline stage currently sets it. It is reserved for future use (Contract 01, Section 12). In Phase 1, all events have `trace_id = None` unless an adapter explicitly sets it. No adapter does.
- There is no index on `trace_id`, so queries filtering by correlation ID require a full table scan unless `correlation_ids` on `ReplayRequest` is used (which fetches by `event_id`, not `trace_id`).

## 3. Native Reference Correlation

### 3.1 `NativeRef` and `native_message_refs`

`NativeRef` (Contract 01, Section 3) is the structured native reference type: `(adapter, native_channel_id, native_message_id, native_thread_id)`. When an adapter codec decodes an inbound event, it may carry an `EventRelation` with `target_native_ref` set and `target_event_id = None`, indicating that the relation target is known in native space but not yet mapped to a canonical event.

The `native_message_refs` table provides the mapping from native IDs to canonical `event_id`. A `UNIQUE(adapter, native_channel_id, native_message_id)` constraint ensures idempotent correlation. When `native_channel_id` is `NULL`, SQLite's UNIQUE cannot dedupe (`NULL != NULL`), so `store_native_ref` performs an explicit resolve-before-insert check to ensure NULL-channel refs are also idempotent.

**Implemented behavior:**

- `StorageBackend.resolve_native_ref(adapter, native_channel_id, native_message_id)` queries this table and returns the canonical `event_id` or `None`.
- `RelationResolver.resolve_event_relations()` calls this for every unresolved relation on an event.
- `RelationResolver.resolve_relation()` calls this for a single relation.
- Unresolved native refs are preserved. The relation retains `target_native_ref` and delivery falls back to `fallback_text` (Contract 04, Section 8.3).

**Gap:**

- No background re-resolution. If a native ref cannot be resolved at ingress time (the target event hasn't arrived yet), it stays unresolved permanently unless the event is replayed. No scheduled or triggered re-resolution mechanism exists.

### 3.2 `source_native_ref`

The `CanonicalEvent.source_native_ref` field carries the inbound native message reference from the adapter codec. The pipeline persists this as an inbound `NativeMessageRef` after canonical event storage (Contract 01, Section 1, Field Notes).

**Implemented behavior:**

- Persisted as four split columns: `source_native_adapter`, `source_native_channel_id`, `source_native_message_id`, `source_native_thread_id`.
- All four are `NULL` for outbound events or internally created events.

## 4. Relation Resolution

### 4.1 `RelationResolver`

The resolver lives at `core/planning/relation_resolution.py`. It accepts a storage protocol object (anything with a `resolve_native_ref` method) and provides:

- `resolve_event_relations(event)`: resolves all unresolved relations on an event. Returns the original event if nothing changed, or a new event with resolved relations.
- `resolve_relation(relation)`: resolves a single relation's native ref to a canonical event ID.
- `create_relation_event(source_event, relation_type, target_native_ref, key)`: creates a new canonical event representing a relation, with correct `parent_event_id`, `lineage`, `depth`, and `trace_id` propagation.

**Implemented behavior:**

- Resolution happens during ingress, after decode and before storage (Contract 01, Section 10).
- If resolution succeeds, the relation is updated: `target_event_id` is set.
- If resolution fails, the native ref is preserved. Routing and rendering continue without error.

**Gap:**

- Adapters do not resolve relations. The resolver is a core pipeline concern. Adapters provide `target_native_ref` on relations; the pipeline resolves them (Contract 04, Section 8).
- No re-resolution of failed relations after the initial pass. Replay can re-trigger resolution for specific events via `BEST_EFFORT` mode, but this is manual.

### 4.2 `event_relations` Storage Format (Resolved)

The `event_relations` table uses split nullable columns to store unresolved native references:

| Column                                | Purpose                                           |
| ------------------------------------- | ------------------------------------------------- |
| `target_event_id TEXT`                | Canonical event ID of the target, once resolved   |
| `target_native_adapter TEXT`          | NativeRef.adapter when canonical ID not yet known |
| `target_native_channel_id TEXT`       | NativeRef.native_channel_id                       |
| `target_native_message_id TEXT`       | NativeRef.native_message_id                       |
| `target_native_thread_id TEXT`        | NativeRef.native_thread_id                        |
| `metadata TEXT NOT NULL DEFAULT '{}'` | Relation metadata                                 |

When a relation is unresolved, `target_event_id` is `NULL` and the four `target_native_*` columns carry the native reference. The relation resolution stage resolves these to `target_event_id` by calling `resolve_native_ref` against `native_message_refs`. At load time, `_row_to_relation` reconstructs the in-memory `EventRelation.target_native_ref` from the split columns. `CanonicalEvent.relations` are reconstructed from `event_relations` on every `get` and `query` call.

> **Historical note (resolved):** Earlier contract drafts documented a single `target_native_ref TEXT` column (JSON blob). Contracts 01 and 03 have been updated to match the split-column implementation. No runtime code ever used the JSON-blob format.

## 5. Replay as a Lineage/Debugging Tool

The replay engine (`core/storage/replay.py`, Contract 07) is the primary debugging tool for event lineage and pipeline behavior.

### 5.1 What Replay Provides for Debugging

| Mode          | Debugging Value                                                                                                |
| ------------- | -------------------------------------------------------------------------------------------------------------- |
| `STRICT`      | Verifies event existence and kind registration. No side effects. Useful for integrity checks.                  |
| `RE_RENDER`   | Re-runs rendering without side effects. Captures output in `ReplayResult`. Useful for testing renderers.       |
| `RE_ROUTE`    | Re-runs routing with current configuration. No side effects. Useful for testing route changes.                 |
| `BEST_EFFORT` | Full re-processing including delivery. The only mode with side effects. Useful for retrying failed deliveries. |
| `DRY_RUN`     | All stages through rendering, delivery skipped. Useful for previewing what BEST_EFFORT would do.               |

### 5.2 Lineage Preservation in Replay

Every `ReplayResult` carries the `lineage` tuple from the source event (Contract 07, Section 3.3). This means replay output can be correlated back to the original derivation chain.

Replay never mutates historical events. All modes pass events read-only through pipeline stages (Contract 07, Section 3.3, invariant 1).

### 5.3 Replay Limitations for Debugging

- **No receipt deduplication.** Replaying events with existing successful delivery receipts produces duplicate receipts (phase-1-limitations.md, Section 0).
- **No historical renderer/adapter versions.** Replay uses the current pipeline configuration. If renderers or adapters have changed, replay output will differ from original delivery (Contract 07, Section 6).
- **No replay rate limiting.** A large replay against a live adapter could overwhelm it.
- **No progress tracking or resumption.** Replay runs to completion or failure without checkpoints.
- **No file or stream source.** Replay reads from storage only.

## 6. Diagnostician

The `Diagnostician` class (`core/observability/metrics.py`) records structured diagnostic events during pipeline execution and replay.

### 6.1 What It Records

| Method                                                  | Category             | Records                               |
| ------------------------------------------------------- | -------------------- | ------------------------------------- |
| `record_planner_failure(event_id, error)`               | `planner_failures`   | Routing or planning failures          |
| `record_renderer_failure(event_id, target, error)`      | `renderer_failures`  | Rendering failures per target adapter |
| `record_storage_failure(event_id, operation, error)`    | `storage_failures`   | Storage operation failures            |
| `record_adapter_failure(event_id, adapter, error)`      | `adapter_failures`   | Adapter delivery failures             |
| `record_replay_skip(event_id, reason)`                  | `replay_skips`       | Events skipped during replay          |
| `record_replay_downgrade(event_id, original, fallback)` | `replay_downgrades`  | Mode downgrades during replay         |
| `record_correlation_miss(event_id, native_ref)`         | `correlation_misses` | Native refs that failed to resolve    |

Each method emits a structured log entry and increments an internal counter. The `snapshot()` method returns all counters as a plain dict.

### 6.2 Diagnostician Limitations

- **No lineage awareness.** The Diagnostician records failures keyed by `event_id` and adapter/target names. It does not record `parent_event_id`, `lineage`, or `trace_id` alongside failures. A diagnostic for event `evt-42` does not tell you what derived event or source event it relates to.
- **No temporal indexing.** Counters are aggregated (Counter objects), not time-series. There is no way to ask "how many failures in the last 5 minutes?"
- **No orphan detection.** The Diagnostician does not detect events with broken lineage (missing parents) or unresolved native refs that have been stuck for a long time.
- **No alerting.** The Diagnostician records and logs; it does not trigger alerts, notifications, or escalation.
- **In-memory only.** State is lost on restart. There is no persistence of diagnostic counters.

## 7. Known Gaps Summary

### 7.1 Absent Lineage Walking APIs

No API exists to traverse the derivation tree. Walking the parent chain requires manual `get(parent_event_id)` loops. Walking descendants requires a full table scan or application-level bookkeeping. The `lineage` tuple provides the full ancestry chain on each event, but there is no query interface that accepts an event ID and returns the full derivation tree.

Specific missing operations:

- "Give me all events derived from event X."
- "Give me the full ancestor chain for event X."
- "Give me all events in the derivation tree of event X."
- "How many derivation steps separate event A from event B?"

The `idx_events_parent` index supports "find events whose parent is X" via a direct query, but this is not exposed as a `StorageBackend` method.

### 7.2 Orphan Detection

No mechanism detects orphaned events: events whose `parent_event_id` references a canonical event that does not exist in storage. This can happen if:

- An event was stored but its parent was not (storage write failure mid-transaction).
- Events were manually deleted from the database.
- A derived event was stored before the source event arrived (ordering issue).

The Diagnostician does not detect orphans. No background check or startup verification scans for broken parent references.

### 7.3 `trace_id` Population

`trace_id` is never populated by the pipeline. All events carry `trace_id = None` unless an adapter explicitly sets it. No adapter currently does. The field exists, survives serialization, and is propagated by `RelationResolver.create_relation_event()`, but it is inert in Phase 1.

### 7.4 Diagnostician Lineage Gaps

As noted in Section 6.2, diagnostic records lack lineage context. This means diagnostic output (e.g., "adapter failure for event evt-42 on matrix-home") cannot be traced back to the source event or the full derivation chain without manually querying storage.

### 7.5 `event_relations` Documentation Drift (Resolved)

Section 4.2 previously documented a schema inconsistency between contracts. Contracts 01 and 03 have been updated to document the split `target_native_*` columns. This gap is resolved. No further doc-only updates are needed for this issue.

### 7.6 No Orphaned Native Ref Cleanup

`native_message_refs` accumulate over time. No pruning or cleanup mechanism exists. Unresolved relations (those where `target_native_ref` could not be mapped) remain in `event_relations` with `target_event_id = NULL` indefinitely.

### 7.7 No Cross-Event Correlation Query

No API supports "find all events related to event X" across all relation types. The `idx_relations_event` index supports "find relations where event_id = X" (outgoing relations), and `idx_relations_target` supports "find relations where target_event_id = X" (incoming relations). But these require raw SQL. They are not exposed as `StorageBackend` methods.

## 8. What Works Today

Despite the gaps, the current system provides solid lineage foundations:

| Capability                      | Mechanism                                               | Status                             |
| ------------------------------- | ------------------------------------------------------- | ---------------------------------- |
| Derivation chain recording      | `parent_event_id` + `lineage` tuple                     | Working, persisted, indexed        |
| Depth tracking                  | `depth` field                                           | Working, persisted                 |
| Native-to-canonical correlation | `native_message_refs` + `resolve_native_ref`            | Working, idempotent, indexed       |
| Relation resolution at ingress  | `RelationResolver`                                      | Working, graceful fallback on miss |
| Full re-processing via replay   | `ReplayEngine` with 5 modes                             | Working, deterministic             |
| Failure recording               | `Diagnostician` with 7 categories                       | Working, structured logging        |
| Native ref preservation on miss | Split `target_native_*` columns on unresolved relations | Working                            |
| Fallback text delivery          | `fallback_text` on `EventRelation`                      | Working                            |
| Receipt lineage for delivery    | `attempt_number` + `parent_receipt_id`                  | Working, ordered, queryable        |

## 9. No-Feature-Expansion Statement

This document describes the current state. It does not introduce, propose, or imply any new runtime features. Specifically, the following are not being added:

- No lineage walking API
- No orphan detection background job
- No `trace_id` auto-population
- No Diagnostician lineage enrichment
- No admin API, CLI command, webhook endpoint, or management interface
- No new storage schema, event kind, or query API
- No native ref cleanup or pruning mechanism

Any future work in these areas belongs in separate tracks with their own contracts.

## 10. References

- `docs/contracts/01-canonical-event-contract.md`: CanonicalEvent, EventRelation, NativeRef definitions, immutability rules, relation persistence rules
- `docs/contracts/03-storage-contract.md`: StorageBackend protocol, SQLite schema, storage method semantics
- `docs/contracts/04-routing-planning-contract.md`: Relation resolution flow, fallback rendering, delivery failure taxonomy
- `docs/contracts/07-replay-event-log-contract.md`: ReplayMode, ReplayRequest, replay constraints, storage backend protocol for replay
- `docs/contracts/16-production-connectivity-readiness.md`: Adapter connectivity status, fake delivery scope
- `docs/contracts/phase-1-limitations.md`: Phase 1 constraints, validation gaps, replay caveats, protocol-neutral readiness
- `src/medre/core/planning/relation_resolution.py`: RelationResolver implementation
- `src/medre/core/observability/metrics.py`: Diagnostician implementation
- `src/medre/core/storage/sqlite.py`: Actual SQLite schema (authoritative for column definitions)
- `src/medre/core/storage/replay.py`: ReplayEngine implementation

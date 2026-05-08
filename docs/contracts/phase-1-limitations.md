# Phase 1 Limitations

> Document version: 3
> Last updated: 2026-05-08

This document explicitly records what Phase 1 does **not** implement, what is reserved for future phases, and what behavioral contracts are locked in for backward compatibility.

---

## 0.5. Delivery Failure Semantics (Track 3)

### What Phase 1 Implements

| Feature | Location | Status |
|---|---|---|
| Delivery failure taxonomy | `DeliveryFailureKind` enum | 6 categories: PLANNER, RENDERER, ADAPTER_TRANSIENT, ADAPTER_PERMANENT, TARGET_NOT_FOUND, DEADLINE_EXCEEDED |
| RetryExecutor | `RetryExecutor` class | Backoff computation, exhaustion detection, retry/dead-letter receipt construction |
| Receipt lineage | `DeliveryReceipt.attempt_number`, `parent_receipt_id` | Explicit 1-indexed attempt numbering and parent linkage |
| Lineage persistence | `delivery_receipts` table columns | `attempt_number INTEGER NOT NULL DEFAULT 1`, `parent_receipt_id TEXT` |
| Lineage query | `list_receipts_for_plan()` | Returns receipts ordered by attempt_number for a plan+adapter pair |
| Target-scoped failure | `deliver_to_targets` | Each target classified independently; failure_kind on DeliveryOutcome |
| Dead-letter receipts | `deliver_to_target` | On retry exhaustion, appends `dead_lettered` receipt after primary receipt |
| Deadline check | `deliver_to_target` | Checks `plan.deadline` before rendering |
| Failure classification | `RetryExecutor.classify_failure()` | Static taxonomy classifier used by pipeline |

### Phase 1 Delivery Failure Guarantees

| Guarantee | Description |
|---|---|
| Failure taxonomy complete | Every delivery failure is classified into one of 6 `DeliveryFailureKind` members |
| Receipt lineage ordered | Receipts linked by `attempt_number` and `parent_receipt_id`; queryable in order |
| Target-scoped isolation | One target's failure does not affect sibling targets; each has its own receipt chain |
| Append-only receipts | Dead-letter receipt appended AFTER primary receipt; ordering preserved |
| Deterministic classification | Transient vs permanent classification uses exception type taxonomy, not heuristics |
| Retry decisions are pure | `RetryExecutor` is stateless; no side effects in backoff/exhaustion computation |

### What Phase 1 Does NOT Implement for Delivery Failure

- **No background retry scheduler.** Retry is synchronous/receipt-level only. The pipeline records `next_retry_at` on failed receipts but does not automatically re-attempt delivery. A future scheduler or manual replay via `BEST_EFFORT` mode is required.
- **No retry budget or rate limiting.** No per-adapter or per-plan retry rate limiting.
- **No dead-letter queue management.** Dead-lettered events are recorded as receipts but no admin interface or reprocessing UI exists.
- **No receipt deduplication.** Replay against events with existing receipts may produce duplicate receipts.
- **No adapter-level error customization.** Error classification uses Python exception types; adapters cannot declare custom retryable/permanent error codes.

---

## 0. Replay Determinism (Track 1)

### What Phase 1 Implements

Five replay modes with explicit, testable guarantees:

| Mode | Stages | Side Effects | Error Handling |
|------|--------|-------------|----------------|
| `STRICT` | store (verify only) | None | Re-raise |
| `RE_RENDER` | store, render | None | Re-raise |
| `RE_ROUTE` | store, route, plan | None | Re-raise |
| `BEST_EFFORT` | store, route, plan, render, deliver | Adapter delivery | Capture errors |
| `DRY_RUN` | store, route, plan, render, deliver (skip) | None | Re-raise |

### Phase 1 Replay Guarantees

| Guarantee | Description |
|-----------|-------------|
| Immutability | Replay never mutates historical `CanonicalEvent` instances |
| No storage writes (non-BEST_EFFORT) | STRICT, RE_RENDER, RE_ROUTE, DRY_RUN produce zero storage side effects |
| Deterministic ordering | Results yielded in storage query order or correlation_id list order |
| Lineage preservation | Every `ReplayResult` carries the source event's lineage tuple |
| Diagnostician wiring | Optional `Diagnostician` records skips, downgrades, failures |
| target_adapters filtering | Delivery plans filtered by adapter name; opaque plans passed through |
| Schema version acceptance | Events with `schema_version >= CURRENT_SCHEMA_VERSION` pass STRICT replay |

### What Phase 1 Does NOT Implement for Replay

- No separate `RETRY` mode. Retry is BEST_EFFORT replay scoped to events with failed delivery receipts (selection pattern, not a mode).
- No receipt deduplication during replay (would require receipt-aware delivery).
- No dead-letter queue integration (Track 3).
- No replay rate limiting per adapter.
- No replay progress tracking or resumption.
- No file or stream source for replay events (storage only).
- No renderer/adapter history (replay uses current pipeline configuration).
- No `reprocess` vs `replay_only` mode distinction from earlier spec.

### Retry Semantics (Honest Documentation)

Retry in Phase 1 is a **selection pattern**, not a distinct replay mode:

1. Query `delivery_receipts` for `status in ("failed", "dead_lettered")`.
2. Collect `event_id` values.
3. Issue `ReplayRequest(mode=BEST_EFFORT, correlation_ids=<collected_ids>)`.

A true retry mode requires receipt deduplication, dead-letter queue integration, and retry budgets. These belong in Track 3 (delivery failure executor).

## 1. Schema Migration

### Current State

- **`CURRENT_SCHEMA_VERSION = 1`** is the baseline compatibility contract.
- **No migrations are executed.** The `_MigrationRegistry` provides a registry-only hook (`register` / `get` API) but no automatic migration pipeline.
- Events with `schema_version > 1` are accepted at construction without transformation.
- Events with `schema_version < 1` are rejected by `CanonicalEvent.__post_init__`.

### Contract Guarantees

| Guarantee | Description |
|-----------|-------------|
| New fields append with defaults | Future schema versions add fields; existing consumers read `v1` fields normally |
| Existing fields deprecated, not removed | A deprecated field remains populated for at least one version cycle |
| Unknown fields preserved | msgspec skips unknown struct fields during decode (forward compatibility) |
| `schema_version >= 1` | Enforced at construction; the minimum valid version is 1 |

### What Phase 1 Does NOT Do

- No automatic payload migration on decode
- No schema negotiation between adapters and runtime
- No deprecation warnings at runtime
- No schema version downgrade logic
- Adapters are responsible for producing events at the version they support

---

## 2. Protocol-Neutral Readiness

### What Exists

The canonical event model is transport-agnostic by design:

| Feature | Location | Status |
|---------|----------|--------|
| Correlation IDs | `trace_id` field on `CanonicalEvent` | Available, not populated by default |
| Idempotency keys | `metadata.custom["idempotency_key"]` | Convention; not enforced |
| Principal/auth context | `metadata.custom["principal"]` | Reserved; not populated |
| Request/response lineage | `lineage` + `parent_event_id` | Mechanism exists |
| Inbound provenance | `source_adapter` + `source_transport_id` | Always populated |
| Event kind registry | `EventKind` constants + `KNOWN_KINDS` | 18 kinds across 7 domains |

### What Phase 1 Does NOT Implement

- No HTTP/webhook server or listener
- No RPC framework or API surface
- No authentication or authorization framework
- No Matrix transport implementation
- No real transport adapters (only the event model and contracts)
- No protocol-specific fields beyond what adapters define in `metadata.native`

### Future Webhook Readiness

The following protocol-neutral concepts are documented here for future reference but are **not** implemented:

| Concept | Notes |
|---------|-------|
| **Correlation IDs** | `trace_id` on `CanonicalEvent`; maps to HTTP `X-Correlation-ID` or similar headers |
| **Idempotency keys** | Consumers should use `metadata.custom["idempotency_key"]` for deduplication |
| **Principal/auth context** | Reserved in `metadata.custom["principal"]`; no auth framework exists |
| **Request/response lineage** | Use `parent_event_id` and `lineage` to correlate request-response pairs |
| **Inbound provenance** | `source_adapter` + `source_transport_id` identify the origin; extensible for new transports |

---

## 3. Event Taxonomy

### Locked-In Kinds (18 total)

The following 18 event kinds are the canonical taxonomy for Phase 1:

**Message domain** (6): `message.created`, `message.text`, `message.reacted`, `message.edited`, `message.deleted`, `message.file`

**Telemetry domain** (2): `telemetry.received`, `telemetry.position`

**Presence domain** (1): `presence.changed`

**Identity domain** (1): `identity.updated`

**Delivery domain** (5): `delivery.accepted`, `delivery.queued`, `delivery.sent`, `delivery.confirmed`, `delivery.failed`

**System domain** (2): `system.audit`, `system.lifecycle`

**Plugin domain** (1): `plugin.custom`

### Taxonomy Notes

- Kinds follow `<domain>.<action>` naming convention.
- The `plugin.custom` kind reserves a namespace for extension events.
- Plugins should append sub-kinds in the payload rather than inventing new top-level kinds.
- The taxonomy is exported in `EventKind` constants and `KNOWN_KINDS` frozenset.

### Divergence from Earlier Spec

The initial spec document listed a simplified taxonomy (`telemetry`, `position`, `presence`, `metrics.update`, `channel.announcement`, `plugin.event`, `delivery.receipt`, `transform.output`, `policy.action`). The code taxonomy is more granular:

| Spec Kind | Code Equivalent |
|-----------|-----------------|
| `telemetry` | `telemetry.received` |
| `position` | `telemetry.position` |
| `presence` | `presence.changed` |
| `delivery.receipt` | Tracked via `DeliveryReceipt` records, not event kinds |
| `plugin.event` | `plugin.custom` |
| `metrics.update` | Not implemented (future) |
| `channel.announcement` | Not implemented (future) |
| `transform.output` | Not implemented (future) |
| `policy.action` | Not implemented (future) |

---

## 4. Serialization

### Current Behavior

- **JSON**: `msgspec.json.encode()` / `msgspec.json.decode()` — deterministic field ordering, forward-compatible (unknown fields skipped).
- **MessagePack**: `msgspec.msgpack.encode()` / `msgspec.msgpack.decode()` — binary encoding, same forward-compatibility.
- **Immutability**: All dict fields wrapped in `_FrozenDict`; tuples for ordered collections.
- **Determinism**: Repeated encoding of the same `CanonicalEvent` produces identical bytes.

### Limitations

- No schema validation on decode (msgspec validates types but not semantic constraints).
- No content-type negotiation.
- No compression or encoding options.

---

## 5. Validation

### What Is Validated

| Invariant | Enforced By | Phase |
|-----------|-------------|-------|
| `event_id` non-empty string | `CanonicalEvent.__post_init__` | Construction |
| `event_kind` non-empty string | `CanonicalEvent.__post_init__` | Construction |
| `schema_version >= 1` | `CanonicalEvent.__post_init__` | Construction |
| `timestamp` timezone-aware | `CanonicalEvent.__post_init__` | Construction |
| `depth >= 0` | `CanonicalEvent.__post_init__` | Construction |
| `lineage` not None | `CanonicalEvent.__post_init__` | Construction |
| `relations` not None | `CanonicalEvent.__post_init__` | Construction |
| `lineage` items non-empty strings | `CanonicalEvent.__post_init__` | Construction |
| `relation_type` in known set | `EventRelation.__post_init__` | Construction |

### What Is NOT Validated

| Not Validated | Notes |
|---------------|-------|
| `event_id` is UUIDv7 | Only checked for non-empty string |
| `event_kind` is registered | Any non-empty string accepted; `is_registered()` available for optional checking |
| Payload structure per kind | Payload is opaque at this layer; schema validators registered via `SchemaRegistry` |
| `parent_event_id` references | No referential integrity check |
| `lineage` ordering | Items are checked for validity but not for chronological ordering |
| `lineage` / `parent_event_id` consistency | Not enforced; `parent_event_id` may or may not appear in `lineage` |

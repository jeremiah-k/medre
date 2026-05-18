# Phase 1 Limitations

> Document version: 3
> Last updated: 2026-05-08

This document explicitly records what Phase 1 does **not** implement, what is reserved for future phases, and what behavioral contracts are locked in as stable.

---

## 0.5. Delivery Failure Semantics (Track 3)

### What Phase 1 Implements

| Feature                   | Location                                              | Status                                                                                                                                                                              |
| ------------------------- | ----------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Delivery failure taxonomy | `DeliveryFailureKind` enum                            | 9 categories: PLANNER_FAILURE, RENDERER_FAILURE, ADAPTER_TRANSIENT, ADAPTER_PERMANENT, ADAPTER_MISSING, TARGET_NOT_FOUND, DEADLINE_EXCEEDED, CAPACITY_REJECTION, SHUTDOWN_REJECTION |
| RetryExecutor             | `RetryExecutor` class                                 | Backoff computation, exhaustion detection, retry/dead-letter receipt construction                                                                                                   |
| Receipt lineage           | `DeliveryReceipt.attempt_number`, `parent_receipt_id` | Explicit 1-indexed attempt numbering and parent linkage                                                                                                                             |
| Lineage persistence       | `delivery_receipts` table columns                     | `attempt_number INTEGER NOT NULL DEFAULT 1`, `parent_receipt_id TEXT`                                                                                                               |
| Lineage query             | `list_receipts_for_plan()`                            | Returns receipts ordered by attempt_number for a plan+adapter pair                                                                                                                  |
| Target-scoped failure     | `deliver_to_targets`                                  | Each target classified independently; failure_kind on DeliveryOutcome                                                                                                               |
| Dead-letter receipts      | `deliver_to_target`                                   | On retry exhaustion, appends `dead_lettered` receipt after primary receipt                                                                                                          |
| Deadline check            | `deliver_to_target`                                   | Checks `plan.deadline` before rendering                                                                                                                                             |
| Failure classification    | `RetryExecutor.classify_failure()`                    | Static taxonomy classifier used by pipeline                                                                                                                                         |

### Phase 1 Delivery Failure Guarantees

| Guarantee                    | Description                                                                          |
| ---------------------------- | ------------------------------------------------------------------------------------ |
| Failure taxonomy complete    | Every delivery failure is classified into one of 6 `DeliveryFailureKind` members     |
| Receipt lineage ordered      | Receipts linked by `attempt_number` and `parent_receipt_id`; queryable in order      |
| Target-scoped isolation      | One target's failure does not affect sibling targets; each has its own receipt chain |
| Append-only receipts         | Dead-letter receipt appended AFTER primary receipt; ordering preserved               |
| Deterministic classification | Transient vs permanent classification uses exception type taxonomy, not heuristics   |
| Retry decisions are pure     | `RetryExecutor` is stateless; no side effects in backoff/exhaustion computation      |

### What Phase 1 Does NOT Implement for Delivery Failure

- **RetryWorker is opt-in only (disabled by default).** The `RetryWorker` background scheduler exists and handles `ADAPTER_TRANSIENT` failures when a `RetryPolicy` is configured on the route or delivery plan. Without a `RetryPolicy`, transient failures are not automatically retried. The RetryWorker does not restart failed adapters, does not attempt non-transient failure kinds, and does not confirm final delivery ACK from the remote side. Manual replay via `BEST_EFFORT` mode remains available for operator-initiated re-delivery.
- **No retry budget or rate limiting.** No per-adapter or per-plan retry rate limiting.
- **No dead-letter queue management.** Dead-lettered events are recorded as receipts but no admin interface or reprocessing UI exists.
- **No receipt deduplication.** Replay against events with existing receipts may produce duplicate receipts.
- **No adapter-level error customization.** Error classification uses Python exception types; adapters cannot declare custom retryable/permanent error codes.

---

## 0. Replay Determinism (Track 1)

### Replay Determinism: What Phase 1 Implements

Five replay modes with explicit, testable guarantees:

| Mode          | Stages                                     | Side Effects     | Error Handling |
| ------------- | ------------------------------------------ | ---------------- | -------------- |
| `STRICT`      | store (verify only)                        | None             | Re-raise       |
| `RE_RENDER`   | store, render                              | None             | Re-raise       |
| `RE_ROUTE`    | store, route, plan                         | None             | Re-raise       |
| `BEST_EFFORT` | store, route, plan, render, deliver        | Adapter delivery | Capture errors |
| `DRY_RUN`     | store, route, plan, render, deliver (skip) | None             | Re-raise       |

### Phase 1 Replay Guarantees

| Guarantee                           | Description                                                               |
| ----------------------------------- | ------------------------------------------------------------------------- |
| Immutability                        | Replay never mutates historical `CanonicalEvent` instances                |
| No storage writes (non-BEST_EFFORT) | STRICT, RE_RENDER, RE_ROUTE, DRY_RUN produce zero storage side effects    |
| Deterministic ordering              | Results yielded in storage query order or correlation_id list order       |
| Lineage preservation                | Every `ReplayResult` carries the source event's lineage tuple             |
| Diagnostician wiring                | Optional `Diagnostician` records skips, downgrades, failures              |
| target_adapters filtering           | Delivery plans filtered by adapter name; opaque plans passed through      |
| Schema version acceptance           | Events with `schema_version >= CURRENT_SCHEMA_VERSION` pass STRICT replay |

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

| Guarantee                                      | Description                                                                                                 |
| ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| New fields append with defaults                | Future schema versions add fields; existing consumers read `v1` fields normally                             |
| Existing fields may be deprecated, not removed | A deprecated field remains populated for at least one version cycle once a stability guarantee is in effect |
| Unknown fields preserved                       | msgspec skips unknown struct fields during decode (forward compatibility)                                   |
| `schema_version >= 1`                          | Enforced at construction; the minimum valid version is 1                                                    |

### What Phase 1 Does NOT Do

- No automatic payload migration on decode
- No schema negotiation between adapters and runtime
- No deprecation warnings at runtime
- No schema version downgrade logic
- Adapters are responsible for producing events at the version they support

---

## 2. Protocol-Neutral Readiness (Track 5)

This section documents how the existing canonical event model, metadata namespaces, and adapter contracts support future externally initiated adapters (webhooks, request/response systems) without requiring schema changes. No concrete transport, server, or auth framework is built here. The goal is verified readiness: the mechanisms exist, they survive round-trip serialization, and their usage conventions are locked in.

### 2.1 What Exists Now

The canonical event model is transport-agnostic by design. Every concept needed for externally initiated adapters maps to an existing field or namespace:

| Concept                  | Canonical Location                                      | Status                                                     | Round-Trip Verified |
| ------------------------ | ------------------------------------------------------- | ---------------------------------------------------------- | ------------------- |
| Correlation IDs          | `CanonicalEvent.trace_id`                               | Optional `str`, survives JSON/msgpack                      | Yes                 |
| Idempotency keys         | `EventMetadata.custom["idempotency_key"]`               | Convention, `_FrozenDict`-protected                        | Yes                 |
| Principal/auth context   | `EventMetadata.custom["principal"]`                     | Reserved dict slot, not populated                          | Yes                 |
| Request/response lineage | `CanonicalEvent.parent_event_id` + `lineage`            | Mechanism exists, immutable tuples                         | Yes                 |
| Inbound provenance       | `CanonicalEvent.source_adapter` + `source_transport_id` | Always populated, extensible for new transports            | Yes                 |
| Transport protocol       | `TransportMetadata.protocol`                            | Free-form `str`, already used for `"mqtt"`, `"lxmf"`, etc. | Yes                 |
| Gateway identity         | `TransportMetadata.gateway_id`                          | Optional `str` for relay/proxy identification              | Yes                 |
| Native field passthrough | `NativeMetadata.data`                                   | Adapter-specific opaque dict, `_FrozenDict`-protected      | Yes                 |
| Plugin extensibility     | `EventMetadata.custom`                                  | Reverse-DNS namespaced key-value pairs                     | Yes                 |
| Event kind extensibility | `plugin.custom` kind + `KNOWN_KINDS`                    | Plugin-defined events without schema changes               | Yes                 |

### 2.2 Usage Patterns for Future Externally Initiated Adapters

These patterns show how a future adapter would populate existing fields. They are conventions, not enforced contracts. No adapter implementing these patterns exists in Phase 1.

#### Correlation via `trace_id`

A future HTTP webhook adapter receives a request with an `X-Correlation-ID` header. The adapter maps it to `trace_id`:

```python
CanonicalEvent(
    ...,
    trace_id=inbound_headers.get("x-correlation-id"),  # or generated UUIDv7
)
```

The `trace_id` field is optional (`str | None`), so events without correlation context simply leave it as `None`. Downstream consumers and the delivery pipeline can use `trace_id` to correlate events across hops without inspecting metadata.

#### Idempotency via `metadata.custom`

A future adapter receives a webhook with an `Idempotency-Key` header. The adapter stores it in the existing `custom` namespace:

```python
EventMetadata(
    custom={
        "idempotency_key": "req_abc123",
        # other custom keys as needed
    }
)
```

Deduplication logic (to be built in a future phase) would look up `metadata.custom["idempotency_key"]` in storage before processing. The key namespace is flat under `custom`, following the same reverse-DNS convention used by plugins.

#### Principal/Auth Context via `metadata.custom`

A future adapter receiving authenticated requests maps the caller identity:

```python
EventMetadata(
    custom={
        "principal": {
            "type": "bearer_token",
            "subject": "service-account-42",
            "claims": {"role": "operator"},
        },
    }
)
```

The shape of the `principal` dict is a future adapter concern, not a canonical contract. The `custom` namespace provides the container. No auth framework, token validation, or permission checking exists in Phase 1.

#### Request/Response Lineage

A future request/response adapter creates a response event linked to the request:

```python
# Request event
request_event = CanonicalEvent(
    event_id="req-001",
    ...,
    parent_event_id=None,
    lineage=(),
)

# Response event
response_event = CanonicalEvent(
    event_id="resp-001",
    ...,
    parent_event_id="req-001",
    lineage=("req-001",),
)
```

The existing `parent_event_id` and `lineage` fields support this pattern directly. The `lineage` tuple is append-only and immutable, preserving the full chain from origin to current event.

#### Inbound Provenance

A future webhook adapter identifies itself and the external caller:

```python
CanonicalEvent(
    ...,
    source_adapter="webhook-incoming",     # Adapter instance name
    source_transport_id="api-client-42",   # External caller identity
    source_channel_id="/webhooks/alerts",  # Endpoint path or channel
)
```

These three fields are already required or optional on every `CanonicalEvent`. A new adapter simply populates them with values meaningful to its transport. The routing engine and identity resolver treat them the same as any other adapter's values.

### 2.3 What Phase 1 Does NOT Implement

The following do not exist anywhere in Phase 1. This list is exhaustive for Track 5:

- No HTTP server, webhook listener, or REST API endpoint
- No RPC framework, gRPC service, or request/response handler
- No authentication or authorization framework
- No token validation, API key checking, or permission system
- No webhook configuration, secret management, or signature verification
- No Matrix transport implementation
- No concrete transport adapters (only the event model and contracts)
- No protocol-specific fields beyond what adapters define in `metadata.native`
- No inbound rate limiting per source
- No request routing or URL dispatching
- No TLS termination or certificate management
- No admin API or management interface
- No plugin ecosystem beyond boundary scaffolding (see Section 5.3 of the plugin API contract)
- No real transports, no webhook server, no auth framework
- RetryWorker is opt-in (disabled by default); no active adapter restart; no final delivery ACK
- No receipt deduplication during replay

### 2.4 Verified Test Coverage

The following existing test coverage validates that the protocol-neutral mechanisms work correctly through construction, serialization, and immutability:

| Test Class                                             | What It Verifies                                                                |
| ------------------------------------------------------ | ------------------------------------------------------------------------------- |
| `TestCanonicalEvent.test_construction_with_all_fields` | `trace_id` survives construction                                                |
| `TestCanonicalEvent.test_default_optional_fields`      | `trace_id` defaults to `None`                                                   |
| `TestJsonRoundTrip`                                    | `trace_id`, `metadata.custom`, `lineage` survive JSON encode/decode             |
| `TestMsgpackRoundTrip`                                 | Same fields survive msgpack encode/decode                                       |
| `TestImmutability`                                     | All protocol-neutral fields are deeply frozen after construction                |
| `TestConstructorInputIsolation`                        | Mutable inputs (dicts for `custom`, lists for `lineage`) are defensively copied |
| `TestEventMetadata.test_full_metadata`                 | All sub-namespaces including `custom` and `native` populate correctly           |
| `TestUnknownMetadataFields`                            | Unknown keys in `custom` are preserved through round-trip                       |

### 2.5 Custom Namespace Convention for External Adapters

Future externally initiated adapters should namespace their `custom` keys to avoid collisions:

| Key Pattern       | Purpose                                 | Example                                   |
| ----------------- | --------------------------------------- | ----------------------------------------- |
| `idempotency_key` | Deduplication key from external request | `"req_abc123"`                            |
| `principal`       | Authenticated caller identity           | `{"type": "bearer", "subject": "svc-42"}` |
| `http.method`     | HTTP verb (if applicable)               | `"POST"`                                  |
| `http.path`       | URL path (if applicable)                | `"/webhooks/alerts"`                      |
| `http.headers.*`  | Selected inbound headers                | `{"content-type": "application/json"}`    |
| `ext.service`     | External service name (if applicable)   | `"AlertService"`                          |
| `ext.method`      | External method name (if applicable)    | `"SendAlert"`                             |

These are conventions, not enforced fields. Adapters populate only what they need. The `custom` dict is frozen at construction and survives all serialization paths.

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

| Spec Kind              | Code Equivalent                                        |
| ---------------------- | ------------------------------------------------------ |
| `telemetry`            | `telemetry.received`                                   |
| `position`             | `telemetry.position`                                   |
| `presence`             | `presence.changed`                                     |
| `delivery.receipt`     | Tracked via `DeliveryReceipt` records, not event kinds |
| `plugin.event`         | `plugin.custom`                                        |
| `metrics.update`       | Not implemented (future)                               |
| `channel.announcement` | Not implemented (future)                               |
| `transform.output`     | Not implemented (future)                               |
| `policy.action`        | Not implemented (future)                               |

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

| Invariant                         | Enforced By                    | Phase        |
| --------------------------------- | ------------------------------ | ------------ |
| `event_id` non-empty string       | `CanonicalEvent.__post_init__` | Construction |
| `event_kind` non-empty string     | `CanonicalEvent.__post_init__` | Construction |
| `schema_version >= 1`             | `CanonicalEvent.__post_init__` | Construction |
| `timestamp` timezone-aware        | `CanonicalEvent.__post_init__` | Construction |
| `depth >= 0`                      | `CanonicalEvent.__post_init__` | Construction |
| `lineage` not None                | `CanonicalEvent.__post_init__` | Construction |
| `relations` not None              | `CanonicalEvent.__post_init__` | Construction |
| `lineage` items non-empty strings | `CanonicalEvent.__post_init__` | Construction |
| `relation_type` in known set      | `EventRelation.__post_init__`  | Construction |

### What Is NOT Validated

| Not Validated                             | Notes                                                                              |
| ----------------------------------------- | ---------------------------------------------------------------------------------- |
| `event_id` is UUIDv7                      | Only checked for non-empty string                                                  |
| `event_kind` is registered                | Any non-empty string accepted; `is_registered()` available for optional checking   |
| Payload structure per kind                | Payload is opaque at this layer; schema validators registered via `SchemaRegistry` |
| `parent_event_id` references              | No referential integrity check                                                     |
| `lineage` ordering                        | Items are checked for validity but not for chronological ordering                  |
| `lineage` / `parent_event_id` consistency | Not enforced; `parent_event_id` may or may not appear in `lineage`                 |

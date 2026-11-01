# Accepted context ownership contract

> **Classification:** Normative
> **Authority:** Ownership boundary for context persisted about an accepted
> event: the three-way partition into durable correlation/decision facts,
> privately owned frozen work snapshots, and opaque independently owned
> content, with the exact field-level inventory, terminal dispositions, and
> fail-closed reconstruction rules for `canonical_events.metadata` and
> `delivery_outbox.metadata`.
> **Audience:** Runtime builders, storage authors, reviewers.

## Conformance

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHALL**, **SHALL NOT**,
**SHOULD**, **SHOULD NOT**, **RECOMMENDED**, **MAY**, and **OPTIONAL** in this
document are to be interpreted as described in RFC 2119.

## 1. Scope

Delivering one accepted event persists context in three surfaces: the
serialized opaque `EventMetadata` namespaces in `canonical_events.metadata`
and the typed `event_routing_facts` rows, both written by the admission path
(`_serialize_metadata` and `routing_facts_op` in
`src/medre/core/storage/sqlite/serde.py`, applied by `admit_ingress` in
`src/medre/core/storage/sqlite/_ingress.py`), and the
route/destination/decision JSON object in `delivery_outbox.metadata`, written
by `OutboxManager.create_for_delivery`
(`src/medre/core/engine/pipeline/outbox_manager.py`). This contract partitions
every persisted field of those surfaces into three ownership classes, fixes
which statuses retire which fields, and defines fail-closed reconstruction
for frozen work snapshots.

This contract grants no collection-scope expansion. The
[storage column classification contract](storage-column-classification.md)
remains the column-level authority this page builds on: both metadata
surfaces carry the `independently_owned_content` class there, and this page
changes no column class, no probe, and no collector decision. The
[content copy classification](content-copy-classification.md) and the
[content collection guard](content-collection-guard.md) keep their
probe-level and collection-boundary authority unchanged.

## 2. The three ownership classes

| Class                | Meaning                                                                                                                                                 |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Fact                 | Durable, bounded, typed correlation/decision value written once from the accepted decision.                                                             |
| Frozen work snapshot | Privately owned copy of the accepted in-flight work context; exists only while retry or uncompleted native work requires it; never readable as content. |
| Opaque content       | Value whose lifetime is content ownership rather than any delivery fact.                                                                                |

The decision rule for the boundary:

- A value is a fact only when its producer writes a bounded, typed shape with
  a stable identity or decision role: identifiers, closed-vocabulary
  descriptors, numeric parameters, and scheduling bounds. Facts are retained
  correlation and recovery authority; a collector MUST NOT remove them as
  content.
- Arbitrary destination/provider metadata MUST NOT be declared content-free
  merely because it describes a route. Free-form labels, nested adapter
  metadata, origin labels, and policy allowlist values stay opaque content
  even when a route, target, or plan carried them into storage.
- The whole route configuration MUST NOT be retained where identity suffices:
  keep stable identity fields plus an optional nonreversible digest for audit
  rather than the entire route configuration by default. A digest MUST be
  nonreversible; a full config copy owned by the outbox row is a frozen work
  snapshot, not a fact.
- Every persisted field MUST hold exactly one class from this page's
  inventory. A new persisted key or namespace lands with its class and
  inventory row in the same change.

## 3. Terminal outcomes and dispositions

### 3.1 Outbox status vocabulary

The outbox status vocabulary partitions into terminal statuses (`sent`,
`cancelled`, `dead_lettered`, `abandoned`) and non-terminal statuses
(`pending`, `in_progress`, `retry_wait`, `queued`), per
`src/medre/core/engine/pipeline/delivery_state.py`. Terminal settlement is a
guarded transition; a stale callback or a superseded worker MUST NOT settle
another attempt's row.

### 3.2 Disposition per class

- **Facts** are never retired by delivery state. They persist for the row's
  lifetime and survive collection as retained identity and recovery
  authority, mirroring the `immutable_identity` and `operational_authority`
  column classes of the storage column classification contract.
- **Frozen work snapshots** retire exactly on guarded terminal settlement:
  `sent`, `cancelled`, `dead_lettered`, and `abandoned` retire the snapshot.
  `pending`, `in_progress`, `retry_wait`, and `queued` MUST NOT retire it.
  Retirement MUST NOT discard an uncertain radio or native operation that
  still needs recovery evidence, and a failed or rolled-back transition MUST
  preserve the original snapshot.
- **Opaque content** is never retired by delivery state. Its lifetime is
  content ownership; it remains until its owner's policy acts, and delivery
  terminality alone never makes it content-free.

**Current persisted shape:** the full dispatch context is inlined in
`delivery_outbox.metadata`, that column is classified
`independently_owned_content` in the registry, and no retirement path removes
the snapshot; the non-`{}` metadata value keeps the `outbox_records` family
blocker active for the row's lifetime, including after terminal settlement
and collection.

**Standing fact:** the bounded routing namespace persists as typed rows in
`event_routing_facts` — classified `immutable_identity` column by column —
written by the same admission transaction as the canonical event, never as
inline metadata JSON. The production serde serializes only the opaque
namespaces into `canonical_events.metadata`, omitting absent namespaces and
the empty `custom` mapping, so a plain routed text event serializes to `{}`:
its `opaque_envelope` blocker is inactive from admission through settlement
and collection, and it retains exactly the `outbox_records` blocker after
every target settles and all mandatory owners release. Rows admitted before
this separation keep their stored bytes: their inline routing remains the
row's own stored truth, reads coalesce to it whenever no typed row exists,
and a non-`{}` metadata value keeps the `opaque_envelope` blocker active for
the row's lifetime. The guard tests pin that baseline.

### 3.3 Fail-closed reconstruction

Reconstruction from a frozen work snapshot MUST fail closed on unknown or
changed snapshot shape. `reconstruct_retry_delivery_plan`
(`src/medre/core/engine/pipeline/retry_plan.py`) restores the accepted plan
exactly and checks it against the outbox identity (event, plan, route,
adapter, channel, target membership, destination equality, target identity,
and a bounded route trace); any disagreement raises instead of proceeding.
Never-dispatched work without a complete snapshot raises instead of
defaulting. No recovery path MAY fall back to the current router
configuration, invent a destination, or guess a strategy when the required
frozen context is absent or malformed; snapshot failure is terminal to
reconstruction.

### 3.4 Authorized reads after retirement

After retirement, an authorized read MUST NOT return retired snapshot bytes:
the snapshot is absent, not empty. Metadata-only outbox projections never
return snapshot bytes at any time. An authorized owner-scoped event read
returns the event, never the retired work snapshot. A content read for
collected content returns `unavailable`, never a value fabricated from
current configuration.

## 4. Field-level inventory

### 4.1 `canonical_events.metadata` — serialized `EventMetadata` and typed routing facts

Producer: the source adapter supplies the namespaces at admission; the
admission path serializes the opaque namespaces of `EventMetadata` into the
column and writes any routing namespace as a typed `event_routing_facts` row
in the same transaction (`_serialize_metadata` and `routing_facts_op` in
`src/medre/core/storage/sqlite/serde.py`, applied by `admit_ingress` in
`src/medre/core/storage/sqlite/_ingress.py` and by `append` in
`src/medre/core/storage/sqlite/_event.py`). The serialized top-level key set
is exactly the opaque namespaces the event carried: `transport`, `radio`,
`telemetry`, `native`, `custom`. Absent namespaces and the empty `custom`
mapping are omitted, so an event without opaque metadata serializes to `{}`.
The `routing` namespace never persists inline.

| Namespace   | Producer (source)                                                                                                                                                                                 | Consumers (verified)                                                                                                                                                                                                                                             | Class                                           |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| `transport` | Source adapter at admission (`TransportMetadata`: `protocol`, `substrate`, `gateway_id`, `delivery_method`, `delivery_confirmed`, `transport_encrypted`, `signature_valid`, `propagation_state`). | No runtime consumer in core today; round-trips through the storage serde and the metadata-only projections (`get_metadata`, `query_metadata`).                                                                                                                   | Opaque content                                  |
| `routing`   | Router matching during pipeline enrichment (`RoutingMetadata`: `matched_routes`, `fanout_group`, `route_trace`). Persisted as typed `event_routing_facts` columns at admission.                   | `OutboxManager.create_for_delivery` staging (`outbox_manager.py`); delivery-coordinator loop prevention over `route_trace` (`delivery_coordinator.py`); replay route narrowing (`engine/replay/routing.py`); rendering attribution (`rendering/attribution.py`). | Fact (bounded route identifiers and group name) |
| `radio`     | Radio adapters at admission (`RadioMetadata`: `snr`, `rssi`, `channel_index`, `frequency`).                                                                                                       | No runtime consumer in core today; round-trips through the serde and metadata-only projections.                                                                                                                                                                  | Opaque content                                  |
| `telemetry` | Telemetry producers at admission (`TelemetryMetadata.metrics`: free-form scalar mapping).                                                                                                         | No runtime consumer in core today; round-trips through the serde and metadata-only projections.                                                                                                                                                                  | Opaque content                                  |
| `native`    | Source adapter at admission (`NativeMetadata.data`: arbitrary mapping).                                                                                                                           | Pipeline native-reference construction copies `native.data` into `NativeMessageRef.metadata` (`engine/pipeline/runner.py`).                                                                                                                                      | Opaque content                                  |
| `custom`    | Plugins and extensions (free-form mapping).                                                                                                                                                       | No runtime consumer in core today; round-trips through the serde and metadata-only projections.                                                                                                                                                                  | Opaque content                                  |

Post-terminal disposition: each opaque namespace persists with the canonical
event row and holds the `opaque_envelope` wholesale blocker while present;
the collector's delete set never includes the column. Operator recovery reads
namespace values only under owner-scoped event authority; metadata-only
inspection returns structure without granting content. The `routing`
namespace is a fact by type and role — bounded route identifiers — and
**Standing fact:** it persists as typed `event_routing_facts` rows
(`event_id`, `matched_routes`, `fanout_group`, `route_trace` bounded to at
most 16 entries, `recorded_at`), every column classified
`immutable_identity`; a row exists exactly when the admitted event carried
the routing namespace, and duplicate admissions never rewrite it. Production
reads compose `EventMetadata` from the typed row plus the opaque metadata
object: the typed row wins whenever it exists, otherwise the routing value
inline in the metadata JSON is the row's own stored truth and still serves
consumers. Rows admitted before this separation keep their stored bytes and
remain conservative blockers while their metadata value is non-`{}`.

### 4.2 `delivery_outbox.metadata` — outbox JSON keys

Producer: `OutboxManager.create_for_delivery`
(`src/medre/core/engine/pipeline/outbox_manager.py`). The persisted key set
for a destination-addressed staged target is exactly `destination_kind`,
`destination_hash`, `destination_name`, `destination_metadata`,
`capability_level`, `delivery_strategy`, `capability_field`,
`capability_reason`, `deadline`, `dispatch_context`; a channel-addressed
target omits the four `destination_*` keys.

| Key                    | Producer (value source)                                                                             | Consumers (verified)                                                                                                                                                                             | Class                                     |
| ---------------------- | --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------- |
| `destination_kind`     | `target.destination.kind`                                                                           | `_destination_from_outbox` and the snapshot identity checks in `retry_plan.py`.                                                                                                                  | Fact (addressing-scheme descriptor)       |
| `destination_hash`     | `target.destination.destination_hash`; mirrored into `delivery_outbox.target_address` at write time | `_destination_from_outbox` and the snapshot identity checks in `retry_plan.py`.                                                                                                                  | Fact (stable destination identity)        |
| `destination_name`     | `target.destination.destination_name`                                                               | `_destination_from_outbox` in `retry_plan.py`.                                                                                                                                                   | Opaque content (free-form provider label) |
| `destination_metadata` | `target.destination.metadata` (arbitrary adapter addressing values)                                 | `_destination_from_outbox` in `retry_plan.py`.                                                                                                                                                   | Opaque content                            |
| `capability_level`     | `route_plan.capability_level`                                                                       | `reconstruct_retry_delivery_plan` (required key, closed vocabulary); terminal receipts via `capability_receipt_fields` (`receipt_factory.py`).                                                   | Fact (closed decision vocabulary)         |
| `delivery_strategy`    | `route_plan.primary_strategy.method`                                                                | `reconstruct_retry_delivery_plan` (required key, closed vocabulary); terminal receipts via `capability_receipt_fields`.                                                                          | Fact (closed decision vocabulary)         |
| `capability_field`     | `route_plan.capability_field`                                                                       | `reconstruct_retry_delivery_plan` (required key); terminal receipts via `capability_receipt_fields`.                                                                                             | Fact (named capability field)             |
| `capability_reason`    | `route_plan.capability_reason`                                                                      | `reconstruct_retry_delivery_plan` (required key); terminal receipts via `capability_receipt_fields` into `delivery_receipts.capability_reason`, itself classified `independently_owned_content`. | Opaque content (free-form text)           |
| `deadline`             | `route_plan.deadline` (timezone-aware ISO 8601 string or `None`)                                    | `reconstruct_retry_delivery_plan` (required key, validated).                                                                                                                                     | Fact (scheduling bound)                   |
| `dispatch_context`     | `msgspec.to_builtins(OutboxDispatchContext(...))` for `pending=True` staging                        | `reconstruct_retry_delivery_plan` via the retry worker (`src/medre/runtime/retry.py`).                                                                                                           | Frozen work snapshot                      |

Post-terminal disposition and operator recovery follow the class rules of
§3.2: the fact keys persist with the row and survive collection as identity
and recovery authority; `destination_name`, `destination_metadata`, and
`capability_reason` block the `outbox_records` family while present and are
read only under owner-scoped authority; **Current persisted shape:** the
`dispatch_context` snapshot is inlined in the same JSON object, blocks
collection as `independently_owned_content` through that family's content
probe, and no retirement path removes it after terminal settlement.

### 4.3 `dispatch_context` payload — `OutboxDispatchContext`

`OutboxDispatchContext` (`src/medre/core/engine/pipeline/retry_plan.py`)
freezes three members for a staged obligation: `route` (serialized `Route`),
`plan` (serialized `DeliveryPlan`), and `routing` (serialized
`RoutingMetadata`). The snapshot exists only while retry or uncompleted
native work requires the accepted plan; its consumer is
`reconstruct_retry_delivery_plan`, called by the retry worker
(`src/medre/runtime/retry.py`).

| Member                                                                                       | Class                                              | Note                                                                                                                                                                                                                                                                                                                                      |
| -------------------------------------------------------------------------------------------- | -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `route.id`                                                                                   | Fact                                               | Accepted route identifier; checked against the outbox row and `routing.matched_routes`.                                                                                                                                                                                                                                                   |
| `route.source.adapter`, `route.source.event_kinds`, `route.source.channel`                   | Fact                                               | Bounded accepted source selector.                                                                                                                                                                                                                                                                                                         |
| `route.source.origin_label`                                                                  | Opaque content                                     | Free-form source-context label retained verbatim.                                                                                                                                                                                                                                                                                         |
| `route.targets[*].adapter`, `.channel`, `.destination.kind`, `.destination.destination_hash` | Fact                                               | Stable target identity, mirrored by the row's identity columns.                                                                                                                                                                                                                                                                           |
| `route.targets[*].destination.destination_name`, `.destination.metadata`                     | Opaque content                                     | Free-form provider label and arbitrary nested addressing values.                                                                                                                                                                                                                                                                          |
| `route.priority`, `.fanout_strategy`, `.ownership`, `.enabled`, `.forward_typing`            | Fact                                               | Bounded typed descriptors of the accepted route.                                                                                                                                                                                                                                                                                          |
| `route.policy` (allowlist values: source/destination adapters, rooms, channels, senders)     | Opaque content                                     | Arbitrary provider identifiers retained verbatim; the accepted decision is the fact, the allowlist values are not.                                                                                                                                                                                                                        |
| `plan.plan_id`, `.event_id`, `.route_id`                                                     | Fact                                               | Checked against the outbox identity.                                                                                                                                                                                                                                                                                                      |
| `plan.target`                                                                                | Fact / opaque content split as for `route.targets` | Same identity checks as the route target.                                                                                                                                                                                                                                                                                                 |
| `plan.primary_strategy.method`, `.fallback_chain[*].method`                                  | Fact                                               | Closed strategy vocabulary of the accepted plan.                                                                                                                                                                                                                                                                                          |
| `plan.primary_strategy.max_retries`, `.timeout_seconds`, `.retry_policy.*`                   | Fact                                               | Numeric parameters of the accepted plan.                                                                                                                                                                                                                                                                                                  |
| `plan.deadline`                                                                              | Fact                                               | Scheduling bound of the accepted plan.                                                                                                                                                                                                                                                                                                    |
| `plan.target_identity`                                                                       | Fact (stable identity text)                        | Canonical JSON identity of the target (`delivery_target_identity`), recomputed and compared during reconstruction. **Current persisted shape:** the serialized text embeds the destination label and metadata values rather than a nonreversible digest, so any standalone audit anchor under §2's digest rule uses a nonreversible form. |
| `plan.capability_level`, `.capability_field`                                                 | Fact                                               | Closed decision vocabulary.                                                                                                                                                                                                                                                                                                               |
| `plan.capability_reason`                                                                     | Opaque content                                     | Free-form text.                                                                                                                                                                                                                                                                                                                           |
| `routing.matched_routes`, `.route_trace`                                                     | Fact                                               | Bounded route identifiers; `route_trace` is bounded (at most 16 entries).                                                                                                                                                                                                                                                                 |
| `routing.fanout_group`                                                                       | Fact                                               | Bounded group identifier, same standing as route-target channel text.                                                                                                                                                                                                                                                                     |

The snapshot's presence and read authority belong to the exact worker,
attempt, and generation that own the outbox row; a metadata-only outbox read
never returns its bytes. **Current persisted shape:** the snapshot is a JSON
value inside `delivery_outbox.metadata`, so today it is readable wherever the
row's metadata is readable and is classified as one opaque column value
rather than as a separately owned, independently retired representation.

## 5. Guard tests

`tests/test_accepted_context_contract.py` pins this contract:

- the persisted `delivery_outbox.metadata` key set, staged through the real
  `OutboxManager.create_for_delivery(pending=True)` path on real SQLite
  storage, equals the §4.2 inventory exactly — any new key fails the guard
  until it is inventoried;
- the serialized `EventMetadata` top-level key set written through the
  production admission path equals the §4.1 opaque namespace inventory for
  the fully-populated fixture, and a routing-only event serializes to `{}`
  with its routing decision persisted as typed `event_routing_facts` rows;
- `STORAGE_COLUMN_CLASSES["delivery_outbox"]["metadata"]` and
  `STORAGE_COLUMN_CLASSES["canonical_events"]["metadata"]` are both
  `INDEPENDENTLY_OWNED_CONTENT`, so this page's opaque-content designation
  and the classification registry cannot drift apart;
- the production-path blocker baseline for a staged routed text event: the
  staged blocker set (`active_ingress`, `active_outbox`,
  `unsettled_mandatory_owners`, `outbox_records`) and the post-settlement
  residual set (`outbox_records`) are pinned exactly, arbitrary
  `custom`/`native` metadata is pinned as opaque content that no change in
  this contract's stack may exempt, and
  `tests/test_admission_routing_facts.py` pins the typed-facts separation —
  the composed reads, the historical-row conservatism, and the
  semantic-equivalence of the composition.

## 6. Relationship to other contracts

- [storage-column-classification.md](storage-column-classification.md) is the
  column-level authority this page builds on; both metadata columns carry
  `independently_owned_content` there, and this page grants no
  collection-scope expansion.
- [content-copy-classification.md](content-copy-classification.md) defines
  the probe-level blocker distinctions; the `opaque_envelope` and
  `outbox_records` blockers reported here follow it.
- [content-collection-guard.md](content-collection-guard.md) defines the
  atomic collection boundary; the dispositions in §3 compose with its delete
  set, which this page does not change.
- [metadata.md](metadata.md) defines the namespace vocabulary; this page
  fixes the ownership class of each namespace's persisted form.

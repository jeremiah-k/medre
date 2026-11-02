# Storage column classification contract

> **Classification:** Normative
> **Authority:** Column-level classification contract over every persistent
> storage table and column: the four ownership classes, the declared
> registry, and the classification requirement for new columns.
> **Audience:** Runtime builders, storage authors, reviewers.

## Conformance

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHALL**, **SHALL NOT**,
**SHOULD**, **SHOULD NOT**, **RECOMMENDED**, **MAY**, and **OPTIONAL** in this
document are to be interpreted as described in RFC 2119.

## 1. Scope

Every persistent column of every MEDRE-owned storage table carries exactly one
ownership class from a closed four-member vocabulary. The declared registry in
`src/medre/core/storage/classification.py` is the machine-readable authority
for this contract; the schema-walk guard test proves it covers the real SQLite
schema and the equivalence guard test proves it agrees with the enforced
content inventory and the collector's conservative decisions.

This contract classifies columns only. It introduces no access path, no
deletion, no migration, and no collector behavior change. A column class
never authorizes any read, retention action, or collection.

## 2. The four classes

| Class                         | Ownership semantics                                          |
| ----------------------------- | ------------------------------------------------------------ |
| `immutable_identity`          | Written once, never rewritten; identifies or correlates.     |
| `operational_authority`       | Durable delivery/recovery facts guarded transitions rely on. |
| `independently_owned_content` | Content whose lifetime is content ownership, not delivery.   |
| `unclassified`                | Not yet reviewed; treated as potentially content-bearing.    |

1. **Immutable identity** — event IDs, native message IDs, relation targets,
   attempt fingerprints, plan and receipt identifiers, route-target text
   (`delivery_outbox.target_channel`/`target_address`, mirroring the receipt
   family's route correlation), the typed admission routing facts of
   `event_routing_facts` (`matched_routes`, `fanout_group`, and the bounded
   `route_trace`: write-once route identifiers separated from the opaque
   metadata JSON), the outbox work snapshot's identity and shape columns
   (`delivery_outbox_snapshots.outbox_id`, `attempt_number`,
   `snapshot_version`, `snapshot_kind`), closed structural descriptors of
   what a row is (including the durable ingress `provenance` admission
   marker and the receipt family's closed `source` provenance), and
   write-once recorded timestamps.
   Identity columns MAY remain while owned content around them is collected;
   they are deliberately retained correlation facts.
2. **Operational authority** — statuses, ownership and lease facts
   (`worker_id`, `lease_until`, `locked_at`), attempt generations, guarded
   state pointers (`receipt_id` on the outbox), duplicate-prevention
   authority (`payload_hash`), settlement and expiry bounds (`released_at`,
   `revoked_at`, `expired_at`, ceilings), confirmation levels, and
   rebuildable conversation membership fields
   (root, conversation, resolved target, relation type and depth). Authority
   columns decide lifecycle outcomes; a collector MUST NOT remove them as
   content.
3. **Independently owned content** — rendered text, opaque envelope JSON,
   free-form error and provider diagnostic text (including the write-time
   separated `delivery_evidence_diagnostics.diagnostic` surface), the frozen
   outbox work snapshot document
   (`delivery_outbox_snapshots.context_json`), retained blobs, radio plan
   JSON, and adapter cursor state: data whose lifetime is content ownership
   rather than any delivery fact. This class mirrors the
   enforced `CONTENT_SURFACES` inventory in
   `src/medre/core/storage/content.py` exactly; the guard test asserts set
   equality in both directions.
4. **Unclassified** — anything without a reviewed classification. Consumers
   MUST treat an `unclassified` column as potentially content-bearing, and
   the collector's conservative family blocking applies with full force.
   Every persistent column MUST eventually hold a reviewed class; a column
   MAY remain `unclassified` only while that review is outstanding.

Timestamp rule: timestamps on append-only or write-once rows are immutable
identity, including write-once scheduling bounds on append-only rows such as
`delivery_receipts.next_retry_at`, which is written once with its receipt row
and advanced only by new receipt rows, and recorded CAS guard snapshots such
as `operator_delivery_decisions.observed_updated_at`, which is written once
with its decision row; timestamps that guarded transitions compare or
advance on mutable rows (`updated_at`, lease and freshness facts, and
aggregation anchors such as `inbound_pressure_observations.first_seen_at`)
are operational authority, as are the retention activation boundary facts of
`content_retention_epochs`: `activated_at` and its `admission_watermark`
companion bound every retention sweep to events admitted after activation,
and `last_observed_active_at` is the forward-only heartbeat that guarded
heartbeat transitions advance and never move backward.

## 3. Registry requirements

- Every persistent table and every column of that table MUST have exactly one
  registry entry with a valid class. A table or column without an entry fails
  the schema-walk guard test.
- The `delivery_status` view is deliberately unclassified: it persists no
  values of its own, and its columns are classified through
  `delivery_receipts`. A new view MUST be added to the guard test's pinned
  exclusions or classified.
- The registry MUST remain declared data. Deriving classes from DDL, runtime
  behavior, or inventory code is forbidden; the equivalence guard test is
  what keeps the declared data honest.
- New persistent columns MUST land with a reviewed class in the same change.
  A reviewer SHOULD require the class assignment to be explicit in the
  change fragment.

## 4. Relationship to collection conservatism

Column classification composes with, and never replaces, the family-level
conservatism of the collector:

- A probe over a row family holding `independently_owned_content` or
  `unclassified` columns outside the assessed event's own rows in the
  collector-owned candidate tables
  (`event_body_content`, `event_relation_excerpts`) blocks collection, as
  does a probe gating on live ownership authority — including the
  `unretired_outbox_snapshots` gate, which blocks by the pure presence of a
  frozen work snapshot row: the row exists exactly until the guarded
  terminal settlement deletes it, and the gate's lifecycle authority is its
  owning `delivery_outbox` family. The exception is the five
  retained delivery-evidence families — `delivery_receipts`,
  `delivery_observations`, `delivery_outbox` (probed as `outbox_records`),
  `native_message_refs` (probed as `native_references`), and
  `durable_ingress_work` (probed as `ingress_work`): their rows survive
  collection, and the family blocks only while one of its registry
  content-potential columns carries a value on the event's own rows. The
  equivalence guard test derives the collector's blocker families from the
  registry plus the declared probe accounting and asserts set equality with
  `_WHOLESALE_BLOCKER_PROBES`, `_LIVE_BLOCKER_PROBES`, and the declared
  evidence-family mapping.
- Some families block even though none of their columns is classified as
  content or unclassified today (for example `shared_attachment_references`
  reaches `attachment_blobs.data` through a load-authority reference).
  Family-level blocking remains authoritative; column classes alone MUST
  NOT make any family collector-safe.
- The per-probe assessment labels
  (`bounded_candidate`, `shared_reference`, `identity_retained`,
  `authority_or_state`, `requires_independent_policy`, `retained_evidence`)
  emitted by `assess_event_content_release` remain probe-level vocabulary.
  `retained_evidence` marks a family whose rows survive collection and
  which blocks only on content-bearing values; it includes the
  `delivery_evidence_diagnostics` side table, which never blocks. The guard
  test pins their registry-derived equivalents exactly.

## 5. Relation to other contracts

- [storage.md](storage.md) defines the tables, DDL shape, and ownership
  summary; this contract adds the per-column class dimension over that DDL.
- [content-copy-classification.md](content-copy-classification.md) defines
  the probe-level blocker/identity distinction that this contract must
  reproduce, not supersede.
- [content-collection-guard.md](content-collection-guard.md) defines the
  atomic collection boundary; the collector-owned candidate tables in this
  contract are exactly its permitted delete set.
- [content-retention.md](content-retention.md) defines content lifetime and
  accounting; the inventory this contract mirrors is its enforced
  column-level surface list.

The machine-readable schema for a serialized registry export is
[storage-column-classification.schema.json](../schemas/storage-column-classification.schema.json).

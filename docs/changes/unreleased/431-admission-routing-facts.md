# Admission routing facts separate from opaque event metadata

The bounded routing namespace of `EventMetadata` no longer persists inside
the opaque `canonical_events.metadata` JSON object. Admission now writes it
as typed rows in a new `event_routing_facts` table — `event_id`,
`matched_routes`, `fanout_group`, and the `route_trace` bounded to at most
16 entries, plus the write-once `recorded_at` — in the same admission
transaction as the canonical event row, for both the ingress admission path
and the append path. Duplicate admissions never execute their ops, so the
originally accepted decision is written exactly once.

The production serde serializes only the opaque namespaces
(`transport`, `radio`, `telemetry`, `native`, `custom`) into the metadata
column, omitting absent namespaces and the empty `custom` mapping, so an
event without opaque metadata serializes to `{}`. A plain routed text event
therefore no longer carries the `opaque_envelope` wholesale collection
blocker. In this layer alone, historical inline outbox context remains an
`outbox_records` blocker. After the owned work snapshots of fragment 432
retire on terminal settlement, a newly admitted plain routed text event has
no residual blockers. Arbitrary `native`/`custom` values block exactly as
before.

Production reads compose `EventMetadata` from the typed facts row plus the
opaque metadata object: the metadata-only projections (`get_metadata`,
`query_metadata`), the reply-context projection, and the authorized
ingress/outbox/replay owner reads coalesce the typed row (which wins when
present) with the row's own inline routing value, so outbox staging, delivery
loop prevention, replay route narrowing, and rendering attribution keep
working through the composed event object. Rows admitted before the
separation keep their stored bytes: no inline value is rewritten, reads
coalesce to the inline routing whenever no typed row exists, and a
non-`{}` metadata value keeps `opaque_envelope` active for the row's
lifetime.

The SQLite schema version stays frozen at 1; `event_routing_facts` joins the
allowlisted additive DDL set and is created for approved additive
predecessors. Every column is classified `immutable_identity`; the table
adds no content inventory surface and no collection blocker.
`tests/test_admission_routing_facts.py` pins the separation end to end,
including the semantic equivalence of the composed read with the previous
inline serialization; `tests/test_accepted_context_contract.py` pins the
updated blocker baseline and the persisted opaque namespace key set.

Admission validates routing field types before storing facts or omitting the
inline namespace. SQLite requires route-list values to be JSON arrays, and
an executable SQL fragment pins the published constraints to the runtime DDL.

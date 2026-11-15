# Strip the retired inline diagnostic columns from delivery evidence tables

Complete the delivery diagnostic content separation: the evidence tables no
longer carry inline diagnostic columns at all. `delivery_receipts` drops
`error`, `capability_reason`, and `rendering_evidence`; `delivery_observations`
drops `error`; `delivery_outbox` drops `failure_kind_detail` and
`error_summary`; `durable_ingress_work` drops `last_error`. Every diagnostic
value lives only in the append-only `delivery_evidence_diagnostics` side
table, keyed by family, row identity, attempt generation, and field name,
written inside the parent row's transaction exactly as before.

Reads resolve every diagnostic field from the side table. Outbox lookups stay
attempt-scoped, a settled `sent`/`queued` row never surfaces a same-attempt
failure, and appended side-table rows are never rewritten. The
`delivery_status` view no longer projects any diagnostic column; typed read
APIs keep surfacing the same fields, and metadata-only projections synthesize
each diagnostic field as `NULL` so typed row mappers see a complete shape
without any diagnostic content leaving the store.

Existing pre-release databases whose evidence tables still carry the retired
columns are an explicitly allowlisted predecessor layout: initialization
copies every non-empty inline diagnostic value into the side table, rebuilds
the four tables without the columns from the authoritative DDL, recreates
their indexes, and refreshes the `delivery_status` view, all inside the open
transaction. The predecessor forms are derived from the authoritative DDL by
re-inserting the retired column lines at declared anchors, so detection cannot
drift; a mismatched layout is still rejected with recreation guidance.

The content inventory follows the storage change: `delivery_receipts` and
`durable_ingress_work` no longer declare content surfaces (their only content
columns were the retired diagnostics), and the observation and outbox surfaces
reduce to their structured `metadata` column. Side-table diagnostic rows
remain retained evidence that never blocks collection. The column
classification registry drops the retired entries, and the collection guard's
column-aware probes derive from the registry as before.

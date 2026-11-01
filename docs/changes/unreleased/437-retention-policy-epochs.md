# Declare operator content retention with durable activation epochs

The `content_retention` configuration section declares the operator
retention policy as a closed, versioned table: `version` is exactly the
integer 1, `mode` is the closed `retain`/`relay-only` vocabulary, and the
`interval_seconds`/`batch_limit` cadence knobs bound the retention worker.
The section is disabled by default; `mode: relay-only` additionally
requires `enabled: true`, so the ambiguous disabled relay-only scope is
refused at load time, while an enabled `retain` policy is a declared,
observing policy that never collects.
`MEDRE_CONTENT_RETENTION__<FIELD>` environment overrides coerce each
field and revalidate the whole section.

Activating `relay-only` records an immutable activation epoch in the
append-only `content_retention_epochs` table. The epoch's admission
watermark is the store's maximum local admission `created_at` observed
inside the same `BEGIN IMMEDIATE` transaction that inserts the row, and
every retention sweep is bounded to events admitted strictly after it, so
pre-activation history is never swept. Watermarks are monotonic across
epochs, the `last_observed_active_at` heartbeat never moves backward, and
the `activated_by` provenance is closed to `runtime-first-start` and
`operator`. The table joins the exact prerelease additive allowlist and
every column carries a reviewed classification class; the executable
`content-retention-epochs.schema.sql` fragment pins the declared
constraints to the authoritative DDL.

`SQLiteStorage.preview_content_retention` aggregates one epoch's outlook
as bounded, metadata-only outcome counts — `eligible`, `blocked`,
`empty`, `already_collected`, `inconsistent` — using the guarded
collector's own read path, so the preview and the mutation never speak
two dialects. The report carries counts, the single `next_after`
continuation, and the unmeasured copy scopes; it never returns an event
ID enumeration, message text, or any content value, and it sets
`retention_authorized`, `deletion_authorized`, and
`physical_erasure_claimed` to false. A store with no activation epoch
reports the distinct `no_active_epoch` status with zero counts: nothing
can be safely assessed, which is distinct from nothing being eligible.
`docs/spec/content-retention.md` states the declaration, activation
boundary, and preview contract. Collection is disabled by default; the
policy declares the explicit relay-only authority a LIVE worker requires.

Retention bounds and mode vocabulary share a storage-neutral contract.
Unmeasured-copy vocabulary has no SQLite import dependency. Preview paging
accepts its complete admission keyset and schema constraints match the loader.

Configuration delegates shared field validation to the policy value object.
Worker and operator freshness use one backend-neutral grace computation.

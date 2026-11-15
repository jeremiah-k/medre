# Retire the frozen outbox dispatch context through owned work snapshots

The frozen accepted work context for one outbox generation lives in the
typed, versioned, privately owned `delivery_outbox_snapshots` table instead
of `delivery_outbox.metadata`. Storage writes the snapshot row inside the
outbox row's creation transaction — including replay generation allocation —
keyed by the exact outbox identity (`outbox_id` plus the committed creation
generation read from the just-inserted row). Identity and shape columns
(`outbox_id`, `attempt_number`, `snapshot_version`, `snapshot_kind`,
`created_at`) are `immutable_identity` in the classification registry; the
frozen `context_json` document is `independently_owned_content` and joins
the enforced content inventory. The `snapshot_version` field pins the
document shape and `snapshot_kind` selects it: `staged` holds the complete
`OutboxDispatchContext` members (`route`, `plan`, `routing`); `direct`
holds the reduced route-decision context plus the destination scope keys.

New outbox rows persist `metadata = '{}'`; the destination and
route-decision context that used to be inlined is in the snapshot.
Historical rows keep their inline bytes untouched as their frozen context
and keep blocking collection through the existing registry-derived outbox
family content probe.

Retirement deletes the snapshot row only inside guarded terminal
settlement — the terminal `mark_outbox_*` transitions (`sent`,
`cancelled`, `dead_lettered`, `abandoned`) and the receipt-backed
`finalize_deferred_handoff` / `finalize_outbox_terminal` finalizers,
and the guarded operator `cancel_unstarted` decision — in the
same transaction as the status change. `pending`, `in_progress`,
`retry_wait`, and `queued` never retire it, and a stale callback, superseded
worker or generation, native-identity conflict, or failed receipt insert
rolls the retirement back. After retirement the snapshot is absent, not
empty.

Recovery reads the context through the new owner-scoped
`get_outbox_dispatch_context` backend API, which binds the row to the
outbox identity (event, plan, adapter, channel) and fails closed on a
malformed document or an unknown shape version. `reconstruct_retry_delivery_plan`
consumes that typed read, keeps every identity check, reproduces the
pre-change wire payloads exactly, and never falls back to current router
configuration; a post-change row with neither a snapshot nor historical
inline metadata fails closed. Metadata-only outbox projections never return
snapshot bytes. The receipt capability fallback that previously read inline
metadata composes its decision fields from the frozen snapshot for
post-change rows.

An unretired snapshot blocks content release as live work state through the
new `unretired_outbox_snapshots` probe in the shared release-assessment
decision, consumed by both the assessment and the collector; a retired
snapshot blocks nothing, and the canonical-envelope blocker for admitted
events is unchanged. The schema version stays frozen at 1: the table is an
allowlisted additive predecessor applied atomically like the diagnostics
side table. The accepted-context ownership contract's outbox inventories
are restated as standing facts, with the fact dispositions revised to their
new durable typed homes (outbox identity columns plus the immutable
terminal receipt).

A follow-up review also enforced a single snapshot per outbox ID, rejects
contradictory inline metadata plus a frozen snapshot at creation, and forbids
terminal rows from returning orphaned frozen context. Existing compatible
prerelease databases gain the uniqueness index through the allowlisted
additive initialization path.

Reconstruction validates complete frozen document fields, including nested
staged fields and the direct destination shape. Unknown or missing fields,
untyped destination values, mismatched durable addresses and naive deadlines
are refused before dispatch. Executable snapshot-table and owner-index DDL
is published with catalog-equivalence coverage.

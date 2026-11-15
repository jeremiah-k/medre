# Guarded local exact cancellation

`medre resolve` now has a two-step, local-only operator flow for the existing
atomic `cancel_unstarted_delivery` storage decision. Preview shows the raw
persisted outbox ID, attempt generation, status and update version for exactly
one unreserved destination; it grants no send or cancel permission. Apply
requires the operator to resupply those values, select an explicit decision ID
and provenance label, and confirm with `--yes`. A SQLite write transaction
revalidates them and atomically records the cancellation audit. Repeating one
identical decision is idempotent.

The write command refuses Windows, symlinked/hard-linked state files, files or
parent directories writable/readable by nonowners, and state not owned by the
current POSIX process user. The OS file owner is the local privilege boundary;
`--actor-label` is audit provenance, not authentication. No native sends,
replays, ambiguous-send reconciliation, or automatic retention are enabled.

Reject parent traversal and unsafe ancestor directories, and open the exact
absolute path validated for local ownership. Report initialization and audit
write failures with the storage-error exit code; a failed audit write leaves
the destination pending and produces no decision record.

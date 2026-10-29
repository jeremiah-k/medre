# Read-only operator cancellation audit

`medre inspect decisions --event EVENT --storage-path PATH` now lists immutable
cancellation decisions from the authoritative SQLite audit table, without
starting a runtime or performing native I/O. The output includes the exact
outbox/plan/adapter/channel, observed generation, status, and outbox update
`observed_updated_at` (separate from decision `recorded_at`), action,
and the actor label originally supplied by the caller.

`actor_labels_authenticated=false` is an explicit warning: an actor label is
provenance, not an authenticated account. An event with no decisions returns an
empty list; an absent event exits not-found. The command does not grant replay,
cancellation, or any other mutation permission. Operator-applied cancellation
remains an independent storage-level authority pending a trusted local caller
boundary and explicit confirmation UI.

Accept every non-empty canonical event identifier for audit inspection,
independently of the bounds on write-capable cancellation requests.

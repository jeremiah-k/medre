# Page retention candidates in bounded admission order

`SQLiteStorage.page_collection_candidates` is the read-only
`CollectionCandidateSource` seam a retention sweep consumes: each call
returns at most `limit` (1–100) event IDs in the stable
`(created_at, event_id)` ordering of the new
`idx_events_admission_order` index, so discovery never assumes event IDs
are chronological. The index joins the exact prerelease additive
allowlist: an approved additive predecessor missing it is upgraded
atomically from the authoritative DDL slice, and the upgraded catalog is
definition-identical to fresh state.

The activation watermark is a strictly-greater lower bound on every page,
an event admitted at the watermark instant is pre-activation, and a None
watermark matches the empty-store open-epoch semantics of the aggregate
preview. Continuation is the `(created_at, event_id)` keyset of the last
returned row, paging past the epoch scope returns an empty page with a
None continuation, and a new sweep cycle restarts from the scope head by
the caller's decision. The page advance is independent of per-event
collection outcomes — a permanently blocked event cannot starve later
candidates — and a page whose first row does not advance past its keyset
cursor is refused instead of looping.

Candidate selection grants no mutation authority: the selector returns
only event IDs and its ordering key, no content value leaves the
database, and the guarded per-event collector with its `BEGIN IMMEDIATE`
revalidation remains the only mutation boundary.
`docs/spec/content-retention.md` states the candidate-discovery
contract; the selector itself never collects.

Watermark comparisons normalize equivalent UTC zero-fraction spellings
without rewriting admission facts or wrapping indexed columns in SQL functions.

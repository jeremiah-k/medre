# Qualify the retention vertical end to end and measure the mixed workload

The operational content lifecycle v1 slice closes with a qualification
commit: no new product behavior, only the suites, the measurement script,
and the standing facts they establish. Three new test files drive the real
`ContentCollectionWorker` over the real `SQLiteStorage` epoch, selector,
and collector methods (the restart, cancellation, and CLI-activation cases
drive the full runtime) with injected clocks and deterministic
interleaving hooks — no sleeps. The clock/policy suite pins duplicate
activation (append-only epochs, monotonic watermarks, the worker adopting
the newest epoch), the enabled-to-disabled config change across restarts
(durable epoch, no collection while disabled, `awaiting_activation` until
an operator epoch opens), the exact `max(3600, 10 * interval)` heartbeat
staleness boundary (fresh at the threshold, stale one microsecond later),
the strictly exclusive admission watermark (an event at the watermark
instant is never a candidate, before or after activation), optional grant
behavior (a live grant blocks with the blocker surfaced, observed expiry
unblocks a later cycle, revocation and expiry tombstones stay monotonic
across worker restarts), the unexpected-extra-table open refusal with the
worker collecting nothing, three consecutive corrupt-admission-identity
refusals restricting the worker through the real storage path, the
present-empty versus absent body classification, and worker collection in
admission order over deliberately reverse-ordered event IDs.

The race suite proves the plan's invariants through real concurrency
points: a pending sibling fanout blocks and then collects after the real
dispatcher settles it; a queued native handoff keeps its frozen snapshot
until the receipt-backed finalizer settles it; an uncertain in-flight send
fences collection without being touched; a guarded terminal transition
retires the snapshot in its transaction and the context stays
unrecoverable; a hold revoked between the candidate page and the
collector's decision is honored inside the collector's own
`BEGIN IMMEDIATE` revalidation; reply excerpts owned by other events gate
the quoted event until the quoting event's own excerpt is collected;
opaque envelope bytes and content-bearing native-reference metadata block;
shared attachments and radio plans block wholesale with their rows
byte-identical; a second real connection holding the write lock surfaces
the typed transient error, defers with the cursor intact and a bounded
backoff, and changes no data; a failed candidate read defers the cycle
without skipping work; a collection committed before its sweep bookkeeping
is idempotent across restart with no resurrection and no double receipt;
a mid-scan stop ends cleanly with unexamined candidates untouched; a live
bridge adopts a CLI `storage retention activate` epoch without losing
in-flight delivery; and after worker collection the replay and native
submission paths stay denied, `NativeMessageRef` mappings still resolve,
and receipts stay byte-identical. The fake-provider sweep delivers a mixed
ordinary, reply-relating, attachment-bearing, and bodyless workload
through the durable ingress and dispatch path under the enabled
relay-only policy, proving typed routing facts, delivery evidence, native
references, and conversation identity survive collection exactly, with
plausible content-free counters in the diagnostic snapshot and runtime
snapshot. The seven-adapter cross-repo qualification remains
CI-covered in the adapter packages' own matrices.

`scripts/measure_retention_workload.py` records the bounded mixed-workload
measurement: two runs of the same seeded mix (plain text, reply-relating,
attachment-associated, and bodyless events; a configurable settled share
through the real staging and terminal transitions) with the collector
disabled and enabled, reporting phase throughput, per-event collect
duration percentiles, transient-lock occurrences, database and sidecar footprints before and after storage close, and the logical bytes removed. The report is a measurement, not a
performance verdict, and claims no physical space recovery; a smoke test
runs the script end to end with tiny parameters so the harness cannot rot.
`docs/dev/development-priorities.md` and the content lifecycle page state
the qualified order.

The workload counts actual completed staging work, requires completed sweeps
for quiescence, bounds catch-up sweeps by event and page counts, and closes
storage on refusal. WAL snapshots are recorded before close; their subsequent
growth and physical byte erasure remain unmeasured.

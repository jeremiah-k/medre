# Durable replay-run target idempotency

- Promote non-empty replay `run_id` values from receipt-only trace metadata to
  durable outbox execution provenance. Replay generation allocation now claims
  `(DeliveryIdentity, replay_run_id)` inside the same `BEGIN IMMEDIATE`
  transaction that allocates the next effective attempt, so concurrent
  executions of one named run converge on one target generation instead of
  creating sibling dispatches.
- Keep `replay_run_id` explicitly outside `DeliveryIdentity`: different or
  empty run IDs remain intentionally repeatable, while the full event / plan /
  adapter / normalized-channel identity remains the lifecycle key.
- Treat same-run duplicate execution as a skipped operation result rather than
  appending a synthetic outbox-less suppression receipt. This preserves the
  accepted delivery as current lifecycle authority when a replay command is
  repeated.
- Serialize an exact named-run delivery identity inside one MEDRE process before
  capacity admission. This closes the local race where a duplicate could otherwise
  time out on capacity before the winner created its outbox claim and persist an
  unrelated suppression receipt. The gate is process-local ordering only; the
  transactional outbox claim remains the cross-process authority.
- Preserve replay origin across RetryWorker dispatch. The retry attempt keeps
  `source="retry"` while carrying the originating `replay_run_id`, making
  dispatch mechanism and replay provenance independently observable in receipt
  history and the delivery ledger.
- Add a partial unique SQLite index for named replay claims, storage and
  in-memory conformance behavior for the same idempotency rule, and regression
  coverage for concurrent claims, repeatable distinct/empty runs, manager
  duplicate detection, retry provenance, operator projection, and schema
  shape. The prerelease storage schema version intentionally remains `1`; stale
  databases are rejected by shape validation and recreated under the existing
  prerelease policy.
- Update replay warnings/specification language: named-run target admission is
  now concurrency-safe within the shared storage database, but MEDRE still does
  not promise transport exactly-once delivery. Prior live delivery,
  different/empty run IDs, and ambiguous sends later retried or recovered can
  still redeliver.
- Make crash-window provenance visible across event/replay trace, inspect,
  recovery, evidence, and convergence surfaces. Event timelines now include
  durable outbox-generation admissions before a first receipt exists; recovery
  treats current mutable generations independently from older immutable
  authority.
- Align stale-claim recovery with diagnostics by reclaiming `in_progress` rows whose lease is missing or expired, preserving named replay provenance through the RetryWorker path.

- Harden review-time edge cases around durable named-run claims: duplicates now
  consult an existing outbox claim before mutable preflight/capacity decisions,
  row-scoped recovery diagnostics cannot borrow evidence from sibling
  generations, and terminal queue callbacks preserve replay provenance during
  the in-progress callback race without guessing finalized-row provenance.
- Keep operator projections snapshot-consistent and generation-aware: replay
  traces distinguish terminal outbox-only runs from admitted work, evidence and
  recovery reuse the event timeline's outbox snapshot, and human recovery output
  renders either receipt- or outbox-shaped supersession authority safely.

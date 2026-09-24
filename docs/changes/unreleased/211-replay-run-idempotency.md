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
- Make crash-window provenance visible across event/replay trace, inspect, recovery, evidence, and convergence surfaces. Event timelines now include durable outbox-generation admissions before a first receipt exists; recovery treats current mutable generations independently from older immutable authority.
- Align stale-claim recovery with diagnostics by reclaiming `in_progress` rows whose lease is missing or expired, preserving named replay provenance through the RetryWorker path.

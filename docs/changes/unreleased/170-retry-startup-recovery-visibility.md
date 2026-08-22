# Retry startup recovery visibility

## Runtime and diagnostics

- Runtime retry snapshots now expose `abandoned` for the current worker
  generation and `previous_run_in_progress` for durable `in_progress` outbox
  work observed before the new worker starts.
- Retry-worker startup emits `retry_unfinished_work_detected` when pre-existing
  `in_progress` rows are present. The event is diagnostic only: it does not
  claim or mutate work and does not imply that the prior worker was
  abandoned.
- Failure to read startup outbox counts is non-fatal. The snapshot records
  `previous_run_in_progress: null`, and normal retry polling/reclamation
  continues unchanged.
- The startup evidence read is bounded by a 5-second preflight budget; a
  stalled storage backend records `previous_run_in_progress: null` instead
  of blocking retry-worker startup indefinitely.
- `RetryWorker.start()` and `stop()` are now serialised by one lifecycle
  lock: concurrent `start()` calls cannot spawn duplicate workers, and
  `stop()` cannot return while startup evidence capture is still in flight.
- Boolean values in storage status counts are rejected as invalid row
  counts rather than normalised to `True`.

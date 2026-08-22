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

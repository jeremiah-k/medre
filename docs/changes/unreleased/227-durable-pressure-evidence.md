# Durable pre-admission pressure evidence

- Persist one aggregate row per (60-second window, adapter source, outcome)
  for every inbound admission gate refusal, distinguishing typed counted
  loss (`rejected`), admission-timeout loss (`timed_out`), and cursor-safe
  deferral (`deferred`) — the only durable trace of arrivals turned away
  before canonical admission, which previously vanished on restart.
- Store counters and timestamps only, never payloads, sender identity, or
  transport-native content; writes are append-only upserts (no deletes, per
  the storage module's append-only invariant) and growth is rate-bounded to
  one row per key per minute while pressure occurs.
- `acquire_inbound` now returns a truthy result object whose `reason` names
  the refusal cause (`closed`, `queue_full`, `timeout`); bool callers are
  unaffected.
- The refusal path never awaits storage: refusals aggregate into in-memory
  counters flushed by a single-flight background task as count-carrying
  upserts (at most one flush in flight; failures log and drop the batch
  without changing the refusal outcome).
- The refusal window is captured at refusal time (the pending counter key
  carries the aligned window), so a delayed flush never merges refusals
  from different windows; the single-flight flush task drains until no
  counts are pending, and shutdown performs a final flush before storage
  closes so pending counters are not lost.
- A read-only database that predates the additive table reports an empty
  pressure history instead of failing; a pressure read failure marks the
  evidence storage section partial rather than passed.
- Surface the aggregates in the evidence bundle's storage section and via
  the new read-only `medre inspect pressure` command; `list_inbound_pressure_observations`
  bounds reads in SQL when `limit` is set (newest windows, ascending order).
- Add the `inbound_pressure_observations` table as an additive SQLite table
  (no required-columns or schema-version change for existing databases).

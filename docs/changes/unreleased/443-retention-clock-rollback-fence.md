# Refuse future-dated retention heartbeat evidence

A durable activation heartbeat that is in the future relative to the local
clock no longer counts as a fresh lease. Runtime collection pauses in the
existing `awaiting_activation` state instead of sweeping indefinitely after
a clock rollback. The read-only operator status reports `unknown` freshness
for this case instead of claiming it is fresh.

Both surfaces use one core observation-age function; the existing
`max(3600, 10 * interval_seconds)` staleness threshold and conservative
operator reactivation contract remain unchanged. No stored timestamps,
policy grants, or content rows are rewritten.

Retention epoch, heartbeat and candidate-page SQLite operational failures now
use the worker's existing typed transient backoff, rather than repeatedly
logging a generic cycle error at the base poll cadence. The event collector
and the upstream retention read path share one bounded defer calculation;
semantic `StorageError` refusals are not treated as transient.

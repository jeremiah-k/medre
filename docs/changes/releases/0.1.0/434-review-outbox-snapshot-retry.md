# Outbox snapshot read-failure and review contract hardening

- Distinguish transient storage read failures from malformed or unsupported
  frozen dispatch snapshots. Retry workers now release an unstarted claim for
  a later poll and defer its adapter lane by the configured interval instead of
  terminally abandoning valid work or immediately reclaiming it in a loop.
  Corrupt or unknown-version snapshots still fail closed through lifecycle
  abandonment.
- Align the normative storage/routing/collection pages with the typed snapshot
  API, the historical inline fallback, and recognized pre-release DDL additions.
- Keep the source-level deletion safety check strict while permitting the one
  terminal-transaction snapshot retirement statement. Update the async storage
  fakes in retry race and shutdown suites and cover the new error distinction.

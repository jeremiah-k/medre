# Operators inspect and control the retention boundary

`medre storage retention status --storage-path PATH` reports the retention
policy boundary as read-only aggregate metadata: the latest durable
activation epoch (or an explicit `no_active_epoch` status), the
`last_observed_active_at` heartbeat (or `activated_at` only when the heartbeat
is absent) with an honest freshness observation,
the aggregate collected-receipt count from the new read-only
`count_collected_events` storage method, and the bounded retention preview's
outcome counts and disclaimers. The report carries no message text, no event
identifiers, and no native identifiers; unreadable or incomplete storage
fails closed with a path-free refusal. Because the operator's configured
interval is not stored in the database, the heartbeat observation states its
default-interval assumption in the report and reports `unknown` whenever
freshness cannot honestly be assessed.

`medre storage retention activate --storage-path PATH --yes` is the explicit
re/activation path: it requires `--yes` and opens a new operator activation
epoch whose admission watermark is the current admission high-water, so
pre-activation history is never swept. The activation is one small
transaction and is safe against a live database; a running worker observes
the new epoch on its next sweep. Repeating the command appends a new epoch
each time — activation is not idempotent, and watermarks stay monotonic.
The exact-event `medre storage collect --apply` workflow remains the
stopped-storage maintenance path.

`docs/ops/message-storage.md` documents both commands with the
live-storage-safety table next to the collection workflow, and
`docs/spec/content-retention.md` states the command contracts. The outputs
are pinned by `docs/schemas/content-retention-status.schema.json` and
`docs/schemas/content-retention-activation.schema.json`; `tests/test_content_retention_cli.py`
covers the content-free guarantee, the fail-closed refusals, and the
append-only epoch semantics.

Operator status observes epoch, collected receipt count and preview in one
read snapshot, including when another connection activates a newer epoch.

Unparseable activity timestamps yield unknown freshness; structurally invalid
epoch rows fail with the existing path-free refusal. Writable CLI entry points
share the existing-SQLite header gate; status uses the shared policy default
for its stated interval assumption.

# Run one owner-fenced content collection worker under the declared policy

The runtime now activates a single policy-governed content collection
worker in LIVE scope when the operator's `content_retention` section is
enabled with `mode: relay-only`: constructed in `_prepare_startup` with
the one live `SQLiteStorage` handle as both its storage authority and its
candidate source, and started at the post-adapter worker-activation
boundary after the retry and durable-ingress workers. A declared
`retain` policy constructs nothing (the worker's own enabled gate stays
as defense in depth), a disabled section constructs nothing, and REPLAY
scope never constructs the worker because a replay execution reads event
bodies that a concurrent collector must not remove.

Each sweep gates on a fresh durable activation epoch: the first runtime
start under the enabled policy opens the epoch with
`runtime-first-start` provenance whose admission watermark pins
pre-activation history out of scope, and every sweep heartbeats the
epoch forward-only before paging. A stale heartbeat — an epoch whose
last activity observation is older than `max(3600, 10 * interval)`
seconds — suspends collection and heartbeating entirely until a fresh
operator-opened epoch appears, with the inspectable
`awaiting_activation` state.

Every candidate goes to `collect_event_content(event_id, dry_run=False)`
— never to a preview result — so the guarded collector's `BEGIN
IMMEDIATE` revalidation remains the only mutation authority and every
candidate is naturally revisited once per full cursor cycle. Transient
SQLite failures (typed `ContentCollectionTransientError`) abort the
remaining page with the cursor left untouched, back off doubling up to
ten policy intervals and the epoch grace minus one interval, and surface an
observable degraded state;
integrity refusals stay fail-closed, are re-examined next cycle, and
three consecutive refusals within one sweep restrict the worker to a
clean fail-closed stop that refuses further starts.

Shutdown mirrors the retry worker: bounded two-stage stop with honest
abandonment, stopped after the retry worker and before the capacity
drain, pipeline teardown, and storage close, with the same deferred-
cancellation discipline in `stop()`, `_start_failure_cleanup`, and
`_cleanup_core_resources`, and a `has_retained_work` guard that fails
the runtime instead of closing storage under a live collection
transaction.

Worker state is runtime-local observability projected through the new
`content_collection_state` app property, a content-free
`content_collection` block in `diagnostic_snapshot`, and a top-level
`content_collection` block in the runtime snapshot — flags, counters,
and a timestamp only, never event IDs or content values. The
`collection_*` runtime-event vocabulary records started/stopped/
abandoned/start_refused/deferred/restricted/epoch_awaited/failed/swept
through the sanitized event buffer. The spec pages
([content collection guard](../../spec/content-collection-guard.md),
[content retention](../../spec/content-retention.md)) restate the
automatic-invocation norm: permitted only through the operator-declared
relay-only policy inside its durable activation epoch, disabled by
default, and otherwise prohibited as before.

Automatic collection includes the current-epoch write fence and a fresh
per-process startup boundary. Aggregate projection schemas name their actual
producers; pure projection tests remain core-only and provider wiring remains
explicit compatibility coverage.

Transient recovery leaves a policy interval of heartbeat margin so ordinary
backoff does not suspend a live worker.

An explicit first sweep establishes its own startup boundary just as a started
LIVE loop does. Restart exclusion tests include a collected positive control,
and runtime wiring seeds pre-activation history before startup.

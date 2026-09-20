# 195: Startup-skipped routes are enforced, not only assessed

When an adapter fails to start, `compute_startup_readiness` assesses routes
whose source or all targets failed as `SKIPPED` and logs them as skipped —
but the assessment was advisory only: the routes stayed registered on the
router, so the pipeline kept planning deliveries into the never-started
adapter. Every event for that target then dead-lettered with the adapter's
own not-started error (observed physically as a soak's ten
`Session not initialised` rows), contradicting the documented degraded-start
contract that routes referencing failed adapters are skipped.

Startup readiness is now enforced with scope- and reason-aware semantics:

- **LIVE, all targets failed** — the route is removed from the router
  before the runtime accepts work. Every fresh delivery into the failed
  adapters would fail, so the route stops planning.
- **LIVE, source failed** — the route stays registered. Routing a stored
  canonical event keys off the event's recorded source adapter, not a live
  connection: already-admitted durable ingress and other stored work must
  still reach surviving targets, and a source adapter that never started
  cannot deliver fresh live ingress anyway. Pruning the route would
  silently re-route stored input into a false `no-route` outcome.
- **REPLAY scope** — nothing is removed. Replay executes stored events
  explicitly through the configured routes; a pruned route would misreport
  the execution as `no routes matched` instead of delivering to surviving
  targets (or failing per-target, truthfully, when targets are down).
- **DEGRADED routes** (some targets surviving) stay registered — partial
  target loss keeps honest per-target outcomes, including visible failures
  for targets that did not start.

The readiness report (`routes.startup_readiness`) still records every skip
in all scopes; only router enforcement is selective.

The retry worker is also activated at the post-adapter startup boundary
instead of before adapter startup: its first claim cycle can no longer
claim due work while an adapter is still starting and consume an attempt
on the adapter's `not started` refusal — an ordering artifact, not a real
transport failure. After startup settles, persisted terminal evidence is
reconciled normally; unresolved retry rows whose target adapter failed this
startup are rescheduled without incrementing their attempt number. Work for
started targets proceeds through the existing retry authority.

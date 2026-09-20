# 195: Startup-skipped routes are enforced, not only assessed

When an adapter fails to start, `compute_startup_readiness` assesses routes
whose source or all targets failed as `SKIPPED` and logs them as skipped —
but the assessment was advisory only: the routes stayed registered on the
router, so the pipeline kept planning deliveries into the never-started
adapter. Every event for that target then dead-lettered with the adapter's
own not-started error (observed physically as a soak's ten
`Session not initialised` rows), contradicting the documented degraded-start
contract that routes referencing failed adapters are skipped.

Startup readiness is now enforced: routes assessed `SKIPPED` are removed
from the router before the runtime begins accepting work. Routes assessed
`DEGRADED` (some targets surviving) stay registered — partial target loss
keeps honest per-target outcomes, including visible failures for targets
that did not start.

# Require complete happy-path session evidence

Happy-path run sessions now require a successful receipt for every configured
route/target pair of every injected event. Construction, startup, or delivery
failure cannot produce a passing partial fanout, and later successful messages
cannot hide an earlier failure. Both ingress modes use the same configured-route
resolution as normal smoke; callback polling waits for complete available-target
evidence or its deadline. Failure scenarios retain their injected-fault criteria.

Existing `fail_reasons` entries identify the event, route, and target. No report
fields or configuration keys change. Session limitations now describe the lack
of live acknowledgement evidence without assuming radio delivery semantics.
Authoring guidance uses the canonical callback interface.

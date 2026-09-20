# 197: Replay teardown spends the shutdown drain budget once and keeps primary errors

A `best_effort` replay holds adapters open for a bounded pre-stop drain of
in-flight outbound deliveries, then calls `stop()` — which derived a fresh
full `shutdown_drain_timeout_seconds` deadline for its own capacity drain.
One slow-but-alive transfer could therefore consume the documented budget
twice (once in the CLI drain, once in stop), roughly doubling worst-case
teardown latency against the durable-ingress "shared deadline" contract.

The deadline is now single and owner-enforced: `MedreApp.stop()` accepts an
optional absolute `drain_deadline`, and the replay CLI computes the
deadline once, consumes it with the observational pre-stop drain, and hands
the same deadline to `stop()`. Callers that do not pre-drain keep the
previous derived-deadline behavior unchanged.

The replay CLI's teardown also no longer lets a secondary failure mask the
primary one: the replay body's failure or cancellation remains the
operator-facing error, while drain and stop failures are logged (error
level when the body succeeded, warning/error alongside the preserved
primary otherwise) — the previous silent `except Exception: pass` around
the drain and the finally-block `stop()` exception override are gone.
Cancellation still propagates. The drain's observed keys are now the shared
`medre.adapters.diagnostics_keys` contract instead of string literals, and
`stop()` continues to release pipeline and storage on every path.

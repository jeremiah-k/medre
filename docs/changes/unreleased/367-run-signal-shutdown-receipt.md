# Bound the run command's signal shutdown with receipt evidence

The `medre run` shutdown path is now pinned by real-process signal tests: one
SIGINT or SIGTERM after readiness enters teardown within the runtime's
one-second polling interval, teardown completes with every adapter stopped and
honest drain evidence, repeated signals during teardown never interrupt owned
cleanup, a signal before readiness exits bounded through the owned startup
rollback, and a signal arriving while the loop thread runs synchronous work
defers the acknowledgment for the controlled block without delaying the teardown
that follows.

The signal handler records how many shutdown requests Python observed and when the
first one did, without logging or touching streams inside the handler; the
teardown summary snapshots that count and the age of the first request once the
loop is back in coroutine context. This replaces the previously unrecorded
first-handler-observation time when operators investigate a slow Ctrl+C response. The
field report that motivated this change remains unexplained: its recorded
teardown is healthy, so no shutdown rewrite was made and no fix is claimed.

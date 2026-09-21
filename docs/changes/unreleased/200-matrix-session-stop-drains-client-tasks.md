# 200: Matrix session drains SDK request tasks before closing the client

mindroom-nio's `sync_forever` starts each iteration's request coroutines
(sync long-poll, to-device send, keys upload/query/claim) with
`asyncio.ensure_future` into a local `asyncio.as_completed` batch. During
cancellation, the SDK can retain and await those request tasks while unwinding
`sync_forever`. `MatrixSession.stop()` used to wait on the outer sync task and
then close the HTTP client without first breaking that child-request dependency,
so the connector close could race a still-owned request's connection release: an in-flight `keys_query`/`keys_upload`
transport to the homeserver survived the stop, held only by the event loop,
and surfaced later as `ResourceWarning: unclosed socket` /
`PytestUnraisableExceptionWarning` in unrelated tests (campaign finding
F1b, runs 5-7).

`MatrixSession.stop()` now cancels the outer sync loop, gives its cleanup one
event-loop turn, and—while that sync task is still pending—drains tasks bound
to the nio client (identified by bound-method frame locals, without pinning
coroutine names). It then hard-observes/reaps the outer sync task and performs a
second bounded client-task scan before closing the HTTP session. Completed request exceptions are retrieved during the drain, and tasks
that ignore cancellation within the stop timeout receive a terminal-result
callback before they are logged as stragglers, preventing late unobserved-task
warnings. Cancellation of `stop()` itself is still propagated. The
caller-supplied stop timeout is shared across all session-owned teardown
work: detached Megolm recovery tasks, in-flight room joins, the sync-task wait,
the client-bound request drain, and client close. Cancellation-resistant owned
tasks are cancelled, observed only until that absolute deadline, and then
detached with a terminal-result consumer rather than allowing `stop(timeout)`
to overrun its cooperative shutdown budget.

Live proof against the real homeserver: runtime start/stop now completes
with zero ResourceWarnings, zero remaining client sessions, and zero live
TLS transports after the pinned aiohttp graceful-TLS-shutdown window (its
`ssl_shutdown_timeout`, 30 s default, is not configurable through the pinned
mindroom-nio release's public API; the idle keep-alive socket resolves itself
within that window and fds return to baseline). Deterministic regression:
`test_stop_drains_client_bound_tasks_before_close` fails on the previous
stop ordering and passes now.

Classic checkpoint acknowledgement no longer imports the optional Matrix SDK merely
to classify the staged-token mismatch. MEDRE recognizes that specific protocol error
from the raised exception contract, defers only that known recovery race, and leaves
all unrelated acknowledgement failures fatal.

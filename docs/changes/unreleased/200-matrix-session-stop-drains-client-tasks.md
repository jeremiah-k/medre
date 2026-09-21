# 200: Matrix session drains SDK request tasks before closing the client

mindroom-nio's `sync_forever` starts each iteration's request coroutines
(sync long-poll, to-device send, keys upload/query/claim) with
`asyncio.ensure_future` into a local `asyncio.as_completed` batch. When the
sync loop is cancelled, any request still in flight is orphaned by the SDK.
`MatrixSession.stop()` used to close the HTTP client session immediately
after cancelling the sync task, so the connector close raced the orphaned
request's connection release: an in-flight `keys_query`/`keys_upload`
transport to the homeserver survived the stop, held only by the event loop,
and surfaced later as `ResourceWarning: unclosed socket` /
`PytestUnraisableExceptionWarning` in unrelated tests (campaign finding
F1b, runs 5-7).

`MatrixSession.stop()` now drains every task still bound to the nio client
(identifying them by bound-method frame locals, without pinning coroutine
names) while the HTTP session is still open — aiohttp releases each
connection through its normal cancellation path — and only then closes the
client. Completed request exceptions are retrieved during the drain, and tasks
that ignore cancellation within the stop timeout receive a terminal-result
callback before they are logged as stragglers, preventing late unobserved-task
warnings. Cancellation of ``stop()`` itself is still propagated.

Live proof against the real homeserver: runtime start/stop now completes
with zero ResourceWarnings, zero remaining client sessions, and zero live
TLS transports after the pinned aiohttp graceful-TLS-shutdown window (its
`ssl_shutdown_timeout`, 30 s default, is not configurable through nio
0.40.0's public API; the idle keep-alive socket resolves itself within that
window and fds return to baseline). Deterministic regression:
`test_stop_drains_client_bound_tasks_before_close` fails on the previous
stop ordering and passes now.

# 193: Replay best_effort now starts the runtime before re-delivering

`medre replay --mode best_effort` is the documented operator recovery path
for re-delivering unresolved events ("sends real messages"), but the CLI
executed it against a built-not-started runtime: every adapter delivery
failed with `AdapterPermanentError("Adapter not started")` and appended a
failed receipt. Side-effect modes now start the runtime lifecycle
(storage -> pipeline -> adapters) before executing, hold adapters open for
a bounded drain of in-flight outbound deliveries (the configured
`shutdown_drain_timeout_seconds`) so asynchronous transfers (e.g. LXMF
DIRECT) are not aborted by immediate teardown, and stop cleanly
afterwards. Non-side-effect modes (including `dry_run`) keep the
read-only, storage-only path.

Starting the full live lifecycle also dispatched unrelated work, though:
the retry worker claimed due `pending`/`retry_wait` outbox rows (a
selected replay was observed to claim an unrelated pending row and leave
it `abandoned` at teardown), and the durable-ingress worker routed live
ingress received during the replay window. Side-effect replay now starts
the runtime in a dedicated replay-delivery scope (`StartupScope.REPLAY`):
adapters are live for the selected re-delivery only, the ingress and
retry workers stay stopped, and ingress received while scoped crosses the
durable admission boundary to be processed by the next normal live start
(deferred, not lost).

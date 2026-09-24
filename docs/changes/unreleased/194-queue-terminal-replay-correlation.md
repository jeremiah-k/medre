# 194: Queue-backed completions finalize by real terminal correlation, not drain inference

A `best_effort` replay to a queue-backed adapter (e.g. Meshtastic) records a
`queued` receipt at enqueue acceptance. The queue success callback
(`OutboundNativeRefRecord`) and terminal failure callback
(`QueueTerminalRecord`) both carry exact `outbox_id` + `attempt_number`
correlation, but the queued→sent correlation step refused replay-sourced
queued receipts because the callback record itself carries no replay
provenance — so a completed replay row stayed non-terminal and the normal
live retry authority later reclaimed it as crash-orphaned work, re-rendering
and re-transmitting the replayed content over RF.

The queued→sent correlation now trusts the already-validated exact
row/attempt match: a replay-sourced queued receipt is finalized exactly like
a live one, inheriting dispatch `source` and `replay_run_id` from the exact
queued receipt while the outbox row independently preserves replay origin (the
same correlation authority the terminal failure path uses). The interim mechanism that
closed such rows after the teardown drain by inferring per-attempt success
from aggregate queue state is removed — delivery truth is recorded only by
real terminal callbacks through the single lifecycle authority, and the
bounded teardown drain remains purely a wait, never a delivery authority.

The normative pages that still carried the previous replay-only restriction
are reconciled in the same change: delivery-lifecycle §5.3,
diagnostics-evidence §15.3 (the "Replay-only skip warning" signal row is
replaced by the "Replay-lineage finalize" signal, logged at debug) and
§15.4(7), conformance §9.4(2), plus the operator-facing
recovery-and-replay and troubleshooting entries — the operator-visible
skip warning can no longer occur.

Replay-only queued→sent selection emits the documented debug lineage signal
even in the normal single-candidate case, not only when malformed duplicate
candidates exist.

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
a live one, carrying its durable `source`/`replay_run_id` lineage (the same
recovery the terminal failure path always used). The interim mechanism that
closed such rows after the teardown drain by inferring per-attempt success
from aggregate queue state is removed — delivery truth is recorded only by
real terminal callbacks through the single lifecycle authority, and the
bounded teardown drain remains purely a wait, never a delivery authority.

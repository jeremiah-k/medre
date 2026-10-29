# Show ownership-aware recovery guidance

Recovery runbooks and evidence incident summaries share typed current-delivery
advice, including work admitted before its first receipt. Existing dispatch,
queue, retry, uncertain and terminal ownership remains visible. Failed-target
previews select the owning adapter and route; channel-filter limits are explicit.
Guidance emits inspection and dry-run commands, preserving opaque IDs and paths
through shell quoting, without authorizing or submitting a native resend.

Follow-up: recognize persisted `sent` as completed, serialize action commands as
JSON arrays in memory, and share command/fallback policy between the runbook and
evidence summary. Warn about BEST_EFFORT and channel preview limits only when
an actionable scoped preview is offered.

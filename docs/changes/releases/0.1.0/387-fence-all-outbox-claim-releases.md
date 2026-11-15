# Fence every outbox claim release against native attempt reservations

Make the SQLite claim-release guard unconditional: only an owned, unreserved
`in_progress` row can return to `pending` or `retry_wait`. Remove the unsafe
release opt-out from storage and lifecycle contracts. A reserved attempt may
have reached a native transport, so recovery must reconcile it through the
lifecycle service rather than silently requeue it.

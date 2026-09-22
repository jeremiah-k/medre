# 206 — Adapter retry hints and Matrix server-directed backpressure

- `AdapterSendError` can now carry a validated `retry_after_seconds` minimum delay
  for transient transport failures. Delivery lifecycle scheduling persists
  `next_retry_at = max(policy backoff, adapter retry hint)` without changing retry
  exhaustion or permanent-failure rules. No storage schema change is required.
- Matrix converts homeserver `retry_after_ms` from rate-limit responses into the
  generic retry hint and records a shared monotonic cooldown. Sibling Matrix
  deliveries arriving during that window are deferred before `room_send`, reducing
  repeated 429 traffic while preserving the existing deterministic transaction ID
  and durable outbox ownership model.
- Matrix diagnostics add `outbound_rate_limit_events`,
  `outbound_cooldown_deferrals`, and `outbound_cooldown_remaining_seconds`.
- MEDRE still does not impose a guessed proactive Matrix send rate; backpressure is
  driven only by explicit homeserver feedback.

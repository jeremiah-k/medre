# 206 — Adapter retry hints and Matrix server-directed backpressure

- `AdapterSendError` can now carry a validated `retry_after_seconds` minimum delay
  for transient transport failures. Delivery lifecycle scheduling persists
  `next_retry_at = max(policy backoff, adapter retry hint)` without changing retry
  exhaustion or permanent-failure rules. No storage schema change is required.
- Matrix intercepts room-send rate-limit responses through mindroom-nio's filtered
  callback boundary before the SDK sleeps/retries them, without changing the
  client's global 429 policy for sync, join, or key-management requests. A valid
  homeserver `retry_after_ms` becomes the generic retry hint; a positive value also
  extends the shared monotonic cooldown. Missing or invalid hints remain transient
  failures without a shared cooldown. Sibling Matrix deliveries arriving during an
  active cooldown are deferred before `room_send`, reducing repeated 429 traffic
  while preserving the
  existing deterministic transaction ID and durable outbox ownership model.
- Matrix diagnostics add `outbound_rate_limit_events`,
  `outbound_cooldown_deferrals`, and `outbound_cooldown_remaining_seconds`.
- Durable scheduling and the in-memory Matrix cooldown both clamp server timing
  to a 30-day maximum so a hostile or broken value cannot overflow the receipt
  timestamp or park delivery indefinitely.
- MEDRE still does not impose a guessed proactive Matrix send rate; backpressure is
  driven only by explicit homeserver feedback.

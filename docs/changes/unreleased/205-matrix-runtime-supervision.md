# Matrix runtime supervision

- Add proactive stale Classic Sync supervision inside `MatrixSession`. A
  configurable durable-progress deadline recycles the current nio sync owner
  before the existing bounded reconnect path runs, and fails closed if the
  stale loop cannot be cancelled without overlap.
- Bound undecryptable-event room-key recovery independently from logging with
  a rolling outbound request-attempt limit and a concurrent recovery-task cap.
- Expose supervision/throttle diagnostics and keep their shape stable while the
  Matrix adapter is stopped.
- Align Matrix config, JSON Schema, examples, operator documentation, SDK-parity
  authorities, and focused tests with the new runtime policy.
- Enforce the Matrix reconnect-delay cap after jitter and keep live warning
  deduplication independent from missing-room-key recovery admission.
- Harden stale-loop recycling so provider stop-hook failures cannot bypass task
  cancellation, and keep supervision counters scoped to completed recycles and
  true concurrent-capacity rejections.

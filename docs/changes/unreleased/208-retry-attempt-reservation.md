# 208 — Durable retry attempt reservation

- Retry dispatch now reserves its attempt identity durably before invoking the
  transport. The `delivery_outbox` row gains a nullable `active_attempt`
  column: the retry worker commits `active_attempt = attempt_number + 1` via a
  guarded update (`reserve_outbox_attempt`) immediately before transport
  invocation — after claim reconciliation and after the adapter-availability
  and capacity gates, so a deferred row still consumes no attempt. A worker
  that lost its claim cannot reserve and never invokes the transport
  (`docs/spec/delivery-lifecycle.md` §3.4.1,
  `docs/spec/state-machines.md` §2.3).
- This closes the retry claim-window gap documented with the observation
  ledger: previously a callback carrying the newly claimed attempt was
  rejected and lost between claim and finalization while a superseded
  attempt's callback was still admitted. Every callback validator —
  post-handoff observations, queued→sent finalization, and queue terminal
  reporting — now correlates against the effective attempt
  `COALESCE(active_attempt, attempt_number)`, so the reserved attempt is
  admissible for the entire handoff and earlier attempts become stale the
  moment the reservation commits. Finalization consumes the reservation
  atomically with its outcome transition, advancing `attempt_number` and
  clearing `active_attempt` in one guarded statement
  (`docs/spec/routing-delivery.md` §13.3.1).
- A reserved dispatch stamps the reserved number onto the rendered result and
  every receipt it produces (`deliver_to_target(reserved_attempt_number=...)`),
  so adapter callbacks echo exactly the identity the outbox will admit.
  Receipt lineage (`parent_receipt_id`) is unchanged.
- Claim reconciliation now uses the reservation as its crash-recovery
  discriminator: a reclaimed row with a live reservation and persisted
  evidence for that attempt commits the missing outbox transition without
  re-invoking the transport (closing the crash window between receipt
  persistence and outbox finalization), while a reservation without evidence
  is released so the re-dispatch reserves the same number again. The
  defensive next-attempt evidence check for unreserved rows remains.
- Explicit-attempt finalizations are fenced to the reservation they hold:
  a stale worker returning after lease expiry and re-reservation by a newer
  worker cannot regress the row's live attempt identity (the retry worker
  performs no lease renewal during dispatch, so mid-dispatch expiry is a
  real path). Terminal transitions that pass no attempt number
  (abandonment, cancellation of an in-flight dispatch) consume a live
  reservation and record the reserved attempt as the row's final one.
- This changes the prerelease SQLite shape by adding `active_attempt` to
  `delivery_outbox`. Existing stamped prerelease databases that do not match
  the current shape are rejected by design and must be recreated; schema
  version remains `1` until the release compatibility boundary is declared.

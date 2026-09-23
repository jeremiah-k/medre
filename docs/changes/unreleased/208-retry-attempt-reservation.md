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
- The worker renews the claimed row's lease for as long as its reserved
  dispatch runs: one synchronous renewal immediately after the reservation
  (aborting transport when the claim is already lost, and starting the
  dispatch on a fresh lease rather than whatever the claim gates left of
  the original one), then periodic renewal at half the poll interval for
  the claim's lease duration, cancelled when the dispatch completes. A
  live worker's slow transport therefore cannot outlive its claim and
  invite a reclaim that re-dispatches under the same attempt identity.
  Process death, a storage outage, or a failed renewal still expires the
  lease — that is the recovery path claim reconciliation expects, and the
  fences below remain the authority for anything a superseded worker still
  commits.
- Explicit-attempt finalizations are fenced in both reservation states: a
  live reservation must match exactly, while an unreserved row rejects any
  explicit attempt older than its finalized `attempt_number`. Retry-worker
  transitions also require the current claim `worker_id`, and guarded storage
  mutations report whether they committed so a rejected stale worker cannot
  emit false durable success/retry/dead-letter evidence. This covers a worker
  returning after lease expiry (mid-dispatch expiry remains reachable through
  renewal failure or worker death) whether the newer attempt is still
  reserved or has already finalized. Terminal transitions that
  pass no attempt number (abandonment, cancellation of an in-flight dispatch)
  consume a live reservation and record the reserved attempt as the row's
  final one.
- Live-pipeline outbox finalization is also fenced to the pipeline worker that
  created/claimed the row. If its lease expires and a retry worker reclaims the
  same row, the original pipeline result can still append immutable receipt
  evidence but cannot clear or overwrite the newer worker's claim/reservation.
- Queue-backed retry observability is race-stable when an asynchronous terminal
  callback wins just before the retry worker commits its returned `queued`
  receipt. Lifecycle re-reads the authoritative same-attempt outbox state after
  the rejected CAS and projects the already-committed terminal outcome instead
  of misreporting the retry as superseded.
- This changes the prerelease SQLite shape by adding `active_attempt` to
  `delivery_outbox`. Existing stamped prerelease databases that do not match
  the current shape are rejected by design and must be recreated; schema
  version remains `1` until the release compatibility boundary is declared.

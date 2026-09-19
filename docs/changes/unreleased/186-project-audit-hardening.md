# 186 — Project Audit Hardening

User-visible follow-ups from the cross-cutting audit pass. Schema versions
unchanged.

- **Doc consolidation.** Operator, spec, dev, and live-validation docs were
  reconciled against current behavior; stale audit-trail documents that
  referenced removed audit identifiers were pruned; remaining docs were
  cross-linked to the canonical operator/spec entries. No behavioral
  contract changes.
- **Error-terminal outbox atomicity.** Failed queue terminal outcomes
  (`dead_lettered`/`cancelled`/`abandoned`) now commit the immutable failed
  receipt and the outbox attempt → terminal-status transition in one
  storage transaction; outbox identity, attempt number, and non-terminal
  state are revalidated inside the transaction. Stale or duplicate
  callbacks that lose to a competing attempt or state change commit
  neither. (The queued→`sent` path was already atomic — change 164.)
- **Matrix `require_encrypted_rooms` enforcement.** With
  `require_encrypted_rooms=True`, outbound sends to rooms not
  affirmatively established as encrypted are refused with a
  `MatrixSendError` (fail closed — plaintext and unknown-encryption rooms
  are rejected, so a crypto-unavailable `e2ee_optional` session never
  sends), and inbound events from rooms not established as encrypted are
  dropped before durable admission without stalling the sync checkpoint.
  Room encryption status comes from the session's room-state tracking,
  refreshed from nio room state as syncs arrive. Configuration rejects
  `require_encrypted_rooms=True` together with `encryption_mode=plaintext`
  at config load. A new `inbound_filtered_encryption_policy` diagnostic
  counter reports policy drops.

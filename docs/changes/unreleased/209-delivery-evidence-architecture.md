# Delivery evidence architecture

- Distinguish delivery attempt receipts from lifecycle-transition receipts with
  a durable `receipt_kind` field.
- Lifecycle receipts no longer invent a new dispatch attempt number; terminal
  evidence keeps the causative attempt identity.
- Extend the receipt vocabulary with explicit `cancelled` and `abandoned`
  lifecycle statuses so terminal outbox states can converge on one evidence
  model in the follow-up lifecycle consolidation.
- Replace parallel receipt/failure plumbing across target delivery, coordinator,
  and outbox finalization with validated immutable `DeliveryExecutionEvidence`.
- Normalize terminal queue and live-delivery outcomes through lifecycle-owned
  evidence: failed dispatch facts remain attempt receipts while
  `dead_lettered`/`cancelled`/`abandoned` are lifecycle receipts at the same
  attempt identity.
- Commit terminal lifecycle evidence and the matching outbox transition
  atomically for outbox-backed delivery, fenced by the full
  `(event, plan, adapter, channel, outbox, attempt)` identity and worker owner
  where applicable.
- Centralize current-delivery projection behind `DeliveryAuthorityResolver`,
  keyed by `(event_id, delivery_plan_id, target_adapter, target_channel)`.
  Route/source/replay fields are provenance only; empty and absent channels
  normalize to one identity; SQLite projection behavior is pinned to the same
  conformance vectors.
- Make lifecycle storage behavior executable across SQLite and the conformance
  backend with shared transition-sequence tests for reservation, ownership,
  stale-callback, atomic terminalization, and full-identity fencing.
- Enforce core outbox invariants in SQLite itself: positive finalized attempts,
  exact next-attempt reservations only while `in_progress`, a closed status
  vocabulary, and an event-scoped lineage index matching delivery authority.
- Make operator delivery projections use the same event-scoped authority model.
  The delivery ledger now separates `lifecycle_status`, mutable outbox state,
  authoritative receipt identity, causative lifecycle evidence, and latest
  dispatch attempt/result; route/source/replay remain provenance only.
- Make retry/outbox receipt-only deduplication event-scoped so equal plan IDs on
  different canonical events cannot hide one another.
- Remove plan-only current-status and receipt-lineage overloads and their
  obsolete SQLite index: both current authority and historical delivery lineage
  now require canonical `event_id`.
- Tighten outbox-backed receipt eligibility to the exact `(outbox_id, receipt_id)`
  pointer and rank committed generations from mutable outbox attempt state, so a
  late append from an older generation cannot regress current authority.
- Pre-release SQLite shape changes require recreating incompatible databases
  under the existing prerelease schema policy.
- Review hardening aligns every secondary surface with the same authority model:
  retry exhaustion commits linked lifecycle authority instead of pointing at the
  failed attempt, recovery scans consume the generation-aware `delivery_status`
  projection, queue terminal receipt transitions include cancellation and
  abandonment, and reservation cleanup consumes `active_attempt` whenever a row
  leaves `in_progress`.
- Startup schema validation now verifies the new receipt/outbox CHECK constraints,
  and shared in-memory terminal-finalization helpers keep conformance and
  operational fakes aligned with SQLite.

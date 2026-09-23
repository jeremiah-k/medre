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
- Pre-release SQLite shape changes require recreating incompatible databases
  under the existing prerelease schema policy.

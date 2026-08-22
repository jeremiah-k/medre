# Finish retry lifecycle authority

- Move retry-attempt receipt correlation, failure classification, retry scheduling,
  and dead-letter decisions behind `DeliveryLifecycleService`.
- Narrow `RetryWorker` direct storage access to claim/read operations while
  retaining capacity, task scheduling, counters, and runtime-event orchestration;
  lifecycle mutations use a separate storage-protocol view of the shared backend.
- Correlate retry evidence by exact `outbox_id`, target, attempt, and receipt
  lineage. Retry/dead-letter receipts without required outbox correlation are
  invariant violations; failed retry receipts must carry a canonical
  `failure_kind` and are never classified by parsing error text.
- Require complete, valid route-decision metadata during retry reconstruction.
  Missing or malformed strategy/capability/deadline state is abandoned rather
  than silently defaulted to different delivery semantics.
- Reuse persisted retry timestamps and prevent duplicate resend when durable
  queued/sent evidence exists before a later delivery-call exception.
- Dead-letter non-retryable retry failures immediately and preserve exact
  linked retry-exhaustion evidence, including `outbox_id` on generated
  dead-letter receipts.
- Reconcile persisted next-attempt evidence before transport after lease reclaim,
  repairing partial receipt/outbox persistence without duplicate resend.
- Add transition fault injection, strict durable-evidence tests, and architecture
  guards so failed lifecycle persistence is never reported as a committed state
  and durable retry transitions remain reviewable in one authority.
- Correct retry documentation and configuration descriptions so the durable
  outbox is the operational work queue and receipts are immutable evidence.

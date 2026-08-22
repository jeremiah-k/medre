# Retry outbox lifecycle authority

- Route retry-worker abandonment, backoff, retry exhaustion, dead-letter, and
  successful outbox transitions through `DeliveryLifecycleService` instead of
  writing lifecycle state directly from `RetryWorker`.
- Reuse the pipeline runner's lifecycle service in the runtime retry worker so
  live delivery and retry delivery share one state-transition authority.
- Keep polling, claim orchestration, capacity acquisition, runtime counters,
  and operational event emission in `RetryWorker`.
- Add focused lifecycle tests for abandonment, deterministic backoff,
  exhaustion, existing dead-letter evidence, and queued/sent retry success.

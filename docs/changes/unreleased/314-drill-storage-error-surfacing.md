# Drill storage-error surfacing

Failure drills no longer treat a failed receipt read as drill evidence. When
`list_receipts_for_event` raises during `renderer_failure`,
`replay_duplicate_risk`, `capacity_rejection`, or `shutdown_rejection`, the
affected verification step reports `result: "storage_error"` with redacted error
text in a `storage_error` field, the count or suppression comparison and its
derived report block (`receipt_timeline`, `rejection_timeline`) are skipped,
and the drill report is `failed` with the storage failure as its reason. A
broken storage backend can no longer produce false missing-receipt,
zero-count, or suppression verdicts.

An unreadable pre-replay baseline prevents replay and omits the unavailable
receipt count. Combined outcome and storage failures retain both reasons.

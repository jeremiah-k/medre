# Remove remaining legacy compatibility paths

- Delete the error-text failure-kind inference: the recover CLI and
  evidence sections now read the persisted `failure_kind` only, and a
  receipt without one classifies as `unknown` instead of being
  reconstructed from error wording.
- Make `receipt_kind` explicit-only and fail-closed: receipts persist the
  kind at write time, so a record without a valid kind raises instead of
  being guessed from status.
- Remove the dead `_AdapterFactory` dynamic-assembly detector and its
  synthetic tests from the architecture scanner; the factory pattern no
  longer exists anywhere in the tree and the generic dynamic-import
  detections remain.
- Correct misnamed "legacy" wording that described live semantics:
  adapter-only relation binding applies to routes without an explicit
  destination channel, the replay-trace receipt-only state, and the
  lifecycle boundary's persisted-attempt failure-kind derivation.

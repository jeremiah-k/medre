# Terminal ownership adversarial review

- Pass settlement event, outbox, and timestamp explicitly through both receipt-backed
  finalizers, rather than depending on positional SQL placeholders. Preserve
  transaction rollback if settlement fails, and prove deferred `sent` releases
  only its own target dependency.
- Give the read-only content-release assessment a versioned JSON Schema and
  conformance checks for both blocked and advisory-eligible outcomes. Centralize
  copy-classification rules so blocker labels cannot diverge.
- Clarify that an observed deadline cannot reactivate either source or target
  dependencies after clock rollback. No collection or deletion is enabled.

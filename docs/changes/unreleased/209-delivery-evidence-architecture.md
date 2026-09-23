# Delivery evidence architecture

- Distinguish delivery attempt receipts from lifecycle-transition receipts with
  a durable `receipt_kind` field.
- Lifecycle receipts no longer invent a new dispatch attempt number; terminal
  evidence keeps the causative attempt identity.
- Extend the receipt vocabulary with explicit `cancelled` and `abandoned`
  lifecycle statuses so terminal outbox states can converge on one evidence
  model in the follow-up lifecycle consolidation.
- Pre-release SQLite shape changes require recreating incompatible databases
  under the existing prerelease schema policy.

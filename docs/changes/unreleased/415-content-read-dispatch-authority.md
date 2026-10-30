# Content-safe read mediation and exact native dispatch claims

- Canonical event and relation inspection projects identity/routing facts in SQL.
- Typed full-event and reply-context reads require live source authority,
  including opaque bodyless events. Missing owners cannot be recreated by reads.
- Explicit metadata projections withhold opaque receipt, outbox, observation,
  native-reference and radio content before returning rows to inspection callers.

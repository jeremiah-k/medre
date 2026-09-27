# Capability evidence authority

- Persist `capability_level`, `capability_field`, `capability_reason`, and
  `delivery_strategy` directly on delivery receipts and preserve them across
  retry, suppression, dead-letter, cancellation, replay, and storage round trips.
- Make the structured receipt fields the only authority for reports and
  delivery outcome ledgers; `capability_reason` is display text and nothing
  parses reason or error wording to recover structure.
- Extend the prerelease SQLite receipt shape and published receipt schema with
  the structured fields. Existing prerelease databases with the older stamped
  shape fail the normal schema-shape guard and must be recreated/exported per
  the existing prerelease storage policy.

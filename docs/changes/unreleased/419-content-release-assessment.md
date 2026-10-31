# Read-only content-release assessment

- Added event-scoped, metadata-only, read-only copy and owner counting across
  canonical text, relation excerpts, opaque payloads, receipts, native state,
  radio plans, outbox state, and shared attachment references.
- An unclassified local copy or live owner blocks eligibility. Provider stores,
  logs, SQLite WAL, and backups remain unmeasured. The assessment never grants
  deletion or erasure and never selects retained message bytes.

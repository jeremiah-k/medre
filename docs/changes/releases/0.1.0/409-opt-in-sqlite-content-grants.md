# Opt-in bounded body read grants

Core SQLite now provides a persisted optional body-read-grant API with immutable
per-event ceilings, one durable consumer/scope hold, explicit revocation,
fail-closed single-statement authorized body reads, and restart persistence.
Unauthorized callers receive no existence or content bytes. No hold is implicit
at admission. This is **not** whole-runtime content authority: `get`, query,
rendering, recovery, evidence, relation excerpts, and native provider stores
still follow existing retention behavior. No expiry deletes bytes. Those
consumers must be migrated in a subsequent dedicated slice before runtime
retention or erasure can be enabled.

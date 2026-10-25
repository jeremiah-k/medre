# Persist exact radio plans and attempt-local handoff progress

SQLiteStorage now binds immutable fragment plans to full outbox claims and
commits each local native-call boundary under compare-and-swap ownership.
Interrupted submissions recover as unknown; accepted prefixes remain retained.
New sibling generations cannot bypass earlier native progress. Read-only
inspection neither changes evidence nor grants a send.

The required `radio_attempt_progress` table changes prerelease storage shape;
older stamped databases are rejected before DDL with existing explicit
backup/reset guidance. Package version `0.1.0` and schema marker `1` remain.

Production multipart delivery remains disabled. Serialized sender integration,
pacing and per-packet feedback aggregation are the next implementation gate.

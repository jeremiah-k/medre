# Durable Plugin Decisions and Scoped State

Add host-owned SQLite persistence for plugin execution records, deterministic
derived events, and isolated instance/replay state. Evidence, generated events,
and state revisions commit atomically. Repeated commits acknowledge stored
results; stale revisions, conflicting decisions, and changed recovery plans
fail closed. Failed invocations retain classified evidence without writing
plugin state or inventing successful transform/policy decisions.

The execution-record table is additive and preserves existing schema-version-1
evidence. Read-only inspection accepts databases without it. Automatic runtime configuration and operator commands remain separate work.

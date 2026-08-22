# Deterministic SQLite execution path

- Remove the undeclared import-time `aiosqlite` driver switch from
  `SQLiteStorage`; all installs now use the same standard-library `sqlite3`
  implementation behind MEDRE's private single-worker executor.
- Collapse durable ingress, outbox, generic read/write, read-only open, and
  queued-delivery finalization onto their existing synchronous transaction
  authorities instead of maintaining parallel transaction implementations.
- Keep the public storage API asynchronous and retain WAL mode, busy timeout,
  foreign-key enforcement, explicit transactions, and executor-owned resource
  cleanup.
- Replace driver-specific test scaffolding with coverage against the sole
  production implementation, including queued-finalization input guards.

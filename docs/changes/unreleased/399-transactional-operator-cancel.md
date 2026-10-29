# Transactional operator cancellation foundation

- Add an exact-target, generation- and version-fenced SQLite cancellation for
  currently unreserved pending/retry work, with a durable idempotent audit row.
- Reject changed plans, sibling generations, active attempts and replayed
  decision IDs with different parameters. Preserve all native send evidence.
- This is a storage-only prerelease stage: no CLI action, no uncertain-send
  reconciliation, fresh-state validation without upgrade paths for earlier SQLite layouts.

Fence against competing reserved generations and retained claim ownership.
Validate audit idempotency/status constraints when reopening state and preserve
the cancellation/audit transaction on write failure.

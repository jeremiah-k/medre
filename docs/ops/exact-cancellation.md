# Cancel one unstarted destination locally

This action is for an operator with exclusive administrative authority over a
private MEDRE SQLite state file. It **does not** resolve native uncertainty or
send a message. The database must be an owner-only regular file (mode `0600`),
inside an owner-only directory (mode `0700`), with no symlink components. The
command is supported only on POSIX; other platforms refuse the write. Ancestor
directories must be controlled by the file owner or root; writable ancestors
need sticky-bit protection (as with `/tmp`). Parent traversal (`..`) is refused.
Preview and apply use the validated absolute path.

First, stop any conflicting administrative tooling and preview a single
pending/retry-wait destination (use `--no-channel` instead of `--target-channel`
when the exact destination has no channel):

```bash
medre resolve --storage-path /private/state.sqlite --event EVENT \
  --plan-id PLAN --target-adapter matrix --target-channel '!room:server'
```

The JSON response has `apply_authorized: false`. Read the `observed` fields.
Review the event's current recovery evidence before committing. Apply only if
the native handoff has not started, copying _all_ observed values exactly:

```bash
medre resolve --storage-path /private/state.sqlite --event EVENT \
  --plan-id PLAN --target-adapter matrix --target-channel '!room:server' \
  --apply --yes --decision-id UNIQUE_DECISION --actor-label LOCAL_OPERATOR \
  --outbox-id OBSERVED_OUTBOX --expected-generation OBSERVED_GENERATION \
  --expected-status OBSERVED_STATUS --expected-updated-at OBSERVED_VERSION
```

A successful first application reports `committed: true`; repeating the same
exact decision reports `committed: false`. A competing claim or newer attempt
is **refused**, never silently retried or retargeted. An actor label is
unverified provenance: it is not a credential or an authorization token.

Read back the durable record with `medre inspect decisions --event EVENT
--storage-path /private/state.sqlite` (see [Operator decisions](operator-decisions.md)).
Only the storage transaction can authorize cancellation; preview output and
fingerprints never grant write authority.

**Limitations:** This is a first-release local OS owner boundary, not a remote
multiuser RBAC service. It cannot clear a reserved/uncertain native attempt,
advance retries, cancel whole events, or infer recipient non-delivery. Backup
and provider journals are independent retention/identity scopes.

Storage failures exit with code 3. A refused cancellation exits with code 1;
a missing or ambiguous preview target exits with code 5.

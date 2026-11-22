# Resolve one confirmation-pending delivery locally

This action is for an operator with exclusive administrative authority over a
private MEDRE SQLite state file. When a delivery is handed to an adapter-local
queue, the `queued` receipt records only that local acceptance — it never
proves the message was sent. The outbox row therefore rests in the durable
confirmation-pending disposition: it is never claimed, never re-dispatched,
and never counted as delivery success or failure. It leaves `queued` only
through evidence (a deferred-completion callback proving transport hand-off,
a provenance-matched `sent` receipt, or deferred terminal failure feedback)
or through this explicit resolution.

The database must be an owner-only regular file (mode `0600`), inside an
owner-only directory (mode `0700`), with no symlink components. The command
is supported only on POSIX; other platforms refuse the write. Ancestor
directories must be controlled by the file owner or root; writable ancestors
need sticky-bit protection (as with `/tmp`). Parent traversal (`..`) is
refused. Preview and apply use the validated absolute path.

First, stop any conflicting administrative tooling and preview the one
confirmation-pending destination for the exact delivery identity (use
`--no-channel` instead of `--target-channel` when the destination has no
channel):

```bash
medre resolve --storage-path /private/state.sqlite --event EVENT \
  --plan-id PLAN --target-adapter matrix --target-channel '!room:server' \
  --resolution delivered
```

The JSON response has `apply_authorized: false`. Read the `observed` fields
and review the delivery's evidence before committing. Apply only after
deciding the outcome, copying _all_ observed values exactly:

```bash
medre resolve --storage-path /private/state.sqlite --event EVENT \
  --plan-id PLAN --target-adapter matrix --target-channel '!room:server' \
  --resolution delivered \
  --apply --yes --decision-id UNIQUE_DECISION --actor-label LOCAL_OPERATOR \
  --outbox-id OBSERVED_OUTBOX --expected-generation OBSERVED_GENERATION \
  --expected-status queued --expected-updated-at OBSERVED_VERSION
```

`--resolution` is one of `delivered`, `failed`, or `cancelled`. `delivered`
rules the delivery complete (the outbox row reaches `sent`), `failed` rules
it a terminal failure (`dead_lettered` with the content-free
`operator_ruling` failure kind), and `cancelled` rules it withdrawn
(`cancelled`). The resolution never dispatches a send and never fabricates a
receipt. A first successful application reports `committed: true`; the ruled
terminal row is immutable, so a resolution is not repeatable — repeating the
same apply is refused.

The write transaction refuses — without modifying anything — when the
observed state no longer matches (a newer attempt generation, a competing
generation, a concurrent change), when the row is no longer
confirmation-pending, or when committed receipt evidence contradicts the
ruling: a `sent` receipt already settles the delivery, and receipts whose
event, plan, adapter, destination, origin, or replay-run provenance
contradicts the delivery identity can never ground a ruling. An actor label
is unverified provenance: it is not a credential or an authorization token.

**Limitations:** This is a first-release local OS owner boundary, not a
remote multiuser RBAC service. The resolution is recorded on the terminal
outbox row itself; it is not written to the operator decision audit table
(see [Operator decisions](operator-decisions.md) for the audit-only
decisions that are). It cannot clear a reserved or uncertain native attempt,
advance retries, resolve whole events, or infer recipient non-delivery. The
deadline sweep never resolves a confirmation-pending row, so the row can
outlive its delivery deadline until this resolution runs.

Storage failures exit with code 3. A refused resolution exits with code 1;
a missing or ambiguous preview target exits with code 5.

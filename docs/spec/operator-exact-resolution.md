# Transactional exact-destination operator decisions

## Supported storage and local operator authority

`SQLiteStorage.cancel_unstarted_delivery(CancelUnstartedDecision(...))` is a
write-capable, narrowly scoped primitive. It changes **one** current
`pending`/`retry_wait` outbox row to `cancelled`, atomically with an immutable
record in `operator_delivery_decisions`. A decision ID is idempotent only when
its actor, entire event/plan/adapter/channel identity, outbox ID, observed
attempt generation, state and observed update version agree. Reusing an ID with
different parameters fails closed. The atomic transaction checks that there is
no reservation, claim ownership, or competing generation and cannot touch
sibling legs. Competing generations MUST compare the reserved `active_attempt`
when present, rather than only the finalized attempt counter.

The [local operator command](../ops/exact-cancellation.md), `medre resolve`,
provides a confirmed caller boundary through private POSIX state ownership.
Other callers MUST independently authorize the operator; an `actor_id` label
is not authentication. It is not a replay engine and does not prove an earlier
native send was not accepted. The cancellation stops an _upcoming, currently
unreserved_ attempt; any previous receipt and native uncertainty remain in the
immutable evidence log. The primitive refuses current `in_progress`, `queued`,
`sent`, `dead_lettered`, `abandoned` and already `cancelled` states on first
application. It never clears `active_attempt`, calls a provider or sends bytes.

## Snapshot and transaction boundary

The read-only exact preview's observation fingerprint does **not** authorize a
write. The local CLI MUST acquire an exact request from the operator
and provide the outbox ID plus its persisted `attempt_number`, `status`
and `updated_at`. The expected generation is the persisted finalized attempt
counter, including zero for never-dispatched work; it MUST NOT be copied from
a display that normalizes staged work to an effective attempt of one.
The storage transaction revalidates all these fields, complete
delivery identity and the absence of a competing generation under SQLite's
`BEGIN IMMEDIATE` lock. A stale preview, an active worker or a new generation
must produce no audit row and no state change. Restarted identical requests
return idempotent success without repeating the mutation.

The local CLI MUST require explicit confirmation and an owner-only regular
state file in a private directory. It MUST reject symlink components, parent
traversal, hard links, and ancestors outside owner/root control. Writable
ancestor directories MUST have sticky-bit protection. Both preview and apply
MUST open the same absolute path that passed these checks. Storage failures
MUST report the storage-error exit code. Preview output is not a mutation
grant, and cancellation is distinct from native _uncertain handoff_
reconciliation. Automatic retry policy and exact replay execution are unchanged.

## Prerelease storage compatibility

The additional audit table is part of the **required** prerelease SQLite shape.
Before schema creation on an existing database, MEDRE validates the complete
catalog against the current shape or an explicitly allowlisted additive
predecessor, even when the stored schema version remains `1`. The authoritative
allowlist is specified in [storage](storage.md#10-pre-release-database-policy).
For the decision-audit table, the exact single-action
`operator_delivery_decisions` predecessor is one such allowed shape: a writable
open applies its guarded additive migration — a transactional table rebuild
from the authoritative DDL — and a read-only open of that recognized
predecessor reports that a guarded writable migration is available instead of
suggesting a reset. Other approved additive changes follow the same guarded
writable-initialization policy. Any other earlier development layout MUST still
be refused with recreation guidance. The allowlist is explicit and additive,
not a general upgrade framework: unsupported catalogs get no upgrade path.

Qualify candidates against separately selected fresh state; test migration and
rollback against disposable copies, not production or shared development data.
A writable migration of a known predecessor preserves its data and is distinct
from resetting an unsupported database.

The required audit shape MUST preserve unique decision IDs and the action,
observed-status and generation constraints. An audit write failure MUST roll
back cancellation; SQLite write errors surface as storage errors.

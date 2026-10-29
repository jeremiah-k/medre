# Transactional exact-destination operator decisions

## Supported authority (storage-only stage)

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

This stage does **not** expose a public operator command. The caller must have
independently authenticated and authorized the operator; an `actor_id` label is
not authentication. It is not a replay engine and does not prove an earlier
native send was not accepted. The cancellation stops an _upcoming, currently
unreserved_ attempt; any previous receipt and native uncertainty remain in the
immutable evidence log. The primitive refuses current `in_progress`, `queued`,
`sent`, `dead_lettered`, `abandoned` and already `cancelled` states on first
application. It never clears `active_attempt`, calls a provider or sends bytes.

## Snapshot and transaction boundary

The read-only exact preview's observation fingerprint does **not** authorize a
write. A potential future CLI must acquire an exact request from the operator
and provide the outbox ID plus its current persisted `attempt_number`, `status`
and `updated_at`. The expected generation is the persisted finalized attempt
counter, including zero for never-dispatched work; it MUST NOT be copied from
a display that normalizes staged work to an effective attempt of one.
The storage transaction revalidates all these fields, complete
delivery identity and the absence of a competing generation under SQLite's
`BEGIN IMMEDIATE` lock. A stale preview, an active worker or a new generation
must produce no audit row and no state change. Restarted identical requests
return idempotent success without repeating the mutation.

A future write-capable operator CLI must additionally enforce caller identity,
explicit confirmation, storage lease/maintenance authorization, and safe display
of the current snapshot. It must not treat the read-only preview as a mutation
grant, and it must distinguish cancellation from native _uncertain handoff_
reconciliation. Automatic retry policy and exact replay execution are unchanged.

## Prerelease storage compatibility

The additional audit table is part of the **required** prerelease SQLite shape.
MEDRE deliberately refuses older shapes before running `CREATE TABLE` against
an existing database, even if they report the same frozen schema version 1.
**Do not point this candidate at a production or existing development DB.**
Qualify against separately selected fresh state. Pre-first-release development
supports one current SQLite shape. Incompatible state MUST be refused, and
review MUST use fresh state rather than a migration or compatibility reader.
No upgrade path from earlier development layouts is provided.

The required audit shape MUST preserve unique decision IDs and the action,
observed-status and generation constraints. An audit write failure MUST roll
back cancellation; SQLite write errors surface as storage errors.

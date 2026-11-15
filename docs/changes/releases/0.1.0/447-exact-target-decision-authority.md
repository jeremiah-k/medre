# Audited exact-target decision authority

`record_exact_target_decision` records an idempotent operator decision over
one observed exact delivery target under the same `BEGIN IMMEDIATE`
transaction discipline as the exact unstarted cancellation. Unstarted
scheduled work (`pending`/`retry_wait` without a reservation, claim, or
competing generation) accepts the decision as an audit-only acknowledgement:
the outbox row is never modified, nothing is staged, and resumption stays
with the normal claim/attempt fences. The frozen action vocabulary is
`resume_scheduled` and `resolve_uncertain`.

In-progress, queued and accepted-or-uncertain native work refuses the
decision as a typed refusal without an audit row, never inferring
non-acceptance from timeouts, missing native references, restarts, or
content expiry. Terminal `dead_lettered` work refuses to reopen with a
refusal stating a separate send-capable authority would be required, and
settled `sent`/`cancelled` rows refuse fail-closed.

Decision IDs replay idempotently only when the actor, identity, outbox ID,
generation, update version, status and action all agree; reuse with any
different parameter fails closed. The `operator_delivery_decisions` action
CHECK widens to the frozen three-value vocabulary as part of the required
prerelease SQLite shape: older single-action databases are refused at
initialization, and no migration path is provided. No CLI command, engine,
replay, or adapter behavior changes.

# Guarded logical collection of isolated core content

`collect_event_content(event_id, dry_run=True)` MUST default to a read-only
preview. The exact event ID MUST be bounded. An apply request MUST acquire the
SQLite write reservation before checking any owner, grant, record or copy
blocker. If safe, it MAY delete **only** separate canonical text and same-event
relation excerpt rows, in the same transaction that writes its immutable,
non-content collection receipt. Inconsistent admission facts, including a body row whose admission identity says
no body was present, MUST fail closed;
repeated collection MUST be idempotent, including across concurrent writers
and restarts. It MUST NOT delete immutable delivery, native reference, radio,
attachment, or provider state. A successful logical delete is not physical
erasure; WAL, backup, logs and remote copies are outside this API.

## Collectable events and column-aware family blockers

The collectable event set is the settled routed event with independently
classified retained evidence: admitted through durable ingress, fanned out
to ordinary delivery targets, with every mandatory owner released by guarded
terminal transitions. The frozen outbox work snapshot
(`delivery_outbox_snapshots.context_json`) is not automatically content-free:
it may contain arbitrary origin, destination, or policy values (the
[accepted context ownership contract](accepted-context-ownership.md) fixes
its ownership class and terminal disposition). An unretired snapshot MUST
continue to block collection: its row exists exactly until the guarded
terminal settlement deletes it, and the `unretired_outbox_snapshots`
live-ownership gate reports it with its row count. Terminal status alone is
insufficient. Guarded operator cancellation MUST retire the same work snapshot
inside the cancellation/owner-settlement transaction; an audit failure or
stale decision MUST leave the original snapshot untouched. The historical inline dispatch metadata of rows created before
the snapshot table keeps blocking through the outbox family's content probe.
An inconsistent admission identity (missing admission record, unexpected
body rows, missing uncollected body or recreated collected content) MUST be
reported as a blocker by the read-only release assessment; the mutation guard
MUST still refuse inconsistent state. Live-ownership
gates (active ingress work, active outbox claims, unsettled mandatory owners,
unretired outbox snapshots, unrevoked optional grants) MUST block collection
with their row counts, and
the operator-facing refusal MUST name each live owner.

Delivery evidence, native references, and durable ingress work are retained
durable evidence: their rows survive collection and MUST NOT block merely by
existing. A retained-evidence family MUST block collection when any of its
registry-classified content-potential columns
([storage column classification contract](storage-column-classification.md))
carries a value on the event's own rows — historical inline diagnostic text
or non-empty provider metadata JSON. Only SQL NULL represents absent
diagnostic text; literal `'{}'` and empty strings remain conservatively content-bearing. Reviewed JSON metadata
columns additionally recognize the empty object `'{}'` as content-free. The
column set is derived from the declared classification registry, never
hand-written per column, and the read-only release assessment shares this
blocker decision.

The `delivery_evidence_diagnostics` side table is retained durable evidence:
diagnostic rows survive collection, are never deleted by it, and never block
it. Recovery evidence remains valid after content release. A diagnostic row never exists without its parent evidence row. Its
separated text survives collection and never participates in the parent
family's column-aware blocker evaluation.

Opaque envelope and relation records, incoming excerpts owned by other
events, radio plans, attachment associations, and operator decisions REMAIN
wholesale blockers under independent policy. Collection MUST NOT expand its
delete set to any of them, and no scheduler MAY invoke collection
automatically.

Pre-release initialize MAY automatically apply only exactly recognized additive
catalog states: the nullable `expired_at` grant field, the independent
collection-receipt table, the delivery diagnostics table and its identity
index, the `event_routing_facts` table, and the `delivery_outbox_snapshots`
table with its `idx_outbox_snapshot_owner_unique` index. All other schema
changes MUST fail before DDL.

# Demonstrate ordinary text collection on the production paths

The ordinary routed text vertical is provably collectible end to end through
the existing guarded collector. No second delete engine, no new table, no
classification-registry change, and no schema-version change is part of this
slice: the collector's `BEGIN IMMEDIATE` recheck, its atomic content-table
deletes, and its append-only collection receipt are unchanged, and the
release assessment stays the shared blocker decision.

`tests/test_ordinary_text_collection.py` demonstrates the exact route on
production machinery. A normal text event carrying core routing metadata is
admitted through the real admission pipeline, and the durable ingress worker
fans it out through `OutboxManager.create_for_delivery`, staging two sibling
targets with their frozen work snapshots. Settling the first target through
its guarded claim transition leaves collection refused with the pending
sibling's live owner named (`active_outbox`,
`unsettled_mandatory_owners`, `unretired_outbox_snapshots`). After a storage
restart and a mutated route configuration, the pending sibling finalizes
through the real dispatcher from its persisted accepted snapshot — the
reconstructed plan is byte-identical to the accepted one, the mutated
configuration is never consulted — via the actual native/receipt transition,
retiring its snapshot with the guarded settlement. Collection then runs
preview, explicit apply, and idempotent repeats across another restart:
`dry_run`, `collected`, then `already_collected`, each report
schema-conformant and free of body and diagnostic text. Immutable delivery
facts, native references, attempt lineage, typed `event_routing_facts`,
and conversation identity survive collection unchanged, the retired snapshot
is unrecoverable as content, mediated reads answer from the closed
retained/unavailable/denied vocabulary without fabricating text (an admitted
bodyless event reads `unavailable` and collects as `empty`), and no terminal
action becomes resendable.

The independently blocking copy families stay conservative on this vertical:
opaque native metadata, another event's incoming quote excerpt, a shared
attachment, an immutable radio packet plan, historical inline outbox
evidence, and an unresolved native handoff each keep refusing collection,
and the collector's delete set is not expanded to any of them.

The operator contract is unchanged and now demonstrated on this vertical:
the batch defaults to `retain` (`retained_by_policy` labels), previews and
applies are explicit and bounded at 100 events per page, per-event reports
distinguish `blocked`, `dry_run`, `collected`, `already_collected`, and
`empty`, the release assessment distinguishes `blocked` from
`eligible`/`unmeasured`, every report lists its unmeasured scopes, and no
report claims removal from diagnostics, external providers, logs, WAL,
backups, or peers.

`docs/spec/content-collection-guard.md` states the demonstrated vertical as
a standing fact, and the accepted-context ownership contract's guard-test
section names the demonstration suite. The content retention inventory
statements are unchanged: the vertical releases exactly the separately
persisted canonical body rows the inventory already counts, and no inventoried
surface or class changes.

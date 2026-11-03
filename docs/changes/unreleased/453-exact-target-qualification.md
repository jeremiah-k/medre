# Exact-target adversarial qualification

The exact-target property is qualified against real on-disk SQLite state
with genuine transaction interleaving, storage-handle restarts, and scoped
replay that stages real outbox generations and receipts through the fake
presentation adapter.

Write authorities serialize on real transactions. A cancellation or an
audited decision dispatched while a competing writer holds a
`BEGIN IMMEDIATE` transaction waits for that transaction, observes the
committed outcome, and refuses with its typed refusal; the winner's state
is the only committed outcome, no audit row records the loser, and nothing
half-applies. A committed sibling generation supersedes an older
cancellation observation (`A competing delivery generation exists`), while
a cancellation that commits first keeps its terminal row: any later replay
send is a fresh generation above it, never a resurrection. A decision that
commits before a cancellation is the decided outcome, not a conflict; the
reverse order refuses the decision over the settled target.

Recorded decisions, outbox state, and the preview's classification and
fingerprint survive closing and reopening the storage handle. The
identical decision replays idempotently across handles, a reused decision
ID bound to different parameters fails closed, and the normal claim and
attempt fences still refuse superseded observations after the restart.

Scoped replay keeps its staging boundary. Named targets whose persisted
state drifted are refused before a send is staged: an all-refused replay
skips delivery with the bounded `exact_target_selection_refused`
diagnostic evidence and per-target refusal reasons, and a partially
refused replay carries the refusal evidence while staging only the
admitted target. Refused and excluded siblings gain no outbox generation,
no replay receipt, and no diagnostic identifier beyond the bounded reason.
A cancelled sibling stays cancelled across a scoped replay of another
target and refuses further decisions over its pre-cancel observation.

One selection never widens into siblings. A scoped replay stages exactly
one new generation and one replay receipt for the selected identity;
sibling channels and sibling adapters of the same event keep their rows
byte-identical and receive nothing.

A stale observation never sends. Superseded expectations classify as
`stale` evidence only, contested live generations classify as `ambiguous`,
and execution admission — which accepts no caller-supplied expectation —
re-reads persisted state through the same selector the preview uses and
refuses to stage for drifted targets. A cancellation committing between an
admitting observation and delivery never loses: the deliver stage
re-derives the persisted state and any replay send lands as a new
generation above the cancelled one.

The recovery-observation specification gains a qualification-boundaries
subsection recording what this fake/core-tier qualification proves and
what it reserves: native acceptance and independently observed recipient
delivery are not proven here.

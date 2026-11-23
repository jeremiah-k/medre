# Tested operator procedure for the default-off content-ceiling sweep

The hard content-privacy ceiling's read-denial takes effect at admission
time, but the logical purge is carried by `retry.ceiling_sweep`, which
stays disabled by default and remains an open owner decision. This change
gives the operator a tested procedure for that surface without flipping
the default and without overstating what a receipt proves.

[Auditing the hard content privacy
ceiling](../../ops/content-ceiling-audit.md) now pins the four observable
states — `not elapsed`, `expired / read-denied but not purged`,
`purged and receipt verified`, and `logical purge is not physical
erasure` — with the exact read-only command and JSON marker for each, a
bounded audit → enforce → verify rehearsal on a disposable copy captured
with the stopped-state snapshot procedure, a failure and corruption
policy (a failed sweep tick rolls back to expired-not-purged with no
partial purge, receipts are immutable, verification failures point at the
late-write boundary, and a corrupt store is restored from a verified
snapshot rather than hand-repaired), and an opt-in rollout profile whose
measured consequences are stated plainly from
[the 2026-11-23 pressure campaign](../../dev/ceiling-sweep-pressure-measurement-2026-11-23.md):
a worst tick of 5,005–5,006 ms when an external writer holds the SQLite
write reservation past the busy timeout, receipt tails that scale with
elapsed-backlog depth, and two budgets still unverified by measurement
(continuous WAL high-water mark, interruption inside an open purge
transaction). Deadline maintenance keeps its distinct authority — it
retires provably unstarted outbox rows only — and neither sweep can send:
native submission authority ends at the hard ceiling and the maintenance
worker never claims delivery work.

New tests pin the operator-facing contract behind that page: the audit
CLI reports each state marker distinctly and every report carries
`physical_erasure_claimed: false`, the documented rollout profile loads
through the real loader with the intended values while the loader default
stays `enabled: false`, a failed enforcement tick leaves the store
observably `elapsed_pending` with no partial purge and no receipt, and
the enforcement transition retires dispatch context without marking
anything sent or fabricating a settlement. No production behavior,
schema, or CLI surface changes in this work.

"""Recovery ownership diagnostics.

Canonical modules:

- :mod:`models` — data classes for recovery ownership actions, ledgers,
  and summaries.
- :mod:`builder` — pure functions that build ledgers and summaries from
  outbox snapshots.
- :mod:`classification` — startup reclamation classification helpers.
- :mod:`recovery_source` — enum identifying which subsystem reclaimed
  ownership.

Import directly from the canonical modules; this package does not
re-export symbols.

Vocabulary note: "recovery" means identification and classification
--------------------------------------------------------------------
In this package, "recovery" refers to **identifying and classifying**
stale or orphaned outbox state — not to repairing or mutating it.
The ``RecoverySource`` and ``RecoveryOwnershipAction`` enums use the
word "recovery" in the diagnostic sense (the module was invoked during
a recovery/diagnostic pass), not in the operational sense (it does not
fix anything).  All functions in this package are pure read-model logic
that accept snapshots, classify, and return frozen data structures.
No function in this package performs storage writes or transitions
outbox rows.

Persistence authority boundaries
--------------------------------
Recovery diagnostics classify ownership of outbox rows (including
``in_progress`` rows whose leases have expired) but do **not** modify
them.  Recovery **must not**:

- Fabricate delivery success or create fake receipts.
- Mutate historical evidence (receipts are append-only).
- Delete canonical facts (events, receipts, terminal outbox rows).
- Transition outbox rows to ``sent`` or ``dead_lettered`` — those
  terminal transitions are the exclusive authority of the delivery
  pipeline, not recovery diagnostics.

All functions in this package are pure read-model logic.  They accept
snapshots, classify, and return frozen data structures.  No function in
this package performs storage writes.
"""

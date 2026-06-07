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

Persistence authority boundaries
--------------------------------
Recovery diagnostics may **repair** stale operational ownership (claiming
``in_progress`` outbox rows whose lease has expired) and record
orphan/ownership findings.  Recovery **must not**:

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

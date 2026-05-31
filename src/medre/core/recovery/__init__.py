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
"""

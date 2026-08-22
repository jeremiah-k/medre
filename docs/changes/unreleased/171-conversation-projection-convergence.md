# Conversation projection convergence

## Core and storage

- Add `conversation_membership` as the mutable, rebuildable current-state view
  of relation ancestry. Canonical events, relation rows, and native-message
  refs remain immutable evidence and are never rewritten by graph repair.
- Add deterministic reverse lookup for native relation targets so children
  admitted before their parent/native mapping can be repaired when that target
  later becomes available.
- Recompute conversation membership transitively after event/native-ref arrival,
  serialize projection repairs to prevent stale-write races, and perform an
  idempotent full rebuild at runtime startup before pipeline workers or adapters
  start.
- Preserve the existing ingress-time `root_event_id` / `conversation_id` fields
  as historical admission snapshots. Routing and rendering consume an
  in-memory event copy overlaid with the current projection.
- Re-resolve late native relation targets on in-memory delivery copies without
  writing the resolved `target_event_id` back into `event_relations`. A newly
  persisted outbound native identity starts repair from native-dependent children
  instead of re-reading the already-projected anchor event.
- Define deterministic cycle behavior: cycle members use the
  lexicographically-smallest cycle event ID as their projection root.
- Keep the pre-release SQLite schema version at `1`; databases that lack the
  new current shape are rejected rather than migrated or read through a
  compatibility path.

## Correctness evidence

- Prove child-before-parent and parent-before-child arrival orders converge to
  identical semantic membership.
- Cover native-target late resolution, first-resolvable relation re-selection,
  multi-hop descendant repair, deterministic cycles, deep ancestry beyond the
  Python recursion limit, and idempotent rebuilds.
- Inject a projection-write failure and prove restart rebuild converges from the
  partial repair to the same final graph.
- Guard startup ordering and evidence immutability with architecture tests, and
  prove a rebuild failure closes storage before pipeline/adapters can start.

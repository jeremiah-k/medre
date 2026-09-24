# Resolved delivery history and generated lifecycle state space

- Complete the historical storage contract around the full typed
  `DeliveryIdentity`: receipt history now includes channel scope, and outbox
  history exposes every durable generation for the same exact identity.
  Incomplete historical identities are rejected instead of silently widening
  a query.
- Introduce `ResolvedDeliverySnapshot` as the shared read model joining one
  delivery's immutable receipts, outbox generations, lifecycle authority,
  current operational generation, latest dispatch attempt, and loaded
  causative receipt.
- Move delivery ledger, runtime evidence, and convergence diagnostics onto the
  resolved snapshot so those consumers no longer rebuild partial authority /
  outbox / attempt joins independently.
- Add model-generated lifecycle state-space conformance across the in-memory
  backend and SQLite, covering every operation edge reachable from the claimed
  execution state rather than relying only on hand-picked adversarial
  sequences.
- Add an exhaustive guarded-terminalization truth table across every persisted
  outbox status, reserved in-progress state, all lifecycle terminal outcomes,
  current/finalized/future attempt identities, and worker-fence variants.

- Harden the runtime evidence JSON Schema around strict section envelopes plus
  closed config, route, storage, and recovery payload shapes. Runtime-generated config, storage-path,
  and error bundles are schema-validated so machine drift fails tests instead
  of being silently accepted.

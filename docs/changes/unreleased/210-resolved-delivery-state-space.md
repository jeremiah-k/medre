# Resolved delivery history and generated lifecycle state space

- Complete the historical storage contract around the full typed
  `DeliveryIdentity`: receipt history now includes channel scope, and outbox
  history exposes every durable generation for the same exact identity.
  Incomplete historical identities are rejected instead of silently widening
  a query. Current authority uses the same `delivery_status(DeliveryIdentity)`
  contract, removing the last scalar event/plan/adapter/channel lookup surface;
  the value object itself normalizes empty and absent channel identities.
- Introduce `ResolvedDeliverySnapshot` as the shared read model joining one
  delivery's immutable receipts, outbox generations, lifecycle authority,
  current operational generation, latest dispatch attempt, and loaded
  causative receipt.
- Move delivery ledger, runtime evidence, and convergence diagnostics onto the
  resolved snapshot so those consumers no longer rebuild partial authority /
  outbox / attempt joins independently.
- Scope exact operational history reads to `DeliveryIdentity` as well:
  queued-to-sent callback correlation, queue-terminal lineage recovery,
  replay same-run suppression, and post-insert receipt readback no longer scan
  every receipt for the canonical event and then reconstruct target identity
  with local filters.
- Add model-generated lifecycle state-space conformance across the in-memory
  backend and SQLite, covering every operation edge reachable from the claimed
  execution state rather than relying only on hand-picked adversarial
  sequences.
- Add an exhaustive guarded-terminalization truth table across every persisted
  outbox status, reserved in-progress state, all lifecycle terminal outcomes,
  current/finalized/future attempt identities, and worker-fence variants.
- Harden the runtime evidence JSON Schema around strict section envelopes plus
  closed config, route, storage, and recovery payload shapes.
  Runtime-generated config, storage-path, and error bundles are
  schema-validated so machine drift fails tests instead
  of being silently accepted.
- Replace the scalar-heavy terminal outbox storage call with a validated
  `TerminalOutboxFinalization` command. Lifecycle evidence is now the single
  source of identity, generation, terminal status, failure classification, and
  mutable outbox error summary; optional failed-attempt evidence is
  lineage-checked before storage is called.
- Apply the same command-shape rule to queued-to-sent finalization with
  `QueuedDeliveryFinalization`. SQLite now fences that atomic native-ref + sent
  receipt + outbox commit by the full delivery identity, not only outbox ID and
  attempt number, so internally coherent evidence for the wrong sibling target
  cannot finalize the row.

- Review hardening makes the finalization evidence contract single-source in
  the remaining edge cases: queued-to-sent native refs must agree with their
  immutable receipt on normalized channel as well as event/adapter/message,
  callbacks that omit channel materialize the validated outbox target channel,
  and optional failed-attempt terminal evidence must carry a non-empty receipt
  ID. Generated memory-backend rejection tests snapshot mutable outbox rows
  before operations so in-place fake mutations cannot make negative assertions
  vacuous. Receipt-kind classification is shared with the authority resolver.

# Pin the accepted context ownership contract

`docs/spec/accepted-context-ownership.md` defines the three-way ownership
boundary for context persisted about an accepted event: durable bounded
typed correlation/decision facts, privately owned frozen work snapshots that
exist only while retry or uncompleted native work requires them, and opaque
independently owned content. The contract states the decision rule that
arbitrary destination/provider metadata is never content-free merely because
it describes a route: stable identity fields plus an optional nonreversible
digest cover audit needs instead of the whole route configuration. It fixes
terminal dispositions — frozen snapshots retire exactly on `sent`,
`cancelled`, `dead_lettered` or `abandoned` settlement and never for
`pending`, `in_progress`, `retry_wait` or `queued` — fail-closed
reconstruction for unknown or changed snapshot shape with no fallback to
current router configuration, and the absence of retired snapshot bytes in
authorized reads.

The page carries the exact field-level inventory with producers, verified
consumers, ownership classes, post-terminal dispositions and operator-recovery
requirements for the serialized `EventMetadata` namespaces persisted in
`canonical_events.metadata`, for the JSON keys
`OutboxManager.create_for_delivery` writes into `delivery_outbox.metadata`,
and for the `OutboxDispatchContext` snapshot payload consumed by
`reconstruct_retry_delivery_plan`. Standing-fact notes record where the
current persisted shape differs from the contract disposition: the full
dispatch context is inlined in `delivery_outbox.metadata`, both metadata
columns are classified `independently_owned_content` in the storage column
registry, and an ordinary routed text event retains exactly the
`opaque_envelope` and `outbox_records` blockers after every target settles
and all mandatory owners release. The contract grants no collection-scope
expansion; the storage column classification contract remains the
column-level authority this page builds on.

`tests/test_accepted_context_contract.py` pins the contract: the staged
outbox metadata key set through the real `OutboxManager.create_for_delivery`
path on real SQLite storage for both staging shapes, the serialized
`EventMetadata` namespace key set through the production admission path, the
registry equivalence of both metadata columns with
`independently_owned_content`, and the production-path blocker baseline for
staged and settled routed text events. Permanently conservative opaque
content — arbitrary `custom` and `native` metadata among it — is pinned
never to flip.

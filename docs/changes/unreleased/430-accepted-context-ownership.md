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
requirements. The original accepted-context audit identified historical inline
outbox metadata as a collection blocker. The implementation in this stack
separates bounded routing facts into `event_routing_facts` (fragment 431) and
moves new outbox dispatch context into owned `delivery_outbox_snapshots`
(fragment 432). New `delivery_outbox.metadata` values are `{}`; the frozen
snapshot retires at terminal settlement. For a new plain routed text event,
once ingress finishes and all targets settle, the residual blocker list is
empty. Historical rows retain their original inline metadata bytes and
conservative collection behavior.

`tests/test_accepted_context_contract.py` pins the accepted inventory across
both staged and direct snapshot shapes, the opaque namespace serialization,
and the production-path staged/settled blocker baseline. The legacy inline
outbox metadata key set belongs to historical rows only.

Permanently conservative opaque content — arbitrary `custom` and `native`
metadata among it — is pinned never to flip.

# Durable ingress and checkpoint ownership

MEDRE owns canonical ingress durability. Transport SDKs may parse, decrypt,
recover, and order native events, but a transport cursor MUST NOT advance past
an event until MEDRE has durably accepted that event.

## Admission boundary

Durable admission atomically creates all of the following SQLite state:

- the canonical event and inline relations;
- the inbound native-message reference, when the transport provides one; and
- a durable ingress-work marker.

The native message identity is the idempotency key when present. Re-admission of
that identity MUST return the original canonical event identity and MUST NOT
create another event or work item.

Admission does not mean external delivery completed. It means MEDRE has retained
the ingress fact and durable evidence that downstream work remains.

## Provenance

Adapters with protocol-level timeline provenance SHOULD map it to one of:

- `live`: admit and route;
- `recovered`: admit and route; or
- `history`: admit, retain the suppression record, and do not route.

Protocol provenance takes precedence over the generic adapter-start timestamp
heuristic. A protocol that proves an event is continuity recovery MUST NOT have
that event discarded merely because its server timestamp predates process
startup.

## Checkpoint ownership

Application-owned transport checkpoints are persisted in MEDRE storage. The
transport SDK may stage an in-memory next cursor while processing a response,
but MEDRE MUST persist its checkpoint only after every relevant event in that
response has crossed the durable admission boundary.

A failure before checkpoint commit MUST leave the previous committed cursor
recoverable so the transport can replay the uncommitted response.

## Delivery guarantee

MEDRE does not claim exactly-once external delivery. External transports cannot
all prove whether a send crossed the network boundary immediately before a
process crash.

The durable ingress guarantee is narrower and achievable: once an inbound event
is accepted, MEDRE MUST NOT silently lose the event or forget that downstream
work remains. Delivery remains at-least-once/idempotent where transport
capabilities allow it.

## Worker recovery and evidence

Pending ingress work is claimed with a bounded lease. The active worker renews the
lease while routing/planning is still running so another generation cannot reclaim
the same event merely because one processing attempt exceeds the initial lease. A
clean processing failure returns the work to `pending` until the bounded retry
budget is exhausted, after which the row becomes terminal `failed`; cancellation
or process death leaves the lease in
place so another worker generation can reclaim it after expiry. Completing ingress
work means routing/planning reached the existing durable outbox boundary; it does not
mean every external target acknowledged delivery.

Runtime diagnostics expose whether the durable-ingress worker is running plus its
per-generation processed and failure counts. Protocol-specific continuity evidence,
such as Matrix abandoned-room recovery causes, remains adapter/checkpoint metadata
rather than being generalized into transport-neutral semantics.

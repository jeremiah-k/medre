# Durable ingress and checkpoint ownership

MEDRE owns canonical ingress durability. Transport SDKs may parse, decrypt,
recover, and order native events, but a transport cursor MUST NOT advance past
an event until MEDRE has durably accepted that event.

## Admission boundary

Durable admission atomically creates all of the following SQLite state:

- the canonical event and inline relations;
- the inbound native-message reference, when the transport provides one;
- a durable ingress-work marker; and
- when the adapter supplies verified plaintext bytes via
  `InboundAttachmentContent`, the content-addressed blob, the
  event/content association, and the payload descriptor rewrite — all in
  the same transaction (see [storage.md §4.12–§4.13](storage.md#412-attachment_blobs)).

The native message identity is the idempotency key when present. Re-admission of
that identity MUST return the original canonical event identity and MUST NOT
create another event or work item.

Admission does not mean external delivery completed. It means MEDRE has retained
the ingress fact and durable evidence that downstream work remains.

The default runtime `publish_inbound` callback MUST use this admission boundary
for every storage-backed adapter, not only transports that expose explicit cursor
or recovery provenance. Protocol-aware adapters MAY use `admit_inbound` to
provide richer provenance, but they do not bypass the same canonical admission
transaction. Storage-less direct app construction is a test-only compatibility
path and does not provide this guarantee.

## Inbound attachment bytes

Binary attachment data travels separately from the persisted canonical
envelope through the narrow `InboundAttachmentContent` value: an adapter
supplies bytes it has already bounded, integrity-verified, and (for
encrypted sources) decrypted, and core computes the authoritative content
identity (SHA-256) and measured length **from the supplied bytes** — a
wire-declared size is evidence at most, never authority.

The admission decision is honest under both failure classes:

- **Transient failures stay retryable but bounded.** Permit contention,
  transfer timeout, and network failure raise `DurableIngressDeferredError`
  before the event is admitted. Matrix keeps the native event pending without
  advancing its source cursor, while MEDRE persists a retry count keyed by the
  native room/event identity. After three transient attachment-acquisition
  failures, the event admits descriptor-only with `fetch_exhausted`; ordinary
  post-admission durable-ingress deferrals retain their existing work-row
  semantics and do not consume the terminal processing-failure budget. The
  retry-count checkpoint is bounded: entries clear on admission or when an
  event is consumed without a further dispatch, and recording past a fixed
  cap evicts the least recently deferred identity.
- **Permanent media problems admit descriptor-only.** Malformed locators,
  unsupported encrypted-media structures, digest mismatches, oversized or
  quota-rejected content, and media that is gone at the source produce an
  honest unavailable descriptor (one of the stable reason codes — see
  [event-model.md §5.5](event-model.md#55-messagefile-attachment-descriptor))
  and the event admits without bytes. The sync cursor keeps advancing; a
  permanently unavailable attachment MUST NOT poison the cursor or be
  disguised as retained content.

Deduplication interacts with admission idempotency: re-admitting a known
native identity returns the original canonical event and never replaces the
originally retained bytes, and identical content admitted by different
events is stored once and consumes quota once.

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

Pending ingress work is claimed with a bounded lease immediately before
processing; the configured batch size caps work per cycle rather than creating a
queue of pre-leased items. The SQLite work table is the durable backlog. A worker
owns at most one ingress row at a time, so backlog growth does not create an
unbounded number of in-memory processing tasks. The active worker renews the lease
while routing/planning is still running so another generation cannot reclaim the
same event merely because one processing attempt exceeds the initial lease. A clean
processing failure returns the work to `pending` until the bounded retry budget is
exhausted, after which the row becomes terminal `failed`; cancellation or process
death leaves the lease in place so another worker generation can reclaim it after
expiry. Completing ingress work means routing/planning reached the existing durable
outbox boundary; it does not mean every external target acknowledged delivery.

Runtime diagnostics expose whether the durable-ingress worker is running plus its
per-generation processed, failure, lost-lease, terminal-failure, deferral, and
forced-cancellation counts, together with the currently active event ID when one is
claimed.
Protocol-specific continuity evidence,
such as Matrix abandoned-room recovery causes, remains adapter/checkpoint metadata
rather than being generalized into transport-neutral semantics.

## Capacity and shutdown handoff

A durable ingress row MUST be completed only after routing/planning has either
reached a terminal suppression decision or transferred delivery responsibility to
the durable outbox. Capacity rejection, shutdown rejection, or failure to create
the required outbox item is therefore a **deferral**, not successful ingress
completion. The work row remains retryable so admitted ingress cannot disappear
merely because delivery capacity was unavailable. Operational deferral atomically
returns the row to `pending` and rolls back that claim's `attempts` increment;
therefore repeated congestion does not consume the terminal poison-work retry
budget. A deferred row is not reclaimed again in the same worker poll cycle.

During graceful shutdown the runtime stops replay first, then tells the durable
ingress worker to stop claiming new rows. The active ingress row and the subsequent
in-flight delivery/replay drain share one
`limits.shutdown_drain_timeout_seconds` deadline, so congestion cannot consume
the configured drain budget twice. The worker may finish its currently claimed
event while delivery capacity remains open; the runtime then stops accepting new
delivery work and uses only the deadline remainder to drain capacity. If ingress
does not finish before the deadline, the worker task is cancelled and its lease is
intentionally left to expire rather than being immediately released; this avoids a
restart racing side effects that may still be covered by the corresponding outbox
lease. Late adapter callbacks may still cross durable admission while adapters are
shutting down; those rows remain pending for the next runtime generation.

# Content lifetime and accounting

## Present storage contract

Canonical events and delivery evidence remain retained under the storage
ownership contract. No automatic expiry or collection default is introduced by this
specification. Exact-event, operator-confirmed logical collection is optional.
The storage owner MUST remain the sole mutation authority; inspection MUST NOT
grant deletion, native submission,
retry, or lifecycle promotion.

`medre inspect content --storage-path PATH` MUST open validated SQLite storage
read-only and return aggregate counts and logical byte lengths. It MUST NOT
return payloads, identifiers, samples, file paths, or native credentials. The
schema is [content-inventory.schema.json](../schemas/content-inventory.schema.json).
The compound aggregate query MUST observe one SQLite read snapshot. The
command scans retained fields and is an explicit inspection, not a periodic
health probe. Every inventoried table MUST be present. Incomplete storage MUST
be refused without creating tables or reporting an absent surface as empty. Byte lengths include UTF-8 encoding and JSON structure; they
are neither message counts nor physical database, WAL, or disk usage.

## Terminal owner settlement and read-only release classification

Normal outbox generations have independent content dependencies. Successful
owned ingress completion and each guarded terminal target transition MUST
settle their exact dependency in the **same transaction** as the lifecycle
state. Deferred or failed ingress MUST retain the source dependency; pending,
queued, and retry-wait targets MUST retain their target dependencies. Replay
and quote-context reads MUST borrow the live source rather than create an
implicit owner. An existing optional grant MAY remain valid after source
completion only until its original explicit expiry, ceiling or revocation.
A bounded `reply_quotes` grant can support quote text after ingress completes;
reply enrichment MUST NOT create such grants implicitly.

`SQLiteStorage.assess_event_content_release(event_id)` (schema: [event-content-release-assessment.schema.json](../schemas/event-content-release-assessment.schema.json)) is a read-only,
metadata-only event-scoped assessment. It MUST classify candidate canonical
body/excerpt rows separately from known opaque storage copies, active owners,
shared attachment references, and unmeasured external/physical copies. An
unclassified copy or active holder MUST result in `blocked`; no local blocker
with remaining external uncertainty MUST result in `unmeasured`. A narrow
`core_subset_status=eligible` is advisory only; neither status nor counts
MAY authorize deletion. The method MUST NOT select body, excerpt, metadata,
receipt-error or packet bytes into application memory. Content collection
remains disabled.

## Content surfaces and consumers

The inventory covers canonical envelope JSON and metadata, separately stored
`event_body_content` text; relation keys, separately stored excerpts and metadata; native-reference metadata; receipt and observation diagnostic
fields; outbox error summaries and metadata; the write-time separated
`delivery_evidence_diagnostics` side table for delivery-evidence diagnostic
text; immutable radio plans
and packet progress; ingress last-error text; attachment bytes;
checkpoint cursors and metadata; and reserved plugin values. Empty JSON
envelopes count as stored bytes. Shared attachment blobs count once. An event
payload can contain attachment metadata, sender names, formatted bodies, reply
quotes, or provider extensions; counting only its text field is insufficient.

Structured relation identity rows MAY remain as non-text evidence while
same-event excerpt text is removed, but any free-form relation key or metadata
MUST block collection. Target IDs and correlation facts are deliberately retained.
The collected bytes remain exactly the separately persisted canonical
`event_body_content` rows and same-event `event_relation_excerpts` rows.
Incoming relation excerpts belonging to other events are observed as blockers, not
owned copies that a collector may remove. The collectable event set is the
settled ordinary routed event: retained delivery evidence families (delivery
receipts, observations, outbox records, native references, ingress work)
block only while one of their registry-classified content columns carries a
value on the event's rows, and the write-time separated
`delivery_evidence_diagnostics` side table is retained durable evidence that
survives collection. Unclassified core rows, opaque copies, shared
attachments, radio plans, and active owners still block. An empty blocker list
MUST NOT be advertised as global erasure or external-copy clearance.

The column-level ownership class of every persistent storage column is
defined by the [storage column classification
contract](storage-column-classification.md); the inventoried surfaces above
are its reviewed content-bearing columns.

A retention implementation MUST distinguish these requirements before granting
any deletion:

- Unfinished ingress, queued delivery, retries and recovery require their exact
  content until the owning lifecycle establishes a terminal outcome.
- Delivery diagnostic text separated into `delivery_evidence_diagnostics` is
  retained durable evidence: it remains valid recovery evidence after content
  release, and collection never removes it.
- Radio plan fingerprints and packet contents participate in exact-attempt
  recovery and observation validation. Removing text alone MUST NOT silently
  invalidate those contracts or authorize replay.
- Native reply mapping can survive without a message body. A quoted fallback
  cannot reconstruct the original text from an event or native ID. A retention
  policy MUST define the quotation window and the visible missing-content
  behavior without inventing text or changing relation authority.
- New replies may carry an independent copy of the quoted content. Relation
  fallback text, metadata, attachments and rendering evidence require the same
  accounting as canonical payloads.
- History queries and replay need an explicit content-unavailable outcome once
  content expiry exists. Immutable correlation and outcome facts MUST NOT be
  presented as complete retained message history.

## Boundaries

Correlation identifiers can be sensitive even when bodies are absent; their
bytes are outside the content totals. Provider profiles, native histories,
logs, snapshots, exported files, SQLite free pages and WAL are independently
retained and unmeasured. The report MUST list these boundaries and MUST set
`retention_authorized` to false. It is not a deletion-safety assessment or an
erasure certificate. Disabling ingress attachment acquisition does not purge
previously stored bytes. Database encryption and secure physical erasure are
separate contracts from content lifetime.

## Advisory per-event dependency preview (no expiry)

`medre inspect content --event EVENT --storage-path PATH` MUST report only
aggregated, event-scoped core dependency counts: ingress and outbox statuses,
outgoing and incoming relation rows, receipts, observations, attachment
associations and radio packet plans in one SQLite read snapshot. It
MUST NOT select payload bodies, excerpts, native packet bytes or credentials.
The returned `delivery_hold_candidate` is a **possible** hold inferred from
current statuses, not a lease or a safe-to-delete decision. Ingress states
other than `completed` and outbox states other than `sent` or `cancelled`
MUST conservatively remain candidates, including failed and unknown states.
Concurrent owners may change status after the observation. A false value MUST
NOT authorize expiry. An unknown event or incomplete storage MUST fail closed. The report
MUST declare the external provider, peer, backup and log copies it cannot
measure, including uncounted core projections and physical SQLite remnants.
Incoming relation counts describe referring rows, not copies of every original
message byte; zero counts MUST NOT be interpreted as erasure. Its schema is
`content-dependency-preview.schema.json`. This is measurement only, not typed
persisted content availability or deletion.

The [content availability policy preview](content-availability-authority.md)
defines versioned decisions and hold trust boundaries independently of these
counts. Neither preview changes runtime reads or authorizes content deletion.

See [content-copy-classification.md](content-copy-classification.md) for
the authoritative blocker/identity distinction, and
[content-collection-guard.md](content-collection-guard.md) for atomic collection.

## Narrow logical collection

The storage backend MAY expose `collect_event_content(event_id, dry_run=True)`
for an **exact event ID**, with a metadata-only result conforming to
[event-content-collection.schema.json](../schemas/event-content-collection.schema.json).
A dry-run MUST NOT change content or authority. Applying collection MUST first
recheck all current core owners, optional grants, incoming excerpts, unclassified
copies, and retained-evidence family content values inside the same SQLite
write transaction as deleting the **separate** canonical text/excerpt rows and
recording an immutable collection
receipt. Unclassified rows, shared attachments and undecidable states MUST
refuse collection; optimistic assumptions are forbidden. Repeated calls MUST
not resurrect content or authorize native sends. The receipt preserves the
historical collection decision but does not record message bytes. No operation
may delete durable delivery facts, native references, radio plans, receipts,
delivery observations, the separated diagnostics side table, attachments,
provider state, WAL, backups or external copies. Logical removed
byte counts MUST NOT be presented as disk savings or secure erasure.
Automatic or periodic collection is enabled only through the operator
retention policy declared below; absent that declaration it remains
disabled.

## Operator retention policy declaration and activation boundary

The operator retention policy is a strict versioned declaration: the
`content_retention` configuration section and its matching value object.
`version` MUST be exactly the integer `1`; booleans are refused.
`mode` is the closed vocabulary `retain` and `relay-only`. The section is
disabled by default: an omitted section authorizes no collector and no
policy. `mode: relay-only` additionally REQUIRES `enabled: true`; the
ambiguous disabled relay-only scope is refused at load time.
`enabled: true` with `mode: retain` is valid and declares an observing
policy that never collects. `interval_seconds` (1–86400, default 300) and
`batch_limit` (1–100, default 25) bound a retention worker's cadence and
batch size; they authorize nothing by themselves. The
`MEDRE_CONTENT_RETENTION__<FIELD>` environment overrides coerce each field
and revalidate the whole section, including the enabled/mode combination.

Activating `relay-only` MUST durably establish the activation boundary in
the append-only `content_retention_epochs` table: one immutable row per
activation carrying the closed `mode`, the `activated_at` timestamp, the
`admission_watermark`, the `last_observed_active_at` heartbeat, and the
`activated_by` provenance closed to `runtime-first-start` and `operator`.
The watermark MUST be the store's maximum local admission `created_at`
observed inside the same `BEGIN IMMEDIATE` transaction that inserts the
epoch row (absent only when the store held no events at activation), and
every retention sweep MUST be bounded to events admitted strictly after
that watermark, so pre-activation history is never swept. Watermarks are
monotonic across epochs: an empty store keeps the previous epoch's
watermark and a lower observation never pulls the boundary backward. The
heartbeat MUST never move backward; a clock rollback is a no-op by
construction. Epoch rows are not event-scoped, carry no foreign key, hold
no content, and are append-only: the heartbeat is the only update and no
delete exists.

Each new collection worker, including an explicitly invoked first sweep,
MUST establish a new activation
boundary when its predecessor epoch is still fresh. A fresh heartbeat
proves only that some previous worker was active recently; it cannot prove
that the operator did not run MEDRE with retention disabled in the
intervening interval. The resulting conservative new watermark MAY leave
previously eligible content retained. A stale predecessor MUST still fail
closed into `awaiting_activation` until an explicit operator epoch is opened;
a worker MUST NOT silently renew a stale epoch. Once running, a worker
MUST reuse its current epoch unless an operator opens a newer one.

A background collector MUST recheck the newest epoch identity and event
admission watermark in the same `BEGIN IMMEDIATE` transaction as deletion
and the collection receipt. An epoch replaced after candidate paging MUST
NOT authorize collection. The worker MUST restart its keyset from the new
epoch scope instead of treating epoch rotation as malformed content.
Heartbeat writes MUST refuse to update a superseded epoch. The manual
exact-event collector remains a separate, explicitly confirmed operator
operation and does not inherit the background policy epoch guard.

The aggregate policy preview
(`SQLiteStorage.preview_content_retention`, schema
[content-retention-preview.schema.json](../schemas/content-retention-preview.schema.json))
is read-only, bounded, and metadata-only. It MUST examine at most 100
epoch-scoped events per call in one SQLite read snapshot, MUST run the
same read-only decision the guarded collector uses, and MUST report only
per-event outcome counts — `eligible`, `blocked`, `empty`,
`already_collected`, `inconsistent` — reusing the collector's statuses so
the preview and the mutation never speak two dialects. It MUST NOT return
an event ID enumeration, message text, or any content value; the single
`next_after` `(created_at, event_id)` keyset continuation is the only
identifier. The read API MUST accept that pair as an optional `after` cursor,
using the same strict watermark and keyset ordering, so all bounded pages
can be examined. This exposes one cursor event ID per page, not an event
ID enumeration; the operator CLI MUST continue to omit this cursor. It MUST set
`retention_authorized`, `deletion_authorized`, and
`physical_erasure_claimed` to false and MUST list the unmeasured copy
scopes. A store with no activation epoch MUST report the distinct
`no_active_epoch` status with zero counts: collection cannot safely be
assessed, which is distinct from nothing being eligible. The preview
never grants mutation authority; the guarded per-event collector with its
`BEGIN IMMEDIATE` revalidation remains the only mutation boundary.

Bounded candidate discovery
(`SQLiteStorage.page_collection_candidates`, the
`CollectionCandidateSource` seam a retention sweep consumes) pages one
epoch's candidate event IDs. Each call MUST run one read-only SELECT that
returns at most `limit` (1–100) event IDs in the stable
`(created_at, event_id)` ordering of the `idx_events_admission_order`
index — an admission-time ordering that never assumes event IDs are
chronological — and MUST NOT select body, excerpt, metadata, or any
content value into the caller. The watermark lower bound is strict: an
event admitted at exactly the watermark instant is pre-activation and
MUST NOT be a candidate, and a `None` watermark records the empty-store
activation semantics, so every current event is in scope. Continuation is
the `next_after` `(created_at, event_id)` keyset of the last returned
row; paging past the epoch scope returns an empty page with a `None`
continuation, and a new sweep cycle restarts from the scope head by the
caller's decision. The page advance is independent of per-event
collection outcomes — a permanently blocked event never starves later
candidates — and a page whose first row does not advance past its keyset
cursor MUST be refused instead of looping. Selection grants no mutation
authority: the guarded per-event collector with its `BEGIN IMMEDIATE`
revalidation remains the only mutation boundary.

Retention under a declared relay-only policy is: "Best-effort, bounded
logical collection of eligible isolated core text after accepted owners
settle; refused or unmeasured copies are retained and reported." It is
not physical erasure, a hard privacy deadline, or retention for
attachments, provider stores, or plugin-held content.

The runtime's single owner-fenced collection worker invokes collection
automatically only under this declared relay-only policy: it is
constructed solely in LIVE scope for the one live storage handle, gates
every sweep on a fresh durable activation epoch (the first runtime start
under the enabled policy opens one with `runtime-first-start`
provenance; a stale heartbeat suspends collection and heartbeating until
a fresh operator-opened epoch appears), pages candidates through the
bounded selector above, and mutates only through the guarded per-event
collector. Transient SQLite failures defer the remaining page under a
bounded backoff with an observable degraded state. Backoff MUST be at most
ten policy intervals and at most the epoch grace minus one interval, reserving
time for processing. Repeated integrity refusals restrict the worker
(fail-closed stop) until operator
intervention. REPLAY scope never constructs the worker, a declared
`retain` policy never collects, and disabling the section stops future
collection while already collected bytes never reappear. The exact-event
`medre storage collect` workflow remains the operator's explicit path
for stopped storage.

## Bounded operator workflow

`medre storage collect --storage-path PATH` MUST default to `--mode retain`
without mutation. `--mode relay-only` MUST preview at most `--limit` (1–100,
default 25) lexicographically ordered events; `--after EVENT` MUST permit
continuation. Applying changes MUST require `--mode relay-only --apply --yes`
and MUST re-evaluate each event inside its write transaction, regardless of
preview results. The bridge SHOULD be stopped before an apply campaign.
The CLI MUST NOT install a timer, apply retention settings to future events,
or claim to remove WAL, backups or provider copies. Its byte counters report
only logical bytes removed from the classified core subset. Every batch,
including retain mode and empty pages, MUST list unmeasured copy scopes.
Collection errors for an individual event MUST produce a content-free `refused`
entry while preserving previous committed progress and allowing later events
in the page to be processed. `events_refused` counts those entries; an apply
batch is not an all-or-nothing transaction. `--after` MUST accept any persisted
canonical event identifier, without a separate CLI-only length ceiling.
A pre-existing non-SQLite file, including an empty file, MUST be refused by
apply without initializing a new database. Read-only previews require the
current validated schema; approved additive predecessors can be upgraded by
normal writable initialization before a preview campaign.

Admission-watermark comparison MUST exclude equivalent UTC spellings of the
same instant, including a zero microsecond written with or without a fraction.
The comparison normalizes the boundary, never the indexed admission column;
stored facts and cursor keys remain unchanged.

# Content lifetime and accounting

## Present storage contract

Canonical events and delivery evidence remain retained under the storage
ownership contract. No automatic expiry or collection default is introduced by this
specification. Exact-event, operator-confirmed logical collection is optional. The storage owner MUST remain the sole
mutation authority; inspection MUST NOT grant deletion, native submission,
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
fields; outbox destinations, error summaries and metadata; immutable radio plans
and packet progress; ingress provenance and errors; attachment bytes;
checkpoint cursors and metadata; and reserved plugin values. Empty JSON
envelopes count as stored bytes. Shared attachment blobs count once. An event
payload can contain attachment metadata, sender names, formatted bodies, reply
quotes, or provider extensions; counting only its text field is insufficient.

Structured relation identity rows MAY remain as non-text evidence while
same-event excerpt text is removed, but any free-form relation key or metadata
MUST block collection. Target IDs and correlation facts are deliberately retained.
The first classified collection subset contains only separately persisted
canonical `event_body_content` and `event_relation_excerpts` rows. Incoming
relation excerpts belonging to other events are observed as blockers, not
owned copies that a collector may remove. All unclassified core rows, shared
attachments and active owners block this narrow subset. An empty blocker list
MUST NOT be advertised as global erasure or external-copy clearance.

A retention implementation MUST distinguish these requirements before granting
any deletion:

- Unfinished ingress, queued delivery, retries and recovery require their exact
  content until the owning lifecycle establishes a terminal outcome.
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

## Narrow logical collection (operator initiated only)

The storage backend MAY expose `collect_event_content(event_id, dry_run=True)`
for an **exact event ID**, with a metadata-only result conforming to
[event-content-collection.schema.json](../schemas/event-content-collection.schema.json).
A dry-run MUST NOT change content or authority. Applying collection MUST first
recheck all current core owners, optional grants, incoming excerpts and
unclassified copies inside the same SQLite write transaction as deleting the
**separate** canonical text/excerpt rows and recording an immutable collection
receipt. Unclassified rows, shared attachments and undecidable states MUST
refuse collection; optimistic assumptions are forbidden. Repeated calls MUST
not resurrect content or authorize native sends. The receipt preserves the
historical collection decision but does not record message bytes. No operation
may delete durable delivery facts, native references, radio plans, receipts,
attachments, provider state, WAL, backups or external copies. Logical removed
byte counts MUST NOT be presented as disk savings or secure erasure.
Automatic or periodic collection remains disabled.

## Bounded operator workflow

`medre storage collect --storage-path PATH` MUST default to `--mode retain`
without mutation. `--mode relay-only` MUST preview at most `--limit` (1–100,
default 25) lexicographically ordered events; `--after EVENT` MUST permit
continuation. Applying changes MUST require `--mode relay-only --apply --yes`
and MUST re-evaluate each event inside its write transaction, regardless of
preview results. The bridge SHOULD be stopped before an apply campaign.
The CLI MUST NOT install a timer, apply retention settings to future events,
or claim to remove WAL, backups or provider copies. Its byte counters report
only logical bytes removed from the classified core subset.

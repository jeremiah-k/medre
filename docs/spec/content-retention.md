# Content lifetime and accounting

## Present storage contract

Canonical events and delivery evidence remain retained under the storage
ownership contract. No content expiry, deletion policy, or retention default is
introduced by this specification. The storage owner MUST remain the sole
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

## Content surfaces and consumers

The inventory covers canonical payload and metadata; relation keys, fallback
text and metadata; native-reference metadata; receipt and observation diagnostic
fields; outbox destinations, error summaries and metadata; immutable radio plans
and packet progress; ingress provenance and errors; attachment bytes;
checkpoint cursors and metadata; and reserved plugin values. Empty JSON
envelopes count as stored bytes. Shared attachment blobs count once. An event
payload can contain attachment metadata, sender names, formatted bodies, reply
quotes, or provider extensions; counting only its text field is insufficient.

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

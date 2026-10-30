# Admission-to-handoff content authority (prerelease implementation)

This page describes the **implemented** v1 core SQLite authority boundary. It is
not a retention policy, automatic collection, a plugin permission system, or a
promise that copies held by transports or SQLite backups have been erased.

## Immutable facts and separate bytes

`canonical_events` retains the immutable envelope, excluding original string
`payload.body` and `payload.text`; their bytes live in `event_body_content`
with explicit field identity, preserving absent versus empty string and both
fields when both are supplied. `event_body_identity.present_at_admission` is
an immutable observation of original string body/text presence, never an
assertion that these bytes remain accessible. Relation identity stays in
`event_relations`; a string fallback excerpt, even the empty string, is stored
in `event_relation_excerpts`. Source metadata, other payload fields, relation
keys and metadata, attachment blobs, evidence, packet plans, and native stores
can still contain message bytes. Content separation is not erasure.

## Transactional dependencies

Every full canonical event projection has a source dependency, committed
**atomically** with admission and durable ingress work. Opaque payload fields,
reaction keys and provider/custom metadata can contain content even without a
canonical body or excerpt; no bodyless bypass is permitted. Append-only insertion
uses an `append` owner. Each new ordinary outbox generation adopts an independent
`outbox` dependency inside its insert transaction while the source remains live.
Duplicate admission cannot replace the original
bytes or recreate an owner. A new **replay** generation adopts no dependency of
its own and never inherits a live outbox hold; its dispatch authority is the
still-live admission dependency, observed in the same snapshot as the read, so
replay fails closed once that hold is released or expired. The source is never
released merely because one of several targets becomes terminal.

An optional UTC `content_deadline` may be given to SQLite ingress admission.
The deadline is not a default retention rule. Observing expiration releases
its source dependency **logically and durably**, including when attempting to
stage a late outbox, and a subsequent backward clock adjustment cannot
reactivate it, even if optional-hold issuance is refused after observing expiry.
Missing dependency rows never authorize retained content. No row deletion or physical content collection is performed.

## Restricted trusted core reads

`read_event_for_ingress` and `read_event_for_outbox` return typed
`AuthorizedEventRead` results for live durable work or outbox ownership;
`read_body_for_outbox` returns typed `ContentBodyRead`. Denied responses do not
expose event/body bytes. These operations perform authorization and content
projection in one SQLite transaction and apply a monotonic expiry observation.
Production callers omit `now`: storage samples UTC after acquiring its SQLite
transaction, so executor/lock waits cannot reuse an earlier permission check.
An explicit `now` is available to trusted controlled-clock qualification.
An admitted body that is missing or has a non-text storage value is a storage
error, never fabricated text or a silently empty event.
Outbox reads authorize a generation through its own target-scoped dependency
or, for replay generations, through the live admission dependency.
`authorize_native_submission` checks the live generation content dependency
before native submission. This local check is not an atomic transaction with a
remote transport and cannot undo an accepted operation.

The durable ingress processing path and outbox retry path use owner-bound
reads through the required storage contract, while the target service gates native
submission. This does not assert that every possible data read is mediated.

## Metadata-only reads and replay authority

Normal `get()`, `get_envelope()`, `get_metadata()`, `query()`,
`query_metadata()` and `list_relations()` are metadata-only. SQLite selects
only identity/routing columns, never opaque `payload`, native/custom
metadata, relation text, relation keys or relation metadata. These APIs do
not grant access to stored bytes. A caller requiring content must use the
trusted owner-held `read_event_for_ingress`, `read_event_for_outbox`, or
`read_event_for_replay` API and handle typed unavailable/denied results.

Replay selection uses metadata, and explicit replay reads content only while
its original admission/append owner remains active. It creates no new
retention grants. Replay content-dependent stages fail closed once that
source authority is released, even if other content copies remain on disk.
Every content-dependent replay stage checks the source hold again. Store-only
selection remains metadata-only and needs no content borrow. Existing events
without an admission dependency fail closed; duplicate admission and relation
appends never backfill a missing owner or extend retention.

`read_reply_context` returns typed retained, unavailable or denied results.
It selects only canonical body/text and the authorized source metadata needed
for provider-owned sender projection, excluding unrelated payload and the
target's own relation excerpts. A non-string explicit body keeps the canonical
body absent even if a secondary text field was stored. Relation enrichment
uses this dedicated source-held API; expired targets retain native relation
identity through metadata-only reads and produce an honest missing-quote
fallback. Missing authority methods never fall back to unrestricted reads.

Timeline, operator inspection and evidence collectors use metadata views.
Receipt, outbox, observation and native-ref inspection explicitly request
`metadata_only=True`; SQL withholds opaque fields before Python receives rows.
The same rule covers receipt listing, recovery pagination and native-reference
inspection. Radio inspection selects progress/provenance without `plan_json`
or wire text, and marks `content_withheld`. Packet output includes ordinal,
state and whether a handoff was recorded, rather than opaque serialized
handoff strings; continuation assessment remains
advisory and requires full plan validation during authorized dispatch.
Persisted recovery records retain their original contents and remain sensitive. Authorized delivery and retry paths
still consume complete events, while metadata-only reply inspection can
preserve native reply identity without manufacturing expired text.

## Remaining content and release boundaries

This change is **logical read mediation and dispatch-start fencing**, not
physical retention enforcement. The core still stores other retained copies
in rendered evidence, packet continuations, attachments and native reference
stores, while providers maintain their own independent queues and journals.
Privileged internal storage/recovery APIs and already-hydrated in-memory
objects are outside the metadata-only interface. Authorization at the SQLite
boundary is not atomic with a remote SDK submission; it cannot un-send an
accepted, partially accepted or uncertain native operation. Logical expiry
must never erase attempt evidence or imply an uncertain operation did not
happen.

Before automatic collection: inventory all content-bearing copies, define
terminal-owner settlement and shared attachment release, test outbox/retry
and reply fallbacks, and qualify provider-native retention, WAL and backups.
No automatic deletion or physical erasure is enabled.

No storage migrations are provided for superseded, unsupported prerelease
schemas. SQLite schema version remains 1. A later declared supported baseline
may introduce deliberately tested forward-only additive migrations.

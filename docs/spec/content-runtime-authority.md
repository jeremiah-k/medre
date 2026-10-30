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

An ingress-scoped content dependency is created **in the same SQLite commit**
as canonical admission and durable ingress work when there is string body/text
or a relation fallback excerpt. This includes a retained secondary `text` field
when an explicit non-string `body` makes the body-presence fact false. Append-only event insertion uses an `append`
owner. Each new outbox generation adopts an independent target-scoped
`outbox` dependency inside its own insert transaction, only when an eligible
source dependency remains live. Duplicate admission cannot replace the original
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
An admitted body that is missing or has a non-text storage value is a storage
error, never fabricated text or a silently empty event.
Outbox reads authorize a generation through its own target-scoped dependency
or, for replay generations, through the live admission dependency.
`authorize_native_submission` additionally checks the in-progress outbox
owner and live content dependency just before adapter invocation. It is a
**local dispatch-start fence**, not an atomic transaction with remote/native
submission. It cannot undo a packet already accepted by a transport.

The durable ingress processing path and outbox retry path use owner-bound
reads where SQLite exposes them, while the target service gates native
submission. This does not assert that every possible data read is mediated.

## Explicitly incomplete enforcement and release gates

The existing general-purpose `storage.get()`, `storage.query()` and
`list_relations()` methods still hydrate retained content. Replay and some
history/inspection paths still use them. A preloaded in-memory event or
adapter-owned queue is also outside the SQLite read gate. Before enabling an
operator expiry/collection policy, these paths MUST be replaced with explicit
metadata-only, owner-held or denied results, including honest missing-quote
fallbacks and replay refusal without authority. Rendering evidence, packet
continuations, attachments, provider journals, WAL, backups and independent
copies must be inventoried and governed appropriately. Current runtime behavior
**must not be described as complete content retention enforcement**.

No storage migrations are provided for superseded, unsupported prerelease
schemas. SQLite schema version remains 1. A later declared supported baseline
may introduce deliberately tested forward-only additive migrations.

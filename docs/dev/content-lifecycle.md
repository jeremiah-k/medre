# Content lifecycle and consumer retention

This is a proposed design, not an implemented expiry configuration. Core stores
canonical payloads inline, relations can copy quote text, and providers own
separate native stores and journals. The implemented
[content inventory](../schemas/content-inventory.schema.json) measures logical
core surfaces; it grants no deletion authority. The implemented privacy boundary
remains [security and privacy](../spec/security-privacy.md#35-message-content-at-the-relay-boundary).

## Separate the lifetimes

| Term                 | Meaning                                                                                    | Example consumer                                        |
| -------------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------- |
| Event envelope       | Immutable identity, origin, time and relation identity of an admitted event.               | Routing history and correlation.                        |
| Message content      | Text, attachments, rendered parts, excerpts and content-bearing metadata.                  | Pending delivery, display and replay.                   |
| Delivery deadline    | Last time policy allows an unsent target to begin native submission.                       | Outbox dispatcher.                                      |
| Reply context        | Bounded excerpt and attribution sufficient for a readable fallback quote.                  | Relation enrichment.                                    |
| Native binding       | Destination-scoped mapping from a canonical event to a native message.                     | Native reply selection and loop prevention.             |
| Ownership record     | Accepted/uncertain attempt identity, generation and minimal duplicate-prevention evidence. | Retry and native recovery.                              |
| Consumer hold        | A bounded grant to retain a specified part of content for a named purpose.                 | Delivery, reply context, history or an opted-in plugin. |
| Content availability | An explicit retained, expired, unavailable or policy-denied result.                        | Every consumer reading content.                         |

Content expiry does not mean an event never existed, and it does not authorize
another send. Native bindings remain private metadata even after text expires.
An acknowledgement at one destination does not settle another fanout target.
A settled handoff establishes the reported local/native ownership level, not a
recipient read receipt. Transfer to a durable native queue can release a core
content dependency without permitting deletion of that queue's own content.

## Operator profiles and advanced controls

Offer a small set of understandable profiles before exposing individual durations:

- **Relay only:** retain content needed for admitted delivery, then release full
  bodies. Reply excerpts may live in a bounded process-local cache. Retain only
  the necessary correlation and ownership records on disk after handoff.
- **Recent context:** release archive bodies after delivery; retain a bounded
  excerpt window, with separately selected memory or durable context storage.
  A 24-hour context window is a starting proposal, not an installed default.
- **History:** retain full content for an explicit operator-selected period.
  History and reply-context limits remain independent of delivery deadlines.

Keep the existing storage behavior for existing installations until an explicit
policy selection and validated migration. For new installations, recommend
recent context once that profile is implemented and qualified. Do not advertise
an unbounded archive as a requirement for reliable bridging.

Advanced controls need separate limits for full bodies, excerpts, attachments,
correlation metadata and optional consumers. Memory caches need both byte and
age limits; restart deliberately loses their contents. Native references can
support replies after a restart without reconstructing expired quotes.

## Consumer holds

A hold names its consumer, event/content reference, content scope, purpose,
requested expiry and granted expiry. It carries no credentials or complete
message body. Optional consumer namespaces are operator-approved, for example
`plugin:search` rather than an arbitrary plugin-supplied path.

In this design, a lifecycle module owns grants, release, expiry and availability.
Consumers use that interface instead of independent TTL tables or direct database
reads. An envelope-only consumer does not need a body-retention grant; excerpts,
full text and attachments have separately approved scopes.
Delivery and reply enrichment are the first real consumers; a plugin loader is
not a prerequisite for implementing their contract.

1. Capture mandatory delivery dependencies atomically with durable ingress and
   outbox admission. Ingress can need content before any outbox row exists.
2. Grant reply/history holds under the selected operator profile.
3. Allow a future plugin to request only its approved content scopes and maximum
   lifetime. Requested retention can be shortened or denied by policy.
4. Make grants idempotent by consumer and content identity. Renewal cannot move
   a hard privacy deadline or restore expired content.
5. Persist durable grants with their content transaction. Memory-only grants
   disappear on restart. Recovery must not resurrect them from stale plugin state.
6. Expose the consumers retaining content and their deadlines through metadata-only
   inspection. Do not include message text in a policy explanation.

A plugin must handle unavailable content explicitly; granting read access does
not grant indefinite retention. Disabling a plugin releases its optional holds
without removing another consumer's content. The existing plugin package is
protocol scaffolding without a loader or enforced capability grants. Implement
permission enforcement and lifecycle ownership together before exposing this
service to plugins. Installed Python code is trusted code, not a sandbox; a
plugin's independently exported copy is outside core's erasure claim and needs
explicit inventory and operator disclosure.

## Deadlines and handoff safety

A delivery deadline controls unsent work. Check it at admission, recovery and
immediately before the persisted native-submission fence. Derive it from a
trusted local admission time and operator policy; remote timestamps or TTL hints
do not create local authority. Test with an injected aware clock and an explicit
inclusive expiry boundary. Carry the selected deadline with the immutable attempt
into provider-owned queues; checking it only before core queue admission would
allow stale work to begin native submission later.

On deadline expiry, prevent further unsubmitted targets from starting, settle
that target's expired outcome, and release its content dependency transactionally.
Already submitted or accepted operations retain their ownership fences. Do not
relabel them as unsent or reissue them because content or a lease expired.

Soft retention windows may defer release while admitted work still needs content.
A hard privacy ceiling cannot be extended indefinitely by a retry, failed plugin,
new replay or unresolved handoff. Policy must either prevent incompatible work
at admission or explicitly expire unsent work at that ceiling. Uncertain native
acceptance can retain minimal fences without retaining an entire body forever;
content removal must not erase those fences. No worker purges content across an
in-flight native call without a coordinated, persisted state transition.

## Content availability and storage changes

Inline canonical JSON and copied relation text prevent a safe implementation
based on clearing `payload.body` after a callback. Preserve immutable envelope
identity and ownership evidence while separating content storage and its
availability projection. Define the envelope/content reference contract and its
migration before changing stored representations.

Consumers receive content plus availability metadata, or a typed unavailable
result. Reply enrichment can then choose, in order: an authorized native target,
a permitted retained excerpt, or an honest reply marker. It cannot infer the
original text from an identifier. Replay previews report unavailable content
instead of producing a misleading empty send.

A purge plan covers content copies in canonical payloads, relation fallback and
`original_text`, receipt/rendering evidence, outbox/native handoff journals,
radio packet plans and attachment references. Shared blobs remain retained while
any authorized consumer needs them. Provider declarations identify native stores
and their deletion limitations; core cannot promise to erase remote recipients,
third-party histories or backups.

Logical release, removal from live SQLite rows and physical reclamation are
separate operations. Report each scope honestly. File encryption, key disposal,
WAL/checkpoint behavior and backups need their own documented protection model.

## Coordinate with the plugin execution draft

[Plugin execution PR 299](https://github.com/jeremiah-k/medre/pull/299) explores
operator grants, scoped state and durable execution decisions on a separate draft
branch. Reuse and reconcile that work before implementing plugin retention; do
not create a competing execution or grant system. It is not merged runtime
functionality, and its execution records do not establish content-retention
permission. Content holds need explicit scopes, privacy ceilings and release
semantics in addition to execution identity.

## Implementation and qualification order

1. Keep the inventory and add a metadata-only dependency projection and policy
   preview. A preview lists holds, expiry and unknown/provider-owned copies;
   it never treats a byte count as permission to delete.
2. Introduce the content-reference/availability representation with explicit
   migration and legacy reads. Preserve immutable envelope and attempt identity.
3. Wire delivery and reply-context holds, including durable ingress before fanout,
   partial fanout, queued feedback, restart and unavailable-quote fallback.
4. Add opt-in release and bounded collection with race tests against dispatch,
   reader leases, attachment sharing and repeated expiry/restart.
5. Integrate optional plugin holds only with the actual plugin permission and
   ownership system. Define cooperative external-copy disclosure separately.

Acceptance includes a completed target beside a pending target, an uncertain
native handoff at the privacy ceiling, a plugin renewal beyond its grant, plugin
removal while a reply still needs the excerpt, a crash during release, a shared
attachment, an expired replay and a restart with an empty memory cache. Expiry
must reduce content while preserving correct refusal to resend uncertain work.

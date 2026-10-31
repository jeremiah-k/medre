# Content lifecycle and consumer retention

Retention profiles, optional plugin holds and automatic expiry remain a
proposed design, not an implemented configuration. The storage model they
extend is implemented: canonical body text and relation excerpts are stored
separately from the immutable event envelope, mandatory content dependencies
settle transactionally with admission, outbox staging and terminal lifecycle
transitions, runtime content reads are owner-scoped, and exact-event
isolated-content collection runs as a guarded, opt-in relay-only operator
workflow with automatic and periodic collection disabled. The implemented read
and settlement boundary is
[content runtime authority](../spec/content-runtime-authority.md); the
collection contract is
[guarded logical collection](../spec/content-collection-guard.md). The implemented
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
| Content availability | An explicit retained, unavailable or denied result.                                        | Every consumer reading content.                         |

Content expiry does not mean an event never existed, and it does not authorize
another send. Native bindings remain private metadata even after text expires.
An acknowledgement at one destination does not settle another fanout target.
A settled handoff establishes the reported local/native ownership level, not a
recipient read receipt. Transfer to a durable native queue can release a core
content dependency without permitting deletion of that queue's own content.

## Operator profiles and advanced controls

Offer a small set of understandable profiles before exposing individual durations:

- **Relay only:** retain content needed for admitted delivery, then release full
  bodies. Reply excerpts can live in a bounded process-local cache. Retain only
  the necessary correlation and ownership records on disk after handoff.
- **Recent context:** release archive bodies after delivery; retain a bounded
  excerpt window, with separately selected memory or durable context storage.
  A 24-hour context window is a starting proposal, not an installed default.
- **History:** retain full content for an explicit operator-selected period.
  History and reply-context limits remain independent of delivery deadlines.

Retention profiles require completed runtime integration and qualification
before activation. The implemented step in the relay-only direction is
bounded, operator-initiated collection of isolated canonical text and
same-event excerpt rows through `medre storage collect --mode relay-only`;
it is a collection workflow, not a selectable profile. Prerelease schema
changes use fresh development state, and existing databases accept only the
explicitly allowlisted additive catalog additions, without alternate readers
or data transformations for earlier layouts. Recent context
is a proposed first-release default once implemented and qualified. An unbounded
archive is not a requirement for reliable bridging.

Advanced controls need separate limits for full bodies, excerpts, attachments,
correlation metadata and optional consumers. Memory caches need both byte and
age limits; restart deliberately loses their contents. Native references can
support replies after a restart without reconstructing expired quotes.

## Consumer holds

A hold names its consumer, event/content reference, content scope, purpose,
requested expiry and granted expiry. It carries no credentials or complete
message body. Optional consumer namespaces are operator-approved, for example
`plugin:search` rather than an arbitrary plugin-supplied path.

In the implemented core storage authority, the storage backend owns optional
grant issuance, revocation, durable expiry observation and read mediation
through `issue_optional_body_hold`, `revoke_optional_body_hold` and
`read_body_for_consumer`; consumers use that interface instead of independent
TTL tables or direct database reads. An envelope-only consumer does not need a
body-retention grant; excerpts,
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
   disappear on restart. Recovery cannot resurrect them from stale plugin state.
6. Expose the consumers retaining content and their deadlines through metadata-only
   inspection. Do not include message text in a policy explanation.

A plugin is expected to handle unavailable content explicitly; granting read
access does not grant indefinite retention. Disabling a plugin releases its
optional holds without removing another consumer's content. The existing
plugin package is
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

Soft retention windows can defer release while admitted work still needs content.
A hard privacy ceiling cannot be extended indefinitely by a retry, failed plugin,
new replay or unresolved handoff. Policy either prevents incompatible work
at admission or explicitly expires unsent work at that ceiling. Uncertain native
acceptance can retain minimal fences without retaining an entire body forever;
content removal does not erase those fences. No worker purges content across an
in-flight native call without a coordinated, persisted state transition.

## Content availability and storage changes

Canonical body text and relation excerpts are stored in separate rows, so
release operates on those rows rather than clearing `payload.body` after a
callback; the immutable envelope keeps its identity and ownership evidence.
One envelope/content-reference contract and its validated stored
representation are implemented in the v1 core SQLite authority boundary
described by [content runtime authority](../spec/content-runtime-authority.md).
Incompatible prerelease layouts are refused and replaced with fresh
development state; existing databases accept only explicitly allowlisted
additive catalog additions, and no alternate legacy read paths exist.

Consumers receive content plus availability metadata, or a typed retained,
unavailable or denied result. Reply enrichment can then choose, in order: an
authorized native target, a permitted retained excerpt, or an honest reply
marker. It cannot infer the original text from an identifier. Replay
content-dependent stages fail closed once source authority is released and
report a named denied skip outcome instead of producing a misleading empty
send.

The read-only release assessment classifies candidate canonical text and
excerpt rows separately from opaque envelope, relation, receipt, outbox,
native, radio and evidence copies, active owners and shared attachment
references. Arbitrary relation keys or metadata, and incoming excerpts stored
by other events, block collection, and unmeasured external or physical copies
stay reported as unmeasured. The first classified collection subset contains
only separately persisted canonical text and same-event excerpt rows. Shared
blobs remain retained while any authorized consumer needs them. Provider
declarations identify native stores and their deletion limitations; core
cannot promise to erase remote recipients, third-party histories or backups.

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

The first four steps of this order are implemented:

1. The inventory, the metadata-only dependency projection and the versioned
   availability policy preview are in place. A preview lists holds, expiry and
   unknown/provider-owned copies; it never treats a byte count as permission to
   delete.
2. One content-reference/availability representation is validated on fresh
   state. Immutable envelope and attempt identity are preserved throughout
   runtime processing; incompatible stored layouts are refused, with only
   explicitly allowlisted additive catalog additions applied to existing
   databases.
3. Delivery and reply-context holds are wired through owner-scoped reads,
   including durable ingress before fanout, partial fanout, queued feedback,
   restart and unavailable-quote fallback.
4. Opt-in release and bounded collection are implemented: an apply request
   acquires the SQLite write reservation before rechecking every owner, grant,
   record and copy blocker, and repeated collection stays idempotent across
   concurrent writers and restarts.
5. Optional plugin holds still require the actual plugin permission and
   ownership system. Cooperative external-copy disclosure is defined
   separately.

Acceptance includes a completed target beside a pending target, an uncertain
native handoff at the privacy ceiling, a plugin renewal beyond its grant, plugin
removal while a reply still needs the excerpt, a crash during release, a shared
attachment, an expired replay and a restart with an empty memory cache. Expiry
reduces retained content while preserving correct refusal to resend uncertain
work.

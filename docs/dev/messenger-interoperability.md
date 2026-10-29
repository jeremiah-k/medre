# Messenger interoperability and contract readiness

MEDRE's envelope can carry additional messenger data, but that alone does not
make a feature interoperable. The shared contract also needs identity scope,
validation, rendering, fallback, authorization, and lifecycle rules. Keep the
envelope small; introduce shared payload contracts when adapter fixtures prove
that more than one implementation can use the same semantics.

This document records design decisions and extension criteria. Implemented
behavior is defined by the [transport profiles](../spec/transport-profiles/README.md),
[text selection](../spec/text-selection.md), and
[transient typing](../spec/transient-typing.md) contracts. XMPP, Chatmail, and
SimpleX Chat are supported adapters. Multiple attachments and puppeting remain
extension work. The protocol references below were checked on 2026-10-17.

## Decisions before a stable freeze

| Concern              | Decision                                                                                                                                                   | Evidence needed before freezing the feature                                                           |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Envelope             | Keep opaque IDs, scoped native refs, relations, and optional metadata namespaces. Payload mappings are extensible, without a universal per-kind validator. | Constructor, storage round-trip, and complete nested-schema drift checks.                             |
| Attachments          | Publish the existing nine-field descriptor and its retained/unavailable invariant. Keep one primary attachment as the implemented contract.                | Retention, unavailable state, invalid metadata, and adapter media fixtures.                           |
| Rich content         | Preserve a plain-text representation; add typed formatting only with real decoder and renderer fixtures.                                                   | Native output plus honest text fallback for each supported feature.                                   |
| Multiple attachments | Design an ordered attachment collection at the message-content layer when an adapter needs it. Do not silently replace the singular descriptor.            | Album grouping, per-item retention, partial failure, ordering, and fallback tests.                    |
| Read/display markers | Keep user reading activity separate from MEDRE delivery attempts and transport confirmations.                                                              | Actor, conversation, target-message scope, privacy policy, and duplicate/coalescing rules.            |
| Typing/chat state    | Treat it as expiring conversation activity rather than durable message history.                                                                            | Expiry, stop notification, capability negotiation, bounded rate, and no-replay tests.                 |
| Encryption           | Retain the identity-encryption boolean with its narrow meaning. Model message security and negotiated requirements separately when needed.                 | Per-conversation security state and failure behavior, without inferring secrecy from an adapter flag. |
| Unknown kinds        | Preserve ingress evidence but suppress unrecognized delivery kinds. `plugin.custom` is the explicit extension envelope.                                    | Wildcard routing cannot bypass the capability decision; known kinds have explicit policies.           |
| Puppeting            | Keep original authorship, display attribution, and the authenticated sending account separate.                                                             | Per-account native refs, retry identity stability, and mutation authorization.                        |

Caption, alt text, and spoiler markers need actual descriptor/message-content
semantics and preservation through retention and rendering. Adding them only to
JSON Schema would disagree with the strict runtime descriptor parser. Retained
content already has a SHA-256 content reference; a source checksum is different
untrusted provenance and needs an algorithm and verification state. Do not add a
second ambiguous checksum field.

## XMPP is an interoperability reference

Implement one XMPP adapter for compatible clients and servers rather than one
adapter per client. Use existing XMPP implementations and their discovery and
protocol handling. Keep XML, JIDs, resource handling, and extension negotiation
inside the adapter. Shared MEDRE concepts describe meaning, not XMPP stanza
syntax. XEPs have individual maturity and namespace versions; citing a XEP is
not evidence that every client implements it.

| Existing standard                                                                                                                                     | MEDRE seam and integration rule                                                                                                                                                       |
| ----------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [XEP-0030: Service Discovery](https://xmpp.org/extensions/xep-0030.html)                                                                              | Negotiate features at the actual peer/room scope. Adapter-wide capability declarations are conservative limits, not proof that every destination supports a feature.                  |
| [XEP-0045: Multi-User Chat](https://xmpp.org/extensions/xep-0045.html)                                                                                | Treat rooms as destination contexts. Preserve occupant identity and session scope; do not equate a nickname with a stable authenticated person.                                       |
| [XEP-0359: Stable Stanza IDs](https://xmpp.org/extensions/xep-0359.html)                                                                              | Map trusted native IDs through NativeRef/NativeMessageRef while retaining the assigning `by` scope. Validate the assigner's advertised support before trusting IDs for deduplication. |
| [XEP-0461: Replies](https://xmpp.org/extensions/xep-0461.html)                                                                                        | Map to reply relations, preserving target context and native identity; let relation binding decide whether the destination target exists.                                             |
| [XEP-0308: Message Correction](https://xmpp.org/extensions/xep-0308.html)                                                                             | Map to edits only with the protocol's same-sender and target rules. A new stanza ID is distinct from the corrected message's ID.                                                      |
| [XEP-0424: Retraction](https://xmpp.org/extensions/xep-0424.html)                                                                                     | Preserve retraction semantics; never turn a display request into a promise of secure deletion. Apply destination ownership checks.                                                    |
| [XEP-0444: Reactions](https://xmpp.org/extensions/xep-0444.html)                                                                                      | Translate the sender's replacement reaction set deliberately, including removals; a whole set is not a sequence of independent additions.                                             |
| [XEP-0393: Message Styling](https://xmpp.org/extensions/xep-0393.html)                                                                                | Follow the text-styling rules rather than assuming every Markdown parser means the same thing. Retain readable text for targets without styling.                                      |
| [XEP-0184: Delivery Receipts](https://xmpp.org/extensions/xep-0184.html) and [XEP-0333: Displayed Markers](https://xmpp.org/extensions/xep-0333.html) | Keep receipt and displayed/read evidence distinct. Receipt scope and sender identity matter; neither is an internal retry instruction.                                                |
| [XEP-0085: Chat State](https://xmpp.org/extensions/xep-0085.html)                                                                                     | Map composing/paused and other negotiated states into ephemeral activity, not message bodies or generic online presence.                                                              |
| [XEP-0384: OMEMO](https://xmpp.org/extensions/xep-0384.html)                                                                                          | Keep encryption negotiation and cryptographic identity in the adapter/session. Static transport capabilities cannot prove a particular message was encrypted.                         |

XEP-0194 is **User Chatting**, not a server-side bot API. XEP-0330 is **Pubsub
Subscription**, not read receipts. Use the standards above instead of those
investigation references: [XEP-0194](https://xmpp.org/extensions/xep-0194.html),
[XEP-0330](https://xmpp.org/extensions/xep-0330.html).

## Rendering is semantic preservation, not one universal format

Keep the Renderer → RenderingResult → Adapter boundary. Decoders produce
canonical content and relations. Pure target renderers choose a native output
or an explicit degraded representation from RenderingContext and the target's
capabilities. Adapters send the rendered result rather than reformatting it.
Existing native renderer factories are the extension seam; a second templating
engine or protocol-specific branches in core are unnecessary.

Discovery belongs to adapter/session code. A destination's negotiated feature
snapshot needs to reach planning and the frozen rendering context without network
calls inside a renderer. That per-destination snapshot is not a wired shared
interface today; qualify a narrow query/snapshot seam with the first XMPP adapter,
rather than treating static AdapterCapabilities as peer negotiation. Bound and
invalidate session discovery state without turning core into an XMPP feature
registry.

Plain text remains the portable content floor, not a requirement to flatten all
features. A target can preserve styles, replies, media, and reactions when its
renderer and negotiated capabilities support them. Unsupported semantics need an
honest fallback or suppression, and rendering evidence should identify any loss.
For example, retain a reply's target relation even when rendering its quote as
text. Do not mix protocol fallback text back into the original message body and
then quote it a second time.

Before standardizing formatted content, collect Matrix, XMPP, and SimpleX input
fixtures. Prefer plain text plus a bounded, validated optional formatting model
over arbitrary HTML as the shared source of truth. Define style ranges and
Unicode indexing, links and mentions, code blocks, escaping, and sanitization
before claiming lossless interchange. Retain native formatting in its versioned
namespace until those common semantics are proven. Do not infer user identity
or permissions from a formatted mention.

## Ephemeral activity is a different lifecycle

[XEP-0085](https://xmpp.org/extensions/xep-0085.html) defines chat-specific states,
including composing and paused. Matrix has
[typing notifications](https://spec.matrix.org/latest/client-server-api/#typing-notifications)
with timeout behavior and ephemeral room events. Protocol support does not imply
that MEDRE's Matrix adapter bridges typing; it does not expose that capability.

An open CanonicalEvent payload can encode a typing flag, but the durable
admission/outbox/replay path does not give that flag an appropriate lifetime.
The intended seam is an optional, bounded activity path that reuses route and
context selection while expiring/coalescing activity by source actor and
conversation. Keep native protocol states in the adapter; translate only shared
meaning. Do not add durable typing events merely because the envelope accepts
them. Presence, typing, delivered, and displayed are distinct concepts.

When implementing it, start with a small typed activity value carrying source
actor/context, activity state, and expiry. Drop expired activity, avoid history
replay and offline backlog delivery, clear state on disconnect/stop, and respect
recipient support and operator privacy policy. A platform without activity
support receives no invented chat message. Implement this with real Matrix/XMPP
fixtures before introducing a generic activity framework or additional queues.

## Puppeting separates authorship from presentation and authentication

A display prefix, avatar, or nickname is attribution; it is not an authenticated
source identity. Preserve the original event's source identity and native
provenance even when sending through a platform account representing that user.
Never store credentials in canonical payloads, rendered content, or native refs.

The [Matrix Application Service API](https://spec.matrix.org/latest/application-service-api/)
provides a concrete namespace and masquerading model. That mechanism belongs in
a Matrix adapter/session, not in the universal event schema. XMPP room nicknames
are not equivalent to Matrix virtual users or user-authorized accounts.

Select and authorize the sending principal before delivery, and keep it stable
through retries. Persist a non-secret principal identifier with the outbound
native mapping if the adapter can use multiple principals. A mapping created
after sending is insufficient for retry recovery: persist the selected principal
with the dispatch/outbox record before the first attempt and recover that same
decision on retry. The existing contracts do not establish this multi-principal
authorization fact; it needs qualification before freezing that delivery feature.
Renderers can then omit a redundant bot prefix only from a trusted presentation
mode, never from an unverified payload flag claiming that a puppet is active. Existing
NativeMessageRef metadata can carry adapter-owned evidence, but core's present
relation binding is not proof of future multi-principal authorization. An edit
or deletion needs both the original source-author proof and authorization for
the particular target copy's sending principal. Never let an attribution string
or a profile change confer mutation authority.

This boundary can support bot attribution, virtual-user puppeting, or explicitly
authorized accounts without rewriting immutable source events or putting tokens
in core models. Add a core principal field only if multiple adapters demonstrate
a shared authorization requirement that adapter-owned mapping evidence cannot
satisfy. Puppeting is not a claim of end-to-end encryption across the bridge.

## Candidate integration seams

| Candidate           | Verified integration seam                                                                                                                                                                                                 | First proof                                                                                                      |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| XMPP                | Standard client/session protocol plus negotiated extensions; one adapter can serve many clients.                                                                                                                          | Text and room/direct contexts, stable-ID scope, replies/corrections, feature discovery, and honest fallback.     |
| SimpleX             | [Official bot API](https://github.com/simplex-chat/simplex-chat/blob/stable/bots/README.md): headless CLI, JSON WebSockets, correlated commands and asynchronous events; TypeScript SDK is optional for a Python adapter. | Local sidecar connection, unsolicited-event handling, reconnect/idempotence, text, and retained media contracts. |
| Delta Chat/chatmail | [Official JSON-RPC server](https://github.com/chatmail/core/blob/main/deltachat-rpc-server/README.md) offers a process boundary instead of embedding its Rust core.                                                       | Account/chat native identity, stdio process lifecycle, text, and attachment ownership.                           |

Use XMPP as the first conformance reference and SimpleX as a concrete privacy-
oriented integration. The implemented SimpleX adapter uses the official Python
SDK and an owned native worker; the CLI API discussion below describes an
alternative integration boundary. The SimpleX CLI API is unauthenticated and
localhost-bound by default; protect the local endpoint and keep it separate from
public runtime control surfaces. Dispatch by correlation first: a WebSocket
message with a `corrId` matching a pending command is a command response,
even when its `resp.type` is unknown. Complete that pending command with an
explicit unsupported-response error, release its pending state, and retain the
redacted tag as diagnostic evidence; never leave the caller waiting indefinitely.
Unknown **unsolicited events** (without a pending command correlation) may be
ignored without inventing canonical kinds or crashing the session. On disconnect,
fail outstanding commands explicitly; test unknown tags, reordered replies,
late replies, and reconnection against the pinned sidecar API. Pin that revision
in the adapter repository. There is no requirement to depend on the
TypeScript SDK or embed SimpleX's Haskell runtime in MEDRE core.

## Evidence required for a freeze

Freeze the envelope separately from each payload, activity, renderer, and
security contract. Schema shape alone cannot establish interoperability. Before
freezing a shared feature, require adapter-owned captured/sanitized examples,
codec round trips, target-native output and fallback assertions, cross-adapter
identity and relation tests, and lifecycle tests covering retry/replay behavior.
For ephemeral features, require the inverse: no durable replay and bounded expiry.

Prioritize an XMPP/Matrix fixture pair for rich content and chat activity, then
qualify the SimpleX process seam against those semantics. Build narrow features
from that evidence rather than adding every scout-listed field to the envelope.
Polls, contact cards, expiry, membership/roles, locations, and fan-out addressing
remain feature contracts to design against concrete providers; they are not
promised by the open payload mapping or a generic capability flag.

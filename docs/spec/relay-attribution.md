# Relay attribution and sender claims

Provider-owned attribution projection MAY distinguish a display name from its
unannotated sender claim and verification description. Prefix formatting MUST
NOT change sender identity or upgrade the assurance of a name carried in text.

The sender_raw and sender_short_raw template aliases select the provider's
unannotated names. When a provider does not supply them, formatting MUST fall
back to the existing sender and sender_short names. Explicit empty names MUST
remain empty. The sender_verification alias MUST report unknown when the
provider supplies no verification description. Existing sender aliases retain
the provider's default presentation, including trust annotations.

A bridge MAY expose these strings as provenance in outbound metadata. Receiving
an embedded display name or verification claim MUST NOT make it authoritative
for identity reconciliation, native target binding, or mutation permission.

The sender_compact alias MUST select at most the first five Unicode code points
of sender_raw. It MUST preserve an explicit empty raw name. This abbreviation
is presentation only; source metadata MUST retain the full provider claim.

## Structured relay context

Core's `build_bridge_context` and `normalize_bridge_context` define the shared
v1 context published in [bridge-context.schema.json](../schemas/bridge-context.schema.json).
Adapters MAY carry this context in their native metadata. It records canonical
source correlation, platform, conversation, full sender claims, origin label,
verification description and scoped native references independently of visible
prefixes. Raw native metadata and custom payloads MUST NOT be copied into it.

Context strings MUST be at most 1,024 Unicode code points; identifiers MUST NOT
be truncated. Context MUST contain at most 32 relation hints. Unknown fields
MUST be removed on receipt; invalid context MUST be ignored without discarding
the message. The builder retains at most the first 32 relations. Destinations
using text-only relation fallback MUST omit structured relation hints via
`include_relations=False`. These operations MUST NOT mutate the source event.

Received context MUST remain a remote relay claim. It MUST NOT replace the
actual native sender, local canonical event ID, source native reference, or
relation binding authority. Metadata transport does not imply that an ordinary
native client displays the original sender as its own authenticated identity.

## Plain body extraction

Renderers MAY use `extract_message_body` to select text without adding relation
fallbacks. A string `body` accompanied by `formatting` MUST take precedence over
a stale legacy `text` alias. Unstyled messages retain legacy text-first behavior.

## Prefix validation and origin precedence

Adapters MUST reject unsupported configured placeholders before delivery and
MUST reject formatter errors when rendering an unvalidated configuration.
Missing source values MUST remain distinct from unsupported placeholders.
Numeric truncation suffixes are available only on the declared label variables
of constrained-display transports; other transports MUST reject those suffixes.

Origin labels MUST resolve from the explicit route/context label, then the
supplied attribution snapshot, then the source adapter's configured label.
Explicit empty strings MUST suppress lower-precedence labels. Attribution
snapshots and projected fields MUST NOT override source envelope or
native-reference identifiers. Balanced unsupported fields (including format
specifiers and attribute/index access) MUST be rejected during validation;
unmatched literal braces retain their existing semantics. Provider renderers MUST
use `format_checked_relay_prefix` for configured or resolved templates and
reject formatting errors, including with unvalidated configuration. The legacy
`format_relay_prefix` retains its original lexical behavior.
Sender-label selection MUST NOT change solely because a message is a reaction.


## Shared rendering policy API

Providers use `compose_relation_text` from `medre.core.rendering.relations`
for supplied text and `extract_event_text` from
`medre.core.rendering.text_helpers` for kind-based body selection. The latter
is also exported by `medre.core.rendering`. The pre-release migration's
superseded relation helpers are removed once all providers adopt these APIs.
Presence events retain their kind-specific user/status description when
relation context is added.

The composer MUST describe every unrepresented relation in canonical tuple
order before authored message text, preserving that text once. Native
renderers MAY omit the exact relations represented by their protocol fields.
Reaction-event and delete-event payload bodies MUST NOT become additional
message text, including when every relation is represented natively.
An ordinary message carrying reaction context MUST retain its authored body.
An edited, deleted or reacted event without a matching relation MUST preserve
its kind with a neutral marker (`[edited]`, `[deleted]` or `[reacted]`), even
when it also carries reply/thread context. A natively represented matching
relation MUST NOT produce a duplicate marker.

Reactions MUST use an explicit nonblank canonical relation key without
altering its code points. A preview MUST come only from supplied target text
(`original_text` before `fallback_text`), normalize whitespace, and contain
at most 40 Unicode code points followed by `...` when abbreviated. Missing
quotes MUST be described as `an earlier message`; authored reaction payloads
and opaque IDs MUST NOT be treated as target content. These pure helpers
provide no storage access or retained-content read authority.

`destination_native_message_id` MUST honor a supplied core binding fact over
raw native references. Negative facts, wrong adapter/channel scopes and blank
IDs MUST NOT produce native targets. Direct embedding callers MAY supply a
matching native reference without a fact; callers requiring a core proof
MUST set `require_fact=True`. This lookup does not authorize native mutations.

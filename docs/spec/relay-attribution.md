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

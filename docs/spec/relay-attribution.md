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

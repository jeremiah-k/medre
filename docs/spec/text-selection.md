# Canonical text selection across transports

`medre.core.events.text.select_message_body` owns the closed precedence
contract for the body of a canonical text message. With optional `formatting`
metadata and a string `body`, select the styled `body`: its Unicode code-point
offsets cannot describe an older `text` alias. Otherwise, an unstyled
message selects `text` when present, falling back to `body` and then empty.

`StyledText.from_payload`, `extract_message_body` and provider-owned text
renderers MUST use the same selection. Relation enrichment MUST use that
selection when deriving fallback quotes and `original_text` from a retained
target event. It MUST preserve an explicitly supplied fallback and MUST NOT
substitute a stale body alias for an authoritative empty text.
Plaintext providers MUST reject a
selected non-string instead of stringifying it into native wire content; the
generic rendering helper uses permissive text coercion. The selector MUST NOT change source events, insert formatting,
or decide native relation authority. Relation fallback is applied _after_
selection, and provider budgets and prefixes remain transport-owned.

Native packet budgets, multipart planning, and submission authority remain
owned by their existing core/adapter contracts. Selecting message or quote
text does not grant a native submission or change its confirmation level.
Test selection against both styled/un-styled
messages across every supported adapter, including contact/group destinations
and relation fallbacks.

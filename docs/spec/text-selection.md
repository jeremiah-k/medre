# Canonical text selection across transports

`medre.core.events.text.select_message_body` owns the closed precedence
contract for the body of a canonical text message. With optional `formatting`
metadata and a string `body`, select the styled `body`: its Unicode code-point
offsets cannot describe an older `text` alias. Otherwise, an unstyled legacy
message selects `text` when present, falling back to `body` and then empty.

`StyledText.from_payload`, `extract_message_body` and provider-owned text
renderers MUST use the same selection. Plaintext providers MUST reject a
selected non-string instead of stringifying it into native wire content; the
legacy generic rendering helper may continue coercing text for backwards
compatibility. The selector MUST NOT change source events, insert formatting,
or decide native relation authority. Relation fallback is applied _after_
selection, and provider budgets and prefixes remain transport-owned.

Production MeshCore/Meshtastic output remains bounded by the currently
qualified single-packet renderer. Source text selection does not enable
fragmented native submission. Test selection against both styled/un-styled
messages across every supported adapter, including contact/group destinations
and relation fallbacks.

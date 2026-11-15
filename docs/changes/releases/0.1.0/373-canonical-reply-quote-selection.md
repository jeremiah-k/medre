# Select reply quotes from canonical message text

Use the shared text selector when deriving relation fallback quotes and
`original_text` metadata from retained target events. Unstyled `text`
takes precedence over a body alias, while styled offsets remain bound to
`body`. An authoritative empty text does not borrow stale body content.
Preserve explicit fallbacks, stored source events and native relation authority.

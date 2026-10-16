# Reject trailing newlines in route identifiers

Require route IDs to match their complete alphanumeric/underscore/hyphen
grammar. A trailing newline previously passed the typed model's regex match.
Use full-string validation and equivalent editor-schema patterns so malformed
identifiers are rejected before route expansion.

# Preview configured route topology

- Add `medre routes diagram` with per-adapter terminal summaries, full-target
  Mermaid route maps, and JSON output for structured consumers.
- Abbreviate room and address identifiers by default; exact identifiers are
  opt-in via `--full-context`. Abbreviation is not redaction. Keep endpoint
  identities independent of display labels and disambiguate colliding labels.
- Mark partial Mermaid previews, escape terminal controls and collapse matching
  reverse route groups into a bidirectional overview edge.

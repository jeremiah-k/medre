# Relation-complete graph lookups and read-only campaign evidence

- Conversation ancestry, membership repair and reaction classification use
  metadata projections with relations, independent of expiring quote reads.
  Live admission holds cannot truncate ancestor chains or hide reaction edges.
- Native peer campaign evidence queries the disposable fixture's complete
  canonical rows through strict read-only SQLite, without replay expiry writes
  or contention for the runtime's write reservation. The campaign collector
  is privileged test tooling, not a general-purpose content-read API.
- The radio inspection guide now accurately states that wire text and
  scoped handoff references are withheld. Relation-enrichment tests exercise
  their typed reply-context method and late excerpts do not adopt new owners.

No schema, adapter API version, package version, or retention policy changes.

# Reverse relation traversal

- Add a storage read API for listing unique source event IDs whose stored
  relations target a canonical event, ordered by first relation insertion.
- Document the existing `target_event_id` SQLite index as the authority for
  reverse relation lookups.
- Add deterministic coverage for duplicate relations from one source, multiple
  source events, and targets with no inbound relation edges.
- Establish the reverse-traversal primitive needed by later out-of-order
  conversation graph repair without changing canonical event evidence.

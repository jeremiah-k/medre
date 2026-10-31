# Add guarded, idempotent event-scoped collection

A new opt-in storage operation collects only isolated canonical text and
same-event excerpts, rechecking every core blocker in the same SQLite
transaction. An immutable metadata-only receipt makes collection idempotent;
external, physical, and unclassified copies are explicitly excluded.
No automatic execution is introduced.

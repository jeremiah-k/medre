# Add guarded, idempotent event-scoped collection

A new opt-in storage operation collects only isolated canonical text and
same-event excerpts, rechecking every core blocker in the same SQLite
transaction. An immutable metadata-only receipt makes collection idempotent;
external, physical, and unclassified copies are explicitly excluded.
No automatic execution is introduced.

Admission-identity contradictions refuse collection without deleting unexplained
content. Fresh and additive-upgraded databases share one collection-table DDL.
The operator-only collection API remains classified as a content lifecycle
operation in the storage contract.

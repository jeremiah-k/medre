# Conservative core content-copy classification

The event-scoped content release assessment MUST distinguish canonical text,
same-event relation excerpts, incoming relation excerpts, opaque envelope and
relation metadata, native/evidence records, attachments, and active owners.
The assessment returns only row counts and classifications; no opaque bytes.

Structured relation identity is durable metadata: an event's reply target and
relation type MAY remain after a separately stored quote excerpt is collected.
An arbitrary relation `key` or nonempty `metadata` is unclassified content and
MUST block collection. Incoming excerpts stored by _other_ events, including those resolved
through native message references rather than canonical event IDs, are separate
copies and MUST block a claimed event-scoped release. Unknown or shared copies
MUST remain blockers; an empty local blocker list is not a global erasure claim.
This classification alone authorizes no deletion and no retention scheduler.
The column-level ownership classes behind these probe classifications are
defined by the [storage column classification
contract](storage-column-classification.md), which this page's probe-level
distinctions remain authoritative for.

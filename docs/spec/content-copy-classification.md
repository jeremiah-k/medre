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
copies and MUST block a claimed event-scoped release. Admission/body row inconsistencies MUST block advisory eligibility, even
without a live content owner. Unknown or shared copies
MUST remain blockers; an empty local blocker list is not a global erasure claim.
This classification alone authorizes no deletion and no retention scheduler.
The [storage column classification contract](storage-column-classification.md)
defines the column-level ownership classes behind these probe
classifications; this page remains authoritative for the probe-level
distinctions.

Retained delivery evidence families — delivery receipts, delivery
observations, outbox records, native references, and durable ingress work —
are durable evidence, not copies that block by existing. A family blocks a
claimed release exactly when one of its registry-classified content columns
still carries a value on the assessed event's rows; only SQL NULL represents
absent diagnostic text, while reviewed JSON metadata columns additionally
recognize the empty object `'{}'`. The write-time separated
`delivery_evidence_diagnostics` side table is retained durable evidence: its rows survive collection and never
block it. The assessment reports these surfaces, and the release assessment
shares one blocker decision with the collector, labeled
`retained_evidence`.

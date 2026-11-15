# Collect settled routed events through the guarded collector

A settled routed event — admitted through durable ingress, fanned out to
ordinary delivery targets, with every mandatory owner released by guarded
terminal transitions — is collectable through `collect_event_content` only
when its retained evidence is classified and content-free. Production outbox
staging normally persists route and plan snapshots in `metadata.dispatch_context`;
these can carry opaque destination, origin, or policy values. Until their
lifetime has an explicitly reviewed representation, such snapshots still
block collection even after terminal settlement. No exemption is inferred
from the key name alone. Live-ownership gates (active ingress work,
active outbox claims, unsettled mandatory owners, unrevoked optional grants)
and the wholesale policy blockers (opaque envelope and relation records,
incoming excerpts owned by other events, radio plans, attachment
associations, operator decisions) refuse collection unchanged, and the
delete set is unchanged: canonical `event_body_content` rows, same-event
`event_relation_excerpts` rows, and the immutable collection receipt, in one
transaction.

Delivery evidence families (delivery receipts, observations, outbox records,
native references, durable ingress work) are retained durable evidence:
their rows survive collection, and a family blocks only while one of its
registry-classified content-potential columns carries a value on the
event's own rows. Only SQL NULL represents absent diagnostic text. Reviewed JSON metadata
columns additionally recognize the empty object `'{}'`; the column set is
derived from the declared storage column classification registry. Historical
rows with inline diagnostic text or non-empty provider metadata keep
blocking, conservatively. The write-time separated
`delivery_evidence_diagnostics` side table is retained durable evidence: it
survives collection, is never deleted by it, and never blocks it, so
recovery evidence remains valid after content release.

The read-only release assessment and the collector agree through one shared
blocker decision, and the assessment reports the evidence families and the
diagnostics surface with the `retained_evidence` label. The durable ingress
`provenance` admission marker is classified as immutable identity, matching
the receipt family's closed `source` provenance, and leaves the enforced
content inventory.

The read-only release assessment now treats missing admission identity,
unexplained body rows, missing uncollected bodies and recreated collected
content as explicit integrity blockers. The guarded collector independently
refuses those corrupt states; neither read path exposes message bytes.

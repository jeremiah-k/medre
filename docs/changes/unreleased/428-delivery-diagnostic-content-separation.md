# Separate delivery diagnostic content from delivery facts

Free-form diagnostic wording on delivery evidence — receipt error,
capability reason, and rendering evidence, outbox failure detail and error
summary, observation error, and ingress last error — is independently owned
content with a content lifetime. Storage separates that text at write time:
new evidence rows keep the legacy inline columns empty and the text lands in
the append-only `delivery_evidence_diagnostics` side table, keyed by the
evidence family, the parent row's identity, the attempt generation, and the
field name, inside the parent row's transaction. A diagnostic row never
exists without its parent evidence row.

Reads coalesce without rewriting: historical inline values win for immutable
receipt and observation rows. Mutable ingress claims prefer the latest
transition diagnostic; mutable outboxes prefer the current attempt. Explicit
clears suppress earlier wording without erasing history. Historical inline
values remain stored, never backfilled or erased. An outbox whose status is `sent` or `queued`
does not resurrect a cleared same-attempt failure from the retained diagnostic
rows. Successful handoffs write no new failure diagnostics; prior diagnostic
rows remain historical evidence even when the outbox later succeeds. Structured provider
metadata stays inline, content-classified, and conservative. Outbox
route-target text (`target_channel`, `target_address`) is classified as
immutable identity, matching the receipt family's route correlation, and
leaves the enforced content inventory. A missing diagnostics table is an
approved additive predecessor shape upgraded atomically at initialization.

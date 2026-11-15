# Typed exact-target recovery contract and preview

Selecting one persisted delivery by its full `(event, plan, adapter, channel)`
identity gains a typed, read-only observation contract in
`medre.core.observability.exact_target`. The selection key makes the
no-channel decision explicit at construction — `None` is the deliberate
no-channel identity and the empty string is refused before authority
normalization can fold it into `None`. The preview classifies the target with
a closed 14-token category vocabulary whose nine delivery-state tokens reuse
the shared recovery disposition authority, and reports the raw persisted
`attempt_number` and `active_attempt` counters; no field carries a
display-normalized effective generation that a write path could consume.

The preview exposes metadata only and never returns stored message text.
For outcomes needing source bytes, its replay-authorization read may durably
observe content-dependency expiry under a short write transaction. Terminal,
active-owner and uncertain-native outcomes remain visible after content is
released; only a malformed frozen snapshot becomes ambiguous, while snapshot
I/O errors propagate. Selection failures classify fail-closed: an identity with no outbox row
or contested live generations is `ambiguous`, superseded caller expectations
are `stale`, lost source content is `content_unavailable`, and a frozen
staged plan contradicting the row's persisted binding is
`route_or_plan_changed`. Fixed `scope` and always-false
`execution_authorized` fields keep the observation from doubling as a
mutation permit; later exact-destination work builds selector sharing,
decision authority and any CLI surface on this contract.

The result model is published as `docs/schemas/exact-target-preview.schema.json`
with parity tests pinning the category enum, required field set, and fixed
values against the Python model. The normative specification lives in
`docs/spec/exact-target-recovery.md`.

# core reliability

- Make storage-backed adapter ingress durably admit canonical events before routing
  and delivery, and defer admitted work when capacity/shutdown cannot transfer
  responsibility to the outbox. Operational deferrals remain pending without
  consuming the poison-work terminal retry budget.
- Bound ingress and fan-out in-memory concurrency, and give the active durable-ingress
  row a bounded shutdown grace period without unsafe immediate lease release. If an
  ingress operation suppresses cancellation, shutdown reports the unfinished worker
  and keeps pipeline/storage dependencies available rather than tearing them down.
- Add task-local structured correlation across ingress, plans, targets, outbox attempts,
  receipts, and replay execution.
- Reconstruct replay rendering from persisted historical rendering context and persist
  same-run duplicate suppression evidence for non-empty replay run IDs.
- Separate receipt lifecycle status from transport proof strength with
  `confirmation_level`; built-ins now report local queue, local transport, or remote
  service acceptance honestly, and storage enforces the closed evidence vocabulary.
- Add explicit thread capability planning; built-in adapters advertise deterministic
  fallback rather than unverified native thread semantics.
- Add Prometheus text export for a bounded aggregate projection of numeric/boolean
  diagnostics without labels, configured identifiers, or string-valued data.
- Extend conformance coverage and specifications for the new reliability guarantees.
- Centralize the delivery-confirmation vocabulary and expose structured outcome
  detail for durable deferral decisions instead of parsing diagnostic error text.

> **Storage compatibility note:** this change adds the required
> `delivery_receipts.confirmation_level` column without bumping prerelease schema
> version `1`. Existing SQLite databases created by an earlier prerelease build
> intentionally fail required-column validation and must be backed up/reset using
> the documented prerelease workflow; there is no in-place migration.

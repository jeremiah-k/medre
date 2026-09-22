# 207 — Post-handoff delivery observations and LXMF terminal evidence

- Added a transport-neutral, append-only `delivery_observations` ledger for facts
  that arrive after MEDRE has already handed a delivery attempt to an adapter.
  Core validates exact `outbox_id` + attempt correlation before persistence;
  observations never rewrite receipts or outbox lifecycle state. Attempt
  correlation reads the outbox row's stored attempt number, which advances at
  retry finalization rather than at retry claim; in that bounded claim window a
  prior-attempt callback is admitted, while a live next-attempt callback is
  rejected and lost if it arrives before finalization (documented in
  `docs/spec/routing-delivery.md` §13.3.1).
- Added `OutboundDeliveryObservationRecord` to `AdapterContext`. Adapters report
  post-handoff facts to core; they still do not write storage or control retry
  scheduling.
- LXMF now carries opaque attempt correlation through its asynchronous delivery
  tracking and persists callback-emitted terminal SDK states as delivery
  observations. MEDRE registers both LXMF delivery and failed callbacks and does
  not synthesize unreported terminal states. It snapshots each SDK callback's
  message hash and state before handing the update to the event loop, so later
  SDK mutation cannot change the recorded fact. Immediate handoff remains
  `sent/local_queue`.
- LXMF provider state and MEDRE evidence strength remain separate. An LXMF
  `delivered` observation is recorded with `confirmation_level="unknown"`; MEDRE
  does not synthesize an end-to-end acknowledgement claim.
- `medre trace`, event timelines, storage summaries, and evidence bundles include
  delivery observations and counts.
- This changes the prerelease SQLite shape by adding
  `delivery_observations`. Existing stamped prerelease databases that do not
  match the current shape are rejected by design and must be recreated; schema
  version remains `1` until the release compatibility boundary is declared.

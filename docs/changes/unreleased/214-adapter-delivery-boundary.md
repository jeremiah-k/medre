# Adapter delivery boundary convergence

MEDRE now exposes one closed adapter-to-core delivery contract instead of a
synchronous result plus transport-specific callback records.

- Successful `AdapterContract.deliver()` calls return `AdapterHandoffResult`
  with a closed disposition: `transport_handoff` or `deferred`.
- Asynchronous adapters report later facts through one
  `AdapterContext.report_delivery_feedback` sink using the tagged
  `DeliveryFeedback` union: `DeferredHandoffCompleted`,
  `DeferredHandoffFailed`, or `PostHandoffObservation`.
- Every asynchronous feedback fact carries one immutable
  `DeliveryAttemptProvenance`; duplicate event/plan/outbox/attempt mirror fields
  were removed from the adapter callback contract.
- `AdapterContext` no longer exposes the internal event bus or three separate
  outbound callbacks.
- `AdapterCodec` is decode-only. Outbound presentation remains exclusively in
  renderers.
- Delivery timing/evidence traits (`async_delivery`, `delivery_receipts`, and
  `ack_tracking`) were removed from `AdapterCapabilities`; per-attempt handoff
  and confirmation facts now express those semantics directly.
- Deferred hand-off storage finalization is named
  `DeferredHandoffFinalization` / `finalize_deferred_handoff` and remains
  atomic with outbox and optional native-reference updates. Completion may win
  the queued-receipt persistence race while the outbox is still `in_progress`;
  immutable attempt provenance remains sufficient authority in that window.
- Built-in Matrix, Meshtastic, MeshCore, LXMF, fake adapters, and runtime drills
  now use the same boundary. A synthetic fifth-adapter conformance test proves
  immediate handoff, deferred completion/failure, and post-handoff observation
  without pipeline-specific platform code.
- Native identifiers on hand-off facts are non-empty strings or `None`; absence is
  never represented by an empty transport identifier. Fake adapters normalize
  absent route channels to `None`, report confirmation levels no stronger than
  the boundary they simulate, and the delivery-result JSON Schema enforces the
  same native-ID and deferred-message invariants as the runtime contract.

No persisted database schema version changes are required; the new types are
process-local adapter/runtime contracts and reuse the existing receipt, outbox,
native-reference, and observation evidence models.

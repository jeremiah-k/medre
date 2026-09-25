# Transport attempt provenance and async callback authority

- Add an immutable `DeliveryAttemptProvenance` envelope at delivery admission,
  carrying exact delivery identity, outbox generation, dispatch source, and
  optional replay-run origin through asynchronous adapter hand-off.
- Carry the envelope through Meshtastic queue terminal/native-reference callbacks
  and LXMF delivery observations without placing framework metadata on the wire.
- Make queue terminal lineage envelope-authoritative: validate against durable
  outbox and exact-generation immutable receipt evidence loaded by `outbox_id`,
  use queued receipts only for immutable parent/retry/render linkage, preserve
  retry policy through terminal evidence, and reject contradictions instead of
  reconstructing source/run from receipt timing or mutable-row fallback.
- Apply the same exact-generation receipt provenance fence to post-handoff
  delivery observations and queued-to-sent finalization, failing closed when
  immutable receipt history cannot be validated while preserving the legitimate
  callback-before-receipt race. Outbox-scoped reads ensure malformed identity
  evidence cannot disappear through pre-filtering.
- Restore the independent terminal native-channel fence so transport-reported
  channel evidence cannot contradict the admitted target even when the attempt
  envelope itself is valid.
- Route attempt-envelope validation failures on contradictory renderer output
  through the renderer failure path: persist a `RENDERER_FAILURE` attempt
  receipt with failure evidence instead of raising an unclassified error.
- Validate renderer event/adapter/channel identity before every adapter hand-off,
  including direct/outbox-less delivery where no attempt envelope exists.
- Add deterministic coverage for live/replay/retry races, contradictory lineage,
  Meshtastic callback propagation, and LXMF delivery observations.
- Require the envelope on every asynchronous callback record: terminal, queued-to-sent,
  and observation records reject construction without `attempt_provenance`, the legacy
  source-preference selector for envelope-less callbacks is removed, and the supplemental
  sent receipt always carries the envelope's dispatch provenance.

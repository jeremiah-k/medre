# Transport attempt provenance and async callback authority

- Add an immutable `DeliveryAttemptProvenance` envelope at delivery admission,
  carrying exact delivery identity, outbox generation, dispatch source, and
  optional replay-run origin through asynchronous adapter hand-off.
- Carry the envelope through Meshtastic queue terminal/native-reference callbacks
  and LXMF delivery observations without placing framework metadata on the wire.
- Make queue terminal lineage envelope-authoritative: validate against durable
  outbox/queued-receipt evidence, use queued receipts only for immutable parent
  linkage, and reject contradictions instead of reconstructing source/run from
  receipt timing or mutable-row fallback.
- Apply the same queued-receipt provenance fence to post-handoff delivery
  observations and queued-to-sent finalization, failing closed when immutable
  receipt history cannot be validated while preserving the legitimate
  callback-before-receipt race.
- Add deterministic coverage for live/replay/retry races, contradictory lineage,
  Meshtastic callback propagation, and LXMF delivery observations.

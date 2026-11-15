# Remove the reserved capability policy seam

- Drop `RenderingContext.capability_policy` and the mirrored
  `RenderingEvidence.capability_policy` field. The seam was reserved, never
  populated by the pipeline, and persisted as a permanent `null` key in
  `rendering_evidence`.
- Keep `CapabilityDecisionResolver` plus `delivery_strategy` as the single
  capability decision authority; there is no second policy layer and no
  forward-compatibility field anticipating one.
- Remove the reserved-field contract paragraphs from the adapter runtime
  specification, conformance limitations, and transport limitations appendix.

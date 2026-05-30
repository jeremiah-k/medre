# Transport Capability Semantics and Delivery Evidence

Document implemented transport capability semantics, rendering budget behavior, suppression/truncation evidence, relation/reaction degradation, replay parity expectations, and unknown capability behavior.

## Changed

- `docs/spec/adapter-runtime.md`: Added CapabilityLevel to three-level decision mapping table (§ 6.2.1). Updated `RenderingContext.capability_level` from reserved to populated from CapabilityDecision (§ 10.1). Updated evidence signal description for `capability_level` (§ 10.5).

- `docs/spec/routing-delivery.md`: Added unknown event kind passthrough semantics note (§ 6.3.3). Added fail-closed note for unknown relation types (§ 6.3.4). Added known gap about dormant fallback capability level in production transport profiles (§ 6.3.2).

- `docs/spec/diagnostics-evidence.md`: Added § 14.8 capability-evidence derivation in report dicts (suppression_reason, capability_field, capability_level, delivery_strategy). Added § 14.8.2 delivery_state_by_target enrichment documentation. Added § 14.9 rendering budget enforcement and evidence (LXMF max_text_chars, truncation metrics, RE_RENDER gap).

- `docs/spec/conformance.md`: Added § 7 transport capability semantics and delivery evidence conformance with test coverage table and six documented known gaps.

- `docs/spec/appendices/transport-limitations.md`: Fixed stale "Relations should be capability-gated" language to reflect implemented behavior. Added § 6 capability semantics known gaps (dormant fallback, no live validation, RE_RENDER context, in-memory replay evidence, thread deferral, capability_policy reserved).

- `docs/ops/recovery-and-replay.md`: Added capability filtering during replay section documenting `_filter_plans_by_capability` behavior, all-suppressed ReplayResult output shape, and capability re-evaluation at replay time.

- `docs/ops/troubleshooting.md`: Added capability suppressed row to Failure Category Quick Reference. Added Capability Suppression diagnosis section with SQL queries and resolution steps. Added capability_suppressed to Inspect Follow-Up Quick Reference.

- `docs/ops/operator-workflows.md`: Added `capability_suppressed` to failure kinds table and common failure patterns table. Added "Suppressed why (capability)" investigation workflow.

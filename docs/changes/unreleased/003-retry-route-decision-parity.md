# Retry Route-Decision Parity

Persist route-decision metadata (capability_level, delivery_strategy,
capability_field, capability_reason, deadline) in outbox item metadata at
creation time, and recover it during retry reconstruction so retry delivery
matches the original live delivery decision.

## Changed

- `src/medre/core/engine/pipeline/runner.py` — `_create_outbox_for_delivery()` now persists `capability_level`, `delivery_strategy`, `capability_field`, `capability_reason`, and `deadline` in the outbox `metadata` dict alongside destination keys
- `src/medre/core/engine/pipeline/retry_plan.py` — `reconstruct_retry_delivery_plan()` now recovers capability/strategy/deadline from `item.metadata` instead of always defaulting to `capability_level=None` and `strategy="direct"`. Updated module and function docstrings to document the new recovery semantics
- `docs/spec/routing-delivery.md` — added §6.4 (Route-Decision Metadata Persistence); updated §6.1 capability_field doc; updated §7.4 retry flow to document metadata recovery

## Added

- `tests/test_retry_route_decision_parity.py` — 18 new tests covering: capability level roundtrip (fallback/native/unsupported), deadline roundtrip, legacy metadata degradation (empty/None/destination-only), strategy validation (all 6 known methods + unknown fallback), capability field/reason roundtrip, full roundtrip with all fields
- `tests/test_retry_plan_reconstruction.py` — replaced `TestCapabilityFieldsUnreconstructed` with `TestCapabilityFieldsRoundtrip` (11 tests covering recovery, degradation, validation, and full roundtrip)
- `tests/test_outbox_route_decision_metadata.py` — 7 new tests verifying the persistence side: route-decision keys present in metadata, None values stored correctly, destination keys coexist with route-decision keys

# Delivery coordinator decomposition

## Core pipeline

- Extract per-target delivery orchestration from `PipelineRunner` into an
  orchestration-only `DeliveryCoordinator`. Routing/planning, target execution,
  retry/lifecycle decisions, and outbox transitions remain owned by their
  existing authorities.
- Preserve the established preflight order and bounded ordered fan-out while
  replacing one deeply nested delivery closure with named coordination phases.
- Once a delivery-capacity slot is acquired, release it on every exit path.
  Cancellation during outbox creation no longer leaks capacity, and an outbox
  finalization failure no longer strands capacity or stale in-flight shutdown
  identity. Outbox-finalization errors still propagate to the caller.
- Add architecture guards proving the coordinator performs no direct storage
  mutations and resource regressions covering cancellation/finalization faults.

# Delivery coordinator review fixes

## Runtime and evidence

- Capacity-controller replacement now goes through
  `PipelineRunner.set_capacity_controller()`, so runtime drills and scenarios
  update the `DeliveryCoordinator` authority instead of mutating an obsolete
  runner-local field.
- Failed adapter and renderer outcomes now retain the persisted
  `DeliveryReceipt` already produced by `TargetDeliveryService`, preserving
  outcome-to-evidence correlation. Coordinator outcomes and outbox finalization
  use the exact stored row, including its storage-assigned receipt sequence.
- The runner no longer mirrors delivery capacity state; the coordinator is the
  single owner of delivery-capacity wiring.
- Run-session capacity-rejection setup now fails cleanly before mutating runtime
  capacity state when no pipeline runner is available, and its operator guidance
  correctly describes the durable suppression receipt.

## Maintenance

- Architecture guards now assert the concrete coordinator receiver, the single
  capacity-wiring boundary, and the unconditional placement/order of lease,
  outbox-finalization, and capacity-release cleanup.
- Delivery-coordinator audit text and tests were reconciled with the extracted
  architecture.

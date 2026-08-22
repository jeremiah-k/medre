# Delivery coordinator review fixes

## Runtime and evidence

- Capacity-controller replacement now goes through
  `PipelineRunner.set_capacity_controller()`, so runtime drills and scenarios
  update the `DeliveryCoordinator` authority instead of mutating an obsolete
  runner-local field.
- Failed adapter and renderer outcomes now retain the persisted
  `DeliveryReceipt` already produced by `TargetDeliveryService`, preserving
  outcome-to-evidence correlation.
- The runner no longer mirrors delivery capacity state; the coordinator is the
  single owner of delivery-capacity wiring.

## Maintenance

- Architecture guards now assert the concrete coordinator receiver and the
  single capacity-wiring boundary.
- Delivery-coordinator audit text and tests were reconciled with the extracted
  architecture.

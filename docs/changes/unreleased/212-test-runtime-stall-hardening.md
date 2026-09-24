# Test and diagnostic runtime stall hardening

- Release the synthetic delivery-capacity reservation used by the
  `capacity_rejection` run-session scenario and drill before normal runtime
  shutdown. The scenario previously left its intentionally occupied semaphore
  counted as real in-flight work, so every invocation waited the configured
  10-second shutdown-drain deadline even after the scenario had completed.
- No production capacity limits, retry delays, or shutdown deadlines changed.
  The runtime change only releases test/diagnostic state that MEDRE itself
  acquired solely to simulate capacity exhaustion.

# Test and diagnostic runtime stall hardening

- Release the synthetic delivery-capacity reservation used by the
  `capacity_rejection` run-session scenario and drill before normal runtime
  shutdown. The scenario previously left its intentionally occupied semaphore
  counted as real in-flight work, so every invocation waited the configured
  10-second shutdown-drain deadline even after the scenario had completed.
- Keep the normal Python-version test matrix, but collect coverage only on the
  existing Python 3.13 coverage leg instead of tracing the same suite four
  times. The coverage leg emits only the XML report consumed by Codecov; the
  unused HTML report is no longer generated. Matrix jobs use concise pytest
  output, still report skips/xfails and the 25 slowest tests, and explicitly
  retain the default exclusions for local-integration and soak tiers when
  overriding pytest's marker expression in CI.
- No production capacity limits, retry delays, or shutdown deadlines changed.
  The runtime change only releases test/diagnostic state that MEDRE itself
  acquired solely to simulate capacity exhaustion.

# Validate direct runtime configuration before assembly

- Reject invalid adapter wrapper flags/kinds and duplicate adapter or route IDs
  when callers construct `RuntimeConfig` directly instead of loading YAML.
- Validate attachment budgets in the programmatic runtime path, matching YAML
  and environment-configured deployments. Invalid declarations fail before
  storage or adapter construction.

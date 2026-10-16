# Fail-closed route and retry configuration

Reject non-boolean route `enabled` values rather than treating quoted strings
as truthy. Adapter IDs in route lists are now required to be normalized
strings; implicit scalar-to-string coercion is removed. Explicit `null`
`policy`, `retry`, `context_map`, and allowlist values now fail validation
instead of silently relaxing configured controls or raising a raw exception.

Global retry polling intervals and per-route retry backoff/delay values must
be finite. The same rules are enforced for YAML, environment overrides, and
programmatic runtime construction before the retry worker is created.

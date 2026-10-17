# Guided bridge: clearer terminal choices and next steps

- Interactive bridge setup accepts numbered adapter and direction choices in
  addition to exact identifiers. Scripted flags continue to require canonical
  instance IDs and direction values; generated YAML is unchanged.
- An empty first context prompt now asks again instead of ending with a late
  validation error. Two-way route previews explain the expected topology-cycle
  warnings without suggesting that live echo prevention is guaranteed.
- After saving, setup prints copyable commands with the actual output path.
  Offline smoke examples use the compiled route legs so reverse-only routes
  and bidirectional mappings suggest the correct source and context.
- Matrix's provider-owned preflight rejects unmatchable alias-form inbound
  contexts even without an allowlist; the compatibility snapshot is advanced
  to the bundled source-context fix. Adapter-owned offline-sample tests expect
  transport-prefixed instance IDs supplied by MEDRE core; the compatibility
  snapshot must be refreshed again after the adapter test fixes are committed.

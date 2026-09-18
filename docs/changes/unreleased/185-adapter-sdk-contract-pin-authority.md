# Adapter SDK contract pin authority

## CI and dependency validation

- Adapter installed-SDK contract tests now derive expected package versions from
  the exact pins in `pyproject.toml` instead of duplicating version literals in
  test code. Renovate dependency bumps therefore fail only when an SDK surface
  MEDRE consumes actually changes or when the installed environment disagrees
  with project metadata.
- Contract probes bind the call shapes MEDRE actually uses instead of freezing
  unrelated SDK defaults, parameter ordering, numeric enum values, or payload
  limits that can change compatibly.
- MeshCore APP_START contract coverage accepts additive SDK handshake arguments
  while continuing to require exactly one handshake on initial connection and
  one on SDK-owned reconnect.
- LXMF lifecycle coverage checks callable/observable ownership surfaces rather
  than source-code substrings for private implementation details. Deterministic
  local integration remains the executable RNS/LXMF lifecycle check.
- A default-suite structural guard keeps the LXMF, Meshtastic, and MeshCore
  optional SDK groups exact-pinned without freezing their version numbers in a
  second authority.

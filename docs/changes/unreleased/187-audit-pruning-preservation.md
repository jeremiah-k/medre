# 187 — Audit Pruning Preservation

Follow-up consistency fixes for the audit/history consolidation. Runtime behavior
is unchanged.

- Mechanical prerelease-shape, native-metadata version, structured
  `channel_room_map`, and strict transport-attribution guards are preserved in a
  focused test module instead of depending on the removed generated
  current-state inventory.
- Current developer/operator references and the consolidated prerelease
  changelog no longer duplicate SDK pin versions; `pyproject.toml` remains
  the exact dependency-version authority.
- The native-metadata schema guide points to the focused parity guard that now
  owns source/schema/example drift detection.

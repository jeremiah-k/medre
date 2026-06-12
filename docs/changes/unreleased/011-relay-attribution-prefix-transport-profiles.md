# Relay Attribution Prefix — Transport Profile Documentation

Document cross-transport relay attribution prefix model, config fields,
and truncation semantics for all four transports (Matrix, Meshtastic,
MeshCore, LXMF).

## Changed

- `docs/spec/transport-profiles/meshcore.md`: Added `meshcore_relay_prefix`
  config field (string, default `""`). Added §Relay Attribution Prefix
  describing template syntax, truncation (byte-safe before `max_text_bytes`),
  metadata keys, and attribution caveat.
- `docs/spec/transport-profiles/lxmf.md`: Added `lxmf_relay_prefix` config
  field (string, default `""`). Added §Relay Attribution Prefix describing
  template syntax, truncation (character-budget before `max_text_chars`),
  metadata keys, and attribution caveat.
- `docs/spec/transport-profiles/meshtastic.md`: Added §Relay Attribution
  Prefix with authoritative supported template variable table (canonical
  `source_*` fields plus aliases), formatting rules, truncation semantics,
  metadata keys, Matrix-bound prefix cross-reference, and attribution caveat.
- `docs/spec/transport-profiles/matrix.md`: Added §Relay Attribution Prefix
  documenting Matrix prefix resolution (now target-local via
  `MatrixConfig.relay_prefix`), application points,
  metadata keys, and attribution caveat.
- `docs/spec/routing-delivery.md`: Added §17.5 (Relay Attribution Prefix —
  Single Authority Caveat) documenting that prefix text is human-readable
  attribution only, not delivery evidence; metadata namespace is
  authoritative for machine-readable provenance.
- `docs/schemas/adapter-config.schema.json`: Added `meshcore_relay_prefix`
  (string, default `""`) to MeshCoreConfig. Added `lxmf_relay_prefix`
  (string, default `""`) to LxmfConfig.
- `docs/dev/relay-prefix-attribution-audit.md`: Updated MeshCore and LXMF
  outbound prefix behavior sections to reflect current implementation
  (`meshcore_relay_prefix`, `lxmf_relay_prefix`). Updated Cross-Transport
  Gaps §1 and §3 to reflect that MeshCore and LXMF now have prefix support.

## Configuration

- `meshcore_relay_prefix`: New optional string field on MeshCore adapter
  config. Default `""` (no prefix). When non-empty, the value is a
  `{placeholder}` template resolved by the shared prefix formatter against
  relay attribution data.
- `lxmf_relay_prefix`: New optional string field on LXMF adapter config.
  Default `""` (no prefix). Same template syntax as `meshcore_relay_prefix`.

Existing configs without these fields use defaults (no prefix). No config
migration required.

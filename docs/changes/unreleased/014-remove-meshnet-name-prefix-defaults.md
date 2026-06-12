# Remove meshnet_name from Prefix Defaults and matrix_relay_prefix from MeshtasticConfig

Removed `matrix_relay_prefix` field from `MeshtasticConfig`. Matrix prefix
is now target-local only via `MatrixConfig.relay_prefix`. Removed
`meshnet_name` from all transport profile config tables, prefix template
variable tables, and operator documentation. `{origin_label}` is the single
MEDRE-generic source label for cross-platform prefix templates.

## Changed

- `src/medre/config/adapters/meshtastic.py`: Removed `matrix_relay_prefix`
  field and its docstring. Updated `radio_relay_prefix` docstring to list
  `{origin_label}` instead of `{meshnet_name}` in supported template
  variables.
- `docs/spec/transport-profiles/meshtastic.md`: Removed `meshnet_name` and
  `matrix_relay_prefix` from Configuration Fields table. Replaced
  `{source_meshnet_name}` / `{meshnet_name}` with `{source_origin_label}` /
  `{origin_label}` in template variable tables. Updated Matrix-Bound Prefix
  section to remove `matrix_relay_prefix` fallback reference.
- `docs/spec/transport-profiles/matrix.md`: Removed `meshnet_name` from
  template recommendations. Updated backward-compat fallback description to
  remove `MeshtasticConfig.matrix_relay_prefix` reference.
- `docs/spec/transport-profiles/meshcore.md`: Removed `meshnet_name` from
  Configuration Fields table. Updated prefix recommendation to prefer
  `{origin_label}` without mentioning `{meshnet_name}`.
- `docs/spec/transport-profiles/lxmf.md`: Removed `meshnet_name` from
  Configuration Fields table. Updated prefix recommendation to prefer
  `{origin_label}` without mentioning `{meshnet_name}`.
- `docs/spec/routing-delivery.md`: Removed `meshnet_name` from §17.5.3
  Label and Identity Distinctions table. Updated recommendation text.
  Removed `meshnet_name` from shared formatter variable list. Removed
  `MeshtasticConfig.matrix_relay_prefix` backward-compat fallback reference.
- `docs/dev/relay-prefix-attribution-audit.md`: Removed `meshnet_name` from
  all per-transport variable tables and config tables. Removed
  `matrix_relay_prefix` from MeshtasticConfig fields. Updated cross-transport
  gap descriptions. Updated origin_label distinction table.
- `docs/schemas/adapter-config.schema.json`: Removed `matrix_relay_prefix`
  from MeshtasticConfig schema.
- `docs/ops/configuration.md`: Removed `meshnet_name` from Meshtastic,
  MeshCore, and LXMF config reference tables.
- `docs/ops/running-medre.md`: Removed `meshnet_name` from TOML examples.
- `docs/ops/live-validation/*.md`: Removed `meshnet_name` from config
  snippets.
- `src/medre/config/sample.py`: Replaced `meshnet_name` with `origin_label`
  in sample config.
- `examples/configs/*.toml`: Replaced `meshnet_name` with `origin_label`.
  Removed `matrix_relay_prefix` from Meshtastic sections. Added
  `relay_prefix` to Matrix sections where prefix examples were shown.

## Configuration

- `origin_label`: Operator-facing string on all four adapter configs.
  Replaces `meshnet_name` as the prefix template variable for
  cross-platform relay attribution. Default `""`.
- `matrix_relay_prefix`: **Removed** from `MeshtasticConfig`. Matrix prefix
  is `MatrixConfig.relay_prefix` (target-local) only.
- `meshnet_name`: Removed from transport profile config tables. The
  underlying field no longer exists on adapter configs.

Existing adapter configs that contain `meshnet_name` or `matrix_relay_prefix`
**will not load** — these keys are no longer recognized by the adapter config
loader. Operators must rename `meshnet_name` to `origin_label` and
`matrix_relay_prefix` to `MatrixConfig.relay_prefix` before reloading the
config. Route policy sub-tables reject unknown keys, but `meshnet_name` and
`matrix_relay_prefix` were adapter-level fields, so existing route configs are
unaffected.

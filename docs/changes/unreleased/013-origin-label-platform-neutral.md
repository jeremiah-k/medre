# origin_label — Platform-Neutral Source Label

Added platform-neutral `origin_label` to all adapter configs. Added
`source_origin_label` to `RelayAttribution` with `{origin_label}` template
alias. Matrix prefix is now target-local via `MatrixConfig.relay_prefix`.
LXMF renderer is target-aware. Meshtastic/MeshCore/LXMF prefixes now
describe the source, not the target, via source-attribution registry.

## Changed

- `docs/spec/routing-delivery.md`: Rewrote §17.5 as Relay Attribution
  Prefix with six subsections: Purpose and Scope, origin_label definition,
  Label and Identity Distinctions, Renderer Lookup, Shared Formatter and
  Variable Schema, False Delivery Claims. Defines `origin_label` as the
  canonical platform-neutral operator label, distinct from `meshnet_name`
  (transport-specific), `source_sender_id` (native ID), and `route_id`.
  Documents source-attribution registry lookup and target-local Matrix
  prefix model.
- `docs/spec/transport-profiles/matrix.md`: Updated §Relay Attribution
  Prefix to describe two-path prefix resolution (target-local
  `MatrixConfig.relay_prefix` preferred, `MeshtasticConfig.matrix_relay_prefix`
  backward-compat fallback). Added `origin_label` and `relay_prefix` to
  Configuration Fields table. Recommends `{origin_label}` over
  `{meshnet_name}` in cross-platform templates.
- `docs/spec/transport-profiles/meshtastic.md`: Added `origin_label` to
  Configuration Fields table. Added `{source_origin_label}` to canonical
  template variables and `{origin_label}` alias. Added
  `relay_prefix_origin_label` metadata key. Updated Matrix-Bound Prefix
  section to describe target-local Matrix prefix and source-attribution
  registry lookup.
- `docs/spec/transport-profiles/meshcore.md`: Added `origin_label` to
  Configuration Fields table. Updated §Relay Attribution Prefix to
  recommend `{origin_label}` over `{meshnet_name}` and note
  source-attribution registry.
- `docs/spec/transport-profiles/lxmf.md`: Added `origin_label` to
  Configuration Fields table. Updated §Relay Attribution Prefix to
  describe target-aware prefix resolution and source-attribution registry
  lookup for `{origin_label}`.
- `docs/dev/relay-prefix-attribution-audit.md`: Added origin_label section
  documenting the concept, source-attribution registry, and distinction from
  other labels. Updated per-transport outbound prefix sections to note
  `origin_label` resolution and target-local/target-aware models. Updated
  Cross-Transport Gaps: gap 3 (prefix config ownership) now describes
  target-local model; gap 5 (namespace inconsistency) notes `origin_label`
  as the MEDRE-generic label.
- `docs/schemas/adapter-config.schema.json`: Added `relay_prefix` (string,
  default `""`) to MatrixConfig. `origin_label` was already present from
  Wave 0.

## Configuration

- `origin_label`: Operator-facing string on all four adapter configs
  (`MatrixConfig`, `MeshtasticConfig`, `MeshCoreConfig`, `LxmfConfig`).
  Default `""`. Used as `{origin_label}` in prefix templates.
- `relay_prefix`: New string field on `MatrixConfig`. Default `""`.
  Target-local prefix template for Matrix outbound renders. When empty,
  the renderer falls back to the source adapter's `matrix_relay_prefix`.

Existing configs without these fields use defaults. No config migration
required.

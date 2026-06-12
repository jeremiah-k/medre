# Adapter Projection / Core Boundary Documentation

## Summary

Document the structural boundary between core rendering (generic
`RelayAttribution` fields, shared `format_relay_prefix` formatter) and
adapter-adjacent native projection (transport-specific identity key mapping).
Document the `origin_label` precedence chain including route-level
`source_origin_label`/`dest_origin_label`.

## Behavior

- Core rendering operates exclusively on generic `RelayAttribution` fields.
  Core does not inspect Matrix MXIDs, Meshtastic node info longname/shortname,
  MeshCore pubkey prefixes, or LXMF identity hashes.
- Adapter-adjacent projection modules map transport-specific native metadata
  onto the generic fields. This is where platform-specific identity extraction
  lives.
- `origin_label` precedence: route-level `source_origin_label`/`dest_origin_label`
  (after direction-aware expansion) > adapter config `origin_label` (via
  source-attribution registry) > empty string.
- Channel-specific origin labels are not implemented. The workaround is to use
  separate routes per channel with their own direction-aware labels.
- mmrelay `KEY_MESHNET` is an external wire-compatibility field, not a MEDRE
  model/config/template variable.

## Affected files

- `docs/dev/relay-prefix-attribution-audit.md`: Added Projection Architecture
  and Core Boundary section. Updated origin_label section to document route-level
  labels and full precedence chain. Updated per-renderer enrichment paragraphs.
  Added Cross-Transport Gap 9 (no per-channel origin labels).
- `docs/spec/routing-delivery.md`: Updated §17.5.2 to document route-level
  labels and precedence chain. Updated §17.5.4 to describe precedence-based
  renderer lookup. Added §17.5.8 Projection Architecture and Core Boundary.
- `docs/spec/transport-profiles/matrix.md`: Updated Renderer lookup paragraph
  to describe precedence chain.
- `docs/spec/transport-profiles/meshtastic.md`: Updated Template syntax
  paragraph and Matrix-Bound Prefix section with precedence chain.
- `docs/spec/transport-profiles/meshcore.md`: Updated Template syntax
  paragraph with precedence chain.
- `docs/spec/transport-profiles/lxmf.md`: Updated Template syntax paragraph
  with precedence chain.
- `docs/schemas/routing-config.schema.json`: Updated source_origin_label and
  dest_origin_label descriptions to mention precedence over adapter config.
- `examples/configs/live-matrix-meshtastic.toml`: Added source_origin_label
  example comment on bidirectional route.
- `examples/configs/fake-bridge-smoke.toml`: Added source_origin_label
  example on bidirectional route.
- `src/medre/config/sample.py`: Added source_origin_label/dest_origin_label
  comments to route example.

## Attribution Surface

Canonical template variables (unchanged from fragment 015):

- `{origin_label}` — operator-defined source label (route-level > adapter-level)
- `{sender}` — primary sender display name
- `{sender_short}` — abbreviated sender label
- `{sender_id}` — native sender identifier
- `{sender_handle}` — sender handle or address
- `{platform}` — source platform name
- `{route_id}` — matched route identifier
- `{channel}` — source room or channel ID

Old variables `{longname}`, `{shortname}`, `{shortname5}`, `{from_id}`,
`{meshnet_name}` remain unknown placeholders.

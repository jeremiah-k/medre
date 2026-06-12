# Clean Attribution Surface — Canonical Variables Only, mmrelay Wire Isolation

Finalized the attribution surface to use only canonical template variables.
Removed all documentation of old compatibility aliases (`{longname}`,
`{shortname}`, `{shortname5}`, `{from_id}`) as supported formatter variables.
Documented mmrelay `KEY_MESHNET` as a temporary, isolated wire compatibility
field.

## Changed

- `docs/dev/relay-prefix-attribution-audit.md`: Removed all "Compatibility
  aliases" tables from Matrix, Meshtastic, MeshCore, and LXMF sections.
  Replaced with explicit statement that old variables are unknown placeholders.
  Updated Meshtastic default from `"{shortname5}[M]: "` to
  `"{sender_short}: "`. Added KEY_MESHNET isolation section documenting
  it as temporary mmrelay wire compatibility only. Updated origin_label
  distinction table to use `{sender_id}` instead of `{from_id}`. Updated
  Cross-Transport Gaps to reference canonical variable names.
- `docs/spec/transport-profiles/meshtastic.md`: Rewrote template variable
  tables to list actual canonical fields and preferred aliases from source.
  Updated default from `"{shortname5}[M]: "` to `"{sender_short}: "`.
  Updated known limitation to reference `source_sender_label`.
- `docs/spec/transport-profiles/meshcore.md`: Updated prefix default note
  to reference `{sender}`/`{sender_short}` instead of `{longname}`/
  `{shortname}`.
- `docs/spec/transport-profiles/lxmf.md`: Updated prefix default note
  to reference `{sender}`/`{sender_short}` instead of `{longname}`/
  `{shortname}`.
- `docs/spec/routing-delivery.md`: Updated §17.5.3 to use `{sender_id}`
  instead of `{from_id}`. Updated §17.5.5 to list canonical aliases
  (`sender`, `sender_short`, `sender_id`, `sender_handle`, `platform`,
  `route_id`, `channel`, `origin_label`) instead of old aliases.
- `docs/schemas/adapter-config.schema.json`: Updated Meshtastic
  `radio_relay_prefix` default from `"{shortname5}[M]: "` to
  `"{sender_short}: "`.
- `examples/configs/live-matrix-meshtastic.toml`: Updated prefix examples
  from `{longname}` to `{sender}` and `{shortname5}` to `{sender_short}`.
- `examples/configs/live-matrix-meshtastic-channel-map.toml`: Updated
  prefix examples from `{longname}` to `{sender}` and `{shortname5}` to
  `{sender_short}`.
- `docs/changes/unreleased/014-remove-meshnet-name-prefix-defaults.md`:
  Updated final paragraph to state `meshnet_name` field no longer exists
  on adapter configs (not "may still exist").

## Attribution Surface

Canonical template variables (the only supported ones):

- `{sender}` — primary sender display name
- `{sender_short}` — abbreviated sender label
- `{sender_id}` — native sender identifier
- `{sender_handle}` — sender handle or address
- `{platform}` — source platform name
- `{route_id}` — matched route identifier
- `{channel}` — source room or channel ID
- `{origin_label}` — operator-defined source label

Old variables `{longname}`, `{shortname}`, `{shortname5}`, `{from_id}`,
`{meshnet_name}` are unknown placeholders — left unchanged in rendered
output, reported in `unknown_variables`.

## mmrelay Wire Compatibility

`KEY_MESHNET` (`meshtastic_meshnet`) is an external mmrelay wire field
read/written only when `mmrelay_compatibility=True`. It is populated from
generic `origin_label`/`source_origin_label` via `derive_meshnet_value()`.
It is temporary and isolated in `src/medre/interop/mmrelay.py`. Not a
MEDRE attribution variable, config field, or template variable.

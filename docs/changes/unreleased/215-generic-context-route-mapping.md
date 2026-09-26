# Generic context route mapping

The Matrix/Meshtastic-specific `channel_room_map` ontology is removed.
Route-level endpoint mapping is now a generic `context_map` of opaque
endpoint contexts, compiled into ordinary neutral core `Route` legs at the
configuration seam.

- `RouteConfig.context_map` replaces `channel_room_map`. It is keyed by the
  SOURCE-side opaque context; each entry is a structured table with exactly
  one of `dest_context` (an opaque dest-side context) or `dest_destination`
  (a structured destination, forward-only) plus optional per-entry
  `source_origin_label` / `dest_origin_label`. Bare-string entries and the
  old scalar entry shape are rejected.
- Contexts are opaque strings owned by their adapters. Generic config
  applies no transport-specific syntax validation, and no adapters beyond
  the existing Matrix/Meshtastic/MeshCore/LXMF/fake set exist; any
  adapter pair can be mapped, including synthetic transports unknown to
  the runtime.
- Config→route expansion moved into one authority,
  `medre.config.route_expansion` (`expand_route_config` /
  `expand_route_configs`), which raises `ConfigValidationError`. The
  runtime route engine no longer expands routes, holds platform knowledge,
  or needs an `adapter_platforms` argument: `build_runtime_routes` takes
  the config set only, `register_routes` derives routes and the
  expanded-ID→config-route provenance from the compiler's legs, and
  adapter-reference validation remains the sole runtime-owned check
  (`RouteValidationError`).
- Expanded mapping legs are emitted in `sorted(key)` order and named with a
  stable source-context token: `"{route_id}__map<token>__fwd"` and
  `"{route_id}__map<token>__rev"`. The token is derived from the source
  context, so inserting/removing an unrelated map entry does not rename
  existing legs, replacing the
  platform-named `__ch{key}__matrix_to_meshtastic` /
  `__ch{key}__meshtastic_to_matrix` IDs.
- `medre routes plan` reads the compiler per route: legs carry
  `mapping_source_context` / `mapping_dest_context` provenance instead of
  parsed route IDs, label sides follow config-relative leg direction
  (no platform logic), structured-destination legs expose
  `dest_destination_kind` / `dest_destination_hash` /
  `dest_destination_name`, and fan-in warnings are generic (same
  `dest_context` shared by multiple entries when only forward legs exist).
- Duplicate `dest_context` values are allowed for forward-only fan-in
  (`source_to_dest`) and rejected with a `ConfigValidationError` when
  reverse legs exist (`bidirectional`, `dest_to_source`). Entries with a
  structured `dest_destination` require `source_to_dest` and never
  participate in fan-in. `channel_room_map` configs fail the generic
  unknown-key rejection with a pointed hint toward `context_map`.

Migration: pre-release cutover — configs using `channel_room_map` must move
to `context_map` entries with `dest_context` / `dest_destination`; there is
no compatibility alias. No persisted database schema version changes are
required; routing configuration is not storage state.

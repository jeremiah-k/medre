# 016: Direction-aware route origin labels

## Summary

Replace the single `origin_label` field on `[routes.<id>]` with
direction-aware `source_origin_label` and `dest_origin_label`. Forward
expansion (source→dest) uses `source_origin_label`; reverse expansion
(dest→source) uses `dest_origin_label`. The prior `origin_label` field
is removed cleanly — MEDRE is pre-release, so no alias is provided.

## Behavior

- `source_origin_label`: source-side label applied to forward legs
  (source→dest direction). When set, overrides the source adapter's
  `origin_label` in relay-prefix attribution for forward traffic.
- `dest_origin_label`: source-side label applied to reverse legs
  (dest→source direction). Same semantics as `source_origin_label` but
  used when direction is swapped during route expansion.
- Both default to `None` (unset). When `None`, renderers fall back to
  the source adapter's `origin_label`.
- Both must be strings; booleans and other non-string types are
  rejected with `ConfigValidationError`.

## Affected files

- `src/medre/config/routes.py` — `RouteConfig` dataclass and parser
- `src/medre/runtime/route_engine.py` — `_expand_route_config()`,
  `_expand_channel_room_map_route()`
- `docs/schemas/routing-config.schema.json` — schema definition
- `src/medre/config/sample.py` — sample config comments
- `tests/test_routes.py` — parsing and validation tests
- `tests/test_route_origin_label_context.py` — direction-aware expansion tests

## Migration

Replace any `[routes.<id>]` sections using `origin_label`:

```toml
# Before
[routes.bridge]
source_origin_label = "East Relay"   # was origin_label

# After (bidirectional with different labels per direction)
[routes.bridge]
source_origin_label = "East Relay"
dest_origin_label = "West Relay"
```

## Deferral note

Per-channel origin labels (setting different labels per channel in a
`channel_room_map` route) are not implemented. The workaround is to use
separate routes per channel with their own labels.

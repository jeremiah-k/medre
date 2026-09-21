# 199: route config coerces directionality and room aliases at construction

Programmatically constructed routes behaved differently from YAML-loaded
ones, and the difference silently killed all deliveries:

- `RouteConfig` accepted a plain string for `directionality` on the
  programmatic path (the YAML loader coerced it). The route engine compares
  enum identity, so an uncoerced `"bidirectional"` matched no expansion
  branch: the runtime registered zero routes and ran with no delivery paths
  in either direction, without any error.
- `from_dict` aliases `source_room`/`dest_room` to their `*_channel`
  runtime forms; the direct constructor did not. A programmatic
  bidirectional Matrix<->radio route therefore kept `source_channel=None`,
  and every radio->Matrix delivery failed with
  `AdapterPermanentError("no room_id in result")` because the reverse
  leg's Matrix target carried no room.

`RouteConfig.__post_init__` is now the single normalization authority for
both construction paths: `directionality` strings are coerced (invalid or
unhashable values raise `ConfigValidationError` naming the route), and
room/channel conflicts are rejected while rooms alias to channels when the
channel form is absent. `_expand_all_routes` validates directionality before
selecting either the standard or `channel_room_map` expansion path, so a
tampered/unrecognized value can never silently drop an enabled route.

The YAML config loader also applied strict _path_-placeholder validation to
every adapter string field, so the documented Matrix `relay_prefix`
renderer template `[{sender}/{origin_label}]:` (the template includes a
trailing space after the colon) failed config load while
the identical programmatic config worked. Path expansion is now
field-aware: `*_path`/`*_dir`/`*_file` fields keep strict
unknown-placeholder validation; other string fields expand known path
placeholders leniently (`MedrePaths.expand_known_placeholders`) and leave
renderer tokens verbatim. The documented example template passes
`medre config check` end to end.

# 196: Structured LXMF route destinations honored end-to-end

The routing-delivery spec (§2.3/§2.4/§2.6) has always defined structured
`RouteDestination` addressing — `RouteTarget.destination` with
`kind="lxmf_destination"` and a 32-hex-character destination hash — as the
normative form for entity-targeted delivery, and the durable layer fully
supported it (outbox metadata `destination_kind`/`destination_hash`/
`destination_name`/`destination_metadata`, retry reconstruction,
`delivery_target_identity`). The public config could not express it:
`RouteConfig` had only `dest_channel`, and the interim renderer read the
hash out of that channel field, contradicting the spec's
channel/destination separation while the documented §2.6 YAML form
silently produced permanent "cannot recall identity" failures.

`routes.<id>.dest_destination` now parses and validates the structured
destination (`kind` ∈ channel/lxmf_destination/meshcore_contact/
matrix_room; per-kind requirements per §2.3; mutually exclusive with
`dest_channel`, `dest_room`, and `channel_room_map`; exactly one
`dest_adapter` — one transport-specific addressing authority per route),
flows through `_expand_route_config` onto
`RouteTarget.destination`, and is threaded via a new
`RenderingContext.target_destination` (populated by the delivery pipeline
from the plan target) so the LXMF renderer addresses the payload from the
structured destination when present. `dest_channel` remains the supported
transport-defined address selector (the form every existing physical
harness uses — unchanged behavior), with documented precedence:
structured destination, else channel selector, else the historical empty
destination that fails permanently at the delivery boundary. Reverse
expansion legs deliver to the route's source side, which carries no
destination. Retry/replay identity is unchanged: `delivery_target_identity`
already includes the structured destination. The routing-delivery §2.6
example now shows the real loader schema (the previous `from:`/`to:` YAML
was never an accepted loader form), and the transport profile states the
single precedence rule.

The published routing JSON schema now enforces the same per-kind destination
requirements and selector/single-target exclusivity as the loader, so invalid
structured routes fail consistently before runtime assembly.

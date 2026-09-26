"""Direction-aware origin-label assignment for ``context_map`` expansion.

These tests prove that the compiler (:mod:`medre.config.route_expansion`)
threads ``RouteConfig.source_origin_label`` and
``RouteConfig.dest_origin_label`` into the correct per-leg
``RouteSource.origin_label`` field for both possible source orientations:

* **Source = radio, Dest = chat** — the forward leg carries
  ``source_origin_label`` and the reverse leg carries
  ``dest_origin_label``.
* **Source = chat, Dest = radio** — same rule: the forward leg carries
  the *source* label and the reverse leg carries the *dest* label.

The label is direction-aware relative to which side is the route's
declared source, not which transport the adapters speak.

Also covers the two sentinel states renderers rely on:

* ``None`` / unset → ``RouteSource.origin_label is None`` so renderers
  fall back to the source adapter's ``origin_label``.
* explicit ``""`` → ``RouteSource.origin_label == ""`` so renderers
  suppress the adapter fallback (empty label wins).

These behaviours must survive structured config parsing unchanged, so tests
construct ``RouteConfig`` directly and via ``from_dict`` to cover both entry
points.
"""

from __future__ import annotations

from medre.config.route_expansion import expand_route_config
from medre.config.routes import (
    ContextMapEntry,
    RouteConfig,
    RouteConfigSet,
    RouteDirectionality,
)
from medre.core.routing.models import Route

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONTEXT_MAP = {
    "0": ContextMapEntry(dest_context="!room0:example.com"),
    "1": ContextMapEntry(dest_context="!room1:example.com"),
}


def _map_config(
    *,
    route_id: str = "bridge",
    directionality: RouteDirectionality = RouteDirectionality.BIDIRECTIONAL,
    source_adapters: tuple[str, ...] = ("radio_adapter",),
    dest_adapters: tuple[str, ...] = ("chat_adapter",),
    source_origin_label: str | None = None,
    dest_origin_label: str | None = None,
    context_map: dict[str, ContextMapEntry] | None = None,
) -> RouteConfig:
    return RouteConfig(
        route_id=route_id,
        source_adapters=source_adapters,
        dest_adapters=dest_adapters,
        directionality=directionality,
        context_map=context_map or _CONTEXT_MAP,
        source_origin_label=source_origin_label,
        dest_origin_label=dest_origin_label,
    )


def _leg(legs: list, direction: str, index: int = 0) -> Route:
    """Return the single expanded route with the given direction and map index."""
    suffix = "fwd" if direction == "source_to_dest" else "rev"
    matches = [
        leg.route
        for leg in legs
        if leg.direction == direction
        and leg.route.id.endswith(f"__map{index}__{suffix}")
    ]
    assert len(matches) == 1, (
        f"expected exactly one {direction!r} leg on map index {index!r}, "
        f"got ids={[leg.route.id for leg in legs]}"
    )
    return matches[0]


# ===========================================================================
# Source = radio adapter → Dest = chat adapter
# ===========================================================================


def test_forward_leg_uses_source_origin_label() -> None:
    rc = _map_config(source_origin_label="East Net", dest_origin_label="West Net")
    legs = expand_route_config(rc)
    fwd = _leg(legs, "source_to_dest")
    assert fwd.source.adapter == "radio_adapter"
    # Forward (source→dest) leg carries source_origin_label.
    assert fwd.source.origin_label == "East Net"


def test_reverse_leg_uses_dest_origin_label() -> None:
    rc = _map_config(source_origin_label="East Net", dest_origin_label="West Net")
    legs = expand_route_config(rc)
    rev = _leg(legs, "dest_to_source")
    assert rev.source.adapter == "chat_adapter"
    # Reverse (dest→source) leg carries dest_origin_label.
    assert rev.source.origin_label == "West Net"


# ===========================================================================
# Source = chat adapter → Dest = radio adapter (swapped declaration)
#
# The forward (source→dest) leg still carries source_origin_label and
# the reverse (dest→source) leg still carries dest_origin_label: label
# sides follow the route's declared source/dest, not the adapter IDs.
# ===========================================================================


def test_forward_leg_is_source_label_with_swapped_declaration() -> None:
    rc = _map_config(
        route_id="mesh_bridge",
        source_adapters=("chat_adapter",),
        dest_adapters=("radio_adapter",),
        source_origin_label="Src",
        dest_origin_label="Dst",
    )
    legs = expand_route_config(rc)
    fwd = _leg(legs, "source_to_dest")
    assert fwd.source.adapter == "chat_adapter"
    assert fwd.source.origin_label == "Src"


def test_reverse_leg_is_dest_label_with_swapped_declaration() -> None:
    rc = _map_config(
        route_id="mesh_bridge",
        source_adapters=("chat_adapter",),
        dest_adapters=("radio_adapter",),
        source_origin_label="Src",
        dest_origin_label="Dst",
    )
    legs = expand_route_config(rc)
    rev = _leg(legs, "dest_to_source")
    assert rev.source.adapter == "radio_adapter"
    assert rev.source.origin_label == "Dst"


def test_both_labels_applied_across_all_contexts() -> None:
    """Every expanded context leg gets the direction-correct label."""
    rc = _map_config(
        source_origin_label="S",
        dest_origin_label="D",
    )
    for leg in expand_route_config(rc):
        if leg.direction == "source_to_dest":
            assert leg.route.source.origin_label == "S"
        else:
            assert leg.route.source.origin_label == "D"


# ===========================================================================
# Sentinel states: None (fallback) and "" (suppress fallback)
# ===========================================================================


def test_none_labels_yield_none_origin_label() -> None:
    rc = _map_config()
    for leg in expand_route_config(rc):
        assert leg.route.source.origin_label is None


def test_unspecified_labels_default_to_none() -> None:
    """Structured ``from_dict`` path with no labels yields ``None``."""
    rc = RouteConfig.from_dict(
        "bridge",
        {
            "source_adapters": ["radio_adapter"],
            "dest_adapters": ["chat_adapter"],
            "directionality": "bidirectional",
            "context_map": {
                "0": {"dest_context": "!room0:example.com"},
                "1": {"dest_context": "!room1:example.com"},
            },
        },
    )
    for leg in expand_route_config(rc):
        assert leg.route.source.origin_label is None


def test_empty_string_source_label_suppresses_fallback_on_forward() -> None:
    rc = _map_config(source_origin_label="", dest_origin_label="West Net")
    legs = expand_route_config(rc)
    assert _leg(legs, "source_to_dest").source.origin_label == ""


def test_empty_string_dest_label_suppresses_fallback_on_reverse() -> None:
    rc = _map_config(source_origin_label="East Net", dest_origin_label="")
    legs = expand_route_config(rc)
    assert _leg(legs, "dest_to_source").source.origin_label == ""


def test_empty_string_labels_on_both_legs() -> None:
    rc = _map_config(
        source_adapters=("chat_adapter",),
        dest_adapters=("radio_adapter",),
        source_origin_label="",
        dest_origin_label="",
    )
    for leg in expand_route_config(rc):
        assert leg.route.source.origin_label == ""


# ===========================================================================
# source_to_dest / dest_to_source selectivity still honours labels
#
# When only one leg is created, the label assignment honours which
# leg it is (forward → source label, reverse → dest label).
# ===========================================================================


def test_source_to_dest_only_carries_source_label() -> None:
    rc = _map_config(
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        source_origin_label="Only Forward",
        dest_origin_label="Should Not Appear",
        context_map={"0": ContextMapEntry(dest_context="!r0:example.com")},
    )
    legs = expand_route_config(rc)
    # Only the forward leg is created.
    assert len(legs) == 1
    assert legs[0].route.id == "bridge__map0__fwd"
    assert legs[0].route.source.origin_label == "Only Forward"


def test_dest_to_source_only_carries_dest_label() -> None:
    rc = _map_config(
        directionality=RouteDirectionality.DEST_TO_SOURCE,
        source_origin_label="Should Not Appear",
        dest_origin_label="Only Reverse",
        context_map={"0": ContextMapEntry(dest_context="!r0:example.com")},
    )
    legs = expand_route_config(rc)
    # Only the reverse leg is created.
    assert len(legs) == 1
    assert legs[0].route.id == "bridge__map0__rev"
    assert legs[0].route.source.origin_label == "Only Reverse"


def test_dest_to_source_swapped_declaration_carries_dest_label() -> None:
    """source=chat, dest=radio, dest_to_source → the reverse leg carries
    dest_origin_label."""
    rc = _map_config(
        route_id="mesh_bridge",
        source_adapters=("chat_adapter",),
        dest_adapters=("radio_adapter",),
        directionality=RouteDirectionality.DEST_TO_SOURCE,
        source_origin_label="Fwd",
        dest_origin_label="Rev",
        context_map={"0": ContextMapEntry(dest_context="!r0:example.com")},
    )
    legs = expand_route_config(rc)
    assert len(legs) == 1
    assert legs[0].route.id == "mesh_bridge__map0__rev"
    assert legs[0].route.source.origin_label == "Rev"


def test_full_set_expansion_preserves_labels() -> None:
    """RouteConfigSet-level expansion keeps per-direction labels intact."""
    from medre.config.route_expansion import expand_route_configs

    rc = _map_config(source_origin_label="S", dest_origin_label="D")
    legs = expand_route_configs(RouteConfigSet(routes=(rc,)))
    assert len(legs) == 4
    for leg in legs:
        if leg.direction == "source_to_dest":
            assert leg.route.source.origin_label == "S"
        else:
            assert leg.route.source.origin_label == "D"

"""Direction-aware origin-label assignment for ``channel_room_map`` expansion.

These tests prove that :func:`_expand_channel_room_map_route` threads
``RouteConfig.source_origin_label`` and ``RouteConfig.dest_origin_label``
into the correct per-leg ``RouteSource.origin_label`` field for both
possible source orientations:

* **Source = Matrix, Dest = Meshtastic** — the forward leg
  (``matrix_to_meshtastic``) carries ``source_origin_label`` and the
  reverse leg (``meshtastic_to_matrix``) carries ``dest_origin_label``.
* **Source = Meshtastic, Dest = Matrix** — the forward leg
  (``meshtastic_to_matrix``) carries ``source_origin_label`` and the
  reverse leg (``matrix_to_meshtastic``) carries ``dest_origin_label``.

In both orientations the **forward** (source→dest) leg receives the
*source* label and the **reverse** (dest→source) leg receives the *dest*
label.  The label is direction-aware relative to which side is the
route's declared source, not which platform happens to be Matrix.

Also covers the two sentinel states renderers rely on:

* ``None`` / unset → ``RouteSource.origin_label is None`` so renderers
  fall back to the source adapter's ``origin_label``.
* explicit ``""`` → ``RouteSource.origin_label == ""`` so renderers
  suppress the adapter fallback (empty label wins).

These behaviours must survive the YAML migration (config produced as
plain dicts) unchanged, so tests construct ``RouteConfig`` directly and
via ``from_toml_dict`` to cover both shapes.
"""

from __future__ import annotations

from medre.config.routes import (
    RouteConfig,
    RouteConfigSet,
    RouteDirectionality,
)
from medre.runtime.route_engine import build_runtime_routes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Adapter platforms for the matrix/meshtastic pair.  Two source
# orientations are exercised: matrix→mesh and mesh→matrix.
_PLATFORMS_MATRIX_SOURCE = {
    "matrix_adapter": "matrix",
    "mesh_adapter": "meshtastic",
}
_PLATFORMS_MESH_SOURCE = {
    "mesh_adapter": "meshtastic",
    "matrix_adapter": "matrix",
}

_CRM = {"0": "!room0:example.com", "1": "!room1:example.com"}


def _expand(
    rc: RouteConfig,
    platforms: dict[str, str],
) -> list:
    rcs = RouteConfigSet(routes=(rc,))
    return build_runtime_routes(rcs, platforms)


def _crm_config(
    *,
    route_id: str = "bridge",
    directionality: RouteDirectionality = RouteDirectionality.BIDIRECTIONAL,
    source_adapters: tuple[str, ...] = ("matrix_adapter",),
    dest_adapters: tuple[str, ...] = ("mesh_adapter",),
    source_origin_label: str | None = None,
    dest_origin_label: str | None = None,
    channel_room_map: dict[str, str] | None = None,
) -> RouteConfig:
    return RouteConfig(
        route_id=route_id,
        source_adapters=source_adapters,
        dest_adapters=dest_adapters,
        directionality=directionality,
        channel_room_map=channel_room_map or _CRM,
        source_origin_label=source_origin_label,
        dest_origin_label=dest_origin_label,
    )


def _leg(routes: list, direction: str, channel: str = "0"):
    """Return the single expanded route whose id contains the direction and channel."""
    matches = [r for r in routes if direction in r.id and f"__ch{channel}__" in r.id]
    assert len(matches) == 1, (
        f"expected exactly one {direction!r} leg on channel {channel!r}, "
        f"got ids={[r.id for r in routes]}"
    )
    return matches[0]


# ===========================================================================
# Source = Matrix → Dest = Meshtastic
# ===========================================================================


class TestMatrixSourceLabelAssignment:
    """source=Matrix, dest=Meshtastic: forward leg is matrix_to_meshtastic."""

    def test_forward_matrix_to_mesh_uses_source_origin_label(self) -> None:
        rc = _crm_config(
            source_adapters=("matrix_adapter",),
            dest_adapters=("mesh_adapter",),
            source_origin_label="East Net",
            dest_origin_label="West Net",
        )
        fwd = _leg(_expand(rc, _PLATFORMS_MATRIX_SOURCE), "matrix_to_meshtastic")
        assert fwd.source.adapter == "matrix_adapter"
        # Forward (source→dest) leg carries source_origin_label.
        assert fwd.source.origin_label == "East Net"

    def test_reverse_mesh_to_matrix_uses_dest_origin_label(self) -> None:
        rc = _crm_config(
            source_adapters=("matrix_adapter",),
            dest_adapters=("mesh_adapter",),
            source_origin_label="East Net",
            dest_origin_label="West Net",
        )
        rev = _leg(_expand(rc, _PLATFORMS_MATRIX_SOURCE), "meshtastic_to_matrix")
        assert rev.source.adapter == "mesh_adapter"
        # Reverse (dest→source) leg carries dest_origin_label.
        assert rev.source.origin_label == "West Net"


# ===========================================================================
# Source = Meshtastic → Dest = Matrix
# ===========================================================================


class TestMeshSourceLabelAssignment:
    """source=Meshtastic, dest=Matrix: forward leg is meshtastic_to_matrix."""

    def test_forward_mesh_to_matrix_uses_source_origin_label(self) -> None:
        rc = _crm_config(
            route_id="mesh_bridge",
            source_adapters=("mesh_adapter",),
            dest_adapters=("matrix_adapter",),
            source_origin_label="Radio Net",
            dest_origin_label="Matrix Net",
        )
        fwd = _leg(_expand(rc, _PLATFORMS_MESH_SOURCE), "meshtastic_to_matrix")
        assert fwd.source.adapter == "mesh_adapter"
        # Forward (source→dest) leg carries source_origin_label.
        assert fwd.source.origin_label == "Radio Net"

    def test_reverse_matrix_to_mesh_uses_dest_origin_label(self) -> None:
        rc = _crm_config(
            route_id="mesh_bridge",
            source_adapters=("mesh_adapter",),
            dest_adapters=("matrix_adapter",),
            source_origin_label="Radio Net",
            dest_origin_label="Matrix Net",
        )
        rev = _leg(_expand(rc, _PLATFORMS_MESH_SOURCE), "matrix_to_meshtastic")
        assert rev.source.adapter == "matrix_adapter"
        # Reverse (dest→source) leg carries dest_origin_label.
        assert rev.source.origin_label == "Matrix Net"


# ===========================================================================
# Direction-aware: forward leg always gets source label, regardless of
# which platform is the route's declared source.
# ===========================================================================


class TestDirectionAwareConsistency:
    """The forward (source→dest) leg carries source_origin_label in both
    orientations; the reverse (dest→source) leg carries dest_origin_label."""

    def test_forward_leg_is_source_label_in_both_orientations(self) -> None:
        # Matrix source.
        rc_m = _crm_config(
            source_adapters=("matrix_adapter",),
            dest_adapters=("mesh_adapter",),
            source_origin_label="Src",
            dest_origin_label="Dst",
        )
        fwd_m = _leg(_expand(rc_m, _PLATFORMS_MATRIX_SOURCE), "matrix_to_meshtastic")
        assert fwd_m.source.origin_label == "Src"

        # Meshtastic source.
        rc_s = _crm_config(
            route_id="mesh_bridge",
            source_adapters=("mesh_adapter",),
            dest_adapters=("matrix_adapter",),
            source_origin_label="Src",
            dest_origin_label="Dst",
        )
        fwd_s = _leg(_expand(rc_s, _PLATFORMS_MESH_SOURCE), "meshtastic_to_matrix")
        assert fwd_s.source.origin_label == "Src"

    def test_reverse_leg_is_dest_label_in_both_orientations(self) -> None:
        rc_m = _crm_config(
            source_adapters=("matrix_adapter",),
            dest_adapters=("mesh_adapter",),
            source_origin_label="Src",
            dest_origin_label="Dst",
        )
        rev_m = _leg(_expand(rc_m, _PLATFORMS_MATRIX_SOURCE), "meshtastic_to_matrix")
        assert rev_m.source.origin_label == "Dst"

        rc_s = _crm_config(
            route_id="mesh_bridge",
            source_adapters=("mesh_adapter",),
            dest_adapters=("matrix_adapter",),
            source_origin_label="Src",
            dest_origin_label="Dst",
        )
        rev_s = _leg(_expand(rc_s, _PLATFORMS_MESH_SOURCE), "matrix_to_meshtastic")
        assert rev_s.source.origin_label == "Dst"

    def test_both_labels_applied_across_all_channels(self) -> None:
        """Every expanded channel leg gets the direction-correct label."""
        rc = _crm_config(
            source_adapters=("matrix_adapter",),
            dest_adapters=("mesh_adapter",),
            source_origin_label="S",
            dest_origin_label="D",
            channel_room_map={"0": "!r0:e.com", "1": "!r1:e.com", "2": "!r2:e.com"},
        )
        routes = _expand(rc, _PLATFORMS_MATRIX_SOURCE)
        for ch in ("0", "1", "2"):
            fwd = _leg(routes, "matrix_to_meshtastic", ch)
            assert fwd.source.origin_label == "S"
            rev = _leg(routes, "meshtastic_to_matrix", ch)
            assert rev.source.origin_label == "D"


# ===========================================================================
# Sentinel states: None (fallback) and "" (suppress fallback)
# ===========================================================================


class TestOriginLabelSentinels:
    """``None``/unset → ``RouteSource.origin_label is None`` (renderer falls
    back to adapter origin_label).  Explicit ``""`` → origin_label is ``""``
    so renderers suppress the adapter fallback."""

    def test_none_labels_yield_none_origin_label(self) -> None:
        rc = _crm_config(
            source_adapters=("matrix_adapter",),
            dest_adapters=("mesh_adapter",),
        )
        routes = _expand(rc, _PLATFORMS_MATRIX_SOURCE)
        for r in routes:
            assert r.source.origin_label is None

    def test_unspecified_labels_default_to_none(self) -> None:
        """from_toml_dict path (YAML produces plain dicts): no labels → None."""
        rc = RouteConfig.from_toml_dict(
            "t",
            {
                "source_adapters": ["matrix_adapter"],
                "dest_adapters": ["mesh_adapter"],
                "directionality": "bidirectional",
                "channel_room_map": {"0": "!r0:example.com"},
            },
        )
        routes = _expand(rc, _PLATFORMS_MATRIX_SOURCE)
        for r in routes:
            assert r.source.origin_label is None

    def test_empty_string_source_label_suppresses_fallback_on_forward(self) -> None:
        rc = _crm_config(
            source_adapters=("matrix_adapter",),
            dest_adapters=("mesh_adapter",),
            source_origin_label="",
            dest_origin_label="Reverse",
        )
        fwd = _leg(_expand(rc, _PLATFORMS_MATRIX_SOURCE), "matrix_to_meshtastic")
        # Empty string is preserved — renderers must NOT fall back to adapter.
        assert fwd.source.origin_label == ""

    def test_empty_string_dest_label_suppresses_fallback_on_reverse(self) -> None:
        rc = _crm_config(
            source_adapters=("matrix_adapter",),
            dest_adapters=("mesh_adapter",),
            source_origin_label="Forward",
            dest_origin_label="",
        )
        rev = _leg(_expand(rc, _PLATFORMS_MATRIX_SOURCE), "meshtastic_to_matrix")
        assert rev.source.origin_label == ""

    def test_empty_string_labels_on_both_legs(self) -> None:
        rc = _crm_config(
            source_adapters=("mesh_adapter",),
            dest_adapters=("matrix_adapter",),
            source_origin_label="",
            dest_origin_label="",
        )
        routes = _expand(rc, _PLATFORMS_MESH_SOURCE)
        for r in routes:
            assert r.source.origin_label == ""


# ===========================================================================
# source_to_dest / dest_to_source selectivity still honours labels
# ===========================================================================


class TestDirectionalitySelectivity:
    """When only one leg is created, the label assignment honours which
    leg it is (forward → source label, reverse → dest label)."""

    def test_source_to_dest_only_carries_source_label(self) -> None:
        rc = _crm_config(
            source_adapters=("matrix_adapter",),
            dest_adapters=("mesh_adapter",),
            directionality=RouteDirectionality.SOURCE_TO_DEST,
            source_origin_label="Only Forward",
            dest_origin_label="Should Not Appear",
            channel_room_map={"0": "!r0:example.com"},
        )
        routes = _expand(rc, _PLATFORMS_MATRIX_SOURCE)
        # Only the matrix_to_meshtastic (forward) leg is created.
        assert len(routes) == 1
        assert "matrix_to_meshtastic" in routes[0].id
        assert routes[0].source.origin_label == "Only Forward"

    def test_dest_to_source_only_carries_dest_label(self) -> None:
        rc = _crm_config(
            source_adapters=("matrix_adapter",),
            dest_adapters=("mesh_adapter",),
            directionality=RouteDirectionality.DEST_TO_SOURCE,
            source_origin_label="Should Not Appear",
            dest_origin_label="Only Reverse",
            channel_room_map={"0": "!r0:example.com"},
        )
        routes = _expand(rc, _PLATFORMS_MATRIX_SOURCE)
        # Only the meshtastic_to_matrix (reverse) leg is created.
        assert len(routes) == 1
        assert "meshtastic_to_matrix" in routes[0].id
        assert routes[0].source.origin_label == "Only Reverse"

    def test_dest_to_source_mesh_source_carries_dest_label(self) -> None:
        """source=Meshtastic, dest=Matrix, dest_to_source → matrix_to_meshtastic
        leg (the reverse leg) carries dest_origin_label."""
        rc = _crm_config(
            route_id="mesh_bridge",
            source_adapters=("mesh_adapter",),
            dest_adapters=("matrix_adapter",),
            directionality=RouteDirectionality.DEST_TO_SOURCE,
            source_origin_label="Fwd",
            dest_origin_label="Rev",
            channel_room_map={"0": "!r0:example.com"},
        )
        routes = _expand(rc, _PLATFORMS_MESH_SOURCE)
        assert len(routes) == 1
        assert "matrix_to_meshtastic" in routes[0].id
        assert routes[0].source.origin_label == "Rev"

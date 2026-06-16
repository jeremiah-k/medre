"""Origin-label provenance tests for the route-plan model.

Verifies how :func:`medre.runtime.route_plan.build_route_plan` attributes
each leg's ``source_origin_label_source`` — the field that records where
the resolved origin label came from.

Precedence chain (mirrors the expansion code):

1. ``per_entry`` — a ``channel_room_map`` entry's
   ``source_origin_label`` / ``dest_origin_label`` (highest priority).
2. ``route`` — the route-level ``source_origin_label`` /
   ``dest_origin_label``.
3. ``unset`` — no label resolved at all.

The ``adapter`` attribution is a defensive display heuristic in
:func:`medre.runtime.route_plan._resolve_origin_label_source`: it fires
only when the resolved value on the expanded leg coincidentally matches
the source adapter's configured ``origin_label`` with no entry/route
attribution.  Through the normal expansion path the resolved value is
always the route-level or entry-level label, so the ``adapter`` branch
is exercised here via a direct helper invocation with a synthetic
:class:`~medre.core.routing.models.Route`.
"""

from __future__ import annotations

from pathlib import Path

from medre.config.loader import load_config
from medre.config.routes import RouteConfig
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.runtime.route_plan import _resolve_origin_label_source, build_route_plan

# ---------------------------------------------------------------------------
# Shared adapter scaffold
# ---------------------------------------------------------------------------

_ADAPTERS = """\
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: tok
      room_allowlist: ['!a:fake.local', '!b:fake.local']
      encryption_mode: plaintext
      origin_label: AdapterMatrix
  meshtastic:
    radio:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: AdapterMesh
"""


def _load(tmp_path: Path, routes_yaml: str) -> object:
    """Write a config with the shared adapters + *routes_yaml* and load it."""
    yaml_text = (
        "runtime:\n"
        "  name: plan-provenance\n"
        "storage:\n"
        "  backend: memory\n" + _ADAPTERS + routes_yaml
    )
    p = tmp_path / "config.yaml"
    p.write_text(yaml_text)
    config, _source, _paths = load_config(str(p))
    return config


def _crm_leg(plan, route_id: str, channel: str):
    """Return the single channel_room_map leg for *route_id* on *channel*."""
    entry = next(e for e in plan.routes if e.route_id == route_id)
    matches = [leg for leg in entry.legs if leg.channel_room_map_key == channel]
    assert (
        len(matches) == 1
    ), f"expected one leg on channel {channel!r}, got {len(matches)}"
    return matches[0]


# ===========================================================================
# 1. Per-entry label wins → source = "per_entry"
# ===========================================================================


def test_per_entry_label_wins(tmp_path: Path) -> None:
    """A channel_room_map entry's source_origin_label is attributed to per_entry."""
    config = _load(
        tmp_path,
        "routes:\n"
        "  mesh_to_matrix:\n"
        "    source_adapters: [radio]\n"
        "    dest_adapters: [main]\n"
        "    directionality: source_to_dest\n"
        "    channel_room_map:\n"
        "      0:\n"
        "        room: '!a:fake.local'\n"
        "        source_origin_label: EntryLabel\n",
    )
    plan = build_route_plan(config)
    leg = _crm_leg(plan, "mesh_to_matrix", "0")
    assert leg.source_origin_label == "EntryLabel"
    assert leg.source_origin_label_source == "per_entry"


# ===========================================================================
# 2. Route-level label wins (no per-entry) → source = "route"
# ===========================================================================


def test_route_level_label_when_no_per_entry(tmp_path: Path) -> None:
    """With no per-entry label, the route-level label is attributed to route."""
    config = _load(
        tmp_path,
        "routes:\n"
        "  mesh_to_matrix:\n"
        "    source_adapters: [radio]\n"
        "    dest_adapters: [main]\n"
        "    directionality: source_to_dest\n"
        "    source_origin_label: RouteLabel\n"
        "    channel_room_map:\n"
        "      0: '!a:fake.local'\n",
    )
    plan = build_route_plan(config)
    leg = _crm_leg(plan, "mesh_to_matrix", "0")
    assert leg.source_origin_label == "RouteLabel"
    assert leg.source_origin_label_source == "route"


def test_route_level_label_on_non_crm_forward_leg(tmp_path: Path) -> None:
    """A non-channel_room_map route's forward leg uses source_origin_label."""
    config = _load(
        tmp_path,
        "routes:\n"
        "  matrix_to_radio:\n"
        "    source_adapters: [main]\n"
        "    dest_adapters: [radio]\n"
        "    directionality: source_to_dest\n"
        "    source_origin_label: ForwardOnly\n"
        "    source_room: '!a:fake.local'\n"
        "    dest_channel: '1'\n",
    )
    plan = build_route_plan(config)
    entry = next(e for e in plan.routes if e.route_id == "matrix_to_radio")
    assert len(entry.legs) == 1
    leg = entry.legs[0]
    assert leg.source_origin_label == "ForwardOnly"
    assert leg.source_origin_label_source == "route"


# ===========================================================================
# 3. Adapter attribution (defensive heuristic) → source = "adapter"
#
# Through the normal build_route_plan path this branch is unreachable
# (resolved == route_label, so when route_label is None resolved is also
# None → "unset").  It is exercised here by invoking the helper directly
# with a synthetic Route whose origin_label coincides with the adapter's.
# ===========================================================================


def test_adapter_attribution_via_direct_helper() -> None:
    """The 'adapter' attribution fires when resolved matches the adapter label.

    Constructed scenario: route.source.origin_label == adapter.origin_label
    with no route-level or per-entry attribution.  This is the display
    heuristic that attributes a coincidental match to the adapter fallback.
    """
    rc = RouteConfig(
        route_id="synthetic",
        source_adapters=("radio",),
        dest_adapters=("main",),
        # source_origin_label is None (default) → route_label is None.
    )
    # Build a synthetic Route whose origin_label matches the adapter's.
    route = Route(
        id="synthetic",
        source=RouteSource(
            adapter="radio",
            event_kinds=(),
            channel=None,
            origin_label="AdapterMesh",  # matches the meshtastic adapter
        ),
        targets=[RouteTarget(adapter="main", channel=None)],
        enabled=True,
    )
    source = _resolve_origin_label_source(
        route=route,
        rc=rc,
        is_forward=True,
        resolved="AdapterMesh",
        adapter_platforms={"radio": "meshtastic", "main": "matrix"},
        adapter_origin_labels={"radio": "AdapterMesh", "main": "AdapterMatrix"},
    )
    assert source == "adapter"


def test_adapter_attribution_requires_non_empty_match() -> None:
    """An empty resolved value is never attributed to 'adapter'."""
    rc = RouteConfig(
        route_id="synthetic",
        source_adapters=("radio",),
        dest_adapters=("main",),
    )
    route = Route(
        id="synthetic",
        source=RouteSource(
            adapter="radio",
            event_kinds=(),
            channel=None,
            origin_label="",
        ),
        targets=[RouteTarget(adapter="main", channel=None)],
        enabled=True,
    )
    source = _resolve_origin_label_source(
        route=route,
        rc=rc,
        is_forward=True,
        resolved="",
        adapter_platforms={"radio": "meshtastic"},
        adapter_origin_labels={"radio": ""},
    )
    # Empty resolved with no route/entry label → "unset", not "adapter".
    assert source == "unset"


# ===========================================================================
# 4. Explicit empty per-entry suppresses fallback → source = "per_entry", ""
# ===========================================================================


def test_explicit_empty_per_entry_suppresses_fallback(tmp_path: Path) -> None:
    """An explicit '' per-entry label is preserved as per_entry with empty value.

    The empty string is distinct from None: None means "fall back", while
    '' means "suppress".  The provenance source stays 'per_entry' and the
    resolved label is the empty string.
    """
    config = _load(
        tmp_path,
        "routes:\n"
        "  mesh_to_matrix:\n"
        "    source_adapters: [radio]\n"
        "    dest_adapters: [main]\n"
        "    directionality: source_to_dest\n"
        "    source_origin_label: RouteDefault\n"
        "    channel_room_map:\n"
        "      0:\n"
        "        room: '!a:fake.local'\n"
        "        source_origin_label: ''\n",
    )
    plan = build_route_plan(config)
    leg = _crm_leg(plan, "mesh_to_matrix", "0")
    assert leg.source_origin_label == ""
    assert leg.source_origin_label_source == "per_entry"


# ===========================================================================
# 5. Explicit empty route-level suppresses adapter → source = "route", ""
# ===========================================================================


def test_explicit_empty_route_level_suppresses_adapter(tmp_path: Path) -> None:
    """An explicit '' route-level label is preserved as route with empty value.

    The empty string suppresses any adapter fallback.  The provenance
    source is 'route' (the route-level label is set, just empty) and the
    resolved label is the empty string.
    """
    config = _load(
        tmp_path,
        "routes:\n"
        "  mesh_to_matrix:\n"
        "    source_adapters: [radio]\n"
        "    dest_adapters: [main]\n"
        "    directionality: source_to_dest\n"
        "    source_origin_label: ''\n"
        "    channel_room_map:\n"
        "      0: '!a:fake.local'\n",
    )
    plan = build_route_plan(config)
    leg = _crm_leg(plan, "mesh_to_matrix", "0")
    assert leg.source_origin_label == ""
    assert leg.source_origin_label_source == "route"


# ===========================================================================
# 6. Null/unset per-entry falls back → source = "route" or "adapter"
# ===========================================================================


def test_explicit_null_per_entry_falls_back_to_route(tmp_path: Path) -> None:
    """An explicit YAML null per-entry label falls back to the route-level.

    This is the counterpart to the explicit empty-string test above:
    null means "fall back through the chain", so the route-level label
    wins.
    """
    config = _load(
        tmp_path,
        "routes:\n"
        "  mesh_to_matrix:\n"
        "    source_adapters: [radio]\n"
        "    dest_adapters: [main]\n"
        "    directionality: source_to_dest\n"
        "    source_origin_label: RouteDefault\n"
        "    channel_room_map:\n"
        "      0:\n"
        "        room: '!a:fake.local'\n"
        "        source_origin_label: null\n",
    )
    plan = build_route_plan(config)
    leg = _crm_leg(plan, "mesh_to_matrix", "0")
    assert leg.source_origin_label == "RouteDefault"
    assert leg.source_origin_label_source == "route"


def test_no_labels_anywhere_yields_unset(tmp_path: Path) -> None:
    """With no per-entry, route, or matching adapter label, source is 'unset'."""
    config = _load(
        tmp_path,
        "routes:\n"
        "  mesh_to_matrix:\n"
        "    source_adapters: [radio]\n"
        "    dest_adapters: [main]\n"
        "    directionality: source_to_dest\n"
        "    channel_room_map:\n"
        "      0: '!a:fake.local'\n",
    )
    plan = build_route_plan(config)
    leg = _crm_leg(plan, "mesh_to_matrix", "0")
    assert leg.source_origin_label is None
    assert leg.source_origin_label_source == "unset"


# ===========================================================================
# 7. Bidirectional reverse leg uses dest-side labels
# ===========================================================================


def test_bidirectional_reverse_leg_uses_dest_origin_label(tmp_path: Path) -> None:
    """The reverse leg of a bidirectional route uses dest_origin_label.

    For a non-channel_room_map bidirectional route, the forward leg's
    provenance comes from source_origin_label and the reverse leg's from
    dest_origin_label.
    """
    config = _load(
        tmp_path,
        "routes:\n"
        "  bridge:\n"
        "    source_adapters: [main]\n"
        "    dest_adapters: [radio]\n"
        "    directionality: bidirectional\n"
        "    source_origin_label: FwdLabel\n"
        "    dest_origin_label: RevLabel\n",
    )
    plan = build_route_plan(config)
    entry = next(e for e in plan.routes if e.route_id == "bridge")
    assert len(entry.legs) == 2
    fwd = next(leg for leg in entry.legs if leg.direction == "source_to_dest")
    rev = next(leg for leg in entry.legs if leg.direction == "dest_to_source")
    assert fwd.source_origin_label == "FwdLabel"
    assert fwd.source_origin_label_source == "route"
    assert rev.source_origin_label == "RevLabel"
    assert rev.source_origin_label_source == "route"


def test_bidirectional_reverse_leg_without_dest_label_is_unset(tmp_path: Path) -> None:
    """When dest_origin_label is unset, the reverse leg's source is 'unset'."""
    config = _load(
        tmp_path,
        "routes:\n"
        "  bridge:\n"
        "    source_adapters: [main]\n"
        "    dest_adapters: [radio]\n"
        "    directionality: bidirectional\n"
        "    source_origin_label: FwdOnly\n",
    )
    plan = build_route_plan(config)
    entry = next(e for e in plan.routes if e.route_id == "bridge")
    rev = next(leg for leg in entry.legs if leg.direction == "dest_to_source")
    assert rev.source_origin_label is None
    assert rev.source_origin_label_source == "unset"


# ===========================================================================
# Channel_room_map reverse leg provenance (dest_origin_label side)
# ===========================================================================


def test_crm_bidirectional_reverse_uses_dest_label(tmp_path: Path) -> None:
    """A channel_room_map bidirectional route's reverse leg uses dest labels.

    With Meshtastic declared as source, the forward leg is mesh→matrix
    (uses source_origin_label) and the reverse leg is matrix→mesh (uses
    dest_origin_label).  This mirrors the non-crm bidirectional split.
    """
    config = _load(
        tmp_path,
        "routes:\n"
        "  bridge:\n"
        "    source_adapters: [radio]\n"
        "    dest_adapters: [main]\n"
        "    directionality: bidirectional\n"
        "    source_origin_label: MeshSide\n"
        "    dest_origin_label: MatrixSide\n"
        "    channel_room_map:\n"
        "      0:\n"
        "        room: '!a:fake.local'\n"
        "        source_origin_label: EntryMesh\n"
        "        dest_origin_label: EntryMatrix\n",
    )
    plan = build_route_plan(config)
    entry = next(e for e in plan.routes if e.route_id == "bridge")
    # Bidirectional crm with 1 channel → 2 legs (mesh→matrix and matrix→mesh).
    assert len(entry.legs) == 2
    # Forward leg: radio → main (mesh → matrix).
    fwd = next(
        leg
        for leg in entry.legs
        if leg.source_adapter_id == "radio" and leg.dest_adapter_id == "main"
    )
    assert fwd.source_origin_label == "EntryMesh"
    assert fwd.source_origin_label_source == "per_entry"
    # Reverse leg: main → radio (matrix → mesh).
    rev = next(
        leg
        for leg in entry.legs
        if leg.source_adapter_id == "main" and leg.dest_adapter_id == "radio"
    )
    assert rev.source_origin_label == "EntryMatrix"
    assert rev.source_origin_label_source == "per_entry"

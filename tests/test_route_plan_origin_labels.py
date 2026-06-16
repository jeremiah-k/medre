"""Origin-label provenance tests for the route-plan model.

Verifies how :func:`medre.runtime.route_plan.build_route_plan` resolves
the *effective* origin label for each leg and records its
``source_origin_label_source`` — the field that records where the
resolved origin label came from.

The plan applies the same precedence chain as render-time attribution,
so the reported ``source_origin_label`` is the effective label the
renderer would use, including adapter-level fallback:

1. ``per_entry`` — a ``channel_room_map`` entry's
   ``source_origin_label`` / ``dest_origin_label`` (highest priority).
2. ``route`` — the route-level ``source_origin_label`` /
   ``dest_origin_label``.
3. ``adapter`` — the source adapter's configured ``origin_label``
   fallback, applied at plan time so the plan matches what rendering
   would emit.
4. ``unset`` — no label resolved at any level and the source adapter's
   ``origin_label`` is empty.

An explicit empty string (``""``) at the per-entry or route level
suppresses fallback below that level for that leg; an absent/``null``
label falls through to the next level.
"""

from __future__ import annotations

from pathlib import Path

from medre.config.loader import load_config
from medre.config.routes import RouteConfig
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.runtime.route_plan import (
    _resolve_effective_origin_label,
    build_route_plan,
)

# ---------------------------------------------------------------------------
# Shared adapter scaffolds
# ---------------------------------------------------------------------------

# Adapters with non-empty origin_labels. Used by tests that exercise
# adapter fallback (the plan reports these labels as the effective value
# when no per-entry or route-level label is set).
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

# Same shape as above but with empty origin_labels. Used by tests that
# need to assert the "unset" provenance (no adapter fallback fires).
_ADAPTERS_NO_ORIGIN = """\
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
  meshtastic:
    radio:
      enabled: true
      adapter_kind: fake
      connection_type: fake
"""


def _load(tmp_path: Path, routes_yaml: str, adapters: str = _ADAPTERS) -> object:
    """Write a config with the chosen adapters + *routes_yaml* and load it."""
    yaml_text = (
        "runtime:\n"
        "  name: plan-provenance\n"
        "storage:\n"
        "  backend: memory\n" + adapters + routes_yaml
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
# 3. Adapter fallback (no per-entry, no route-level) → source = "adapter"
#
# When the route sets no per-entry or route-level label, the plan applies
# the source adapter's configured origin_label as the effective value,
# mirroring render-time attribution.
# ===========================================================================


def test_non_crm_route_falls_back_to_adapter_label(tmp_path: Path) -> None:
    """A non-CRM route with no route labels uses the source adapter's label.

    The matrix source adapter has origin_label='AdapterMatrix'. With no
    route-level source_origin_label, the plan reports that value as the
    effective label and attributes it to 'adapter'.
    """
    config = _load(
        tmp_path,
        "routes:\n"
        "  matrix_to_radio:\n"
        "    source_adapters: [main]\n"
        "    dest_adapters: [radio]\n"
        "    directionality: source_to_dest\n"
        "    source_room: '!a:fake.local'\n"
        "    dest_channel: '1'\n",
    )
    plan = build_route_plan(config)
    entry = next(e for e in plan.routes if e.route_id == "matrix_to_radio")
    assert len(entry.legs) == 1
    leg = entry.legs[0]
    assert leg.source_adapter_id == "main"
    assert leg.source_origin_label == "AdapterMatrix"
    assert leg.source_origin_label_source == "adapter"


def test_crm_route_falls_back_to_adapter_label_per_leg(tmp_path: Path) -> None:
    """A channel_room_map route with no per-entry/route labels falls back per leg.

    The meshtastic source adapter (radio) has origin_label='AdapterMesh'.
    Each expanded mesh→matrix leg reports AdapterMesh attributed to
    'adapter'.
    """
    config = _load(
        tmp_path,
        "routes:\n"
        "  mesh_to_matrix:\n"
        "    source_adapters: [radio]\n"
        "    dest_adapters: [main]\n"
        "    directionality: source_to_dest\n"
        "    channel_room_map:\n"
        "      0: '!a:fake.local'\n"
        "      1: '!b:fake.local'\n",
    )
    plan = build_route_plan(config)
    entry = next(e for e in plan.routes if e.route_id == "mesh_to_matrix")
    assert len(entry.legs) == 2
    for leg in entry.legs:
        assert leg.source_adapter_id == "radio"
        assert leg.source_origin_label == "AdapterMesh"
        assert leg.source_origin_label_source == "adapter"


def test_bidirectional_reverse_leg_falls_back_to_reverse_adapter(
    tmp_path: Path,
) -> None:
    """A bidirectional reverse leg falls back to the reverse source adapter.

    With source=main (matrix, AdapterMatrix) and dest=radio (meshtastic,
    AdapterMesh), the forward leg reports AdapterMatrix and the reverse
    leg (whose physical source becomes the radio adapter) reports
    AdapterMesh — both via 'adapter' fallback.
    """
    config = _load(
        tmp_path,
        "routes:\n"
        "  bridge:\n"
        "    source_adapters: [main]\n"
        "    dest_adapters: [radio]\n"
        "    directionality: bidirectional\n",
    )
    plan = build_route_plan(config)
    entry = next(e for e in plan.routes if e.route_id == "bridge")
    assert len(entry.legs) == 2
    fwd = next(leg for leg in entry.legs if leg.direction == "source_to_dest")
    rev = next(leg for leg in entry.legs if leg.direction == "dest_to_source")
    assert fwd.source_adapter_id == "main"
    assert fwd.source_origin_label == "AdapterMatrix"
    assert fwd.source_origin_label_source == "adapter"
    # Reverse leg's physical source is the radio adapter.
    assert rev.source_adapter_id == "radio"
    assert rev.source_origin_label == "AdapterMesh"
    assert rev.source_origin_label_source == "adapter"


def test_per_entry_label_overrides_adapter_fallback(tmp_path: Path) -> None:
    """A per-entry label wins over the source adapter's origin_label."""
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
        "        source_origin_label: EntryOverride\n",
    )
    plan = build_route_plan(config)
    leg = _crm_leg(plan, "mesh_to_matrix", "0")
    assert leg.source_origin_label == "EntryOverride"
    assert leg.source_origin_label_source == "per_entry"


def test_route_level_label_overrides_adapter_fallback(tmp_path: Path) -> None:
    """A route-level label wins over the source adapter's origin_label."""
    config = _load(
        tmp_path,
        "routes:\n"
        "  mesh_to_matrix:\n"
        "    source_adapters: [radio]\n"
        "    dest_adapters: [main]\n"
        "    directionality: source_to_dest\n"
        "    source_origin_label: RouteOverride\n"
        "    channel_room_map:\n"
        "      0: '!a:fake.local'\n",
    )
    plan = build_route_plan(config)
    leg = _crm_leg(plan, "mesh_to_matrix", "0")
    assert leg.source_origin_label == "RouteOverride"
    assert leg.source_origin_label_source == "route"


# ===========================================================================
# 4. Explicit empty per-entry/route suppresses adapter fallback
# ===========================================================================


def test_explicit_empty_per_entry_suppresses_adapter_fallback(
    tmp_path: Path,
) -> None:
    """An explicit '' per-entry label suppresses route and adapter fallback.

    The empty string is distinct from None: None means "fall back", while
    '' means "suppress".  The provenance source stays 'per_entry' and the
    effective label is the empty string, even though the source adapter
    has a non-empty origin_label.
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


def test_explicit_empty_route_level_suppresses_adapter_fallback(
    tmp_path: Path,
) -> None:
    """An explicit '' route-level label suppresses the adapter fallback.

    The empty string suppresses the adapter origin_label.  The
    provenance source is 'route' (the route-level label is set, just
    empty) and the effective label is the empty string.
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
# 5. Explicit null per-entry falls back through the chain
# ===========================================================================


def test_explicit_null_per_entry_falls_back_to_route(tmp_path: Path) -> None:
    """An explicit YAML null per-entry label falls back to the route-level.

    This is the counterpart to the explicit empty-string test above:
    null means "fall back through the chain", so the route-level label
    wins over the adapter fallback.
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


def test_explicit_null_per_entry_falls_back_to_adapter(tmp_path: Path) -> None:
    """An explicit YAML null per-entry label falls through to the adapter.

    With no route-level label, the null per-entry label falls through to
    the source adapter's origin_label (render-time attribution).
    """
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
        "        source_origin_label: null\n",
    )
    plan = build_route_plan(config)
    leg = _crm_leg(plan, "mesh_to_matrix", "0")
    assert leg.source_origin_label == "AdapterMesh"
    assert leg.source_origin_label_source == "adapter"


# ===========================================================================
# 6. Adapter with empty origin_label → unset (not "adapter")
# ===========================================================================


def test_no_labels_anywhere_yields_unset(tmp_path: Path) -> None:
    """No labels at any level + empty adapter origin_label → source 'unset'."""
    config = _load(
        tmp_path,
        "routes:\n"
        "  mesh_to_matrix:\n"
        "    source_adapters: [radio]\n"
        "    dest_adapters: [main]\n"
        "    directionality: source_to_dest\n"
        "    channel_room_map:\n"
        "      0: '!a:fake.local'\n",
        adapters=_ADAPTERS_NO_ORIGIN,
    )
    plan = build_route_plan(config)
    leg = _crm_leg(plan, "mesh_to_matrix", "0")
    assert leg.source_origin_label is None
    assert leg.source_origin_label_source == "unset"


def test_bidirectional_reverse_leg_without_dest_label_is_unset_when_adapter_empty(
    tmp_path: Path,
) -> None:
    """When dest_origin_label is unset and the reverse adapter has no label,
    the reverse leg's source is 'unset'."""
    config = _load(
        tmp_path,
        "routes:\n"
        "  bridge:\n"
        "    source_adapters: [main]\n"
        "    dest_adapters: [radio]\n"
        "    directionality: bidirectional\n"
        "    source_origin_label: FwdOnly\n",
        adapters=_ADAPTERS_NO_ORIGIN,
    )
    plan = build_route_plan(config)
    entry = next(e for e in plan.routes if e.route_id == "bridge")
    rev = next(leg for leg in entry.legs if leg.direction == "dest_to_source")
    assert rev.source_origin_label is None
    assert rev.source_origin_label_source == "unset"


# ===========================================================================
# 7. Bidirectional route-level labels override adapter fallback
# ===========================================================================


def test_bidirectional_reverse_leg_uses_dest_origin_label(tmp_path: Path) -> None:
    """The reverse leg of a bidirectional route uses dest_origin_label.

    For a non-channel_room_map bidirectional route, the forward leg's
    provenance comes from source_origin_label and the reverse leg's from
    dest_origin_label. Both override any adapter fallback.
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


# ===========================================================================
# 8. Direct helper contract: returns (effective_label, source) tuple
#
# The helper is exercised end-to-end through build_route_plan above; these
# focused unit tests pin the tuple return shape and the empty-match guard.
# ===========================================================================


def test_resolve_effective_origin_label_adapter_branch_direct() -> None:
    """The helper returns (adapter_label, 'adapter') when the adapter matches.

    Constructed scenario: no per-entry or route-level label, source
    adapter has a non-empty origin_label.  The helper applies the
    adapter fallback directly.
    """
    rc = RouteConfig(
        route_id="synthetic",
        source_adapters=("radio",),
        dest_adapters=("main",),
        # source_origin_label is None (default) → route_label is None.
    )
    route = Route(
        id="synthetic",
        source=RouteSource(
            adapter="radio",
            event_kinds=(),
            channel=None,
            origin_label=None,
        ),
        targets=[RouteTarget(adapter="main", channel=None)],
        enabled=True,
    )
    effective, source = _resolve_effective_origin_label(
        route=route,
        rc=rc,
        is_forward=True,
        adapter_platforms={"radio": "meshtastic", "main": "matrix"},
        adapter_origin_labels={"radio": "AdapterMesh", "main": "AdapterMatrix"},
    )
    assert source == "adapter"
    assert effective == "AdapterMesh"


def test_resolve_effective_origin_label_empty_adapter_is_unset_direct() -> None:
    """An empty adapter origin_label never produces the 'adapter' source.

    With no per-entry or route-level label and an empty adapter label,
    the helper returns (None, 'unset').
    """
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
            origin_label=None,
        ),
        targets=[RouteTarget(adapter="main", channel=None)],
        enabled=True,
    )
    effective, source = _resolve_effective_origin_label(
        route=route,
        rc=rc,
        is_forward=True,
        adapter_platforms={"radio": "meshtastic"},
        adapter_origin_labels={"radio": ""},
    )
    assert source == "unset"
    assert effective is None

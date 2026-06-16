"""Model tests for :func:`medre.runtime.route_plan.build_route_plan`.

Covers the plan model itself — adapter inventory, route expansion into
legs, ``channel_room_map`` fan-out, disabled-route handling, and loop
detection — without touching the CLI.  Provenance (``source_origin_label`` /
``source_origin_label_source``) has its own dedicated file
(:mod:`tests.test_route_plan_origin_labels`).

All configs use ``adapter_kind: fake`` so no SDK or hardware is required.
"""

from __future__ import annotations

from pathlib import Path

from medre.config.loader import load_config
from medre.runtime.route_plan import (
    AdapterSummary,
    RoutePlan,
    RoutePlanEntry,
    RoutePlanLeg,
    build_route_plan,
)

# ---------------------------------------------------------------------------
# YAML config fragments
# ---------------------------------------------------------------------------

# Minimal config with adapters but no routes.
_CONFIG_NO_ROUTES = """\
runtime:
  name: plan-no-routes
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: tok_main
      room_allowlist: ['!room:fake.local']
      encryption_mode: plaintext
  meshtastic:
    radio:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: TestMesh
"""

# A single source_to_dest route with targeting.
_CONFIG_SIMPLE = _CONFIG_NO_ROUTES + """\
routes:
  matrix_to_radio:
    source_adapters: [main]
    dest_adapters: [radio]
    directionality: source_to_dest
    source_room: '!room:fake.local'
    dest_channel: '1'
"""

# A bidirectional route.
_CONFIG_BIDIR = _CONFIG_NO_ROUTES + """\
routes:
  bridge:
    source_adapters: [main]
    dest_adapters: [radio]
    directionality: bidirectional
"""

# A disabled route.
_CONFIG_DISABLED = _CONFIG_NO_ROUTES + """\
routes:
  paused:
    source_adapters: [main]
    dest_adapters: [radio]
    directionality: source_to_dest
    enabled: false
"""

# A channel_room_map route (Matrix→Meshtastic, source_to_dest).
_CONFIG_CHANNEL_ROOM_MAP = """\
runtime:
  name: plan-crm
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: tok_main
      room_allowlist: ['!a:fake.local', '!b:fake.local']
      encryption_mode: plaintext
  meshtastic:
    radio:
      enabled: true
      adapter_kind: fake
      connection_type: fake
routes:
  mesh_to_matrix:
    source_adapters: [radio]
    dest_adapters: [main]
    directionality: source_to_dest
    channel_room_map:
      0: '!a:fake.local'
      1: '!b:fake.local'
"""

# A channel_room_map route with structured entries (per-entry labels).
_CONFIG_CHANNEL_ROOM_MAP_STRUCTURED = """\
runtime:
  name: plan-crm-structured
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: tok_main
      room_allowlist: ['!a:fake.local', '!b:fake.local']
      encryption_mode: plaintext
  meshtastic:
    radio:
      enabled: true
      adapter_kind: fake
      connection_type: fake
routes:
  mesh_to_matrix:
    source_adapters: [radio]
    dest_adapters: [main]
    directionality: source_to_dest
    channel_room_map:
      0:
        room: '!a:fake.local'
        source_origin_label: ChannelA
      1:
        room: '!b:fake.local'
        source_origin_label: ChannelB
"""

# Truly minimal config — runtime only, no adapters, no routes.
_CONFIG_EMPTY = "runtime: {}\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load(tmp_path: Path, yaml_text: str) -> object:
    """Write *yaml_text* to a tmp file and load it as a RuntimeConfig."""
    p = tmp_path / "config.yaml"
    p.write_text(yaml_text)
    config, _source, _paths = load_config(str(p))
    return config


def _entry_by_id(plan: RoutePlan, route_id: str) -> RoutePlanEntry:
    """Return the single plan entry matching *route_id*."""
    matches = [e for e in plan.routes if e.route_id == route_id]
    assert (
        len(matches) == 1
    ), f"expected exactly one entry for {route_id!r}, got {len(matches)}"
    return matches[0]


# ===========================================================================
# 1. Minimal config with no routes → 0 routes, 0 legs, adapters listed
# ===========================================================================


def test_no_routes_plan_has_zero_routes_and_legs(tmp_path: Path) -> None:
    """A config with adapters but no routes yields an empty routes list."""
    config = _load(tmp_path, _CONFIG_NO_ROUTES)
    plan = build_route_plan(config)
    assert plan.routes == []
    assert plan.total_legs == 0
    assert plan.loops == []


def test_no_routes_plan_still_lists_adapters(tmp_path: Path) -> None:
    """Adapters are inventoried even when there are no routes."""
    config = _load(tmp_path, _CONFIG_NO_ROUTES)
    plan = build_route_plan(config)
    assert len(plan.adapters) == 2
    ids = {a.adapter_id for a in plan.adapters}
    assert ids == {"main", "radio"}


# ===========================================================================
# 2. Simple source_to_dest route → 1 leg, correct adapters, direction
# ===========================================================================


def test_simple_route_produces_one_forward_leg(tmp_path: Path) -> None:
    """A source_to_dest route expands to exactly one leg."""
    config = _load(tmp_path, _CONFIG_SIMPLE)
    plan = build_route_plan(config)
    entry = _entry_by_id(plan, "matrix_to_radio")
    assert entry.enabled is True
    assert entry.directionality == "source_to_dest"
    assert entry.error is None
    assert len(entry.legs) == 1
    assert plan.total_legs == 1

    leg = entry.legs[0]
    assert leg.direction == "source_to_dest"
    assert leg.source_adapter_id == "main"
    assert leg.dest_adapter_id == "radio"
    assert leg.source_platform == "matrix"
    assert leg.dest_platform == "meshtastic"
    assert leg.config_route_id == "matrix_to_radio"
    assert leg.enabled is True


def test_simple_route_carries_targeting(tmp_path: Path) -> None:
    """source_room / dest_channel thread onto the leg."""
    config = _load(tmp_path, _CONFIG_SIMPLE)
    plan = build_route_plan(config)
    leg = _entry_by_id(plan, "matrix_to_radio").legs[0]
    assert leg.source_channel == "!room:fake.local"
    assert leg.dest_channel == "1"


# ===========================================================================
# 3. Bidirectional route → 2 legs, one forward one reverse
# ===========================================================================


def test_bidirectional_route_produces_forward_and_reverse(tmp_path: Path) -> None:
    """A bidirectional route expands to one forward and one reverse leg."""
    config = _load(tmp_path, _CONFIG_BIDIR)
    plan = build_route_plan(config)
    entry = _entry_by_id(plan, "bridge")
    assert entry.directionality == "bidirectional"
    assert len(entry.legs) == 2
    assert plan.total_legs == 2

    directions = sorted(leg.direction for leg in entry.legs)
    assert directions == ["dest_to_source", "source_to_dest"]


def test_bidirectional_reverse_leg_swaps_adapters(tmp_path: Path) -> None:
    """The reverse leg's source/dest adapters are swapped relative to config."""
    config = _load(tmp_path, _CONFIG_BIDIR)
    plan = build_route_plan(config)
    entry = _entry_by_id(plan, "bridge")
    fwd = next(leg for leg in entry.legs if leg.direction == "source_to_dest")
    rev = next(leg for leg in entry.legs if leg.direction == "dest_to_source")
    # Forward: main → radio
    assert fwd.source_adapter_id == "main"
    assert fwd.dest_adapter_id == "radio"
    # Reverse: radio → main
    assert rev.source_adapter_id == "radio"
    assert rev.dest_adapter_id == "main"


def test_bidirectional_legs_have_distinct_expanded_ids(tmp_path: Path) -> None:
    """Forward and reverse legs carry distinct expanded_route_id values."""
    config = _load(tmp_path, _CONFIG_BIDIR)
    plan = build_route_plan(config)
    entry = _entry_by_id(plan, "bridge")
    ids = {leg.expanded_route_id for leg in entry.legs}
    assert len(ids) == 2


# ===========================================================================
# 4. Disabled route → entry shows enabled=False, no legs expanded
# ===========================================================================


def test_disabled_route_has_no_legs(tmp_path: Path) -> None:
    """A disabled route produces an entry with zero legs."""
    config = _load(tmp_path, _CONFIG_DISABLED)
    plan = build_route_plan(config)
    entry = _entry_by_id(plan, "paused")
    assert entry.enabled is False
    assert entry.legs == []
    assert entry.error is None
    # total_legs excludes disabled routes.
    assert plan.total_legs == 0


def test_disabled_route_carries_disabled_warning(tmp_path: Path) -> None:
    """Disabled entries annotate themselves with a 'disabled' warning."""
    config = _load(tmp_path, _CONFIG_DISABLED)
    plan = build_route_plan(config)
    entry = _entry_by_id(plan, "paused")
    assert "disabled" in entry.warnings


# ===========================================================================
# 5. channel_room_map route → multiple legs, one per channel
# ===========================================================================


def test_channel_room_map_produces_one_leg_per_channel(tmp_path: Path) -> None:
    """A 2-entry channel_room_map expands to 2 legs."""
    config = _load(tmp_path, _CONFIG_CHANNEL_ROOM_MAP)
    plan = build_route_plan(config)
    entry = _entry_by_id(plan, "mesh_to_matrix")
    assert len(entry.legs) == 2
    assert plan.total_legs == 2


def test_channel_room_map_legs_carry_key_and_room(tmp_path: Path) -> None:
    """Each leg carries its channel_room_map_key and resolved room."""
    config = _load(tmp_path, _CONFIG_CHANNEL_ROOM_MAP)
    plan = build_route_plan(config)
    entry = _entry_by_id(plan, "mesh_to_matrix")
    by_channel = {leg.channel_room_map_key: leg for leg in entry.legs}
    assert set(by_channel) == {"0", "1"}
    assert by_channel["0"].channel_room_map_room == "!a:fake.local"
    assert by_channel["1"].channel_room_map_room == "!b:fake.local"


def test_channel_room_map_leg_source_is_meshtastic(tmp_path: Path) -> None:
    """source_to_dest with a Meshtastic source produces mesh→matrix legs."""
    config = _load(tmp_path, _CONFIG_CHANNEL_ROOM_MAP)
    plan = build_route_plan(config)
    entry = _entry_by_id(plan, "mesh_to_matrix")
    for leg in entry.legs:
        assert leg.source_adapter_id == "radio"
        assert leg.source_platform == "meshtastic"
        assert leg.dest_adapter_id == "main"
        assert leg.dest_platform == "matrix"
        assert leg.direction == "source_to_dest"


# ===========================================================================
# 6. channel_room_map with structured entries → per-entry origin labels
# ===========================================================================


def test_structured_channel_room_map_carries_per_entry_labels(tmp_path: Path) -> None:
    """Structured entries thread their source_origin_label onto each leg."""
    config = _load(tmp_path, _CONFIG_CHANNEL_ROOM_MAP_STRUCTURED)
    plan = build_route_plan(config)
    entry = _entry_by_id(plan, "mesh_to_matrix")
    by_channel = {leg.channel_room_map_key: leg for leg in entry.legs}
    assert by_channel["0"].source_origin_label == "ChannelA"
    assert by_channel["1"].source_origin_label == "ChannelB"


def test_structured_channel_room_map_provenance_is_per_entry(tmp_path: Path) -> None:
    """Per-entry labels are attributed to the 'per_entry' source."""
    config = _load(tmp_path, _CONFIG_CHANNEL_ROOM_MAP_STRUCTURED)
    plan = build_route_plan(config)
    entry = _entry_by_id(plan, "mesh_to_matrix")
    for leg in entry.legs:
        assert leg.source_origin_label_source == "per_entry"


# ===========================================================================
# 7. Adapter summary → each adapter shows transport, enabled, origin_label
# ===========================================================================


def test_adapter_summary_reports_transport_and_enabled(tmp_path: Path) -> None:
    """AdapterSummary carries transport, enabled, and origin_label."""
    config = _load(tmp_path, _CONFIG_NO_ROUTES)
    plan = build_route_plan(config)
    by_id = {a.adapter_id: a for a in plan.adapters}
    main = by_id["main"]
    radio = by_id["radio"]
    assert main.transport == "matrix"
    assert main.enabled is True
    assert radio.transport == "meshtastic"
    assert radio.enabled is True


def test_adapter_summary_reports_origin_label(tmp_path: Path) -> None:
    """The adapter's configured origin_label appears on the summary."""
    config = _load(tmp_path, _CONFIG_NO_ROUTES)
    plan = build_route_plan(config)
    by_id = {a.adapter_id: a for a in plan.adapters}
    # The meshtastic adapter has origin_label: TestMesh.
    assert by_id["radio"].origin_label == "TestMesh"
    # The matrix adapter has no explicit origin_label → empty string.
    assert by_id["main"].origin_label == ""


def test_adapter_summary_is_frozen_dataclass(tmp_path: Path) -> None:
    """AdapterSummary is a frozen dataclass (JSON-safe, immutable)."""
    config = _load(tmp_path, _CONFIG_NO_ROUTES)
    plan = build_route_plan(config)
    summary = plan.adapters[0]
    assert isinstance(summary, AdapterSummary)
    # Frozen dataclass: attribute assignment raises FrozenInstanceError.
    try:
        summary.adapter_id = "x"  # type: ignore[misc]
    except Exception:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("AdapterSummary should be frozen")


# ===========================================================================
# 8. Empty config (runtime: {} only) → plan succeeds with empty routes list
# ===========================================================================


def test_empty_config_plan_succeeds(tmp_path: Path) -> None:
    """A config with only runtime: {} produces a valid empty plan."""
    config = _load(tmp_path, _CONFIG_EMPTY)
    plan = build_route_plan(config)
    assert plan.adapters == []
    assert plan.routes == []
    assert plan.total_legs == 0
    assert plan.loops == []


def test_empty_config_plan_is_route_plan_instance(tmp_path: Path) -> None:
    """The empty plan is still a proper RoutePlan."""
    config = _load(tmp_path, _CONFIG_EMPTY)
    plan = build_route_plan(config)
    assert isinstance(plan, RoutePlan)


# ===========================================================================
# Extra: loop detection surfaces cross-route cycles without blocking
# ===========================================================================


def test_cross_route_loop_detected_as_warning(tmp_path: Path) -> None:
    """Two routes forming A→B and B→A produce a loop annotation.

    Loops are non-blocking (bidirectional bridges are intentional), so
    the plan still succeeds and the loop is reported in ``plan.loops``.
    """
    config = _load(
        tmp_path,
        _CONFIG_NO_ROUTES + """\
routes:
  a_to_b:
    source_adapters: [main]
    dest_adapters: [radio]
    directionality: source_to_dest
  b_to_a:
    source_adapters: [radio]
    dest_adapters: [main]
    directionality: source_to_dest
""",
    )
    plan = build_route_plan(config)
    # A direct loop between main and radio is detected.
    assert len(plan.loops) >= 1
    loop_text = " ".join(plan.loops)
    assert "main" in loop_text
    assert "radio" in loop_text


# ===========================================================================
# Extra: RoutePlanLeg is a frozen dataclass (model contract)
# ===========================================================================


def test_route_plan_leg_is_frozen_dataclass(tmp_path: Path) -> None:
    """RoutePlanLeg instances are immutable."""
    config = _load(tmp_path, _CONFIG_SIMPLE)
    plan = build_route_plan(config)
    leg = plan.routes[0].legs[0]
    assert isinstance(leg, RoutePlanLeg)
    try:
        leg.direction = "reversed"  # type: ignore[misc]
    except Exception:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("RoutePlanLeg should be frozen")

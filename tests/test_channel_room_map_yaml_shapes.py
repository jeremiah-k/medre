"""YAML parsing and validation edge cases for structured ``channel_room_map``.

The smoke tests in ``test_channel_room_map_context_labels.py`` exercise
the polymorphic parsing via ``RouteConfig.from_toml_dict`` (the same
entry point the YAML loader uses).  This file complements them by
verifying the YAML-front path itself: operators write YAML, the strict
parser produces plain dicts, and the structured-entry shape survives
the full ``parse_yaml_config`` → ``RouteConfigSet.from_toml_dict``
chain.

Invariants verified here that are NOT already covered by the smoke
tests:

* A YAML config with structured ``channel_room_map`` entries loads
  correctly through ``parse_yaml_config`` +
  ``RouteConfigSet.from_toml_dict``.
* Quoted Matrix room IDs (``"!roomA:example.org"``) load correctly.
  The ``!`` sigil is the YAML tag prefix, so quoting is required in
  practice; these tests prove the loader handles it.
* Mixed YAML map: one entry as a bare string, one as a structured
  table, in the same ``channel_room_map``.
* A structured entry with only ``room`` (no labels) parses to a
  ``ChannelRoomMapEntry`` that behaves identically to the bare-string
  form (equality with the bare room string).
* YAML ``source_origin_label: ""`` (explicit empty) parses to an
  empty string, not ``None``.
* YAML ``source_origin_label: null`` (explicit null) parses to ``None``
  so route-level fallback is preserved.
* Bare-string ``channel_room_map`` without labels loads unchanged
  through the YAML loader (backward compat at the file format level,
  not just at ``from_toml_dict``).
* Integer channel keys (``0:`` instead of ``"0":``) work for
  structured entries — YAML 1.1 parses unquoted integers as ``int``,
  and the parser normalises them to the string form.

End-to-end ``load_config`` is exercised once to prove the full
file → ``RouteConfig`` path works for the new shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.config._yaml import parse_yaml_config
from medre.config.routes import (
    ChannelRoomMapEntry,
    RouteConfigSet,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _routes_from_yaml(yaml_text: str) -> RouteConfigSet:
    """Parse a YAML string into a ``RouteConfigSet``.

    Mirrors the loader path: strict YAML parse →
    ``RouteConfigSet.from_toml_dict``.  This is exactly what
    ``load_config`` does between reading the file and returning the
    typed config.
    """
    data = parse_yaml_config(yaml_text, source="<test>")
    return RouteConfigSet.from_toml_dict(data)


def _crm_entry(rcs: RouteConfigSet, route_id: str, channel: str) -> ChannelRoomMapEntry:
    """Return the single channel_room_map entry for a route + channel."""
    for r in rcs.routes:
        if r.route_id == route_id:
            assert r.channel_room_map is not None
            entry = r.channel_room_map[channel]
            assert isinstance(entry, ChannelRoomMapEntry)
            return entry
    raise AssertionError(f"route {route_id!r} not found")


_MINIMAL_HEADER = """\
runtime:
  name: crm_yaml_test
"""


def _yaml_with_crm(crm_yaml_block: str) -> str:
    """Wrap a channel_room_map block in a minimal valid YAML document."""
    return (
        _MINIMAL_HEADER
        + "routes:\n"
        + "  bridge:\n"
        + "    source_adapters: [matrix_adapter]\n"
        + "    dest_adapters: [mesh_adapter]\n"
        + "    directionality: bidirectional\n"
        + "    channel_room_map:\n"
        + crm_yaml_block
    )


# ===========================================================================
# 1. Structured entries load through the YAML parser
# ===========================================================================


def test_structured_entry_loads_through_yaml_parser() -> None:
    yaml_text = _yaml_with_crm(
        '      "0":\n'
        '        room: "!room0:example.com"\n'
        '        source_origin_label: "Ops Channel"\n'
        '        dest_origin_label: "Radio 0"\n'
    )
    rcs = _routes_from_yaml(yaml_text)
    entry = _crm_entry(rcs, "bridge", "0")
    assert entry.room == "!room0:example.com"
    assert entry.source_origin_label == "Ops Channel"
    assert entry.dest_origin_label == "Radio 0"


def test_yaml_structured_entry_with_only_room() -> None:
    """A structured entry with just ``room`` parses to a label-less entry."""
    yaml_text = _yaml_with_crm('      "0":\n' '        room: "!room0:example.com"\n')
    rcs = _routes_from_yaml(yaml_text)
    entry = _crm_entry(rcs, "bridge", "0")
    assert entry.room == "!room0:example.com"
    assert entry.source_origin_label is None
    assert entry.dest_origin_label is None


# ===========================================================================
# 2. Quoted Matrix room IDs load correctly
#
# In YAML the ``!`` sigil begins a tag, so a canonical Matrix room ID
# MUST be quoted in the config file.  These tests confirm the loader
# preserves the quoted value verbatim.
# ===========================================================================


def test_quoted_matrix_room_id_loads_verbatim() -> None:
    yaml_text = _yaml_with_crm(
        '      "0":\n'
        '        room: "!roomA:example.org"\n'
        '        source_origin_label: "X"\n'
    )
    rcs = _routes_from_yaml(yaml_text)
    entry = _crm_entry(rcs, "bridge", "0")
    assert entry.room == "!roomA:example.org"


def test_quoted_matrix_room_bare_string_loads_verbatim() -> None:
    """Quoted room ID works in the legacy bare-string form too."""
    yaml_text = _yaml_with_crm('      "0": "!roomA:example.org"\n')
    rcs = _routes_from_yaml(yaml_text)
    entry = _crm_entry(rcs, "bridge", "0")
    assert entry.room == "!roomA:example.org"
    assert entry.source_origin_label is None
    assert entry.dest_origin_label is None


# ===========================================================================
# 3. Mixed map: bare-string and structured entries in the same channel_room_map
# ===========================================================================


def test_mixed_bare_and_structured_entries_in_yaml() -> None:
    yaml_text = _yaml_with_crm(
        '      "0": "!room0:example.com"\n'
        '      "1":\n'
        '        room: "!room1:example.com"\n'
        '        source_origin_label: "Ops"\n'
    )
    rcs = _routes_from_yaml(yaml_text)
    entry0 = _crm_entry(rcs, "bridge", "0")
    entry1 = _crm_entry(rcs, "bridge", "1")
    assert entry0.room == "!room0:example.com"
    assert entry0.source_origin_label is None
    assert entry1.room == "!room1:example.com"
    assert entry1.source_origin_label == "Ops"


def test_mixed_entries_structured_only_room_equals_bare() -> None:
    """A structured entry with only ``room`` compares equal to a bare
    string in the same map.  This proves the backward-compat equality
    semantics survive the YAML loader."""
    yaml_text = _yaml_with_crm(
        '      "0": "!room0:example.com"\n'
        '      "1":\n'
        '        room: "!room1:example.com"\n'
    )
    rcs = _routes_from_yaml(yaml_text)
    route = next(r for r in rcs.routes if r.route_id == "bridge")
    assert route.channel_room_map is not None
    # Both entries behave like bare strings under equality.
    assert route.channel_room_map == {
        "0": "!room0:example.com",
        "1": "!room1:example.com",
    }


# ===========================================================================
# 4. Explicit empty string and explicit null in structured entries
# ===========================================================================


def test_yaml_explicit_empty_string_label_preserved() -> None:
    """``source_origin_label: ""`` is preserved as the empty string."""
    yaml_text = _yaml_with_crm(
        '      "0":\n'
        '        room: "!room0:example.com"\n'
        '        source_origin_label: ""\n'
    )
    rcs = _routes_from_yaml(yaml_text)
    entry = _crm_entry(rcs, "bridge", "0")
    # Empty string, NOT None — explicit-suppression sentinel.
    assert entry.source_origin_label == ""
    assert entry.source_origin_label is not None


def test_yaml_explicit_null_label_parses_to_none() -> None:
    """``source_origin_label: null`` parses to None so route-level fallback applies."""
    yaml_text = _yaml_with_crm(
        '      "0":\n'
        '        room: "!room0:example.com"\n'
        "        source_origin_label: null\n"
    )
    rcs = _routes_from_yaml(yaml_text)
    entry = _crm_entry(rcs, "bridge", "0")
    assert entry.source_origin_label is None


def test_yaml_explicit_empty_dest_label_preserved() -> None:
    """``dest_origin_label: ""`` is preserved as the empty string."""
    yaml_text = _yaml_with_crm(
        '      "0":\n'
        '        room: "!room0:example.com"\n'
        '        dest_origin_label: ""\n'
    )
    rcs = _routes_from_yaml(yaml_text)
    entry = _crm_entry(rcs, "bridge", "0")
    assert entry.dest_origin_label == ""


def test_yaml_explicit_null_dest_label_parses_to_none() -> None:
    yaml_text = _yaml_with_crm(
        '      "0":\n'
        '        room: "!room0:example.com"\n'
        "        dest_origin_label: null\n"
    )
    rcs = _routes_from_yaml(yaml_text)
    entry = _crm_entry(rcs, "bridge", "0")
    assert entry.dest_origin_label is None


def test_yaml_tilde_parses_to_none_for_label() -> None:
    """YAML ``~`` is the canonical null; the entry label must be None."""
    yaml_text = _yaml_with_crm(
        '      "0":\n'
        '        room: "!room0:example.com"\n'
        "        source_origin_label: ~\n"
    )
    rcs = _routes_from_yaml(yaml_text)
    entry = _crm_entry(rcs, "bridge", "0")
    assert entry.source_origin_label is None


# ===========================================================================
# 5. Bare-string channel_room_map backward compat through YAML loader
# ===========================================================================


def test_bare_string_crm_loads_unchanged_through_yaml() -> None:
    """A legacy bare-string ``channel_room_map`` loads unchanged."""
    yaml_text = _yaml_with_crm(
        '      "0": "!room0:example.com"\n' '      "1": "!room1:example.com"\n'
    )
    rcs = _routes_from_yaml(yaml_text)
    route = next(r for r in rcs.routes if r.route_id == "bridge")
    assert route.channel_room_map is not None
    # Backward compat: normalised entries equal their bare room strings.
    assert route.channel_room_map == {
        "0": "!room0:example.com",
        "1": "!room1:example.com",
    }


def test_bare_string_crm_entries_are_channelroommapentry_instances() -> None:
    """Even bare-string values normalise to ``ChannelRoomMapEntry``."""
    yaml_text = _yaml_with_crm('      "0": "!room0:example.com"\n')
    rcs = _routes_from_yaml(yaml_text)
    entry = _crm_entry(rcs, "bridge", "0")
    assert isinstance(entry, ChannelRoomMapEntry)
    assert entry.source_origin_label is None
    assert entry.dest_origin_label is None


# ===========================================================================
# 6. Integer channel keys in YAML
#
# YAML 1.1 parses unquoted small integers as ``int``.  Operators
# commonly write ``0:`` rather than ``"0":``.  The parser must
# normalise int keys to the canonical string form for both bare-string
# and structured entries.
# ===========================================================================


def test_int_channel_key_with_structured_entry_normalised() -> None:
    yaml_text = _yaml_with_crm(
        "      0:\n"
        '        room: "!room0:example.com"\n'
        '        source_origin_label: "Ops"\n'
    )
    rcs = _routes_from_yaml(yaml_text)
    route = next(r for r in rcs.routes if r.route_id == "bridge")
    assert route.channel_room_map is not None
    # Channel key normalised to string "0".
    assert "0" in route.channel_room_map
    entry = route.channel_room_map["0"]
    assert isinstance(entry, ChannelRoomMapEntry)
    assert entry.room == "!room0:example.com"
    assert entry.source_origin_label == "Ops"


def test_int_channel_key_with_bare_string_normalised() -> None:
    yaml_text = _yaml_with_crm('      0: "!room0:example.com"\n')
    rcs = _routes_from_yaml(yaml_text)
    entry = _crm_entry(rcs, "bridge", "0")
    assert entry.room == "!room0:example.com"


def test_mixed_int_and_string_channel_keys_both_normalised() -> None:
    """``0:`` and ``"1":`` both normalise to the same canonical string form."""
    yaml_text = _yaml_with_crm(
        '      0: "!room0:example.com"\n' '      "1": "!room1:example.com"\n'
    )
    rcs = _routes_from_yaml(yaml_text)
    route = next(r for r in rcs.routes if r.route_id == "bridge")
    assert route.channel_room_map is not None
    assert set(route.channel_room_map.keys()) == {"0", "1"}


# ===========================================================================
# 7. End-to-end: load_config (file → RouteConfig)
# ===========================================================================


def test_load_config_full_path_structured_entry(tmp_path: Path) -> None:
    """The full ``load_config`` path parses a structured channel_room_map."""
    from medre.config.loader import load_config

    yaml_text = (
        "runtime:\n"
        "  name: crm_e2e\n"
        "routes:\n"
        "  bridge:\n"
        "    source_adapters: [matrix_adapter]\n"
        "    dest_adapters: [mesh_adapter]\n"
        "    directionality: bidirectional\n"
        "    channel_room_map:\n"
        '      "0":\n'
        '        room: "!room0:example.com"\n'
        '        source_origin_label: "Ops"\n'
        '        dest_origin_label: "Radio"\n'
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text, encoding="utf-8")

    config, _, _ = load_config(str(config_path))
    route = config.routes.routes[0]
    assert route.channel_room_map is not None
    entry = route.channel_room_map["0"]
    assert isinstance(entry, ChannelRoomMapEntry)
    assert entry.room == "!room0:example.com"
    assert entry.source_origin_label == "Ops"
    assert entry.dest_origin_label == "Radio"


def test_load_config_full_path_bare_string_backward_compat(
    tmp_path: Path,
) -> None:
    """The full ``load_config`` path still accepts bare-string maps."""
    from medre.config.loader import load_config

    yaml_text = (
        "runtime:\n"
        "  name: crm_legacy\n"
        "routes:\n"
        "  bridge:\n"
        "    source_adapters: [matrix_adapter]\n"
        "    dest_adapters: [mesh_adapter]\n"
        "    directionality: bidirectional\n"
        "    channel_room_map:\n"
        '      "0": "!room0:example.com"\n'
        '      "1": "!room1:example.com"\n'
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text, encoding="utf-8")

    config, _, _ = load_config(str(config_path))
    route = config.routes.routes[0]
    assert route.channel_room_map is not None
    # Backward compat with legacy flat dict.
    assert route.channel_room_map == {
        "0": "!room0:example.com",
        "1": "!room1:example.com",
    }


# ===========================================================================
# 8. Validation surfaces still fire through the YAML path
#
# Structured entries do not bypass room/channel validation.  These
# guards are already tested directly through ``from_toml_dict`` in the
# smoke tests; here we confirm they also fire through the YAML loader
# so an operator writing YAML gets the same error.
# ===========================================================================


def test_yaml_structured_entry_unknown_key_rejected() -> None:
    from medre.config.errors import ConfigValidationError

    yaml_text = _yaml_with_crm(
        '      "0":\n'
        '        room: "!room0:example.com"\n'
        '        bogus_key: "bad"\n'
    )
    with pytest.raises(ConfigValidationError, match="unknown key"):
        _routes_from_yaml(yaml_text)


def test_yaml_structured_entry_missing_room_rejected() -> None:
    from medre.config.errors import ConfigValidationError

    yaml_text = _yaml_with_crm(
        '      "0":\n' '        source_origin_label: "No Room"\n'
    )
    with pytest.raises(ConfigValidationError, match="missing required 'room'"):
        _routes_from_yaml(yaml_text)


def test_yaml_structured_entry_alias_room_rejected() -> None:
    """Room aliases (``#``) are rejected in structured YAML form."""
    from medre.config.errors import ConfigValidationError

    yaml_text = _yaml_with_crm('      "0":\n' '        room: "#room:example.com"\n')
    with pytest.raises(ConfigValidationError, match="room alias"):
        _routes_from_yaml(yaml_text)


def test_yaml_structured_entry_non_canonical_room_rejected() -> None:
    from medre.config.errors import ConfigValidationError

    yaml_text = _yaml_with_crm('      "0":\n' '        room: "not_a_room"\n')
    with pytest.raises(ConfigValidationError, match="canonical Matrix room ID"):
        _routes_from_yaml(yaml_text)


def test_yaml_bool_source_label_rejected() -> None:
    from medre.config.errors import ConfigValidationError

    yaml_text = _yaml_with_crm(
        '      "0":\n'
        '        room: "!room0:example.com"\n'
        "        source_origin_label: true\n"
    )
    with pytest.raises(ConfigValidationError, match="must be a string"):
        _routes_from_yaml(yaml_text)


def test_yaml_int_source_label_rejected() -> None:
    from medre.config.errors import ConfigValidationError

    yaml_text = _yaml_with_crm(
        '      "0":\n'
        '        room: "!room0:example.com"\n'
        "        source_origin_label: 42\n"
    )
    with pytest.raises(ConfigValidationError, match="must be a string"):
        _routes_from_yaml(yaml_text)

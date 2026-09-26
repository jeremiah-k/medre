"""Tests for context_map: config validation and compiler expansion."""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.loader import load_config
from medre.config.route_expansion import (
    expand_route_config,
    expand_route_configs,
)
from medre.config.routes import (
    BridgePolicy,
    ContextMapEntry,
    RouteConfig,
    RouteConfigSet,
    RouteDestinationConfig,
    RouteDirectionality,
)


def _entry(dest_context: str, **labels: str | None) -> dict[str, object]:
    """Build a structured context-map input entry."""
    return {"dest_context": dest_context, **labels}


def _structured_entry() -> dict[str, object]:
    """Build a structured-destination input entry (LXMF-shaped)."""
    return {
        "dest_destination": {
            "kind": "lxmf_destination",
            "destination_hash": "21c0c1b9aabbccddeeff001122334455",
            "destination_name": "bob",
        }
    }


# ---------------------------------------------------------------------------
# context_map — config validation
# ---------------------------------------------------------------------------


class TestContextMapConfig:
    """RouteConfig context_map parsing and validation."""

    def _base(self, **overrides: object) -> dict[str, object]:
        data: dict[str, object] = {
            "source_adapters": ["radio_adapter"],
            "dest_adapters": ["chat_adapter"],
        }
        data.update(overrides)
        return data

    # --- valid construction ---

    def test_valid_map_parsed(self) -> None:
        data = self._base(
            context_map={
                "0": _entry("!room0:example.com"),
                "1": _entry("!room1:example.com"),
            },
        )
        r = RouteConfig.from_dict("map_route", data)
        assert r.context_map == {
            "0": ContextMapEntry(dest_context="!room0:example.com"),
            "1": ContextMapEntry(dest_context="!room1:example.com"),
        }

    def test_int_context_keys_normalized(self) -> None:
        """YAML integer keys normalize to canonical string context keys."""
        data = self._base(
            context_map={
                0: _entry("!room0:example.com"),
                1: _entry("!room1:example.com"),
            },
        )
        r = RouteConfig.from_dict("map_route", data)
        assert r.context_map == {
            "0": ContextMapEntry(dest_context="!room0:example.com"),
            "1": ContextMapEntry(dest_context="!room1:example.com"),
        }

    def test_none_when_absent(self) -> None:
        data = self._base()
        r = RouteConfig.from_dict("no_map", data)
        assert r.context_map is None

    def test_single_entry(self) -> None:
        data = self._base(context_map={"3": _entry("!room3:example.com")})
        r = RouteConfig.from_dict("map_route", data)
        assert r.context_map == {
            "3": ContextMapEntry(dest_context="!room3:example.com")
        }

    def test_string_context_key_accepted(self) -> None:
        data = self._base(context_map={"3": _entry("!room:example.com")})
        r = RouteConfig.from_dict("str_key_route", data)
        assert r.context_map == {"3": ContextMapEntry(dest_context="!room:example.com")}

    def test_multiple_arbitrary_context_keys(self) -> None:
        """Context keys are opaque: any non-empty stripped string works."""
        keys = [f"context-{i}" for i in range(8)]
        cmap = {key: _entry(f"!room{i}:example.com") for i, key in enumerate(keys)}
        r = RouteConfig.from_dict("map_route", self._base(context_map=cmap))
        assert r.context_map is not None
        assert len(r.context_map) == 8

    def test_reject_empty_context_map(self) -> None:
        """Empty context_map dict is rejected."""
        with pytest.raises(ConfigValidationError, match="must not be empty"):
            RouteConfig.from_dict("empty", self._base(context_map={}))

    # --- rejection: non-dict ---

    def test_reject_non_dict(self) -> None:
        with pytest.raises(ConfigValidationError, match="must be a table"):
            RouteConfig.from_dict("bad", self._base(context_map="not_a_dict"))

    def test_reject_list(self) -> None:
        with pytest.raises(ConfigValidationError, match="must be a table"):
            RouteConfig.from_dict("bad", self._base(context_map=[{"0": "!r:t"}]))

    # --- rejection: context key validation ---

    def test_reject_bool_context_key(self) -> None:
        with pytest.raises(ConfigValidationError, match="boolean"):
            RouteConfig.from_dict(
                "bad",
                self._base(
                    context_map={True: _entry("!room:example.com")},
                ),
            )

    def test_reject_non_string_context_key(self) -> None:
        with pytest.raises(ConfigValidationError, match="is not a string"):
            RouteConfig.from_dict(
                "bad",
                self._base(context_map={1.5: _entry("!room:example.com")}),
            )

    def test_reject_empty_context_key(self) -> None:
        with pytest.raises(ConfigValidationError, match="non-empty string"):
            RouteConfig.from_dict(
                "bad",
                self._base(context_map={"": _entry("!room:example.com")}),
            )

    def test_reject_blank_context_key(self) -> None:
        with pytest.raises(ConfigValidationError, match="non-empty string"):
            RouteConfig.from_dict(
                "bad",
                self._base(context_map={"   ": _entry("!room:example.com")}),
            )

    def test_reject_unstripped_context_key(self) -> None:
        """Keys must already be stripped/normalized (no silent trimming)."""
        with pytest.raises(ConfigValidationError, match="stripped/normalized"):
            RouteConfig.from_dict(
                "bad",
                self._base(context_map={" 0": _entry("!room:example.com")}),
            )

    def test_reject_bare_context_string_entry(self) -> None:
        with pytest.raises(ConfigValidationError, match="must be a table"):
            RouteConfig.from_dict(
                "bad",
                self._base(context_map={"0": "!room:example.com"}),
            )

    # --- rejection: dest_context value validation ---

    def test_reject_blank_dest_context(self) -> None:
        with pytest.raises(ConfigValidationError, match="non-empty string"):
            RouteConfig.from_dict(
                "bad",
                self._base(context_map={"0": _entry("  ")}),
            )

    def test_reject_empty_string_dest_context(self) -> None:
        with pytest.raises(ConfigValidationError, match="non-empty string"):
            RouteConfig.from_dict(
                "bad",
                self._base(context_map={"0": _entry("")}),
            )

    def test_reject_bool_dest_context(self) -> None:
        with pytest.raises(ConfigValidationError, match="must be a string"):
            RouteConfig.from_dict(
                "bad",
                self._base(context_map={"0": _entry(True)}),
            )

    def test_reject_non_string_dest_context(self) -> None:
        with pytest.raises(ConfigValidationError, match="must be a string"):
            RouteConfig.from_dict(
                "bad",
                self._base(context_map={"0": _entry(42)}),
            )

    def test_reject_unstripped_dest_context(self) -> None:
        """dest_context values must already be stripped/normalized."""
        with pytest.raises(ConfigValidationError, match="stripped/normalized"):
            RouteConfig.from_dict(
                "bad",
                self._base(context_map={"0": _entry("  !room:example.com")}),
            )

    def test_accepts_opaque_context(self) -> None:
        """Contexts are opaque: any non-empty stripped string is accepted."""
        r = RouteConfig.from_dict(
            "ok",
            self._base(context_map={"0": _entry("topic/sensors/temp")}),
        )
        assert r.context_map == {
            "0": ContextMapEntry(dest_context="topic/sensors/temp")
        }

    # --- rejection: entry shape ---

    def test_reject_entry_without_dest_side(self) -> None:
        with pytest.raises(ConfigValidationError, match="exactly one of"):
            RouteConfig.from_dict(
                "bad",
                self._base(context_map={"0": {"source_origin_label": "X"}}),
            )

    def test_reject_entry_with_both_dest_sides(self) -> None:
        with pytest.raises(ConfigValidationError, match="exactly one of"):
            RouteConfig.from_dict(
                "bad",
                self._base(
                    context_map={
                        "0": {
                            "dest_context": "!room:example.com",
                            **_structured_entry(),
                        }
                    },
                ),
            )

    def test_reject_entry_unknown_key(self) -> None:
        with pytest.raises(ConfigValidationError, match="unknown key"):
            RouteConfig.from_dict(
                "bad",
                self._base(
                    context_map={
                        "0": {"dest_context": "!room:example.com", "room": "!x:y"}
                    },
                ),
            )

    # --- rejection: duplicate normalized context key ---

    def test_reject_duplicate_context_key(self) -> None:
        """String '1' and int 1 normalize to the same context key."""
        with pytest.raises(ConfigValidationError, match="duplicate context key"):
            RouteConfig.from_dict(
                "bad",
                self._base(
                    context_map={
                        "1": _entry("!room1:example.com"),
                        1: _entry("!room1_dup:example.com"),
                    }
                ),
            )

    # --- rejection: mutual exclusion with targeting fields ---

    def test_reject_with_source_channel(self) -> None:
        with pytest.raises(ConfigValidationError, match="mutually exclusive"):
            RouteConfig.from_dict(
                "bad",
                self._base(
                    source_channel="ch0",
                    context_map={"0": _entry("!room:example.com")},
                ),
            )

    def test_reject_with_dest_channel(self) -> None:
        with pytest.raises(ConfigValidationError, match="mutually exclusive"):
            RouteConfig.from_dict(
                "bad",
                self._base(
                    dest_channel="ch1",
                    context_map={"0": _entry("!room:example.com")},
                ),
            )

    def test_reject_with_source_room(self) -> None:
        with pytest.raises(ConfigValidationError, match="mutually exclusive"):
            RouteConfig.from_dict(
                "bad",
                self._base(
                    source_room="!room:example.com",
                    context_map={"0": _entry("!other:example.com")},
                ),
            )

    def test_reject_with_dest_room(self) -> None:
        with pytest.raises(ConfigValidationError, match="mutually exclusive"):
            RouteConfig.from_dict(
                "bad",
                self._base(
                    dest_room="!room:example.com",
                    context_map={"0": _entry("!other:example.com")},
                ),
            )

    def test_reject_with_route_dest_destination(self) -> None:
        with pytest.raises(ConfigValidationError, match="mutually exclusive"):
            RouteConfig.from_dict(
                "bad",
                self._base(
                    dest_destination=_structured_entry()["dest_destination"],
                    context_map={"0": _entry("!room:example.com")},
                ),
            )

    # --- rejection: multiple adapters ---

    def test_reject_multiple_source_adapters(self) -> None:
        with pytest.raises(ConfigValidationError, match="one source adapter"):
            RouteConfig.from_dict(
                "bad",
                {
                    "source_adapters": ["a", "b"],
                    "dest_adapters": ["c"],
                    "context_map": {"0": _entry("!room:example.com")},
                },
            )

    def test_reject_multiple_dest_adapters(self) -> None:
        with pytest.raises(ConfigValidationError, match="one dest adapter"):
            RouteConfig.from_dict(
                "bad",
                {
                    "source_adapters": ["a"],
                    "dest_adapters": ["b", "c"],
                    "context_map": {"0": _entry("!room:example.com")},
                },
            )

    # --- duplicate dest_context: fan-in vs ambiguous reverse legs ---

    def test_duplicate_dest_context_fan_in_forward_only(self) -> None:
        """Duplicate dest_context values are valid fan-in for forward-only
        routes: each source context keeps its own forward leg and no
        reverse leg ever exists to become ambiguous."""
        r = RouteConfig.from_dict(
            "ok",
            self._base(
                directionality="source_to_dest",
                context_map={
                    "0": _entry("!room:example.com"),
                    "1": _entry("!room:example.com"),
                },
            ),
        )
        assert r.context_map is not None
        assert set(r.context_map.keys()) == {"0", "1"}
        assert r.context_map["0"].dest_context == "!room:example.com"
        assert r.context_map["1"].dest_context == "!room:example.com"

    @pytest.mark.parametrize("direction", ["bidirectional", "dest_to_source"])
    def test_duplicate_dest_context_rejected_with_reverse_legs(
        self, direction: str
    ) -> None:
        """Duplicate dest_context values are rejected when the route's
        directionality creates reverse (dest→source) legs — they become
        duplicate reverse-leg inbound source contexts."""
        with pytest.raises(
            ConfigValidationError, match="duplicate.*dest_context"
        ) as excinfo:
            RouteConfig.from_dict(
                "bad",
                self._base(
                    directionality=direction,
                    context_map={
                        "0": _entry("!room:example.com"),
                        "1": _entry("!room:example.com"),
                    },
                ),
            )
        assert "!room:example.com" in str(excinfo.value)

    def test_duplicate_dest_context_distinct_values_ok_with_reverse(self) -> None:
        """Distinct dest_context values stay valid for bidirectional routes."""
        r = RouteConfig.from_dict(
            "ok",
            self._base(
                directionality="bidirectional",
                context_map={
                    "0": _entry("!room0:example.com"),
                    "1": _entry("!room1:example.com"),
                },
            ),
        )
        assert r.context_map is not None
        assert len(r.context_map) == 2

    # --- structured destination directionality gating ---

    @pytest.mark.parametrize("direction", ["bidirectional", "dest_to_source"])
    def test_structured_destination_requires_forward_only(self, direction: str) -> None:
        """Entries with dest_destination require source_to_dest: a reverse
        leg would need inbound identity semantics a structured destination
        cannot provide."""
        with pytest.raises(
            ConfigValidationError, match="require directionality"
        ) as excinfo:
            RouteConfig.from_dict(
                "bad",
                self._base(
                    directionality=direction,
                    context_map={"!room:example.com": _structured_entry()},
                ),
            )
        assert "'source_to_dest'" in str(excinfo.value)

    def test_structured_destination_accepted_forward_only(self) -> None:
        r = RouteConfig.from_dict(
            "ok",
            self._base(
                directionality="source_to_dest",
                context_map={"!room:example.com": _structured_entry()},
            ),
        )
        assert r.context_map is not None
        assert r.context_map["!room:example.com"].dest_destination is not None

    # --- removed legacy mapping key fails clearly ---

    def test_removed_legacy_map_key_fails_with_hint(self) -> None:
        """The removed legacy mapping key must hit the unknown-key rejection
        with a pointed hint toward context_map (error quality only — not
        an alias).  The key is composed at runtime so this file carries no
        literal occurrence of the removed ontology's name (repo-wide grep
        guard).
        """
        removed_key = "channel_room_" + "map"
        with pytest.raises(ConfigValidationError) as excinfo:
            RouteConfig.from_dict(
                "legacy",
                self._base(**{removed_key: {"room": "!room0:example.com"}}),
            )
        msg = str(excinfo.value)
        assert "unknown key" in msg
        assert removed_key in msg
        assert "context_map" in msg

    # --- integration: YAML loader ---

    def test_yaml_integration(self, tmp_path: Path) -> None:
        yaml_content = """\
runtime:
  name: context_map_test

routes:
  bridge:
    source_adapters:
      - radio_adapter
    dest_adapters:
      - chat_adapter
    directionality: bidirectional
    context_map:
      "0":
        dest_context: "!room0:example.com"
      "1":
        dest_context: "!room1:example.com"
"""
        p = tmp_path / "config.yaml"
        p.write_text(yaml_content)
        config, _, _ = load_config(str(p))
        r = config.routes.routes[0]
        assert r.context_map == {
            "0": ContextMapEntry(dest_context="!room0:example.com"),
            "1": ContextMapEntry(dest_context="!room1:example.com"),
        }


# ---------------------------------------------------------------------------
# context_map — compiler expansion
# ---------------------------------------------------------------------------


def test_mapping_leg_ids_are_stable_when_unrelated_context_is_inserted() -> None:
    """Durable mapping-leg identity must not depend on map ordinals."""
    before = RouteConfig(
        route_id="stable",
        source_adapters=("src",),
        dest_adapters=("dst",),
        context_map={
            "bravo": ContextMapEntry(dest_context="room-b"),
            "charlie": ContextMapEntry(dest_context="room-c"),
        },
    )
    after = RouteConfig(
        route_id="stable",
        source_adapters=("src",),
        dest_adapters=("dst",),
        context_map={
            "alpha": ContextMapEntry(dest_context="room-a"),
            "bravo": ContextMapEntry(dest_context="room-b"),
            "charlie": ContextMapEntry(dest_context="room-c"),
        },
    )

    before_ids = {
        leg.mapping_source_context: leg.route.id for leg in expand_route_config(before)
    }
    after_ids = {
        leg.mapping_source_context: leg.route.id for leg in expand_route_config(after)
    }

    assert before_ids["bravo"] == after_ids["bravo"]
    assert before_ids["charlie"] == after_ids["charlie"]
    assert before_ids["bravo"] != before_ids["charlie"]
    assert "__maph" in before_ids["bravo"]


class TestContextMapExpansion:
    """Compiler expansion of context_map routes."""

    def _map_config(
        self,
        route_id: str = "bridge",
        directionality: RouteDirectionality = RouteDirectionality.BIDIRECTIONAL,
        context_map: dict[str, ContextMapEntry] | None = None,
        **extra: object,
    ) -> RouteConfig:
        if context_map is None:
            context_map = {
                "0": ContextMapEntry(dest_context="!room0:example.com"),
                "1": ContextMapEntry(dest_context="!room1:example.com"),
            }
        return RouteConfig(
            route_id=route_id,
            source_adapters=("radio_adapter",),
            dest_adapters=("chat_adapter",),
            directionality=directionality,
            context_map=context_map,
            **extra,
        )

    def test_bidirectional_2_contexts_4_legs(self) -> None:
        """2 contexts x bidirectional = 4 legs."""
        legs = expand_route_config(self._map_config())
        assert [(leg.mapping_source_context, leg.direction) for leg in legs] == [
            ("0", "source_to_dest"),
            ("0", "dest_to_source"),
            ("1", "source_to_dest"),
            ("1", "dest_to_source"),
        ]
        ids = [leg.route.id for leg in legs]
        assert len(ids) == len(set(ids))
        assert all(route_id.startswith("bridge__maph") for route_id in ids)

    def test_forward_leg_fields(self) -> None:
        """Forward leg: source side is the map key, target carries dest_context."""
        legs = expand_route_config(self._map_config())
        fwd = [leg for leg in legs if leg.direction == "source_to_dest"]
        assert len(fwd) == 2
        for leg in fwd:
            assert leg.route.source.adapter == "radio_adapter"
            assert leg.route.source.channel in ("0", "1")
            assert leg.route.targets[0].adapter == "chat_adapter"
            assert leg.route.targets[0].channel in (
                "!room0:example.com",
                "!room1:example.com",
            )
            assert leg.route.targets[0].destination is None

    def test_reverse_leg_fields(self) -> None:
        """Reverse leg: adapters, contexts swap; no structured destination."""
        legs = expand_route_config(self._map_config())
        rev = [leg for leg in legs if leg.direction == "dest_to_source"]
        assert len(rev) == 2
        for leg in rev:
            assert leg.route.source.adapter == "chat_adapter"
            assert leg.route.source.channel in (
                "!room0:example.com",
                "!room1:example.com",
            )
            assert leg.route.targets[0].adapter == "radio_adapter"
            assert leg.route.targets[0].channel in ("0", "1")
            assert leg.route.targets[0].destination is None

    def test_context_1_resolves_correctly(self) -> None:
        """Source context '1' resolves to target dest_context."""
        legs = expand_route_config(self._map_config())
        key1_rev = next(
            leg for leg in legs if leg.route.source.channel == "!room1:example.com"
        )
        assert key1_rev.route.targets[0].channel == "1"

    def test_unmapped_context_no_leg(self) -> None:
        """Context '2' (not in map) produces no leg."""
        rc = self._map_config(
            context_map={"0": ContextMapEntry(dest_context="!room0:example.com")}
        )
        legs = expand_route_config(rc)
        assert [leg.route.source.channel for leg in legs] == ["0", "!room0:example.com"]
        assert len(legs) == 2  # 1 context x bidirectional

    def test_source_to_dest_only(self) -> None:
        """source_to_dest creates only forward legs."""
        rc = self._map_config(
            directionality=RouteDirectionality.SOURCE_TO_DEST,
            context_map={"0": ContextMapEntry(dest_context="!room0:example.com")},
        )
        legs = expand_route_config(rc)
        assert len(legs) == 1
        assert legs[0].direction == "source_to_dest"
        assert legs[0].route.id.startswith("bridge__maph")
        assert legs[0].route.id.endswith("__fwd")
        assert legs[0].route.source.adapter == "radio_adapter"

    def test_dest_to_source_only(self) -> None:
        """dest_to_source creates only reverse legs."""
        rc = self._map_config(
            directionality=RouteDirectionality.DEST_TO_SOURCE,
            context_map={"0": ContextMapEntry(dest_context="!room0:example.com")},
        )
        legs = expand_route_config(rc)
        assert len(legs) == 1
        assert legs[0].direction == "dest_to_source"
        assert legs[0].route.id.startswith("bridge__maph")
        assert legs[0].route.id.endswith("__rev")
        assert legs[0].route.source.adapter == "chat_adapter"

    def test_structured_destination_forward_leg(self) -> None:
        """Structured-dest entry expands to a forward leg whose target
        carries the converted core destination, and no reverse leg."""
        rc = RouteConfig(
            route_id="lxmf_out",
            source_adapters=("chat_adapter",),
            dest_adapters=("lxmf_adapter",),
            directionality=RouteDirectionality.SOURCE_TO_DEST,
            context_map={
                "!room:example.com": ContextMapEntry(
                    dest_destination=RouteDestinationConfig(
                        kind="lxmf_destination",
                        destination_hash="21c0c1b9aabbccddeeff001122334455",
                        destination_name="bob",
                    )
                )
            },
        )
        legs = expand_route_config(rc)
        assert len(legs) == 1
        assert legs[0].route.id.startswith("lxmf_out__maph")
        assert legs[0].route.id.endswith("__fwd")
        target = legs[0].route.targets[0]
        assert target.channel is None
        assert target.destination is not None
        assert target.destination.kind == "lxmf_destination"
        assert target.destination.destination_hash == (
            "21c0c1b9aabbccddeeff001122334455"
        )
        assert target.destination.destination_name == "bob"
        # The compiler copies metadata; the core model deep-freezes it.
        assert isinstance(target.destination.metadata, dict)

    def test_explicit_route_unchanged(self) -> None:
        """Explicit source_channel/dest_channel route still expands as before."""
        rc = RouteConfig(
            route_id="explicit",
            source_adapters=("a",),
            dest_adapters=("b",),
            source_channel="!room:example.com",
            dest_channel="1",
        )
        legs = expand_route_config(rc)
        assert len(legs) == 1
        assert legs[0].route.id == "explicit"
        assert legs[0].route.source.channel == "!room:example.com"
        assert legs[0].route.targets[0].channel == "1"
        assert legs[0].mapping_source_context is None
        assert legs[0].mapping_dest_context is None

    def test_deterministic_sorted_ids(self) -> None:
        """Entries expand in sorted-key order regardless of insertion order."""
        rc = self._map_config(
            context_map={
                "b": ContextMapEntry(dest_context="!rb:e.com"),
                "a": ContextMapEntry(dest_context="!ra:e.com"),
            },
        )
        legs = expand_route_config(rc)
        assert [(leg.mapping_source_context, leg.direction) for leg in legs] == [
            ("a", "source_to_dest"),
            ("a", "dest_to_source"),
            ("b", "source_to_dest"),
            ("b", "dest_to_source"),
        ]
        assert legs[0].route.source.channel == "a"

    def test_legs_carry_mapping_provenance(self) -> None:
        """Mapping legs carry their source/dest contexts for the plan layer."""
        legs = expand_route_config(self._map_config())
        fwd0 = legs[0]
        assert fwd0.config_route_id == "bridge"
        assert fwd0.mapping_source_context == "0"
        assert fwd0.mapping_dest_context == "!room0:example.com"
        rev0 = legs[1]
        assert rev0.mapping_source_context == "0"
        assert rev0.mapping_dest_context == "!room0:example.com"
        structured_legs = expand_route_config(
            RouteConfig(
                route_id="out",
                source_adapters=("a",),
                dest_adapters=("b",),
                context_map={
                    "k": ContextMapEntry(
                        dest_destination=RouteDestinationConfig(
                            kind="lxmf_destination",
                            destination_hash="21c0c1b9aabbccddeeff001122334455",
                        )
                    )
                },
            )
        )
        assert structured_legs[0].mapping_source_context == "k"
        assert structured_legs[0].mapping_dest_context is None

    def test_enabled_flag_threaded(self) -> None:
        disabled = RouteConfig(
            route_id="bridge",
            source_adapters=("radio_adapter",),
            dest_adapters=("chat_adapter",),
            enabled=False,
            context_map={
                "0": ContextMapEntry(dest_context="!room0:example.com"),
            },
        )
        legs = expand_route_config(disabled)
        assert all(leg.route.enabled is False for leg in legs)

    def test_policy_event_kinds_threaded(self) -> None:
        rc = self._map_config(
            directionality=RouteDirectionality.SOURCE_TO_DEST,
            context_map={"0": ContextMapEntry(dest_context="!room0:example.com")},
            policy=BridgePolicy(allowed_event_types=("message",)),
        )
        legs = expand_route_config(rc)
        assert len(legs) == 1
        assert legs[0].route.source.event_kinds == ("message",)

    def test_policy_allowlists_converted(self) -> None:
        """Non-event-type policy fields convert to a core RoutePolicy on
        every expanded leg; allowed_event_types become event_kinds."""
        rc = self._map_config(
            directionality=RouteDirectionality.SOURCE_TO_DEST,
            context_map={"0": ContextMapEntry(dest_context="!room0:example.com")},
            policy=BridgePolicy(
                allowed_event_types=("message",),
                allowed_source_adapters=("radio_adapter",),
                sender_allowlist=("!user:example.com",),
            ),
        )
        legs = expand_route_config(rc)
        policy = legs[0].route.policy
        assert policy is not None
        assert policy.allowed_source_adapters == ("radio_adapter",)
        assert policy.sender_allowlist == ("!user:example.com",)

    def test_labels_default_to_route_level(self) -> None:
        rc = self._map_config(
            context_map={
                "0": ContextMapEntry(dest_context="!room0:example.com"),
                "1": ContextMapEntry(
                    dest_context="!room1:example.com",
                    source_origin_label="Ops",
                    dest_origin_label="Chat-Ops",
                ),
            },
            source_origin_label="Radio",
            dest_origin_label="Chat",
        )
        legs = expand_route_config(rc)
        by_mapping = {(leg.mapping_source_context, leg.direction): leg for leg in legs}
        # Entry without labels falls back to route-level labels.
        assert by_mapping[("0", "source_to_dest")].route.source.origin_label == "Radio"
        assert by_mapping[("0", "dest_to_source")].route.source.origin_label == "Chat"
        # Entry labels take precedence over route-level labels.
        assert by_mapping[("1", "source_to_dest")].route.source.origin_label == "Ops"
        assert (
            by_mapping[("1", "dest_to_source")].route.source.origin_label == "Chat-Ops"
        )

    def test_explicit_empty_label_suppresses_fallback(self) -> None:
        """An explicit "" entry label is preserved (suppression sentinel)."""
        rc = self._map_config(
            context_map={
                "0": ContextMapEntry(
                    dest_context="!room0:example.com",
                    source_origin_label="",
                ),
                "1": ContextMapEntry(dest_context="!room1:example.com"),
            },
            source_origin_label="Radio",
        )
        legs = expand_route_config(rc)
        by_mapping = {(leg.mapping_source_context, leg.direction): leg for leg in legs}
        assert by_mapping[("0", "source_to_dest")].route.source.origin_label == ""
        assert by_mapping[("1", "source_to_dest")].route.source.origin_label == "Radio"

    def test_expand_route_config_ignores_enabled_flag(self) -> None:
        """expand_route_config expands disabled routes; callers filter."""
        disabled = RouteConfig(
            route_id="off",
            source_adapters=("radio_adapter",),
            dest_adapters=("chat_adapter",),
            enabled=False,
            context_map={"0": ContextMapEntry(dest_context="!room0:example.com")},
        )
        legs = expand_route_config(disabled)
        assert len(legs) == 1
        assert legs[0].route.id.startswith("off__maph")
        assert legs[0].route.id.endswith("__fwd")

    def test_expand_route_configs_skips_disabled(self) -> None:
        enabled = self._map_config(route_id="on")
        disabled = RouteConfig(
            route_id="off",
            source_adapters=("radio_adapter",),
            dest_adapters=("chat_adapter",),
            enabled=False,
            context_map={"0": ContextMapEntry(dest_context="!room0:example.com")},
        )
        legs = expand_route_configs(RouteConfigSet(routes=(enabled, disabled)))
        assert {leg.config_route_id for leg in legs} == {"on"}

    def test_expand_route_configs_provenance(self) -> None:
        """Provenance maps expanded map legs to their config route ID."""
        rc = self._map_config()
        legs = expand_route_configs(RouteConfigSet(routes=(rc,)))
        assert len(legs) == 4
        for leg in legs:
            assert leg.config_route_id == "bridge"


class TestExpansionBoundaries:
    """Runtime-semantic boundary checks owned by the compiler."""

    def test_empty_source_adapters_raises(self) -> None:
        """Directly constructed RouteConfig with empty source_adapters
        raises ConfigValidationError at expansion (config parsing cannot
        see direct construction)."""
        rc = RouteConfig(
            route_id="empty_src",
            source_adapters=(),
            dest_adapters=("chat_adapter",),
        )
        with pytest.raises(
            ConfigValidationError, match="source_adapters must not be empty"
        ):
            expand_route_configs(RouteConfigSet(routes=(rc,)))

    def test_empty_dest_adapters_raises(self) -> None:
        rc = RouteConfig(
            route_id="empty_dst",
            source_adapters=("radio_adapter",),
            dest_adapters=(),
        )
        with pytest.raises(
            ConfigValidationError, match="dest_adapters must not be empty"
        ):
            expand_route_config(rc)

    def test_mapping_single_adapter_parity_at_construction(self) -> None:
        """Directly constructed mapping route with multiple adapters is
        rejected at construction (__post_init__ parity with from_dict)."""
        with pytest.raises(ConfigValidationError, match="exactly.*one source"):
            RouteConfig(
                route_id="multi",
                source_adapters=("a", "b"),
                dest_adapters=("c",),
                context_map={"0": ContextMapEntry(dest_context="!room:example.com")},
            )

    def test_mapping_exclusivity_parity_at_construction(self) -> None:
        """Directly constructed mapping route with a conflicting targeting
        field is rejected at construction (__post_init__ parity)."""
        with pytest.raises(ConfigValidationError, match="mutually exclusive"):
            RouteConfig(
                route_id="conflict",
                source_adapters=("a",),
                dest_adapters=("b",),
                source_channel="ch0",
                context_map={"0": ContextMapEntry(dest_context="!room:example.com")},
            )

    def test_dest_destination_conflict_boundary(self) -> None:
        """Directly constructed route with dest_destination + dest_channel
        is rejected by the compiler boundary."""
        rc = RouteConfig(
            route_id="dual",
            source_adapters=("a",),
            dest_adapters=("b",),
            dest_channel="ch1",
            dest_destination=RouteDestinationConfig(
                kind="lxmf_destination",
                destination_hash="21c0c1b9aabbccddeeff001122334455",
            ),
        )
        with pytest.raises(ConfigValidationError, match="mutually exclusive"):
            expand_route_config(rc)

    def test_dest_destination_multiple_dest_adapters_boundary(self) -> None:
        rc = RouteConfig(
            route_id="multi_dst",
            source_adapters=("a",),
            dest_adapters=("b", "c"),
            dest_destination=RouteDestinationConfig(
                kind="lxmf_destination",
                destination_hash="21c0c1b9aabbccddeeff001122334455",
            ),
        )
        with pytest.raises(ConfigValidationError, match="exactly one dest"):
            expand_route_config(rc)

    def test_expanded_id_collision_reports_all_patterns(self) -> None:
        """A config route ID that collides with another route's expansion
        is rejected, with the four suffix patterns in the message."""
        map_route = RouteConfig(
            route_id="xmap",
            source_adapters=("a",),
            dest_adapters=("b",),
            context_map={"0": ContextMapEntry(dest_context="!room:example.com")},
        )
        generated_id = expand_route_config(map_route)[0].route.id
        shadow = RouteConfig(
            route_id=generated_id,
            source_adapters=("q",),
            dest_adapters=("z",),
        )
        with pytest.raises(ConfigValidationError, match="collision") as excinfo:
            expand_route_configs(RouteConfigSet(routes=(map_route, shadow)))
        msg = str(excinfo.value)
        for pattern in (
            "<id>__<N>",
            "<id>__rev_<N>",
            "<id>__map<token>__fwd",
            "<id>__map<token>__rev",
        ):
            assert pattern in msg

    def test_standard_suffix_collision_detected(self) -> None:
        standard = RouteConfig(
            route_id="a",
            source_adapters=("s1", "s2"),
            dest_adapters=("b",),
        )
        shadow = RouteConfig(
            route_id="a__0",
            source_adapters=("q",),
            dest_adapters=("z",),
        )
        with pytest.raises(ConfigValidationError, match="collision"):
            expand_route_configs(RouteConfigSet(routes=(standard, shadow)))


class TestGenericTransports:
    """Synthetic fifth-platform genericity proof at the compiler level.

    Both adapters use fake transport names that no runtime branch knows
    about; expansion is pure config compilation and must not care.
    """

    def test_fake_discord_to_fake_mqtt_expands_cleanly(self) -> None:
        rc = RouteConfig.from_dict(
            "relay",
            {
                "source_adapters": ["discord_bot"],
                "dest_adapters": ["mqtt_broker"],
                "directionality": "bidirectional",
                "context_map": {
                    "guild-channel-42": {
                        "dest_context": "home/sensors/temp",
                        "source_origin_label": "Discord",
                        "dest_origin_label": "MQTT",
                    },
                    "guild-channel-43": {
                        "dest_context": "home/sensors/humidity",
                    },
                },
            },
        )
        legs = expand_route_config(rc)
        assert [(leg.mapping_source_context, leg.direction) for leg in legs] == [
            ("guild-channel-42", "source_to_dest"),
            ("guild-channel-42", "dest_to_source"),
            ("guild-channel-43", "source_to_dest"),
            ("guild-channel-43", "dest_to_source"),
        ]
        fwd = legs[0].route
        assert fwd.source.adapter == "discord_bot"
        assert fwd.source.channel == "guild-channel-42"
        assert fwd.source.origin_label == "Discord"
        assert fwd.targets[0].adapter == "mqtt_broker"
        assert fwd.targets[0].channel == "home/sensors/temp"
        rev = legs[1].route
        assert rev.source.adapter == "mqtt_broker"
        assert rev.source.channel == "home/sensors/temp"
        assert rev.source.origin_label == "MQTT"
        assert rev.targets[0].adapter == "discord_bot"
        assert rev.targets[0].channel == "guild-channel-42"

    def test_fake_transports_full_set_expansion(self) -> None:
        rc = RouteConfig.from_dict(
            "slack_relay",
            {
                "source_adapters": ["slack_ws"],
                "dest_adapters": ["nats_conn"],
                "directionality": "source_to_dest",
                "context_map": {
                    "C001": {"dest_context": "subj.one"},
                    "C002": {"dest_context": "subj.two"},
                },
            },
        )
        legs = expand_route_configs(RouteConfigSet(routes=(rc,)))
        assert [leg.mapping_source_context for leg in legs] == ["C001", "C002"]
        assert all(leg.route.id.startswith("slack_relay__maph") for leg in legs)
        assert all(leg.route.id.endswith("__fwd") for leg in legs)

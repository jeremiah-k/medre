"""Duplicate-context fan-in behaviour for ``context_map`` routes.

Whether duplicate ``dest_context`` values are legal is decided by the
route's directionality, which is known at config validation time:

* Duplicate dest_context values are **safe** on forward-only
  (``source_to_dest``) routes — fan-in: each source context keeps its
  own forward leg, and the target side disambiguates by the inbound
  source context.
* Duplicate dest_context values are **ambiguous** when the
  directionality also creates reverse (dest→source) legs — they would
  become duplicate reverse-leg inbound source contexts with no way to
  pick one.  Config validation rejects this with
  :class:`~medre.config.errors.ConfigValidationError`.

This file exercises the full directionality matrix, the per-entry
origin-label behaviour on the allowed fan-in path, and the unchanged
"duplicate context keys are still rejected" guarantee at config parse
time.
"""

from __future__ import annotations

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.route_expansion import expand_route_config
from medre.config.routes import (
    ContextMapEntry,
    RouteConfig,
    RouteConfigSet,
    RouteDirectionality,
)

_SHARED_CONTEXT = "!shared:example.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _map_route(
    *,
    route_id: str = "fanin",
    directionality: RouteDirectionality,
    context_map: dict[str, ContextMapEntry],
    source_origin_label: str | None = None,
    dest_origin_label: str | None = None,
) -> RouteConfig:
    """A one-source/one-dest mapping route with configurable direction."""
    return RouteConfig(
        route_id=route_id,
        source_adapters=("radio_adapter",),
        dest_adapters=("chat_adapter",),
        directionality=directionality,
        context_map=context_map,
        source_origin_label=source_origin_label,
        dest_origin_label=dest_origin_label,
    )


def _shared_map(**entry_extras: str) -> dict[str, ContextMapEntry]:
    return {
        "0": ContextMapEntry(dest_context=_SHARED_CONTEXT, **entry_extras),
        "1": ContextMapEntry(dest_context=_SHARED_CONTEXT),
    }


# ===========================================================================
# 1. Allowed: forward-only fan-in with duplicate dest_context values
# ===========================================================================


def test_same_context_source_to_dest_allowed() -> None:
    """Two source contexts fanning into one dest context: allowed.

    ``source_to_dest`` produces only forward legs, so the duplicate
    target context is unambiguous (the inbound source context
    disambiguates the event).  Each entry expands into its own
    deterministic ``{route_id}__map<i>__fwd`` leg.
    """
    cmap = {
        "0": ContextMapEntry(dest_context=_SHARED_CONTEXT, source_origin_label="Ops"),
        "1": ContextMapEntry(
            dest_context=_SHARED_CONTEXT, source_origin_label="Tactical"
        ),
    }
    rc = _map_route(
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        context_map=cmap,
    )
    legs = expand_route_config(rc)

    # Two legs, one per entry, both targeting the shared context.
    assert len(legs) == 2
    by_id = {leg.route.id: leg.route for leg in legs}
    expected_ids = {"fanin__map0__fwd", "fanin__map1__fwd"}
    assert set(by_id) == expected_ids

    for key, label in (("0", "Ops"), ("1", "Tactical")):
        leg = by_id[f"fanin__map{int(key)}__fwd"]
        # Source side: radio adapter, context-scoped.
        assert leg.source.adapter == "radio_adapter"
        assert leg.source.channel == key
        assert leg.source.origin_label == label
        # Target side: chat adapter pointing at the shared context.
        assert len(leg.targets) == 1
        assert leg.targets[0].adapter == "chat_adapter"
        assert leg.targets[0].channel == _SHARED_CONTEXT


def test_per_context_origin_labels_distinct_for_shared_context() -> None:
    """Each entry's leg carries its OWN source_origin_label.

    Even though both legs target the same dest context, the per-entry
    labels must not bleed across entries — entry 0 keeps its label,
    entry 1 keeps its own.
    """
    cmap = {
        "0": ContextMapEntry(dest_context=_SHARED_CONTEXT, source_origin_label="Alpha"),
        "1": ContextMapEntry(dest_context=_SHARED_CONTEXT, source_origin_label="Beta"),
    }
    rc = _map_route(
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        context_map=cmap,
    )
    legs = expand_route_config(rc)

    by_context = {leg.route.source.channel: leg.route for leg in legs}
    assert by_context["0"].source.origin_label == "Alpha"
    assert by_context["1"].source.origin_label == "Beta"
    # Sanity: both target the shared context.
    assert all(leg.route.targets[0].channel == _SHARED_CONTEXT for leg in legs)


def test_fan_in_full_set_expansion() -> None:
    """RouteConfigSet-level expansion of a fan-in route keeps both legs."""
    from medre.config.route_expansion import expand_route_configs

    rc = _map_route(
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        context_map=_shared_map(),
    )
    legs = expand_route_configs(RouteConfigSet(routes=(rc,)))
    assert sorted(leg.route.id for leg in legs) == [
        "fanin__map0__fwd",
        "fanin__map1__fwd",
    ]


# ===========================================================================
# 2. Rejected: any route whose directionality creates reverse legs
# ===========================================================================


@pytest.mark.parametrize("direction", ["dest_to_source", "bidirectional"])
def test_duplicate_context_with_reverse_legs_rejected(direction: str) -> None:
    """Reverse legs turn duplicate dest_context values into ambiguous
    inbound source contexts — rejected at config validation."""
    with pytest.raises(ConfigValidationError) as exc_info:
        RouteConfig.from_dict(
            "fanin",
            {
                "source_adapters": ["radio_adapter"],
                "dest_adapters": ["chat_adapter"],
                "directionality": direction,
                "context_map": {
                    "0": {"dest_context": _SHARED_CONTEXT},
                    "1": {"dest_context": _SHARED_CONTEXT},
                },
            },
        )
    msg = str(exc_info.value)
    assert "duplicate" in msg
    assert "dest_context" in msg
    assert _SHARED_CONTEXT in msg


def test_duplicate_context_rejected_on_direct_construction() -> None:
    """Direct construction enforces the same invariant as from_dict."""
    with pytest.raises(ConfigValidationError, match="duplicate"):
        _map_route(
            directionality=RouteDirectionality.BIDIRECTIONAL,
            context_map=_shared_map(),
        )


def test_rejection_message_lists_duplicate_contexts() -> None:
    """The error lists the sorted duplicate dest_context values."""
    cmap = {
        "0": ContextMapEntry(dest_context="!aaa:example.com"),
        "1": ContextMapEntry(dest_context="!aaa:example.com"),
        "2": ContextMapEntry(dest_context="!bbb:example.com"),
        "3": ContextMapEntry(dest_context="!bbb:example.com"),
    }
    with pytest.raises(ConfigValidationError) as exc_info:
        _map_route(
            directionality=RouteDirectionality.DEST_TO_SOURCE,
            context_map=cmap,
        )
    msg = str(exc_info.value)
    # Sorted duplicate contexts appear in the message.
    assert "!aaa:example.com" in msg
    assert "!bbb:example.com" in msg
    assert "Route 'fanin'" in msg


# ===========================================================================
# 3. Config parsing: duplicate values allowed fwd-only, keys rejected
# ===========================================================================


def test_config_from_dict_allows_duplicate_contexts_forward_only() -> None:
    """``RouteConfig.from_dict`` accepts duplicate dest_context values on
    a forward-only route."""
    data = {
        "source_adapters": ["radio_adapter"],
        "dest_adapters": ["chat_adapter"],
        "directionality": "source_to_dest",
        "context_map": {
            "0": {"dest_context": _SHARED_CONTEXT},
            "1": {"dest_context": _SHARED_CONTEXT},
        },
    }
    rc = RouteConfig.from_dict("fanin_route", data)
    assert rc.context_map is not None
    assert set(rc.context_map.keys()) == {"0", "1"}
    # Both entries normalize to the same dest_context value.
    assert rc.context_map["0"].dest_context == _SHARED_CONTEXT
    assert rc.context_map["1"].dest_context == _SHARED_CONTEXT


def test_duplicate_context_keys_still_rejected() -> None:
    """Duplicate KEYS remain a config-parse error.

    Only duplicate dest_context *values* follow the fan-in rule;
    duplicate keys (e.g. string ``"1"`` and int ``1`` both normalising
    to ``"1"``) are still rejected at parse time.
    """
    data = {
        "source_adapters": ["radio_adapter"],
        "dest_adapters": ["chat_adapter"],
        "context_map": {
            "1": {"dest_context": "!room_one:example.com"},
            1: {"dest_context": "!room_one_dup:example.com"},
        },
    }
    with pytest.raises(ConfigValidationError, match="duplicate context key"):
        RouteConfig.from_dict("bad", data)


# ===========================================================================
# 4. Baseline: unique contexts still expand correctly
# ===========================================================================


def test_unique_contexts_baseline_expands() -> None:
    """A normal context_map with distinct dest contexts expands as before.

    Regression guard: the duplicate-context check must be a no-op when
    there are no duplicates, regardless of directionality.
    """
    cmap = {
        "0": ContextMapEntry(dest_context="!room0:example.com"),
        "1": ContextMapEntry(dest_context="!room1:example.com"),
    }
    rc = _map_route(
        route_id="fanout",
        directionality=RouteDirectionality.BIDIRECTIONAL,
        context_map=cmap,
    )
    legs = expand_route_config(rc)
    # 2 entries x bidirectional = 4 legs.
    assert len(legs) == 4
    ids = sorted(leg.route.id for leg in legs)
    assert ids == [
        "fanout__map0__fwd",
        "fanout__map0__rev",
        "fanout__map1__fwd",
        "fanout__map1__rev",
    ]


# ===========================================================================
# 5. Per-entry label precedence on the allowed fan-in path
#
# When duplicate dest contexts are allowed (forward-only fan-in),
# per-entry ``source_origin_label`` overrides the route-level label; an
# explicit empty string suppresses fallback; ``None`` falls back to
# route-level.
# ===========================================================================


def test_per_entry_label_overrides_route_level_on_fanin() -> None:
    """Per-entry source_origin_label wins over route-level on its leg."""
    cmap = {
        "0": ContextMapEntry(
            dest_context=_SHARED_CONTEXT, source_origin_label="Context 0 Label"
        ),
        "1": ContextMapEntry(dest_context=_SHARED_CONTEXT),  # no entry label
    }
    rc = _map_route(
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        context_map=cmap,
        source_origin_label="Route Default",
    )
    legs = expand_route_config(rc)
    by_context = {leg.route.source.channel: leg.route for leg in legs}
    # Context 0: explicit entry label wins.
    assert by_context["0"].source.origin_label == "Context 0 Label"
    # Context 1: entry label is None -> route-level fallback.
    assert by_context["1"].source.origin_label == "Route Default"


def test_explicit_empty_entry_label_suppresses_route_level_fallback() -> None:
    """An explicit ``""`` entry label is preserved (suppresses fallback).

    Per the ``source_origin_label`` semantics, ``None`` means "fall
    back", while an explicit empty string means "suppress the fallback
    for this entry".  The fan-in path must honour the same precedence
    rules as the unique-context path.
    """
    cmap = {
        "0": ContextMapEntry(dest_context=_SHARED_CONTEXT, source_origin_label=""),
        "1": ContextMapEntry(dest_context=_SHARED_CONTEXT),  # falls back
    }
    rc = _map_route(
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        context_map=cmap,
        source_origin_label="Route Default",
    )
    legs = expand_route_config(rc)
    by_context = {leg.route.source.channel: leg.route for leg in legs}
    # Context 0: explicit "" preserved, not replaced by the route label.
    assert by_context["0"].source.origin_label == ""
    # Context 1: None -> route-level label.
    assert by_context["1"].source.origin_label == "Route Default"


def test_no_labels_no_route_label_fanin_legs_are_none() -> None:
    """With no entry labels and no route label, fan-in legs have ``None``."""
    rc = _map_route(
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        context_map=_shared_map(),
    )
    legs = expand_route_config(rc)
    assert all(leg.route.source.origin_label is None for leg in legs)

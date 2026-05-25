"""Tests for route policy evaluator — pure, stateless access-control logic.

Covers:
* Allow-all (empty allowlists)
* Per-field permit/deny for each allowlist
* Channel target vs source fallback
* Safe allowed_summary truncation
* Frozen immutability of dataclasses
* Evaluation order (first denial wins)
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from medre.config.routes import BridgePolicy
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.policies.route_policy import (
    RouteDecision,
    RoutePolicy,
    evaluate_route_policy,
)
from medre.core.routing.models import RouteTarget


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(
    source_adapter: str = "adapter_a",
    source_transport_id: str = "sender-1",
    source_channel_id: str | None = "!room:server.local",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id="evt-policy-test",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id=source_transport_id,
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "test"},
        metadata=EventMetadata(),
    )


def _target(
    adapter: str | None = "adapter_b",
    channel: str | None = None,
) -> RouteTarget:
    return RouteTarget(adapter=adapter, channel=channel)


# ===================================================================
# Allow-all (empty allowlists)
# ===================================================================


class TestAllowAll:
    """Empty allowlists permit everything."""

    def test_default_policy_allows_everything(self) -> None:
        policy = RoutePolicy()
        decision = evaluate_route_policy(policy, _event(), _target())
        assert decision.allowed is True
        assert decision.reason is None
        assert decision.blocked_field is None
        assert decision.blocked_value is None

    def test_allow_all_with_specific_event(self) -> None:
        policy = RoutePolicy()
        event = _event(
            source_adapter="any_adapter",
            source_transport_id="any_sender",
            source_channel_id="!any:room.local",
        )
        target = _target(adapter="any_dest", channel="any_ch")
        decision = evaluate_route_policy(policy, event, target)
        assert decision.allowed is True


# ===================================================================
# Source adapter
# ===================================================================


class TestSourceAdapter:
    """allowed_source_adapters permits/denies based on event.source_adapter."""

    def test_permit_when_in_allowlist(self) -> None:
        policy = RoutePolicy(allowed_source_adapters=("adapter_a", "other"))
        decision = evaluate_route_policy(policy, _event(source_adapter="adapter_a"), _target())
        assert decision.allowed is True

    def test_deny_when_not_in_allowlist(self) -> None:
        policy = RoutePolicy(allowed_source_adapters=("other",))
        decision = evaluate_route_policy(policy, _event(source_adapter="adapter_a"), _target())
        assert decision.allowed is False
        assert decision.reason == "source_adapter_not_allowed"
        assert decision.blocked_field == "allowed_source_adapters"
        assert decision.blocked_value == "adapter_a"

    def test_empty_tuple_allows_any(self) -> None:
        policy = RoutePolicy(allowed_source_adapters=())
        decision = evaluate_route_policy(policy, _event(source_adapter="anything"), _target())
        assert decision.allowed is True


# ===================================================================
# Dest adapter
# ===================================================================


class TestDestAdapter:
    """allowed_dest_adapters permits/denies based on target.adapter."""

    def test_permit_when_in_allowlist(self) -> None:
        policy = RoutePolicy(allowed_dest_adapters=("adapter_b",))
        decision = evaluate_route_policy(policy, _event(), _target(adapter="adapter_b"))
        assert decision.allowed is True

    def test_deny_when_not_in_allowlist(self) -> None:
        policy = RoutePolicy(allowed_dest_adapters=("other_dest",))
        decision = evaluate_route_policy(policy, _event(), _target(adapter="adapter_b"))
        assert decision.allowed is False
        assert decision.reason == "dest_adapter_not_allowed"
        assert decision.blocked_field == "allowed_dest_adapters"
        assert decision.blocked_value == "adapter_b"

    def test_none_dest_adapter_not_blocked(self) -> None:
        """When target.adapter is None, dest adapter check is skipped."""
        policy = RoutePolicy(allowed_dest_adapters=("only_this",))
        decision = evaluate_route_policy(policy, _event(), _target(adapter=None))
        assert decision.allowed is True


# ===================================================================
# Sender
# ===================================================================


class TestSender:
    """sender_allowlist permits/denies based on event.source_transport_id."""

    def test_permit_when_in_allowlist(self) -> None:
        policy = RoutePolicy(sender_allowlist=("sender-1",))
        decision = evaluate_route_policy(policy, _event(source_transport_id="sender-1"), _target())
        assert decision.allowed is True

    def test_deny_when_not_in_allowlist(self) -> None:
        policy = RoutePolicy(sender_allowlist=("allowed_sender",))
        decision = evaluate_route_policy(
            policy, _event(source_transport_id="blocked_sender"), _target()
        )
        assert decision.allowed is False
        assert decision.reason == "sender_not_allowed"
        assert decision.blocked_field == "sender_allowlist"
        assert decision.blocked_value == "blocked_sender"

    def test_empty_tuple_allows_any_sender(self) -> None:
        policy = RoutePolicy(sender_allowlist=())
        decision = evaluate_route_policy(
            policy, _event(source_transport_id="anyone"), _target()
        )
        assert decision.allowed is True


# ===================================================================
# Room
# ===================================================================


class TestRoom:
    """room_allowlist checks event.source_channel_id for room-like identifiers."""

    def test_permit_when_room_in_allowlist(self) -> None:
        policy = RoutePolicy(room_allowlist=("!room:server.local",))
        decision = evaluate_route_policy(
            policy, _event(source_channel_id="!room:server.local"), _target()
        )
        assert decision.allowed is True

    def test_deny_when_room_not_in_allowlist(self) -> None:
        policy = RoutePolicy(room_allowlist=("!allowed:server.local",))
        decision = evaluate_route_policy(
            policy, _event(source_channel_id="!blocked:server.local"), _target()
        )
        assert decision.allowed is False
        assert decision.reason == "room_not_allowed"
        assert decision.blocked_field == "room_allowlist"
        assert decision.blocked_value == "!blocked:server.local"

    def test_no_room_id_not_blocked(self) -> None:
        """When source_channel_id is None, room check is skipped."""
        policy = RoutePolicy(room_allowlist=("!only:room",))
        decision = evaluate_route_policy(
            policy, _event(source_channel_id=None), _target()
        )
        assert decision.allowed is True

    def test_empty_tuple_allows_any_room(self) -> None:
        policy = RoutePolicy(room_allowlist=())
        decision = evaluate_route_policy(
            policy, _event(source_channel_id="!any:room"), _target()
        )
        assert decision.allowed is True


# ===================================================================
# Channel
# ===================================================================


class TestChannel:
    """channel_allowlist checks target.channel, falling back to source_channel_id."""

    def test_permit_target_channel_in_allowlist(self) -> None:
        policy = RoutePolicy(channel_allowlist=("ch-1",))
        decision = evaluate_route_policy(
            policy, _event(), _target(channel="ch-1")
        )
        assert decision.allowed is True

    def test_deny_target_channel_not_in_allowlist(self) -> None:
        policy = RoutePolicy(channel_allowlist=("ch-allowed",))
        decision = evaluate_route_policy(
            policy, _event(), _target(channel="ch-blocked")
        )
        assert decision.allowed is False
        assert decision.reason == "channel_not_allowed"
        assert decision.blocked_field == "channel_allowlist"
        assert decision.blocked_value == "ch-blocked"

    def test_fallback_to_source_channel_when_target_none(self) -> None:
        """When target.channel is None, source_channel_id is used."""
        policy = RoutePolicy(channel_allowlist=("src-ch",))
        decision = evaluate_route_policy(
            policy, _event(source_channel_id="src-ch"), _target(channel=None)
        )
        assert decision.allowed is True

    def test_fallback_deny_source_channel(self) -> None:
        """Fallback source channel is also subject to allowlist."""
        policy = RoutePolicy(channel_allowlist=("allowed-ch",))
        decision = evaluate_route_policy(
            policy,
            _event(source_channel_id="wrong-ch"),
            _target(channel=None),
        )
        assert decision.allowed is False
        assert decision.reason == "channel_not_allowed"
        assert decision.blocked_value == "wrong-ch"

    def test_no_channel_at_all_not_blocked(self) -> None:
        """When both target.channel and source_channel_id are None, skip."""
        policy = RoutePolicy(channel_allowlist=("ch-1",))
        decision = evaluate_route_policy(
            policy, _event(source_channel_id=None), _target(channel=None)
        )
        assert decision.allowed is True

    def test_empty_tuple_allows_any_channel(self) -> None:
        policy = RoutePolicy(channel_allowlist=())
        decision = evaluate_route_policy(
            policy, _event(source_channel_id="any"), _target(channel="any")
        )
        assert decision.allowed is True

    def test_target_channel_takes_priority_over_source(self) -> None:
        """When target has a channel, source_channel_id is NOT used for channel check."""
        policy = RoutePolicy(channel_allowlist=("target-ch",))
        decision = evaluate_route_policy(
            policy,
            _event(source_channel_id="wrong-src-ch"),
            _target(channel="target-ch"),
        )
        assert decision.allowed is True


# ===================================================================
# Evaluation order
# ===================================================================


class TestEvaluationOrder:
    """First denial wins; order is source→dest→sender→room→channel."""

    def test_source_adapter_checked_before_dest(self) -> None:
        policy = RoutePolicy(
            allowed_source_adapters=("only_src",),
            allowed_dest_adapters=("only_dest",),
        )
        decision = evaluate_route_policy(
            policy, _event(source_adapter="wrong_src"), _target(adapter="wrong_dest")
        )
        assert decision.reason == "source_adapter_not_allowed"

    def test_sender_checked_before_room(self) -> None:
        policy = RoutePolicy(
            sender_allowlist=("good_sender",),
            room_allowlist=("!good:room",),
        )
        decision = evaluate_route_policy(
            policy,
            _event(source_transport_id="bad_sender", source_channel_id="!bad:room"),
            _target(),
        )
        assert decision.reason == "sender_not_allowed"

    def test_room_checked_before_channel(self) -> None:
        policy = RoutePolicy(
            room_allowlist=("!good:room",),
            channel_allowlist=("good_ch",),
        )
        decision = evaluate_route_policy(
            policy,
            _event(source_channel_id="!bad:room"),
            _target(channel="bad_ch"),
        )
        assert decision.reason == "room_not_allowed"


# ===================================================================
# Allowed summary safety
# ===================================================================


class TestAllowedSummary:
    """allowed_summary is safe for logging — no huge list dumps."""

    def test_deny_summary_contains_reason(self) -> None:
        policy = RoutePolicy(allowed_source_adapters=("ok",))
        decision = evaluate_route_policy(policy, _event(source_adapter="bad"), _target())
        assert "source_adapter_not_allowed" in decision.allowed_summary
        assert "bad" in decision.allowed_summary

    def test_allow_summary_with_empty_lists(self) -> None:
        policy = RoutePolicy()
        decision = evaluate_route_policy(policy, _event(), _target())
        assert "any" in decision.allowed_summary

    def test_allow_summary_truncates_large_lists(self) -> None:
        big = tuple(f"item-{i}" for i in range(20))
        policy = RoutePolicy(allowed_source_adapters=big)
        decision = evaluate_route_policy(
            policy, _event(source_adapter="item-0"), _target()
        )
        assert "20 total" in decision.allowed_summary
        # Should NOT contain all 20 items verbatim
        assert "item-19" not in decision.allowed_summary


# ===================================================================
# Immutability
# ===================================================================


class TestImmutability:
    """Frozen dataclasses reject mutation."""

    def test_route_policy_frozen(self) -> None:
        policy = RoutePolicy()
        with pytest.raises(FrozenInstanceError):
            policy.allowed_source_adapters = ("x",)  # type: ignore[misc]

    def test_route_decision_frozen(self) -> None:
        decision = RouteDecision(
            allowed=True,
            reason=None,
            blocked_field=None,
            blocked_value=None,
            allowed_summary="ok",
        )
        with pytest.raises(FrozenInstanceError):
            decision.allowed = False  # type: ignore[misc]


# ===================================================================
# BridgePolicy → RoutePolicy conversion (_convert_bridge_policy)
# ===================================================================


class TestConvertBridgePolicy:
    """Direct unit tests for runtime route-policy conversion.

    Covers the five fields carried from BridgePolicy into RoutePolicy
    (allowed_source_adapters, allowed_dest_adapters, sender_allowlist,
    room_allowlist, channel_allowlist) and the intentional exclusion
    of allowed_event_types (enforced structurally via RouteSource.event_kinds).
    """

    @staticmethod
    def _convert(bp: BridgePolicy) -> RoutePolicy | None:
        """Import and call the private converter under test."""
        from medre.runtime.route_engine import _convert_bridge_policy

        return _convert_bridge_policy(bp)

    def test_all_empty_returns_none(self) -> None:
        """Default (all-empty) BridgePolicy produces no RoutePolicy."""
        bp = BridgePolicy()
        assert self._convert(bp) is None

    def test_only_event_types_returns_none(self) -> None:
        """BridgePolicy with only allowed_event_types still returns None.

        allowed_event_types is excluded from RoutePolicy conversion — it
        is enforced structurally via RouteSource.event_kinds at expansion
        time.
        """
        bp = BridgePolicy(allowed_event_types=("message",))
        assert self._convert(bp) is None

    def test_source_adapters_preserved(self) -> None:
        bp = BridgePolicy(allowed_source_adapters=("src_a", "src_b"))
        rp = self._convert(bp)
        assert rp is not None
        assert rp.allowed_source_adapters == ("src_a", "src_b")
        assert rp.allowed_dest_adapters == ()
        assert rp.sender_allowlist == ()
        assert rp.room_allowlist == ()
        assert rp.channel_allowlist == ()

    def test_dest_adapters_preserved(self) -> None:
        bp = BridgePolicy(allowed_dest_adapters=("dst_a",))
        rp = self._convert(bp)
        assert rp is not None
        assert rp.allowed_dest_adapters == ("dst_a",)
        assert rp.allowed_source_adapters == ()

    def test_sender_allowlist_preserved(self) -> None:
        bp = BridgePolicy(sender_allowlist=("alice", "bob"))
        rp = self._convert(bp)
        assert rp is not None
        assert rp.sender_allowlist == ("alice", "bob")

    def test_room_allowlist_preserved(self) -> None:
        bp = BridgePolicy(room_allowlist=("!room:server",))
        rp = self._convert(bp)
        assert rp is not None
        assert rp.room_allowlist == ("!room:server",)

    def test_channel_allowlist_preserved(self) -> None:
        bp = BridgePolicy(channel_allowlist=("ch0", "ch1"))
        rp = self._convert(bp)
        assert rp is not None
        assert rp.channel_allowlist == ("ch0", "ch1")

    def test_all_five_fields_preserved(self) -> None:
        """All five converted fields are carried into RoutePolicy."""
        bp = BridgePolicy(
            allowed_source_adapters=("src",),
            allowed_dest_adapters=("dst",),
            sender_allowlist=("sender",),
            room_allowlist=("!room:s",),
            channel_allowlist=("ch",),
            # allowed_event_types is intentionally excluded
            allowed_event_types=("message", "reaction"),
        )
        rp = self._convert(bp)
        assert rp is not None
        assert rp.allowed_source_adapters == ("src",)
        assert rp.allowed_dest_adapters == ("dst",)
        assert rp.sender_allowlist == ("sender",)
        assert rp.room_allowlist == ("!room:s",)
        assert rp.channel_allowlist == ("ch",)
        # RoutePolicy does not have allowed_event_types at all
        assert not hasattr(rp, "allowed_event_types")

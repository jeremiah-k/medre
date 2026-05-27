"""Parametric tests for FallbackResolver capability-driven strategy resolution.

Validates that :class:`FallbackResolver` correctly interprets
:class:`AdapterCapabilities` for every event-kind × capability combination,
producing the expected delivery method (``"direct"`` or ``"skip"``).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.events.canonical import CanonicalEvent, EventRelation, NativeRef
from medre.core.events.metadata import EventMetadata
from medre.core.planning.delivery_plan import DeliveryPlan
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.routing.models import RouteTarget

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEQ = 0


def _next_id() -> str:
    global _SEQ
    _SEQ += 1
    return f"evt-{_SEQ:04d}"


def make_event(
    event_kind: str = "message.text",
    relations: tuple[EventRelation, ...] = (),
) -> CanonicalEvent:
    """Build a minimal :class:`CanonicalEvent` for testing."""
    return CanonicalEvent(
        event_id=_next_id(),
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="test_source",
        source_transport_id="transport-0",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload={"text": "hello"},
        metadata=EventMetadata(),
    )


_REPLY_RELATION = EventRelation(
    relation_type="reply",
    target_event_id="evt-parent",
    target_native_ref=NativeRef(
        adapter="test_adapter",
        native_channel_id="ch-0",
        native_message_id="native-001",
    ),
    key=None,
    fallback_text="original",
)


_DEFAULT_TARGET = RouteTarget(adapter="test_target", channel="ch-out")


# ---------------------------------------------------------------------------
# TestFallbackResolverEventKindSupport
# ---------------------------------------------------------------------------


class TestFallbackResolverEventKindSupport:
    """Parameterized tests for ``_resolve_strategy`` across all event kinds."""

    @pytest.mark.parametrize(
        "event_kind,cap_field,cap_value,expected_method",
        [
            # -- Message text -------------------------------------------------
            ("message.text", "text", True, "direct"),
            ("message.text", "text", False, "skip"),
            ("message.created", "text", True, "direct"),
            ("message.created", "text", False, "skip"),
            # -- Reactions (3-level string: native/fallback → direct, unsupported → direct fallback) ---
            ("message.reacted", "reactions", "native", "direct"),
            ("message.reacted", "reactions", "unsupported", "direct"),
            ("message.reacted", "reactions", "fallback", "direct"),
            # -- Edits --------------------------------------------------------
            ("message.edited", "edits", "native", "direct"),
            ("message.edited", "edits", "unsupported", "direct"),
            # -- Deletes ------------------------------------------------------
            ("message.deleted", "deletes", "native", "direct"),
            ("message.deleted", "deletes", "unsupported", "direct"),
            # -- File / attachments -------------------------------------------
            ("message.file", "attachments", True, "direct"),
            ("message.file", "attachments", False, "skip"),
            # -- Presence -----------------------------------------------------
            ("presence.changed", "presence", True, "direct"),
            ("presence.changed", "presence", False, "skip"),
            # -- Telemetry ----------------------------------------------------
            ("telemetry.received", "metadata_fields", True, "direct"),
            ("telemetry.received", "metadata_fields", False, "skip"),
            # -- Unknown / passthrough ----------------------------------------
            ("plugin.custom", "text", False, "direct"),
            ("identity.updated", "text", False, "direct"),
            ("system.audit", "presence", False, "direct"),
        ],
        ids=lambda v: str(v),
    )
    def test_resolve_strategy_returns_expected_method(
        self,
        event_kind: str,
        cap_field: str,
        cap_value: object,
        expected_method: str,
    ) -> None:
        caps = AdapterCapabilities(**{cap_field: cap_value})
        resolver = FallbackResolver()
        event = make_event(event_kind=event_kind)
        strategy = resolver._resolve_strategy(event, caps)
        assert strategy.method == expected_method, (
            f"event_kind={event_kind!r} caps.{cap_field}={cap_value!r} "
            f"→ expected method={expected_method!r}, got {strategy.method!r}"
        )


# ---------------------------------------------------------------------------
# TestFallbackResolverReplyRelation
# ---------------------------------------------------------------------------


class TestFallbackResolverReplyRelation:
    """Verify reply-capability suppression only fires when relations are present."""

    def test_text_with_reply_native_replies_direct(self) -> None:
        """Text event with reply relation and native replies → direct."""
        caps = AdapterCapabilities(replies="native")
        resolver = FallbackResolver()
        event = make_event(event_kind="message.text", relations=(_REPLY_RELATION,))
        strategy = resolver._resolve_strategy(event, caps)
        assert strategy.method == "direct"

    def test_text_with_reply_unsupported_replies_skip(self) -> None:
        """Text event with reply relation and unsupported replies → skip."""
        caps = AdapterCapabilities(replies="unsupported")
        resolver = FallbackResolver()
        event = make_event(event_kind="message.text", relations=(_REPLY_RELATION,))
        strategy = resolver._resolve_strategy(event, caps)
        assert strategy.method == "skip"

    def test_text_without_reply_unsupported_replies_direct(self) -> None:
        """Text event with no relations and unsupported replies → direct (text works fine)."""
        caps = AdapterCapabilities(replies="unsupported")
        resolver = FallbackResolver()
        event = make_event(event_kind="message.text")
        strategy = resolver._resolve_strategy(event, caps)
        assert strategy.method == "direct"

    def test_text_with_non_reply_relation_unsupported_replies_direct(self) -> None:
        """Event with a non-reply relation and unsupported replies → direct."""
        reaction_rel = EventRelation(
            relation_type="reaction",
            target_event_id="evt-parent",
            target_native_ref=None,
            key="👍",
            fallback_text=None,
        )
        caps = AdapterCapabilities(replies="unsupported")
        resolver = FallbackResolver()
        event = make_event(event_kind="message.text", relations=(reaction_rel,))
        strategy = resolver._resolve_strategy(event, caps)
        assert strategy.method == "direct"


# ---------------------------------------------------------------------------
# TestFallbackResolverDeliveryPlan
# ---------------------------------------------------------------------------


class TestFallbackResolverDeliveryPlan:
    """Verify ``resolve_fallback`` returns a complete DeliveryPlan."""

    def test_returns_delivery_plan_instance(self) -> None:
        resolver = FallbackResolver()
        event = make_event(event_kind="message.text")
        caps = AdapterCapabilities()
        plan = resolver.resolve_fallback(event, _DEFAULT_TARGET, caps)
        assert isinstance(plan, DeliveryPlan)

    def test_plan_carries_event_id(self) -> None:
        resolver = FallbackResolver()
        event = make_event(event_kind="message.text")
        caps = AdapterCapabilities()
        plan = resolver.resolve_fallback(event, _DEFAULT_TARGET, caps)
        assert plan.event_id == event.event_id

    def test_plan_carries_target(self) -> None:
        resolver = FallbackResolver()
        event = make_event(event_kind="message.text")
        caps = AdapterCapabilities()
        plan = resolver.resolve_fallback(event, _DEFAULT_TARGET, caps)
        assert plan.target is _DEFAULT_TARGET

    def test_plan_primary_strategy_matches_resolve_strategy(self) -> None:
        resolver = FallbackResolver()
        event = make_event(event_kind="message.file")
        caps = AdapterCapabilities(attachments=False)
        plan = resolver.resolve_fallback(event, _DEFAULT_TARGET, caps)
        assert plan.primary_strategy.method == "skip"

    def test_plan_id_contains_event_id(self) -> None:
        resolver = FallbackResolver()
        event = make_event(event_kind="message.text")
        caps = AdapterCapabilities()
        plan = resolver.resolve_fallback(event, _DEFAULT_TARGET, caps)
        assert event.event_id in plan.plan_id

    @pytest.mark.parametrize(
        "event_kind,caps_kwargs,expected_method",
        [
            ("message.text", {"text": True}, "direct"),
            ("message.text", {"text": False}, "skip"),
            ("message.file", {"attachments": True}, "direct"),
            ("message.file", {"attachments": False}, "skip"),
            ("presence.changed", {"presence": True}, "direct"),
            ("presence.changed", {"presence": False}, "skip"),
            ("telemetry.received", {"metadata_fields": True}, "direct"),
            ("telemetry.received", {"metadata_fields": False}, "skip"),
        ],
        ids=lambda v: str(v),
    )
    def test_plan_strategy_for_various_combinations(
        self,
        event_kind: str,
        caps_kwargs: dict,
        expected_method: str,
    ) -> None:
        resolver = FallbackResolver()
        event = make_event(event_kind=event_kind)
        caps = AdapterCapabilities(**caps_kwargs)
        plan = resolver.resolve_fallback(event, _DEFAULT_TARGET, caps)
        assert plan.primary_strategy.method == expected_method


# ---------------------------------------------------------------------------
# TestFallbackResolverCapabilitySuppressionReceipt
# ---------------------------------------------------------------------------


class TestFallbackResolverCapabilitySuppressionReceipt:
    """Verify strategy method reflects capability suppression for downstream use.

    The pipeline uses ``strategy.method == "skip"`` to produce a
    CAPABILITY_SUPPRESSED receipt.  These tests confirm the resolver
    produces the correct "skip" method that would trigger that path.
    """

    @pytest.mark.parametrize(
        "event_kind,caps_kwargs",
        [
            ("message.text", {"text": False}),
            ("message.created", {"text": False}),
            ("message.file", {"attachments": False}),
            ("presence.changed", {"presence": False}),
            ("telemetry.received", {"metadata_fields": False}),
        ],
        ids=lambda v: str(v),
    )
    def test_skip_strategy_for_unsupported_capabilities(
        self,
        event_kind: str,
        caps_kwargs: dict,
    ) -> None:
        """Unsupported capability produces skip strategy."""
        resolver = FallbackResolver()
        caps = AdapterCapabilities(**caps_kwargs)
        event = make_event(event_kind=event_kind)
        strategy = resolver._resolve_strategy(event, caps)
        assert strategy.method == "skip"

    def test_capability_suppressed_receipt_scenario_text(self) -> None:
        """End-to-end: text=False produces a plan with skip method."""
        resolver = FallbackResolver()
        event = make_event(event_kind="message.text")
        caps = AdapterCapabilities(text=False)
        plan = resolver.resolve_fallback(event, _DEFAULT_TARGET, caps)
        assert plan.primary_strategy.method == "skip"

    def test_capability_suppressed_receipt_scenario_telemetry(self) -> None:
        """End-to-end: metadata_fields=False produces skip for telemetry."""
        resolver = FallbackResolver()
        event = make_event(event_kind="telemetry.received")
        caps = AdapterCapabilities(metadata_fields=False)
        plan = resolver.resolve_fallback(event, _DEFAULT_TARGET, caps)
        assert plan.primary_strategy.method == "skip"

    def test_capability_suppressed_receipt_scenario_reply(self) -> None:
        """End-to-end: replies=unsupported with reply relation → skip."""
        resolver = FallbackResolver()
        event = make_event(
            event_kind="message.text",
            relations=(_REPLY_RELATION,),
        )
        caps = AdapterCapabilities(replies="unsupported")
        plan = resolver.resolve_fallback(event, _DEFAULT_TARGET, caps)
        assert plan.primary_strategy.method == "skip"

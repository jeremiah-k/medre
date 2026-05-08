"""Tests for delivery planning: DeliveryPlan, DeliveryStrategy, RetryPolicy,
FallbackResolver, and RelationResolver.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from meshnet_framework.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeRef,
)
from meshnet_framework.core.planning import (
    DeliveryPlan,
    DeliveryStrategy,
    FallbackResolver,
    RelationResolver,
    RetryPolicy,
)
from meshnet_framework.core.routing import RouteTarget


def _make_event(
    event_kind: str = "message.created",
    event_id: str = "evt-1",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="fake_transport",
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=[],
        relations=[],
        payload={"text": "hello"},
        metadata=EventMetadata(),
    )


# ===================================================================
# DeliveryPlan / DeliveryStrategy / RetryPolicy
# ===================================================================


class TestDeliveryPlan:
    """DeliveryPlan construction, fallback chain, and retry policy."""

    def test_construction_with_primary_strategy(self) -> None:
        target = RouteTarget(adapter="fake_presentation")
        strategy = DeliveryStrategy(method="direct")
        plan = DeliveryPlan(
            plan_id="plan-1",
            event_id="evt-1",
            target=target,
            primary_strategy=strategy,
        )
        assert plan.plan_id == "plan-1"
        assert plan.primary_strategy.method == "direct"
        assert plan.fallback_chain == []
        assert plan.retry_policy is None
        assert plan.deadline is None

    def test_fallback_chain(self) -> None:
        """Fallback strategies are stored in order."""
        primary = DeliveryStrategy(method="direct")
        fb1 = DeliveryStrategy(method="propagated")
        fb2 = DeliveryStrategy(method="opportunistic")
        plan = DeliveryPlan(
            plan_id="plan-2",
            event_id="evt-2",
            target=RouteTarget(adapter="t"),
            primary_strategy=primary,
            fallback_chain=[fb1, fb2],
        )
        assert plan.fallback_chain == [fb1, fb2]
        assert plan.fallback_chain[0].method == "propagated"

    def test_retry_policy_defaults(self) -> None:
        policy = RetryPolicy()
        assert policy.max_attempts == 5
        assert policy.backoff_base == 2.0
        assert policy.max_delay_seconds == 60.0
        assert policy.jitter is True

    def test_delivery_strategy_custom_params(self) -> None:
        strategy = DeliveryStrategy(method="direct", max_retries=10, timeout_seconds=60.0)
        assert strategy.max_retries == 10
        assert strategy.timeout_seconds == 60.0

    def test_plan_with_deadline(self) -> None:
        deadline = datetime(2026, 1, 1, tzinfo=timezone.utc)
        plan = DeliveryPlan(
            plan_id="plan-3",
            event_id="evt-3",
            target=RouteTarget(adapter="t"),
            primary_strategy=DeliveryStrategy(method="direct"),
            deadline=deadline,
            retry_policy=RetryPolicy(max_attempts=3),
        )
        assert plan.deadline is deadline
        assert plan.retry_policy is not None
        assert plan.retry_policy.max_attempts == 3


# ===================================================================
# FallbackResolver
# ===================================================================


class TestFallbackResolver:
    """FallbackResolver downgrades strategies based on adapter capabilities."""

    def test_message_created_uses_direct(self) -> None:
        event = _make_event(event_kind="message.created")
        resolver = FallbackResolver()
        target = RouteTarget(adapter="fake_presentation")
        plan = resolver.resolve_fallback(event, target, {})
        assert plan.primary_strategy.method == "direct"

    def test_reaction_downgrades_when_not_supported(self) -> None:
        event = _make_event(event_kind="message.reacted")
        resolver = FallbackResolver()
        target = RouteTarget(adapter="target")
        caps = {"supports_reactions": False}
        plan = resolver.resolve_fallback(event, target, caps)
        assert plan.primary_strategy.method == "direct"

    def test_reaction_keeps_direct_when_supported(self) -> None:
        event = _make_event(event_kind="message.reacted")
        resolver = FallbackResolver()
        target = RouteTarget(adapter="target")
        caps = {"supports_reactions": True}
        plan = resolver.resolve_fallback(event, target, caps)
        assert plan.primary_strategy.method == "direct"

    def test_edit_downgrades_when_not_supported(self) -> None:
        event = _make_event(event_kind="message.edited")
        resolver = FallbackResolver()
        target = RouteTarget(adapter="target")
        caps = {"supports_edits": False}
        plan = resolver.resolve_fallback(event, target, caps)
        assert plan.primary_strategy.method == "direct"

    def test_delete_downgrades_when_not_supported(self) -> None:
        event = _make_event(event_kind="message.deleted")
        resolver = FallbackResolver()
        target = RouteTarget(adapter="target")
        caps = {"supports_deletes": False}
        plan = resolver.resolve_fallback(event, target, caps)
        assert plan.primary_strategy.method == "direct"

    def test_plan_event_id_matches_source(self) -> None:
        event = _make_event(event_kind="message.created", event_id="evt-x")
        resolver = FallbackResolver()
        target = RouteTarget(adapter="target")
        plan = resolver.resolve_fallback(event, target, {})
        assert plan.event_id == "evt-x"

    def test_plan_target_matches_input(self) -> None:
        event = _make_event()
        resolver = FallbackResolver()
        target = RouteTarget(adapter="my_target", channel="ch-1")
        plan = resolver.resolve_fallback(event, target, {})
        assert plan.target is target


# ===================================================================
# RelationResolver
# ===================================================================


class TestRelationResolver:
    """RelationResolver resolves native refs to canonical event IDs."""

    async def test_resolve_relation_already_resolved(self) -> None:
        """Relation with target_event_id already set is returned unchanged."""
        resolver = RelationResolver(storage=object())
        relation = EventRelation(
            relation_type="reply",
            target_event_id="known-evt",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        result = await resolver.resolve_relation(relation)
        assert result.target_event_id == "known-evt"

    async def test_resolve_relation_raises_without_ref_or_id(self) -> None:
        """Relation with neither target_event_id nor target_native_ref raises ValueError."""
        resolver = RelationResolver(storage=object())
        relation = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        with pytest.raises(ValueError, match="target_event_id"):
            await resolver.resolve_relation(relation)

    async def test_resolve_relation_with_native_ref_via_dummy_storage(self) -> None:
        """resolve_relation looks up native ref via storage.resolve_native_ref."""

        class _DummyStorage:
            async def resolve_native_ref(self, ref: NativeRef) -> CanonicalEvent | None:
                if ref.native_message_id == "msg-1":
                    return _make_event(event_id="resolved-evt")
                return None

        resolver = RelationResolver(storage=_DummyStorage())
        nref = NativeRef(
            adapter="fake_transport",
            native_channel_id="ch-0",
            native_message_id="msg-1",
        )
        relation = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=nref,
            key=None,
            fallback_text=None,
        )
        result = await resolver.resolve_relation(relation)
        assert result.target_event_id == "resolved-evt"

    async def test_resolve_relation_returns_original_when_not_found(self) -> None:
        """If the native ref has no mapping, original relation is returned."""

        class _EmptyStorage:
            async def resolve_native_ref(self, ref: NativeRef) -> None:
                return None

        resolver = RelationResolver(storage=_EmptyStorage())
        nref = NativeRef(
            adapter="a", native_channel_id="c", native_message_id="nope"
        )
        relation = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=nref,
            key=None,
            fallback_text=None,
        )
        result = await resolver.resolve_relation(relation)
        assert result.target_event_id is None

    async def test_create_relation_event(self) -> None:
        """create_relation_event builds a new canonical event with a relation."""
        resolver = RelationResolver(storage=object())
        source = _make_event(event_id="src-1")
        target_nref = NativeRef(
            adapter="fake_presentation",
            native_channel_id="ch-0",
            native_message_id="msg-99",
        )
        new_event = await resolver.create_relation_event(
            source_event=source,
            relation_type="reply",
            target_native_ref=target_nref,
            key=None,
        )
        assert new_event.parent_event_id == "src-1"
        assert "src-1" in new_event.lineage
        assert len(new_event.relations) == 1
        assert new_event.relations[0].relation_type == "reply"
        assert new_event.relations[0].target_native_ref == target_nref
        assert new_event.depth == source.depth + 1

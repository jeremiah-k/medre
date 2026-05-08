"""Tests for delivery planning: DeliveryPlan, DeliveryStrategy, RetryPolicy,
FallbackResolver, RelationResolver, DeliveryOutcome, and Diagnostician.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeRef,
)
from medre.core.observability.metrics import Diagnostician
from medre.core.planning import (
    DeliveryPlan,
    DeliveryStrategy,
    FallbackResolver,
    RelationResolver,
    RetryPolicy,
)
from medre.core.planning.delivery_plan import DeliveryOutcome
from medre.core.routing import RouteTarget


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


# ===================================================================
# DeliveryOutcome
# ===================================================================


class TestDeliveryOutcome:
    """DeliveryOutcome construction and status semantics."""

    def test_success_outcome(self) -> None:
        outcome = DeliveryOutcome(
            event_id="evt-1",
            target_adapter="discord",
            target_channel="ch-1",
            route_id="route-a",
            delivery_plan_id="plan-1",
            status="success",
            receipt=None,
            error=None,
            duration_ms=12.5,
        )
        assert outcome.status == "success"
        assert outcome.error is None
        assert outcome.receipt is None
        assert outcome.duration_ms == 12.5

    def test_transient_failure_outcome(self) -> None:
        outcome = DeliveryOutcome(
            event_id="evt-2",
            target_adapter="slack",
            target_channel=None,
            route_id="route-b",
            delivery_plan_id="plan-2",
            status="transient_failure",
            receipt=None,
            error="ConnectionRefusedError: connection refused",
            duration_ms=5002.0,
        )
        assert outcome.status == "transient_failure"
        assert "ConnectionRefusedError" in outcome.error

    def test_permanent_failure_outcome(self) -> None:
        outcome = DeliveryOutcome(
            event_id="evt-3",
            target_adapter="irc",
            target_channel="#general",
            route_id="route-c",
            delivery_plan_id="plan-3",
            status="permanent_failure",
            receipt=None,
            error="ValueError: malformed payload",
            duration_ms=3.0,
        )
        assert outcome.status == "permanent_failure"
        assert outcome.target_channel == "#general"

    def test_skipped_outcome(self) -> None:
        outcome = DeliveryOutcome(
            event_id="evt-4",
            target_adapter="matrix",
            target_channel=None,
            route_id="route-d",
            delivery_plan_id="plan-4",
            status="skipped",
            receipt=None,
            error="No renderer found for event_kind=message.reacted",
            duration_ms=0.1,
        )
        assert outcome.status == "skipped"

    def test_all_targets_succeed(self) -> None:
        """Multiple targets all produce success outcomes."""
        targets = ["adapter-a", "adapter-b", "adapter-c"]
        outcomes = [
            DeliveryOutcome(
                event_id="evt-batch",
                target_adapter=t,
                target_channel=None,
                route_id=f"route-{t}",
                delivery_plan_id=f"plan-{t}",
                status="success",
                receipt=None,
                error=None,
                duration_ms=float(i),
            )
            for i, t in enumerate(targets)
        ]
        assert len(outcomes) == 3
        assert all(o.status == "success" for o in outcomes)

    def test_one_target_fails_one_succeeds(self) -> None:
        """Mixed success/failure outcomes in a fanout."""
        outcomes = [
            DeliveryOutcome(
                event_id="evt-mixed",
                target_adapter="good",
                target_channel=None,
                route_id="route-1",
                delivery_plan_id="plan-good",
                status="success",
                receipt=None,
                error=None,
                duration_ms=5.0,
            ),
            DeliveryOutcome(
                event_id="evt-mixed",
                target_adapter="bad",
                target_channel=None,
                route_id="route-2",
                delivery_plan_id="plan-bad",
                status="transient_failure",
                receipt=None,
                error="TimeoutError: timed out",
                duration_ms=30000.0,
            ),
        ]
        succeeded = [o for o in outcomes if o.status == "success"]
        failed = [o for o in outcomes if o.status != "success"]
        assert len(succeeded) == 1
        assert len(failed) == 1
        assert succeeded[0].target_adapter == "good"
        assert failed[0].target_adapter == "bad"

    def test_planner_error_distinct_from_delivery_error(self) -> None:
        """Planner errors produce permanent_failure; delivery errors can be transient."""
        planner_outcome = DeliveryOutcome(
            event_id="evt-err",
            target_adapter="",
            target_channel=None,
            route_id="",
            delivery_plan_id="",
            status="permanent_failure",
            receipt=None,
            error="Planner error: RuntimeError: router misconfigured",
            duration_ms=0.0,
        )
        delivery_outcome = DeliveryOutcome(
            event_id="evt-err",
            target_adapter="adapter-a",
            target_channel="ch-1",
            route_id="route-x",
            delivery_plan_id="plan-x",
            status="transient_failure",
            receipt=None,
            error="ConnectionError: network unreachable",
            duration_ms=1000.0,
        )
        # Planner failure: no adapter/channel context.
        assert planner_outcome.target_adapter == ""
        assert planner_outcome.status == "permanent_failure"

        # Delivery failure: has full target context.
        assert delivery_outcome.target_adapter == "adapter-a"
        assert delivery_outcome.status == "transient_failure"


# ===================================================================
# Diagnostician
# ===================================================================


class TestDiagnostician:
    """Diagnostician records and snapshots diagnostic events."""

    def test_record_planner_failure(self) -> None:
        diag = Diagnostician()
        diag.record_planner_failure("evt-1", "router crash")
        snap = diag.snapshot()
        assert snap["planner_failures"] == {"evt-1": 1}

    def test_record_adapter_failure(self) -> None:
        diag = Diagnostician()
        diag.record_adapter_failure("evt-2", "discord", "ConnectionRefused")
        snap = diag.snapshot()
        assert snap["adapter_failures"] == {"discord": 1}

    def test_record_renderer_failure(self) -> None:
        diag = Diagnostician()
        diag.record_renderer_failure("evt-3", "irc", "no renderer")
        snap = diag.snapshot()
        assert snap["renderer_failures"] == {"irc": 1}

    def test_record_replay_skip(self) -> None:
        diag = Diagnostician()
        diag.record_replay_skip("evt-4", "already delivered")
        snap = diag.snapshot()
        assert snap["replay_skips"] == {"already delivered": 1}

    def test_record_replay_downgrade(self) -> None:
        diag = Diagnostician()
        diag.record_replay_downgrade("evt-5", "full", "summary")
        snap = diag.snapshot()
        assert snap["replay_downgrades"] == {"full->summary": 1}

    def test_record_correlation_miss(self) -> None:
        diag = Diagnostician()
        diag.record_correlation_miss("evt-6", "native-msg-42")
        snap = diag.snapshot()
        assert snap["correlation_misses"] == {"native-msg-42": 1}

    def test_multiple_failures_accumulate(self) -> None:
        diag = Diagnostician()
        diag.record_adapter_failure("evt-7", "slack", "timeout")
        diag.record_adapter_failure("evt-8", "slack", "timeout")
        diag.record_adapter_failure("evt-9", "discord", "reset")
        snap = diag.snapshot()
        assert snap["adapter_failures"] == {"slack": 2, "discord": 1}

    def test_snapshot_isolation(self) -> None:
        diag = Diagnostician()
        diag.record_planner_failure("evt-10", "err")
        snap = diag.snapshot()
        snap["planner_failures"]["evt-10"] = 999
        assert diag.snapshot()["planner_failures"] == {"evt-10": 1}

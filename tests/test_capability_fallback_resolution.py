"""Parametric tests for FallbackResolver capability-driven strategy resolution.

Validates that :class:`FallbackResolver` correctly interprets
:class:`AdapterCapabilities` for every event-kind × capability combination,
producing the expected delivery method (``"direct"`` or ``"skip"``).
"""

from __future__ import annotations

import pytest

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events.canonical import EventRelation, NativeRef
from medre.core.planning.capabilities import capability_unsupported
from medre.core.planning.delivery_plan import DeliveryPlan
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.routing import Route, Router, RouteSource
from medre.core.routing.models import RouteTarget
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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

_REPLY_RELATION_FOR_REASON = EventRelation(
    relation_type="reply",
    target_event_id="evt-001",
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
            # -- Reactions (3-level string: native/fallback → direct, unsupported → skip) ---
            ("message.reacted", "reactions", "native", "direct"),
            ("message.reacted", "reactions", "unsupported", "skip"),
            ("message.reacted", "reactions", "fallback", "fallback_text"),
            # -- Edits --------------------------------------------------------
            ("message.edited", "edits", "native", "direct"),
            ("message.edited", "edits", "unsupported", "skip"),
            ("message.edited", "edits", "fallback", "fallback_text"),
            # -- Deletes ------------------------------------------------------
            ("message.deleted", "deletes", "native", "direct"),
            ("message.deleted", "deletes", "unsupported", "skip"),
            ("message.deleted", "deletes", "fallback", "fallback_text"),
            # -- File / attachments -------------------------------------------
            ("message.file", "attachments", True, "direct"),
            ("message.file", "attachments", False, "skip"),
            # -- Presence -----------------------------------------------------
            ("presence.changed", "presence", True, "direct"),
            ("presence.changed", "presence", False, "skip"),
            # -- Telemetry ----------------------------------------------------
            ("telemetry.received", "metadata_fields", True, "direct"),
            ("telemetry.received", "metadata_fields", False, "skip"),
            ("telemetry.position", "metadata_fields", True, "direct"),
            ("telemetry.position", "metadata_fields", False, "skip"),
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
        event = make_event(
            event_kind="message.text",
            relations=(_REPLY_RELATION,),
        )
        strategy = resolver._resolve_strategy(event, caps)
        assert strategy.method == "direct"

    def test_text_with_reply_unsupported_replies_skip(self) -> None:
        """Text event with reply relation and unsupported replies → skip."""
        caps = AdapterCapabilities(replies="unsupported")
        resolver = FallbackResolver()
        event = make_event(
            event_kind="message.text",
            relations=(_REPLY_RELATION,),
        )
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
            key="\U0001f44d",
            fallback_text=None,
        )
        caps = AdapterCapabilities(replies="unsupported")
        resolver = FallbackResolver()
        event = make_event(
            event_kind="message.text",
            relations=(reaction_rel,),
        )
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

    def test_plan_id_stable_for_equivalent_targets(self) -> None:
        """Equivalent target values must not depend on Python object identity."""
        resolver = FallbackResolver()
        event = make_event(event_id="stable-plan-001", event_kind="message.text")
        caps = AdapterCapabilities()
        target_a = RouteTarget(adapter="test_target", channel="ch-out")
        target_b = RouteTarget(adapter="test_target", channel="ch-out")

        plan_a = resolver.resolve_fallback(event, target_a, caps)
        plan_b = resolver.resolve_fallback(event, target_b, caps)

        assert plan_a.plan_id == plan_b.plan_id
        assert plan_a.target_identity == plan_b.target_identity

    def test_plan_carries_capability_decision_fields(self) -> None:
        resolver = FallbackResolver()
        event = make_event(event_kind="message.text")
        caps = AdapterCapabilities(text=False)

        plan = resolver.resolve_fallback(event, _DEFAULT_TARGET, caps)

        assert plan.primary_strategy.method == "skip"
        assert plan.capability_level == "unsupported"
        assert plan.capability_field == "text"
        assert plan.capability_reason is not None
        assert "unsupported" in plan.capability_reason

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
        """Integration: text=False produces a plan with skip method."""
        resolver = FallbackResolver()
        event = make_event(event_kind="message.text")
        caps = AdapterCapabilities(text=False)
        plan = resolver.resolve_fallback(event, _DEFAULT_TARGET, caps)
        assert plan.primary_strategy.method == "skip"

    def test_capability_suppressed_receipt_scenario_telemetry(self) -> None:
        """Integration: metadata_fields=False produces skip for telemetry."""
        resolver = FallbackResolver()
        event = make_event(event_kind="telemetry.received")
        caps = AdapterCapabilities(metadata_fields=False)
        plan = resolver.resolve_fallback(event, _DEFAULT_TARGET, caps)
        assert plan.primary_strategy.method == "skip"

    def test_capability_suppressed_receipt_scenario_reply(self) -> None:
        """Integration: replies=unsupported with reply relation → skip."""
        resolver = FallbackResolver()
        event = make_event(
            event_kind="message.text",
            relations=(_REPLY_RELATION,),
        )
        caps = AdapterCapabilities(replies="unsupported")
        plan = resolver.resolve_fallback(event, _DEFAULT_TARGET, caps)
        assert plan.primary_strategy.method == "skip"


# ---------------------------------------------------------------------------
# TestPipelineGetAdapterCapabilities
# ---------------------------------------------------------------------------


class TestPipelineGetAdapterCapabilities:
    """Verify PipelineRunner._get_adapter_capabilities return paths.

    Covers lines 2568 (adapter=None), 2572 (adapter not in config),
    2576 (valid _capabilities), and 2578 (no _capabilities attr / wrong type).
    """

    @pytest.mark.asyncio
    async def test_returns_default_when_target_adapter_is_none(
        self,
        temp_storage,  # noqa: ANN001 — injected by pytest
    ) -> None:
        """RouteTarget with adapter=None returns default AdapterCapabilities."""
        from medre.core.engine.pipeline import PipelineRunner
        from medre.core.routing import Route

        route = Route(
            id="cap-lookup-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter=None)],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            target = RouteTarget(adapter=None)
            caps = runner._get_adapter_capabilities(target)
            assert isinstance(caps, AdapterCapabilities)
            # Should be a default instance — conservative defaults.
            assert caps == AdapterCapabilities()
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_returns_default_when_adapter_not_in_config(
        self,
        temp_storage,  # noqa: ANN001
    ) -> None:
        """Adapter ID not present in config.adapters returns default caps."""
        from medre.core.engine.pipeline import PipelineRunner
        from medre.core.routing import Route

        route = Route(
            id="cap-missing-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="nonexistent")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            target = RouteTarget(adapter="nonexistent")
            caps = runner._get_adapter_capabilities(target)
            assert isinstance(caps, AdapterCapabilities)
            assert caps == AdapterCapabilities()
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_returns_caps_when_adapter_has_valid_capabilities(
        self,
        temp_storage,  # noqa: ANN001
    ) -> None:
        """Adapter with _capabilities attribute returns them."""
        from medre.adapters.fakes.presentation import FakePresentationAdapter
        from medre.core.engine.pipeline import PipelineRunner
        from medre.core.routing import Route

        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="native",
            attachments=True,
        )

        route = Route(
            id="cap-valid-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            target = RouteTarget(adapter="dest")
            caps = runner._get_adapter_capabilities(target)
            assert isinstance(caps, AdapterCapabilities)
            assert caps.text is True
            assert caps.reactions == "native"
            assert caps.attachments is True
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_returns_default_when_adapter_lacks_capabilities_attr(
        self,
        temp_storage,  # noqa: ANN001
    ) -> None:
        """Adapter without _capabilities returns default caps (line 2578)."""

        class _MinimalAdapter:
            adapter_id = "minimal"

        route = Route(
            id="cap-noattr-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="minimal")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"minimal": _MinimalAdapter()},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            target = RouteTarget(adapter="minimal")
            caps = runner._get_adapter_capabilities(target)
            assert isinstance(caps, AdapterCapabilities)
            assert caps == AdapterCapabilities()
        finally:
            await runner.stop()


# ---------------------------------------------------------------------------
# TestCapabilityUnsupportedReason
# ---------------------------------------------------------------------------


class TestCapabilityUnsupportedReason:
    """Verify capability_unsupported returns correct reason strings."""

    @pytest.mark.parametrize(
        "event_kind,cap_field,cap_value,expect_none",
        [
            ("message.edited", "edits", "unsupported", False),
            ("message.edited", "edits", "native", True),
            ("message.edited", "edits", "fallback", True),
            ("message.deleted", "deletes", "unsupported", False),
            ("message.deleted", "deletes", "native", True),
            ("message.file", "attachments", True, True),
            ("message.file", "attachments", False, False),
            ("message.created", "text", True, True),
            ("message.created", "text", False, False),
            ("presence.changed", "presence", True, True),
            ("presence.changed", "presence", False, False),
            ("telemetry.received", "metadata_fields", True, True),
            ("telemetry.received", "metadata_fields", False, False),
            ("telemetry.position", "metadata_fields", True, True),
            ("telemetry.position", "metadata_fields", False, False),
        ],
    )
    def test_capability_unsupported_reason(
        self,
        event_kind,
        cap_field,
        cap_value,
        expect_none,
    ):
        caps = AdapterCapabilities(**{cap_field: cap_value})
        event = make_event(event_kind=event_kind)
        result = capability_unsupported(event, caps)
        if expect_none:
            assert (
                result is None
            ), f"Expected None for {event_kind} with {cap_field}={cap_value}"
        else:
            assert (
                result is not None
            ), f"Expected reason for {event_kind} with {cap_field}={cap_value}"
            assert event_kind in result

    def test_reply_relation_triggers_unsupported(self):
        """Events with reply relations + replies='unsupported' return reason."""
        caps = AdapterCapabilities(replies="unsupported")
        event = make_event(
            event_kind="message.text",
            relations=(_REPLY_RELATION_FOR_REASON,),
        )
        result = capability_unsupported(event, caps)
        assert result is not None
        assert "replies" in result

    def test_reply_relation_with_fallback_passes(self):
        """replies='fallback' passes through."""
        caps = AdapterCapabilities(replies="fallback")
        event = make_event(
            event_kind="message.text",
            relations=(_REPLY_RELATION_FOR_REASON,),
        )
        assert capability_unsupported(event, caps) is None

    def test_no_relations_passes(self):
        """Events with no relations always pass reply check."""
        caps = AdapterCapabilities(replies="unsupported")
        event = make_event(event_kind="message.text")
        assert capability_unsupported(event, caps) is None

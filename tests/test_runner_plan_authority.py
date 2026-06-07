"""Focused tests that _deliver_one() trusts DeliveryPlan capability provenance.

Verifies the runner reads ``route_plan.capability_level``,
``route_plan.capability_field``, and ``route_plan.capability_reason`` as the
authoritative planning result instead of re-resolving via
CapabilityDecisionResolver.decide().

Covers:
1. Phase 2.5: capability_level == "unsupported" suppresses with plan reason.
2. Phase 2.5: capability_reason is used in operator-visible error text.
3. Phase 2.75: plan-level skip includes capability_reason when available.
4. Phase 2.75: plan-level skip falls back to generic text when reason is None.
5. Plan authority: runner does NOT call the resolver during delivery.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.engine.pipeline import PipelineRunner
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryOutcome,
    DeliveryPlan,
    DeliveryStrategy,
)
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


class TestPhase25TrustsPlanCapabilityLevel:
    """Phase 2.5 reads route_plan.capability_level, not the resolver."""

    async def test_unsupported_level_suppresses_with_plan_reason(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """capability_level='unsupported' suppresses using plan reason."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="unsupported",
        )

        route = Route(
            id="authority-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
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

        event = make_event(
            event_id="authority-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "skipped"
            assert outcome.failure_kind is DeliveryFailureKind.CAPABILITY_SUPPRESSED
            # Error includes the specific capability reason from the plan.
            assert outcome.error is not None
            assert "capability_suppressed" in outcome.error
            assert "reactions unsupported" in outcome.error
            # Adapter never invoked.
            assert len(adapter.delivered_payloads) == 0
        finally:
            await runner.stop()

    async def test_text_false_uses_plan_reason(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """text=False suppresses message.text with plan reason in error."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(text=False)

        route = Route(
            id="text-authority-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.text",),
                channel=None,
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

        event = make_event(
            event_id="text-authority-001",
            event_kind="message.text",
            source_adapter="src",
            source_channel_id="ch-0",
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "skipped"
            assert outcome.failure_kind is DeliveryFailureKind.CAPABILITY_SUPPRESSED
            assert outcome.error is not None
            assert "capability_suppressed" in outcome.error
            assert "text unsupported" in outcome.error
        finally:
            await runner.stop()

    async def test_suppresses_unsupported_with_none_reason_direct_strategy(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Phase 2.5 suppresses even when method='direct' and reason=None.

        The runner must check capability_level=='unsupported' regardless of
        the plan's primary_strategy.method.  When capability_reason is None
        the suppression should still fire with a capability_suppressed error.
        """
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="unsupported",
        )

        route = Route(
            id="direct-unsupported-noreason-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
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

        event = make_event(
            event_id="direct-unsupported-noreason-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        # Hand-crafted plan: native method but unsupported with no reason.
        target = RouteTarget(adapter="dest")
        native_plan = DeliveryPlan(
            plan_id="plan-direct-unsupported-noreason",
            event_id=event.event_id,
            target=target,
            primary_strategy=DeliveryStrategy(method="direct"),
            capability_level="unsupported",
            capability_reason=None,
        )

        try:
            outcomes = await runner.deliver_to_targets(
                event,
                [(route, native_plan)],
            )

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "skipped"
            assert outcome.error is not None
            assert "capability_suppressed" in outcome.error
            # Adapter never invoked.
            assert len(adapter.delivered_payloads) == 0
        finally:
            await runner.stop()


class TestPhase25DoesNotCallResolver:
    """Phase 2.5 must NOT call CapabilityDecisionResolver.decide()."""

    async def test_resolver_not_called_during_delivery(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """CapabilityDecisionResolver.decide() is not called in _deliver_one.

        We call route_event() first (which legitimately uses the resolver),
        then patch the resolver before calling deliver_to_targets() to prove
        the delivery path does not re-resolve.
        """
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="unsupported",
        )

        route = Route(
            id="no-resolver-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
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

        event = make_event(
            event_id="no-resolver-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            # Phase 1: route_event() uses the resolver during planning — that's fine.
            event, route_targets = await runner.route_event(event)
            assert len(route_targets) == 1

            # Phase 2: Patch the resolver's decide method to fail if called.
            # Only the delivery phase is tested here.
            with patch(
                "medre.core.planning.capability_decision.CapabilityDecisionResolver.decide",
                side_effect=AssertionError(
                    "resolver.decide() must not be called in _deliver_one"
                ),
            ):
                outcomes = await runner.deliver_to_targets(event, route_targets)

            # Delivery should still suppress correctly using plan provenance.
            assert len(outcomes) == 1
            assert outcomes[0].status == "skipped"
            assert outcomes[0].failure_kind is DeliveryFailureKind.CAPABILITY_SUPPRESSED
        finally:
            await runner.stop()


class TestPhase275IncludesPlanReason:
    """Phase 2.75 plan-level skip uses route_plan.capability_reason."""

    async def test_plan_skip_includes_capability_reason(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Plan-level skip error includes capability_reason when present."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="unsupported",
        )

        route = Route(
            id="plan-reason-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
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

        event = make_event(
            event_id="plan-reason-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "skipped"
            # The error should contain the specific plan reason.
            assert outcome.error is not None
            # Either Phase 2.5 or 2.75 fires; both include the reason now.
            assert "reactions unsupported" in outcome.error
        finally:
            await runner.stop()

    async def test_plan_skip_generic_when_reason_missing(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Plan-level skip with no reason falls back to generic text."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(text=True)

        route = Route(
            id="generic-skip-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.created",),
                channel=None,
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

        event = make_event(
            event_id="generic-skip-001",
            event_kind="message.created",
            source_adapter="src",
            source_channel_id="ch-0",
        )

        # Build a hand-crafted plan with skip strategy but no reason.
        # capability_level is None (not "unsupported") so Phase 2.5
        # does not intercept — this targets Phase 2.75 specifically.
        target = RouteTarget(adapter="dest")
        skip_plan = DeliveryPlan(
            plan_id="plan-generic-skip",
            event_id=event.event_id,
            target=target,
            primary_strategy=DeliveryStrategy(method="skip"),
            capability_level=None,
            capability_reason=None,
        )

        try:
            outcomes = await runner.deliver_to_targets(
                event,
                [(route, skip_plan)],
            )

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "skipped"
            assert outcome.error is not None
            assert "plan_skip:" in outcome.error
            assert "delivery strategy is 'skip'" in outcome.error
        finally:
            await runner.stop()

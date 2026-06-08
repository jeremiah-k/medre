"""Tests for plan-level skip (Phase 2.75) in _deliver_to_targets_fan_out().

Covers the guard at lines 1708-1741 of pipeline.py:

    if (
        route_plan.primary_strategy.method == "skip"
        and adapter_id
        and adapter_id in self._config.adapters
    ):

Two scenarios:

1. Registered adapter + skip plan → returns skipped/CAPABILITY_SUPPRESSED,
   adapter never called.
2. Missing adapter + skip plan → does NOT return skipped here; falls
   through to deliver_to_target() which produces ADAPTER_MISSING.
"""

from __future__ import annotations

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.engine.pipeline import PipelineRunner
from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


class TestPlanLevelSkip:
    """Verify plan-level skip (Phase 2.75) in _deliver_to_targets_fan_out."""

    @pytest.mark.asyncio
    async def test_registered_adapter_skip_plan_returns_skipped(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Registered adapter with reactions="unsupported" + message.reacted.

        The planner creates a skip plan.  The pipeline returns a
        suppressed/skipped DeliveryOutcome with
        status="skipped" and failure_kind=CAPABILITY_SUPPRESSED
        without calling the adapter.
        """
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="unsupported",
        )

        route = Route(
            id="plan-skip-registered-route",
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
            event_id="plan-skip-reg-001",
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
            assert outcome.target_adapter == "dest"
            assert outcome.route_id == "plan-skip-registered-route"
            # Adapter never invoked.
            assert len(adapter.delivered_payloads) == 0
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_missing_adapter_skip_plan_does_not_suppress(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Missing adapter with skip plan does NOT return CAPABILITY_SUPPRESSED.

        Route targets "missing-dest" which is NOT registered in the adapters
        dict.  The planner uses default capabilities (attachments=False) and
        creates a skip plan for message.file.  But because the adapter is
        missing, the plan-level skip guard does NOT fire — the event falls
        through to deliver_to_target() which produces ADAPTER_MISSING.
        """
        route = Route(
            id="plan-skip-missing-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.file",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="missing-dest")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={},  # Empty — "missing-dest" is not registered.
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="plan-skip-missing-001",
            event_kind="message.file",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={
                "filename": "photo.jpg",
                "url": "https://example.com/photo.jpg",
            },
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            # Must be ADAPTER_MISSING, NOT CAPABILITY_SUPPRESSED.
            assert outcome.status == "permanent_failure"
            assert outcome.failure_kind is DeliveryFailureKind.ADAPTER_MISSING
            assert outcome.failure_kind is not DeliveryFailureKind.CAPABILITY_SUPPRESSED
            assert outcome.target_adapter == "missing-dest"
            assert outcome.error is not None
            assert "not registered" in outcome.error
        finally:
            await runner.stop()

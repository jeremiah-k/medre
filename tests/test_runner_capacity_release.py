"""Test covering runner.py lines 1366-1367: capacity release on outbox skip.

When _create_outbox_for_delivery returns a skip_reason (outbox row not owned
by this pipeline), the runner must release the previously-acquired capacity
slot before returning the skipped outcome.
"""

from __future__ import annotations

from medre.core.engine.pipeline import PipelineRunner
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryPlan,
    DeliveryStrategy,
)
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import StorageBackend
from medre.core.supervision.capacity import CapacityController
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


class _FakeLimits:
    """Minimal limits object for CapacityController."""

    max_inflight_deliveries = 10
    max_inflight_replay_events = 5
    delivery_acquire_timeout_seconds = 1.0


class _StubAdapter:
    """Minimal adapter stub that satisfies the adapter-registry check."""

    async def deliver(self, *args: object, **kwargs: object) -> object:
        return None

    @property
    def platform(self) -> str | None:
        return None


def _make_route() -> Route:
    return Route(
        id="route-cap-skip",
        source=RouteSource(
            adapter="src", channel=None, event_kinds=("message.created",)
        ),
        targets=[RouteTarget(adapter="dest")],
    )


def _make_plan(plan_id: str = "plan-cap-skip") -> DeliveryPlan:
    return DeliveryPlan(
        plan_id=plan_id,
        event_id="evt-cap-skip",
        target=RouteTarget(adapter="dest"),
        primary_strategy=DeliveryStrategy(method="direct"),
    )


def _make_runner(temp_storage: StorageBackend) -> PipelineRunner:
    router = Router(routes=[_make_route()])
    config = make_pipeline_config_for_pipeline(
        storage=temp_storage,
        router=router,
        adapters={"dest": _StubAdapter()},
    )
    return PipelineRunner(config)


class TestCapacityReleaseOnOutboxSkip:
    """When outbox creation returns a skip_reason, release the capacity slot."""

    async def test_capacity_released_on_outbox_skip(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        runner = _make_runner(temp_storage)
        cc = CapacityController(_FakeLimits())
        runner.set_capacity_controller(cc)

        # Capacity starts at the limit.
        assert cc.delivery_current == 0

        # Mock _create_outbox_for_delivery to return a skip_reason.
        async def _mock_create_outbox(*args, **kwargs):
            return (None, False, "", "terminal:sent")

        runner._create_outbox_for_delivery = _mock_create_outbox  # type: ignore[assignment]

        event = make_event(event_id="evt-cap-skip", source_adapter="src")
        route = _make_route()
        plan = _make_plan()

        outcomes = await runner.deliver_to_targets(event, [(route, plan)])
        assert len(outcomes) == 1
        assert outcomes[0].status == "skipped"
        assert outcomes[0].failure_kind == DeliveryFailureKind.OUTBOX_NOT_OWNED

        # Capacity must have been released — current should be back to 0.
        assert (
            cc.delivery_current == 0
        ), "capacity slot was not released after outbox skip"

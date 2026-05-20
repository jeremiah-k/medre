"""Pipeline capacity and shutdown rejection tests.

Verifies that delivery capacity rejection uses CAPACITY_REJECTION and
shutdown uses SHUTDOWN_REJECTION (not DEADLINE_EXCEEDED or generic errors).
"""

from __future__ import annotations

import pytest

from medre.adapters.fake_presentation import FakePresentationAdapter
from medre.adapters.fake_transport import FakeTransportAdapter
from medre.config.model import RuntimeLimits
from medre.core.engine.pipeline import PipelineRunner
from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.runtime.capacity import CapacityController
from medre.core.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_transport() -> FakeTransportAdapter:
    """An unstarted FakeTransportAdapter for creating test events."""
    return FakeTransportAdapter(adapter_id="fake_transport", channel="ch-0")


@pytest.fixture
def fake_presentation() -> FakePresentationAdapter:
    """A FakePresentationAdapter that records delivered events."""
    return FakePresentationAdapter(adapter_id="fake_presentation")


# ===================================================================
# Capacity / Shutdown rejection taxonomy
# ===================================================================


class TestCapacityRejectionTaxonomy:
    """Verify that delivery capacity rejection uses CAPACITY_REJECTION,
    not DEADLINE_EXCEEDED, and that shutdown uses SHUTDOWN_REJECTION.
    """

    async def test_capacity_exhausted_returns_capacity_rejection(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Capacity controller semaphore exhausted → CAPACITY_REJECTION."""
        adapter = FakePresentationAdapter(adapter_id="target")

        route = Route(
            id="cap-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])

        accounting = RuntimeAccounting()
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        config.runtime_accounting = accounting

        runner = PipelineRunner(config)

        # Use a capacity controller with 0 delivery slots → always rejects.
        limits = RuntimeLimits(
            max_inflight_deliveries=1,
            delivery_acquire_timeout_seconds=0.001,
        )
        cc = CapacityController(limits)
        runner.set_capacity_controller(cc)

        await runner.start()

        # Pre-acquire the single slot so the next acquire fails.
        await cc.acquire_delivery()

        event = make_event(event_id="cap-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].failure_kind is DeliveryFailureKind.CAPACITY_REJECTION
            assert outcomes[0].error == "delivery_capacity_exceeded"

            # Semantics: capacity rejection occurs before delivery stage,
            # so no DeliveryReceipt is persisted for this event.
            assert outcomes[0].receipt is None
            receipt_rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("cap-001",),
            )
            assert (
                len(receipt_rows) == 0
            ), "capacity rejection must not persist any delivery receipt"

            # Accounting: capacity_rejections incremented.
            assert accounting.counters().capacity_rejections == 1
        finally:
            await runner.stop()

    async def test_shutdown_rejection_returns_shutdown_rejection(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Capacity controller stopped → SHUTDOWN_REJECTION."""
        adapter = FakePresentationAdapter(adapter_id="target")

        route = Route(
            id="shutdown-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
        )
        router = Router(routes=[route])

        accounting = RuntimeAccounting()
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        config.runtime_accounting = accounting

        runner = PipelineRunner(config)

        limits = RuntimeLimits(max_inflight_deliveries=10)
        cc = CapacityController(limits)
        runner.set_capacity_controller(cc)

        await runner.start()

        # Stop accepting work so acquire returns False immediately.
        cc.stop_accepting()

        event = make_event(event_id="shutdown-001", source_adapter="src")

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "permanent_failure"
            assert outcomes[0].failure_kind is DeliveryFailureKind.SHUTDOWN_REJECTION
            assert outcomes[0].error == "delivery_rejected_shutdown"

            # Semantics: shutdown rejection occurs before delivery stage,
            # so no DeliveryReceipt is persisted for this event.
            assert outcomes[0].receipt is None
            receipt_rows = await temp_storage._read_all(
                "SELECT * FROM delivery_receipts WHERE event_id = ?",
                ("shutdown-001",),
            )
            assert (
                len(receipt_rows) == 0
            ), "shutdown rejection must not persist any delivery receipt"

            # Accounting: capacity_rejections incremented.
            assert accounting.counters().capacity_rejections == 1
        finally:
            await runner.stop()

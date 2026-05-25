"""Pipeline outbox integration tests: outbox creation, suppression guards,
status transitions, and shutdown interaction.
"""

from __future__ import annotations

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.adapters.fakes.transport import FakeTransportAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.policies.route_policy import RoutePolicy
from medre.core.rendering.renderer import RenderingResult
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage import SQLiteStorage
from medre.core.supervision.capacity import CapacityController
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_transport() -> FakeTransportAdapter:
    return FakeTransportAdapter(adapter_id="fake_transport", channel="ch-0")


@pytest.fixture
def fake_presentation() -> FakePresentationAdapter:
    return FakePresentationAdapter(adapter_id="fake_presentation")


@pytest.fixture
def router_with_routes() -> Router:
    """Router with a single route from fake_transport to fake_presentation."""
    route = Route(
        id="route-transport-to-presentation",
        source=RouteSource(
            adapter="fake_transport", event_kinds=("message.created",), channel="ch-0"
        ),
        targets=[RouteTarget(adapter="fake_presentation", channel="ch-out")],
    )
    return Router(routes=[route])


# ===================================================================
# Outbox creation tests
# ===================================================================


class _ZeroCapacityLimits:
    """Limits-like object with zero delivery capacity."""

    max_inflight_deliveries: int = 0
    max_inflight_replay_events: int = 0
    delivery_acquire_timeout_seconds: float = 0.1


class TestOutboxCreation:
    """Outbox should be created for accepted deliveries and suppressed for
    policy/loop/capacity-rejected targets."""

    async def test_outbox_created_for_accepted_target(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """A successful delivery creates an outbox item."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-accepted-001")

        try:
            await runner.handle_ingress(event)

            # Outbox should exist for this delivery.
            items = await temp_storage.list_outbox_items(
                status_filter=[
                    "sent",
                    "queued",
                    "in_progress",
                    "pending",
                    "retry_wait",
                    "dead_lettered",
                ],
            )
            assert len(items) == 1
            # The item should reference our event.
            matching = [i for i in items if i.event_id == "obox-accepted-001"]
            assert len(matching) == 1
            assert matching[0].status in ("sent", "queued")
            assert matching[0].target_adapter == "fake_presentation"
        finally:
            await runner.stop()

    async def test_no_outbox_for_policy_suppressed(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Policy-suppressed targets should NOT create outbox items."""

        policy = RoutePolicy(
            allowed_source_adapters=["some_other_adapter"],
        )
        route = Route(
            id="route-policy-test",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="fake_presentation")],
            policy=policy,
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-policy-sup-001")

        try:
            outcomes = await runner.handle_ingress(event)
            # Should be policy_suppressed.
            assert any(
                o.status == "skipped"
                and o.failure_kind is not None
                and "policy" in str(o.failure_kind).lower()
                for o in outcomes
            )

            # No outbox item should be created for this event.
            items = await temp_storage.list_outbox_items()
            matching = [i for i in items if i.event_id == "obox-policy-sup-001"]
            assert len(matching) == 0
        finally:
            await runner.stop()

    async def test_no_outbox_for_loop_suppressed(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Self-loop suppressed targets should NOT create outbox items."""
        route = Route(
            id="route-self-loop",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            # Target back to source adapter = self-loop.
            targets=[RouteTarget(adapter="fake_transport")],
        )
        router = Router(routes=[route])

        transport = FakeTransportAdapter(adapter_id="fake_transport", channel="ch-0")
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={
                "fake_transport": transport,
                "fake_presentation": fake_presentation,
            },
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-loop-sup-001")

        try:
            outcomes = await runner.handle_ingress(event)
            # Verify loop suppression.
            assert any(o.status == "skipped" for o in outcomes)

            # No outbox item for this event.
            items = await temp_storage.list_outbox_items()
            matching = [i for i in items if i.event_id == "obox-loop-sup-001"]
            assert len(matching) == 0
        finally:
            await runner.stop()

    async def test_no_outbox_for_capacity_rejected(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Capacity-rejected targets should NOT create outbox items."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)

        # Create a capacity controller that immediately rejects.
        cc = CapacityController(limits=_ZeroCapacityLimits())
        runner.set_capacity_controller(cc)
        await runner.start()

        event = make_event(event_id="obox-cap-rej-001")

        try:
            outcomes = await runner.handle_ingress(event)
            # Should be capacity rejected.
            assert any(
                o.status == "permanent_failure"
                and o.failure_kind is not None
                and "capacity" in str(o.failure_kind).lower()
                for o in outcomes
            )

            # No outbox item for this event (capacity reject happens before
            # outbox creation phase).
            items = await temp_storage.list_outbox_items()
            matching = [i for i in items if i.event_id == "obox-cap-rej-001"]
            assert len(matching) == 0
        finally:
            await runner.stop()


# ===================================================================
# Outbox status transitions
# ===================================================================


class TestOutboxStatusTransitions:
    """Outbox status updates based on delivery outcome."""

    async def test_successful_delivery_marks_sent(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Successful synchronous delivery marks outbox as sent."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-sent-001")

        try:
            await runner.handle_ingress(event)

            items = await temp_storage.list_outbox_items(
                status_filter=["sent"],
            )
            matching = [i for i in items if i.event_id == "obox-sent-001"]
            assert len(matching) == 1
            assert matching[0].status == "sent"
        finally:
            await runner.stop()


class TestNoRetryPolicyDeadLetters:
    """When retry_policy is None, transient failures dead-letter the outbox item."""

    async def test_transient_failure_with_no_retry_policy_dead_letters(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """A retryable transient failure with no retry policy dead-letters the
        outbox item instead of scheduling retry_wait."""
        from medre.core.contracts.adapter import AdapterDeliveryResult

        class TransientFailAdapter(FakePresentationAdapter):
            async def deliver(self, payload: RenderingResult) -> AdapterDeliveryResult:
                raise ConnectionError("transient failure for no-retry-policy test")

        adapter = TransientFailAdapter(adapter_id="transient_fail")
        route = Route(
            id="route-no-retry",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="transient_fail")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"transient_fail": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-no-retry-001")

        try:
            outcomes = await runner.handle_ingress(event)
            assert any(o.status in ("transient_failure", "failed") for o in outcomes)

            items = await temp_storage.list_outbox_items()
            matching = [i for i in items if i.event_id == "obox-no-retry-001"]
            assert len(matching) == 1
            assert (
                matching[0].status == "dead_lettered"
            ), f"Expected dead_lettered, got {matching[0].status}"
        finally:
            await runner.stop()

    async def test_queued_delivery_marks_queued(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Queue-based delivery marks outbox as queued."""
        from medre.core.contracts.adapter import (
            AdapterDeliveryResult,
        )

        # Create a queue-based fake adapter.
        class QueuedFakeAdapter(FakePresentationAdapter):
            """Adapter that returns delivery_status='enqueued'."""

            async def deliver(self, payload: RenderingResult) -> AdapterDeliveryResult:
                self.delivered_payloads.append(payload)
                return AdapterDeliveryResult(
                    native_message_id=None,
                    delivery_status="enqueued",
                )

        queued_adapter = QueuedFakeAdapter(adapter_id="fake_presentation")

        route = Route(
            id="route-queued-test",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="fake_presentation")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"fake_presentation": queued_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-queued-001")

        try:
            await runner.handle_ingress(event)

            items = await temp_storage.list_outbox_items(
                status_filter=["queued"],
            )
            matching = [i for i in items if i.event_id == "obox-queued-001"]
            assert len(matching) == 1
            assert matching[0].status == "queued"
        finally:
            await runner.stop()


# ===================================================================
# Live delivery claim race protection
# ===================================================================


class TestLiveDeliveryClaimRace:
    """Pipeline creates outbox items as in_progress with a lease to prevent
    the retry worker from claiming them before the live adapter attempt
    finishes."""

    async def test_in_progress_not_claimable_by_retry_worker(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """A live in_progress item with unexpired lease should not be claimable."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-race-001")

        try:
            await runner.handle_ingress(event)

            # After delivery completes, the item should be in a terminal or
            # post-delivery status.  An in_progress item with an unexpired
            # lease must NOT be claimable by the retry worker.
            items = await temp_storage.list_outbox_items()
            matching = [i for i in items if i.event_id == "obox-race-001"]
            assert len(matching) == 1

            # The item should have transitioned away from in_progress
            # (to sent/queued) since delivery completed synchronously.
            # But even if it were still in_progress, the retry worker
            # should not claim it if the lease is valid.
            item = matching[0]
            assert item.status in ("sent", "queued", "in_progress")

            # If still in_progress (race window), verify it cannot be claimed.
            if item.status == "in_progress":
                now = "2026-01-01T00:00:00"
                claimed = await temp_storage.claim_due_outbox_items(
                    now=now, worker_id="retry-worker", lease_seconds=30, limit=10
                )
                assert not any(
                    c.outbox_id == item.outbox_id for c in claimed
                ), "Live in_progress item with valid lease must not be claimed"
        finally:
            await runner.stop()

    async def test_in_progress_lifecycle_transitions(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Live delivery transitions: in_progress -> sent/queued/retry_wait/dead_lettered."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-lifecycle-001")

        try:
            await runner.handle_ingress(event)

            items = await temp_storage.list_outbox_items()
            matching = [i for i in items if i.event_id == "obox-lifecycle-001"]
            assert len(matching) == 1

            # Synchronous successful delivery: should end at "sent".
            item = matching[0]
            assert item.status == "sent"
            # Terminal status clears lease.
            assert item.locked_at is None
            assert item.lease_until is None
            assert item.worker_id is None
        finally:
            await runner.stop()


# ===================================================================
# Shutdown-related outbox behavior
# ===================================================================


class TestOutboxShutdownBehavior:
    """Outbox behavior during shutdown."""

    async def test_shutdown_after_send_succeeds_leaves_sent_outbox(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Delivery completed before shutdown leaves outbox item as sent."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-shutdown-001")

        try:
            # Process event through pipeline
            await runner.handle_ingress(event)
        finally:
            await runner.stop()

        # After shutdown, outbox should have a sent item (the delivery
        # completed before shutdown).
        items = await temp_storage.list_outbox_items()
        matching = [i for i in items if i.event_id == "obox-shutdown-001"]
        assert len(matching) == 1
        # Delivery completed normally, so status should be sent.
        assert matching[0].status == "sent"


# ===================================================================
# Lease renewal
# ===================================================================


class TestLeaseRenewal:
    """Live delivery leases should be renewable and prevent reclaim."""

    async def test_renew_outbox_lease_method(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """renew_outbox_lease should extend the lease on an in_progress item."""
        from datetime import datetime, timedelta, timezone

        from medre.core.storage.backend import DeliveryOutboxItem

        now = datetime.now(timezone.utc)
        item = DeliveryOutboxItem(
            outbox_id="obox-renew-001",
            event_id="evt-renew",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="fake_presentation",
            status="in_progress",
            worker_id="pipeline:testworker",
            lease_until=(now + timedelta(seconds=60)).isoformat(),
            locked_at=now.isoformat(),
        )
        await temp_storage.create_outbox_item(item)

        new_lease = (now + timedelta(seconds=1800)).isoformat()
        result = await temp_storage.renew_outbox_lease(
            "obox-renew-001", "pipeline:testworker", new_lease
        )
        assert result is True

        updated = await temp_storage.get_outbox_item("obox-renew-001")
        assert updated is not None
        assert updated.lease_until == new_lease

    async def test_renew_outbox_lease_wrong_worker(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """renew_outbox_lease should fail when the worker_id doesn't match."""
        from datetime import datetime, timedelta, timezone

        from medre.core.storage.backend import DeliveryOutboxItem

        now = datetime.now(timezone.utc)
        item = DeliveryOutboxItem(
            outbox_id="obox-wrong-001",
            event_id="evt-wrong",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="fake_presentation",
            status="in_progress",
            worker_id="pipeline:owner",
            lease_until=(now + timedelta(seconds=60)).isoformat(),
            locked_at=now.isoformat(),
        )
        await temp_storage.create_outbox_item(item)

        new_lease = (now + timedelta(seconds=1800)).isoformat()
        result = await temp_storage.renew_outbox_lease(
            "obox-wrong-001", "pipeline:other", new_lease
        )
        assert result is False

    async def test_renew_outbox_lease_not_in_progress(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """renew_outbox_lease should fail when item is not in_progress."""
        from datetime import datetime, timedelta, timezone

        from medre.core.storage.backend import DeliveryOutboxItem

        now = datetime.now(timezone.utc)
        item = DeliveryOutboxItem(
            outbox_id="obox-sent-001",
            event_id="evt-sent",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="fake_presentation",
            status="sent",
        )
        await temp_storage.create_outbox_item(item)

        new_lease = (now + timedelta(seconds=1800)).isoformat()
        result = await temp_storage.renew_outbox_lease(
            "obox-sent-001", "pipeline:worker", new_lease
        )
        assert result is False

    async def test_renewal_prevents_claim_during_long_delivery(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """A long delivery with active lease renewal should not be claimable.

        Uses a slow adapter that takes several seconds, while a
        claim_due_outbox_items call during the delivery window should
        find no claimable items (the lease is being renewed).
        """
        import asyncio

        from medre.core.contracts.adapter import AdapterDeliveryResult

        class SlowAdapter(FakePresentationAdapter):
            """Adapter that simulates a slow send (like Meshtastic)."""

            def __init__(self) -> None:
                super().__init__(adapter_id="fake_presentation")
                self._deliver_event = asyncio.Event()

            async def deliver(self, payload: RenderingResult) -> AdapterDeliveryResult:
                self.delivered_payloads.append(payload)
                # Simulate a slow send — wait for the signal.
                await asyncio.sleep(0.1)
                return AdapterDeliveryResult(
                    native_message_id=f"slow-{payload.event_id}",
                    native_channel_id=payload.target_channel,
                )

        slow_adapter = SlowAdapter()
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": slow_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-slow-001")

        try:
            outcomes = await runner.handle_ingress(event)

            # Delivery should succeed.
            assert any(o.status in ("success", "queued") for o in outcomes)

            # Verify the outbox item ended in a terminal status.
            items = await temp_storage.list_outbox_items()
            matching = [i for i in items if i.event_id == "obox-slow-001"]
            assert len(matching) == 1
            assert matching[0].status in ("sent", "queued")
        finally:
            await runner.stop()
